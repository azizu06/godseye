"""Offline replay of a recorded nav-log run through the real navigation planner.

    python -m tools.replay_nav_log RUN_DIR [--goal X Z] [--inflation-m M]
        [--unknown-traversable] [--json OUT.json] [--trace-jsonl OUT.jsonl]

Reads a nav-log run directory (``manifest.json``, ``events.jsonl``, ``live.jsonl``,
``summary.json``; see ``backend/README.md`` and the reference observer that writes
this schema) and replays whatever pose and occupancy it recorded through the real
``backend.navigation`` / ``backend.navigator`` planning code (``Navigator._plan``,
``Navigator._check``, ``PurePursuit``), driven entirely by the log's own recorded
timestamps and an in-memory command sink. This lets a teammate without rover access
see what the planner would have decided at each recorded moment.

Hardware refusal by construction, not a flag: this module never imports
``backend.app``, never opens a socket, websocket, BLE or serial connection, never
reads a pairing key, and never issues a live command. It only calls pure planning
functions with data already sitting in the log file.

This is a planner-decision replay over whatever pose/occupancy the log captured, not
a sensor or motion replay:

- Raw depth frames, camera images, audio, GPS, motor commands and ESP feedback are
  not in the nav-log and are never reconstructed or guessed here.
- The rover position fed to the planner at each tick is the log's own recorded
  ground-truth pose, never a position integrated from planned commands: this tool
  does not simulate motor physics. It answers "what would the planner have decided
  here", not "where would the rover have ended up".
- Robot inflation radius and whether unknown space counts as traversable are not
  necessarily recorded in the log; unless a manifest/CLI value is supplied, this
  tool uses the same defaults as ``backend.navigation.PlannerConfig`` and says so in
  the report. This is a planner-geometry assumption, not a sensor fabrication.
- This tool does not reproduce ``backend.navigator``'s pursuit feasibility downgrade
  (``pursuit_step_allowed``/lookahead shrink), the Explore person-yield/resume gate,
  or ``ScanPacer`` stationary-checkpoint pacing. A "follow" tick here is the nominal
  pure-pursuit command for that instant, not necessarily the exact command a live
  run would have submitted.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
import time

from backend import navigation, navigator
from backend.occupancy import OccupancySnapshot

REQUIRED_FILES = ('live.jsonl',)
OPTIONAL_FILES = ('manifest.json', 'events.jsonl', 'summary.json')


class ReplayError(Exception):
    """The recorded source cannot be replayed (missing/malformed required input)."""


def _read_jsonl(path: Path):
    lines, malformed = [], []
    with path.open() as handle:
        for lineno, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                malformed.append(dict(line=lineno, error=str(exc)))
    return lines, malformed


class LogSource:
    """Parsed contents of one nav-log run directory. Read-only; nothing is written."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.missing = [name for name in REQUIRED_FILES if not (run_dir / name).is_file()]
        if self.missing:
            raise ReplayError(f'{run_dir}: missing required file(s): {", ".join(self.missing)}')
        self.manifest = None
        self.manifest_error = None
        manifest_path = run_dir / 'manifest.json'
        if manifest_path.is_file():
            try:
                self.manifest = json.loads(manifest_path.read_text())
            except json.JSONDecodeError as exc:
                self.manifest_error = str(exc)
        self.summary = None
        summary_path = run_dir / 'summary.json'
        if summary_path.is_file():
            try:
                self.summary = json.loads(summary_path.read_text())
            except json.JSONDecodeError:
                self.summary = None
        live_lines, self.malformed_live_lines = _read_jsonl(run_dir / 'live.jsonl')
        self.event_count = 0
        events_path = run_dir / 'events.jsonl'
        if events_path.is_file():
            events, _ = _read_jsonl(events_path)
            self.event_count = len(events)

        self.poses: list[tuple[float, float, float, float, str | None]] = []
        self.occupancies: list[tuple[float, dict]] = []
        self.recorded_paths: list[tuple[float, list]] = []
        for entry in live_lines:
            if not isinstance(entry, dict) or 'msg' not in entry:
                continue
            el = entry.get('el')
            msg = entry['msg']
            if not isinstance(el, (int, float)) or not isinstance(msg, dict):
                continue
            t = msg.get('type')
            if t == 'pose':
                position, yaw = msg.get('position'), msg.get('yaw_rad')
                if (isinstance(position, list) and len(position) == 3
                        and isinstance(yaw, (int, float))):
                    self.poses.append((float(el), float(position[0]), float(position[2]),
                                       float(yaw), msg.get('tracking')))
            elif t == 'occupancy':
                if all(k in msg for k in ('origin', 'cell_m', 'width', 'height', 'cells')):
                    self.occupancies.append((float(el), msg))
            elif t == 'path':
                points = msg.get('points')
                if isinstance(points, list):
                    self.recorded_paths.append((float(el), points))
        self.poses.sort(key=lambda p: p[0])
        self.occupancies.sort(key=lambda o: o[0])
        self.run_id = run_dir.name

    def compatibility_notes(self) -> list[str]:
        notes = []
        if self.manifest is None:
            notes.append('missing_or_unparsable_manifest' if self.manifest_error
                         else 'no manifest.json: source compatibility (axes/clock/occupancy schema) unverified')
        elif not {'axes', 'clock', 'occupancy'} <= self.manifest.keys():
            notes.append('manifest.json is missing expected axes/clock/occupancy schema fields')
        if self.malformed_live_lines:
            notes.append(f'{len(self.malformed_live_lines)} malformed line(s) in live.jsonl, skipped')
        if not self.poses:
            notes.append('no recorded pose frames: nothing to replay')
        if not self.occupancies:
            notes.append('no recorded occupancy frames: planner will see map_unknown throughout')
        return notes


