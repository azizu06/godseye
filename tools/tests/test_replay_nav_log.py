"""Hardware-free checks for tools/replay_nav_log.py: tiny synthetic nav-log fixtures only."""
import base64
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import numpy as np

from tools.replay_nav_log import LogSource, ReplayError, main, replay_run


def _occupancy_message(cells: np.ndarray, *, origin=(0., 0.), cell_m=.1) -> dict:
    height, width = cells.shape
    return dict(version=1, type='occupancy', origin=list(origin), cell_m=cell_m,
               width=width, height=height,
               cells=base64.b64encode(cells.astype(np.uint8).tobytes()).decode())


def _pose_message(x, z, yaw, tracking='normal') -> dict:
    return dict(version=1, type='pose', position=[x, 0., z], yaw_rad=yaw, tracking=tracking)


def _write_run(run_dir: Path, live_lines: list[dict], *, manifest: dict | None = None,
              summary: dict | None = None, extra_live_text: str = ''):
    run_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (run_dir / 'manifest.json').write_text(json.dumps(manifest))
    (run_dir / 'events.jsonl').write_text('')
    text = '\n'.join(json.dumps(line) for line in live_lines)
    if text:
        text += '\n'
    (run_dir / 'live.jsonl').write_text(text + extra_live_text)
    if summary is not None:
        (run_dir / 'summary.json').write_text(json.dumps(summary))


def _goal_run_lines():
    free = np.ones((40, 40), dtype=np.uint8)  # 4m x 4m, all free
    lines = [dict(el=0.0, wall=0.0, msg=_occupancy_message(free))]
    track = [(0.0, .5, .5), (.1, .5, .5), (.2, 1.0, .5), (.3, 1.5, .5), (.4, 2.0, .5)]
    for el, x, z in track:
        lines.append(dict(el=el, wall=el, msg=_pose_message(x, z, 1.5707963267948966)))
    return lines


class GoalReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / 'run'
        _write_run(self.run_dir, _goal_run_lines(),
                  manifest=dict(source_sha='abc123', axes='ARKit world metres',
                                clock='wall = Unix seconds', occupancy='cells base64 uint8'),
                  summary=dict(end_reason='arrived', armed_s=.4, path_m=1.5))

    def tearDown(self):
        self.tmp.cleanup()

    def test_reaches_recorded_goal_through_the_real_planner(self):
        source = LogSource(self.run_dir)
        self.assertEqual(source.missing, [])
        self.assertEqual(len(source.poses), 5)
        self.assertEqual(len(source.occupancies), 1)
        report = replay_run(source, kind='goal', goal=(2.0, .5))
        self.assertEqual(report['replay']['stop_reason'], 'arrived')
        self.assertGreaterEqual(report['replay']['replans'], 1)
        self.assertEqual(report['summary_comparison']['recorded_end_reason'], 'arrived')
        self.assertTrue(report['trace'])
        for entry in report['trace']:
            self.assertIn('status', entry)

    def test_cli_writes_json_report(self):
        out = Path(self.tmp.name) / 'report.json'
        code = main([str(self.run_dir), '--goal', '2.0', '.5', '--json', str(out), '--quiet'])
        self.assertEqual(code, 0)
        report = json.loads(out.read_text())
        self.assertEqual(report['replay']['stop_reason'], 'arrived')

    def test_never_opens_a_socket(self):
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('replay must not open sockets')):
            source = LogSource(self.run_dir)
            report = replay_run(source, kind='goal', goal=(2.0, .5))
        self.assertEqual(report['replay']['stop_reason'], 'arrived')

    def test_source_imports_no_networking_or_app_modules(self):
        import ast

        text = Path(__file__).resolve().parents[1].joinpath('replay_nav_log.py').read_text()
        tree = ast.parse(text)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {'backend.app', 'websockets', 'socket', 'serial', 'bleak', 'urllib.request', 'requests'}
        self.assertEqual(imported & forbidden, set())


class ExploreReplayTests(unittest.TestCase):
    def test_finds_a_frontier_from_a_partial_map(self):
        cells = np.zeros((40, 40), dtype=np.uint8)  # mostly unknown
        cells[13:18, 13:18] = 1  # a small known-free patch around the start
        lines = [dict(el=0.0, wall=0.0, msg=_occupancy_message(cells))]
        for el in (0.0, .1, .2):
            lines.append(dict(el=el, wall=el, msg=_pose_message(1.5, 1.5, 0.)))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            _write_run(run_dir, lines)
            source = LogSource(run_dir)
            report = replay_run(source, kind='explore')
        self.assertIn(report['replay']['stop_reason'], (None, 'explore_complete', 'no_path', 'search_limit'))
        self.assertGreaterEqual(report['replay']['replans'], 1)


class MalformedInputTests(unittest.TestCase):
    def test_missing_live_jsonl_is_reported_not_raised_as_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            run_dir.mkdir()
            with self.assertRaises(ReplayError):
                LogSource(run_dir)
            self.assertEqual(main([str(run_dir), '--quiet']), 2)

    def test_malformed_json_line_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            _write_run(run_dir, _goal_run_lines(), extra_live_text='{not json\n')
            source = LogSource(run_dir)
        self.assertEqual(len(source.malformed_live_lines), 1)
        self.assertIn('malformed line(s) in live.jsonl', ' '.join(source.compatibility_notes()))

    def test_malformed_occupancy_cells_are_skipped_not_fatal(self):
        bad = dict(el=0.0, wall=0.0, msg=dict(version=1, type='occupancy', origin=[0., 0.], cell_m=.1,
                                             width=4, height=4, cells=base64.b64encode(b'\x01\x02').decode()))
        lines = [bad] + _goal_run_lines()[1:]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            _write_run(run_dir, lines)
            source = LogSource(run_dir)
            report = replay_run(source, kind='goal', goal=(2.0, .5))
        self.assertEqual(report['recorded']['occupancy_malformed'], 1)
        # No good occupancy at all -> the planner sees map_unknown throughout.
        self.assertEqual(report['replay']['stop_reason'], 'end_of_recorded_log')

    def test_no_recorded_pose_is_reported_without_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            free = np.ones((10, 10), dtype=np.uint8)
            _write_run(run_dir, [dict(el=0.0, wall=0.0, msg=_occupancy_message(free))])
            self.assertEqual(main([str(run_dir), '--quiet']), 2)


if __name__ == '__main__':
    unittest.main()
