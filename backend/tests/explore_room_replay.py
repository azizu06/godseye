"""Deterministic policy replay with raycast desks/bags, NOT hardware readiness.

Run: python -m backend.tests.explore_room_replay [output.json]
The already surveyed ground map and all-around current clearance are idealized.
The forward RGB-D observations/occlusion and voxel novelty are raycast explicitly.
No service, car adapter, fixture endpoint, or production scene is started.
"""

from dataclasses import replace
import json
import math
import sys
import time
import numpy as np
from backend.exploration import (
    ExploreSettings,
    ViewLedger,
    ScanEvidence,
    ScanWitness,
    select_view,
    visible_boundary,
    untried_frontier,
)
from backend.navigation import Grid, PlannerConfig, path_blocked
from backend.occupancy import ScanObservation, frame_evidence


def classroom():
    width, height = 4.0, 6.0
    # AABBs: x0,y0,z0,x1,y1,z1. TEST dimensions describe the fixture only.
    boxes = [
        (0, 0, 0, 0.05, 2.0, height),
        (width - 0.05, 0, 0, width, 2.0, height),
        (0, 0, 0, width, 2.0, 0.05),
        (0, 0, height - 0.05, width, 2.0, height),
    ]
    for x in (1.0, 2.4):
        for z in (1.5, 3.5):
            boxes.append((x, 0, z, x + 0.7, 0.75, z + 0.8))
    boxes += [
        (0.45, 0, 2.8, 0.7, 0.4, 3.05),
        (2.05, 0, 2.7, 2.2, 0.5, 2.9),
    ]  # bags beside aisles
    cells = np.ones((120, 80), np.uint8)
    for x0, _, z0, x1, _, z1 in boxes:
        cells[
            round(z0 / 0.05) : round(z1 / 0.05), round(x0 / 0.05) : round(x1 / 0.05)
        ] = 2
    return Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05), boxes


def capture(position, yaw, boxes, stamp, jitter=0.0):
    origin = np.array([position[0], 0.4, position[1]])
    pitch = -0.2
    horizontal = math.pi / 2
    vertical = math.radians(70)
    right = np.array([-math.cos(yaw), 0, math.sin(yaw)])
    up = np.array(
        [
            -math.sin(yaw) * math.sin(pitch),
            math.cos(pitch),
            -math.cos(yaw) * math.sin(pitch),
        ]
    )
    forward = np.array(
        [
            math.sin(yaw) * math.cos(pitch),
            math.sin(pitch),
            math.cos(yaw) * math.cos(pitch),
        ]
    )
    u, v = np.meshgrid(
        np.linspace(-0.98, 0.98, 64) * math.tan(horizontal / 2),
        np.linspace(-0.98, 0.98, 48) * math.tan(vertical / 2),
    )
    directions = forward + u.reshape(-1, 1) * right + v.reshape(-1, 1) * up
    with np.errstate(divide="ignore", invalid="ignore"):
        floor = -origin[1] / directions[:, 1]
    distance = np.where(floor > 0, floor, np.inf)
    for x0, y0, z0, x1, y1, z1 in boxes:
        with np.errstate(divide="ignore", invalid="ignore"):
            a = (np.array([x0, y0, z0]) - origin) / directions
            b = (np.array([x1, y1, z1]) - origin) / directions
        near = np.minimum(a, b).max(axis=1)
        far = np.maximum(a, b).min(axis=1)
        hit = (far >= near) & (near > 0.05)
        distance = np.minimum(distance, np.where(hit, near, np.inf))
    good = np.isfinite(distance) & (distance >= 0.05) & (distance <= 5.0)
    points = origin + (distance[good] + jitter)[:, None] * directions[good]
    # Match the regular mapping lane's bounded sampling, retaining high confidence.
    if len(points) > 2500:
        points = points[np.linspace(0, len(points) - 1, 2500).astype(int)]
    keys = tuple(frame_evidence(points).keys.tolist())
    return ScanObservation(
        stamp,
        int(stamp * 100),
        tuple(origin),
        yaw,
        horizontal,
        pitch,
        0.0,
        keys,
        len(keys),
        vertical,
    )