class OccupancyTimeline:
    """Recorded occupancy messages, decoded once, looked up by recorded elapsed time."""

    def __init__(self, occupancies, *, inflation_m: float, unknown_traversable: bool, run_id: str):
        self.inflation_m, self.unknown_traversable, self.run_id = inflation_m, unknown_traversable, run_id
        self.good: list[tuple[float, tuple, float, object]] = []
        self.malformed: list[dict] = []
        for el, msg in occupancies:
            try:
                grid = navigation.Grid.from_message(msg)
            except (ValueError, KeyError, TypeError) as exc:
                self.malformed.append(dict(el=el, error=str(exc)))
            else:
                self.good.append((el, grid.origin, grid.cell_m, grid.cells))
        self.els = [g[0] for g in self.good]

    def snapshot_at(self, el: float) -> OccupancySnapshot | None:
        i = bisect.bisect_right(self.els, el) - 1
        if i < 0:
            return None
        occ_el, origin, cell_m, cells = self.good[i]
        return OccupancySnapshot(session=(self.run_id,), revision=i,
                                 accepted_at=time.monotonic() - max(0., el - occ_el),
                                 blockers=(), inflation_m=self.inflation_m, origin=origin, cell_m=cell_m,
                                 cells=cells, floor_y=None, unknown_traversable=self.unknown_traversable)


def _tick(el, x, z, yaw, *, status, v=0., w=0., target=None, waiting_reason=None, path_len=0):
    return dict(el=round(el, 3), x=round(x, 4), z=round(z, 4), yaw=round(yaw, 4), status=status,
               v=round(v, 4), w=round(w, 4),
               target=[round(target[0], 4), round(target[1], 4)] if target else None,
               waiting_reason=waiting_reason, path_len=path_len)


def replay_run(source: LogSource, *, kind: str, goal=None, inflation_m: float = .18,
               unknown_traversable: bool = False, settings=None) -> dict:
    """Replay ``source`` and return a report dict. Never touches hardware or a network."""
    if kind not in ('explore', 'goal'):
        raise ValueError('kind must be explore or goal')
    if kind == 'goal' and goal is None:
        raise ValueError('goal kind requires a goal (x, z)')
    settings = settings or navigator.NavSettings()
    explore = kind == 'explore'
    nav = navigator.Navigator(
        settings, pose=lambda: None, occupancy=lambda: None,
        submit=lambda *a, **k: True, stop=lambda reason: None,
        publish=lambda message: None, armed_mode=lambda: navigator.RUN_MODES[kind])
    timeline = OccupancyTimeline(source.occupancies, inflation_m=inflation_m,
                                 unknown_traversable=unknown_traversable, run_id=source.run_id)
    goal_now = tuple(float(v) for v in goal) if goal else None

    follower = None
    visited: list = []
    last_plan_el = last_check_el = -math.inf
    checked_revision = None
    progress = None
    explore_heading = None
    leg_origin = None
    shown_points: list = []
    trace: list[dict] = []
    replans = checks = 0
    stop_reason = stop_el = None

    prev_el = None
    for el, x, z, yaw, tracking in source.poses:
        if prev_el is not None and el - prev_el > settings.pose_max_age_s + 1e-9:
            stop_reason, stop_el = 'pose_stale', el
            break
        prev_el = el
        if tracking not in (None, 'normal'):
            stop_reason, stop_el = 'tracking_lost', el
            break
        if explore and explore_heading is None:
            explore_heading = yaw

        snapshot = timeline.snapshot_at(el)
        problem = navigator.map_problem(snapshot, settings.map_max_age_s)
        if problem:
            trace.append(_tick(el, x, z, yaw, status='wait', waiting_reason=problem, path_len=len(shown_points)))
            continue

        goal_near = (explore and goal_now is not None and el - last_plan_el >= .5
                    and math.hypot(goal_now[0] - x, goal_now[1] - z) < 1.)
        if follower is None or el - last_plan_el >= settings.replan_s or goal_near:
            last_plan_el = el
            outcome = nav._plan(lambda snap=snapshot: snap, (x, z), goal_now, explore, yaw,
                                explore_heading, tuple(visited))
            replans += 1
            if outcome[0] == 'explore_complete':
                trace.append(_tick(el, x, z, yaw, status='explore_complete', path_len=0))
                stop_el = el
                break
            _, _, next_goal, result = outcome
            if not result.ok:
                if explore and result.reason in {'no_path', 'start_blocked', 'search_limit'}:
                    if next_goal is not None and result.reason == 'no_path':
                        visited.append(next_goal)
                        visited = visited[-64:]
                    follower, goal_now, shown_points = None, None, []
                    trace.append(_tick(el, x, z, yaw, status='wait', waiting_reason=result.reason,
                                       path_len=0))
                    continue
                stop_reason = navigator.PLAN_STOP_REASONS.get(result.reason, result.reason)
                stop_el = el
                break
            goal_now = next_goal
            if explore and leg_origin is None:
                leg_origin = (x, z)
            follower = (follower.replaced(result.points) if follower is not None
                       else navigation.PurePursuit(result.points, settings.follower))
            checked_revision = None
            shown_points = result.points

        if follower is not None and el - last_check_el >= settings.blocked_check_s:
            last_check_el = el
            remaining = [[x, z]] + list(follower.path[follower.segment + 1:])
            _, checked_snapshot, check_problem = nav._check(lambda snap=snapshot: snap, remaining, 0,
                                                            checked_revision)
            checks += 1
            if checked_snapshot is not None:
                checked_revision = checked_snapshot.revision
            if check_problem == 'path_blocked':
                last_plan_el = -math.inf
            elif check_problem:
                stop_reason, stop_el = check_problem, el
                break

        if follower is None:
            trace.append(_tick(el, x, z, yaw, status='no_route', path_len=0))
            continue

        command = follower.step(x, z, yaw)
        if command.arrived:
            if not explore:
                stop_reason, stop_el = 'arrived', el
                trace.append(_tick(el, x, z, yaw, status='arrived', path_len=len(shown_points)))
                break
            if leg_origin is not None and math.hypot(x - leg_origin[0], z - leg_origin[1]) >= 1.2:
                explore_heading = math.atan2(x - leg_origin[0], z - leg_origin[1])
            leg_origin = (x, z)
            if goal_now is not None:
                visited.append(goal_now)
                visited = visited[-64:]
            follower, goal_now, last_plan_el, shown_points = None, None, -math.inf, []
            trace.append(_tick(el, x, z, yaw, status='arrived_leg', path_len=0))
            continue

        v, w = command.v_mps, command.yaw_rate_rps
        if v or w:
            if (progress is None or math.hypot(x - progress[0], z - progress[1]) >= settings.progress_m
                    or abs(math.remainder(yaw - progress[2], math.tau)) >= settings.progress_rad):
                progress = (x, z, yaw, el)
            elif el - progress[3] >= settings.no_progress_s:
                stop_reason, stop_el = 'no_progress', el
                trace.append(_tick(el, x, z, yaw, status='no_progress', v=v, w=w, path_len=len(shown_points)))
                break
        else:
            progress = None
        trace.append(_tick(el, x, z, yaw, status=command.status, v=v, w=w, target=command.target,
                           path_len=len(shown_points)))
    else:
        stop_reason = stop_reason or 'end_of_recorded_log'
        stop_el = source.poses[-1][0] if source.poses else None

    recorded_path_m = sum(math.hypot(b[1] - a[1], b[2] - a[2])
                          for a, b in zip(source.poses, source.poses[1:]))
    return dict(
        run_dir=str(source.run_dir), run_id=source.run_id, kind=kind, goal=list(goal_now) if goal_now else None,
        planner_geometry=dict(inflation_m=inflation_m, unknown_traversable=unknown_traversable),
        recorded=dict(poses=len(source.poses), occupancy_frames=len(source.occupancies),
                     occupancy_malformed=len(timeline.malformed), recorded_paths=len(source.recorded_paths),
                     events=source.event_count, recorded_path_m=round(recorded_path_m, 3)),
        replay=dict(stop_reason=stop_reason, stop_el=round(stop_el, 3) if stop_el is not None else None,
                   replans=replans, checks=checks, ticks=len(trace),
                   final_route_len=len(shown_points)),
        compatibility_notes=source.compatibility_notes(),
        occupancy_malformed=timeline.malformed,
        summary_comparison=(dict(recorded_end_reason=source.summary.get('end_reason'),
                                 recorded_armed_s=source.summary.get('armed_s'),
                                 recorded_path_m=source.summary.get('path_m'),
                                 replay_stop_reason=stop_reason,
                                 replay_stop_el=round(stop_el, 3) if stop_el is not None else None)
                            if source.summary else None),
        limitations=[
            'ground-truth recorded pose is fed to the planner every tick; commands are never '
            'integrated into a simulated position (no motor-physics simulation)',
            'omits Navigator\'s pursuit feasibility downgrade (pursuit_step_allowed/lookahead '
            'shrink), the Explore person-yield/resume gate, and ScanPacer stationary pacing',
            'inflation_m/unknown_traversable are CLI/default planner-geometry assumptions unless '
            'a manifest supplies calibration; they are not necessarily the run\'s real calibration',
        ],
        trace=trace,
    )