def replay(*, jitter=0.0, known=None):
    grid, boxes = classroom()
    settings = ExploreSettings()
    planner = PlannerConfig(
        robot_radius_m=0.20,
        margin_m=0.05,
        unknown_traversable=False,
        footprint_clearance=True,
        snap_radius_m=0.0,
        start_snap_radius_m=0.0,
    )
    position = (0.8, 0.8)
    yaw = 0.0
    stamp = 1.0
    first = capture(position, yaw, boxes, stamp)
    known = set(first.surface_keys) if known is None else set(known)
    ledger = ViewLedger()
    witness = ScanWitness(settings)
    attempted = set()
    trace = []
    started = time.perf_counter()
    selection_times = []
    limited = 0
    reason = None
    for step in range(settings.max_views):
        evidence = ScanEvidence(stamp, position, yaw, known, settings)
        for frame in range(settings.min_frames):
            stamp += 1.0
            obs = capture(
                position, yaw, boxes, stamp, jitter * (1 if frame % 2 == 0 else -1)
            )
            evidence.accept(obs)
        if not evidence.ready:
            reason = "scan_capture_unusable"
            break
        new = evidence.new_surface_keys
        known |= new
        signature = visible_boundary(grid, position, yaw, obs, 0.0, settings)
        ledger.record(position, yaw, signature)
        witness.record(position, yaw, 0, len(new))
        if len(new) < settings.low_gain_surface_voxels:
            attempted.update(signature)
        trace.append(
            dict(
                view=step + 1,
                position=[round(v, 3) for v in position],
                yaw_degrees=round(math.degrees(yaw) % 360),
                new_surface_voxels=len(new),
                observed_surface_voxels=len(known),
                predicted_boundary_cells=len(signature),
            )
        )
        if witness.saturated and not untried_frontier(grid, attempted):
            reason = "scan_diminishing_returns"
            break
        t = time.perf_counter()
        result = select_view(grid, planner, position, yaw, obs, 0.0, ledger, settings)
        selection_times.append(time.perf_counter() - t)
        limited += int(result.search_limited)
        if result.reason:
            reason = (
                "scan_accessible_exhausted"
                if result.reason == "scan_views_exhausted"
                and witness.low >= settings.low_gain_views
                else result.reason
            )
            break
        if path_blocked(grid, result.view.points, planner):
            raise AssertionError("unsafe planned path")
        position, yaw = result.view.position, result.view.yaw
    reason = reason or "scan_budget"
    return (
        dict(
            fixture="4 x 6 m classroom, four desks and two bags, TEST geometry",
            clearance="Ideal freshly observed full footprint; not a physical phone/mount claim",
            sensor="Forward raycast depth,2500high-confidence samples,5m mapping bound",
            policy="Production defaults; travel and settle simulated, evidence captures distinct",
            depth_jitter_m=jitter,
            terminal=reason,
            views=len(trace),
            translated_positions=len(witness.positions),
            heading_sectors=len(witness.sectors),
            low_gain_tail=witness.low,
            selection_limited=limited,
            max_selection_ms=round(max(selection_times, default=0) * 1000, 1),
            elapsed_s=round(time.perf_counter() - started, 2),
            trace=trace,
        ),
        known,
    )


if __name__ == "__main__":
    fresh, known = replay()
    revisited, _ = replay(known=known)
    jittered, _ = replay(jitter=0.002, known=known)
    output = dict(fresh_scan=fresh, revisit=revisited, jittered_revisit=jittered)
    path = (
        sys.argv[1] if len(sys.argv) > 1 else "/tmp/godseye-exploration-room-trace.json"
    )
    with open(path, "w") as stream:
        json.dump(output, stream, indent=2)
    print(
        json.dumps(
            {
                name: {k: v for k, v in report.items() if k != "trace"}
                for name, report in output.items()
            },
            indent=2,
        )
    )
    print(path)