def _load_manifest_summary(manifest: dict | None) -> str:
    if manifest is None:
        return 'manifest.json: none (source compatibility unverified)'
    lines = [f'manifest.json: source_sha={manifest.get("source_sha", "?")}']
    for key in ('axes', 'clock', 'occupancy', 'not_recorded', 'excluded'):
        if key in manifest:
            lines.append(f'  {key}: {manifest[key]}')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run_dir', type=Path, help='nav-log run directory (manifest.json/events.jsonl/live.jsonl)')
    parser.add_argument('--goal', nargs=2, type=float, metavar=('X', 'Z'),
                        help='replay as a goal run to this point; default is an explore replay')
    parser.add_argument('--inflation-m', type=float, default=.18,
                        help='assumed robot_radius_m + margin_m (default .18, matches PlannerConfig defaults)')
    parser.add_argument('--unknown-traversable', action='store_true',
                        help='treat unmapped cells as traversable (flat-terrain prototype policy)')
    parser.add_argument('--json', type=Path, help='write the full report (including per-tick trace) here')
    parser.add_argument('--trace-jsonl', type=Path, help='write one JSON line per replayed tick here')
    parser.add_argument('--quiet', action='store_true', help='suppress the human-readable summary')
    args = parser.parse_args(argv)

    try:
        source = LogSource(args.run_dir)
    except ReplayError as exc:
        print(f'INCOMPATIBLE SOURCE: {exc}')
        return 2

    notes = source.compatibility_notes()
    if not args.quiet:
        print(_load_manifest_summary(source.manifest))
        print(f'live.jsonl: {len(source.poses)} pose, {len(source.occupancies)} occupancy, '
             f'{len(source.recorded_paths)} recorded path frame(s); {source.event_count} event(s)')
        for note in notes:
            print(f'note: {note}')
    if not source.poses:
        print('no recorded pose frames: nothing to replay')
        return 2

    kind = 'goal' if args.goal else 'explore'
    report = replay_run(source, kind=kind, goal=args.goal, inflation_m=args.inflation_m,
                        unknown_traversable=args.unknown_traversable)

    if not args.quiet:
        r = report['replay']
        print(f"replay: kind={kind} stop_reason={r['stop_reason']} at el={r['stop_el']} "
             f"ticks={r['ticks']} replans={r['replans']} checks={r['checks']}")
        if report['summary_comparison']:
            c = report['summary_comparison']
            print(f"recorded: end_reason={c['recorded_end_reason']} armed_s={c['recorded_armed_s']} "
                 f"path_m={c['recorded_path_m']}")
        for limitation in report['limitations']:
            print(f'limitation: {limitation}')

    if args.json:
        args.json.write_text(json.dumps(report, indent=1))
        print(f'wrote {args.json}')
    if args.trace_jsonl:
        with args.trace_jsonl.open('w') as handle:
            for entry in report['trace']:
                handle.write(json.dumps(entry) + '\n')
        print(f'wrote {args.trace_jsonl}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
