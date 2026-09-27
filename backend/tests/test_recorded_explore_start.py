"""Recorded Explore02/03 start decisions; no real transport or sensor operations.

The original logs contain no per-voxel height, confidence or current-depth
provenance. These classified maps prove the guard's decision, not that their
near-camera returns represent physical obstacles. No cells are cleared in
production by this test or by start recovery.
"""
import base64
import json
from pathlib import Path
import time
import unittest

import numpy as np

from backend.navigator import Navigator, NavSettings, obstacle_clearance_m, recoverable_start, recovery_step_allowed, start_clearance_diagnostics
from backend.occupancy import OccupancySnapshot
from backend.prototype import PrototypeActuation, prototype_geometry


class RecordedExploreStartTests(unittest.TestCase):
    def test_recorded_maps_reject_occupied_camera_or_footprint_overlap(self):
        records = json.loads(Path(__file__).with_name('fixtures').joinpath('recorded_explore_start.json').read_text())
        for record, expected_clearance in zip(records, (0., .050608893184434504, .10023381710052474)):
            with self.subTest(run=record['run'], el=record['sample_el']):
                msg = record['occupancy']
                geometry = prototype_geometry(record['estimated_length_m'], record['estimated_width_m'])
                cells = np.frombuffer(base64.b64decode(msg['cells']), np.uint8).reshape(msg['height'], msg['width'])
                snapshot = OccupancySnapshot(('recorded', 1), 1, time.monotonic(), (), geometry.inflation_m,
                                             tuple(msg['origin']), msg['cell_m'], cells, msg['floor_y'], True)
                settings = NavSettings(start_recovery_margin_m=.1524, follower=PrototypeActuation().follower())
                nav = Navigator(settings, pose=lambda: None, occupancy=lambda: snapshot,
                                submit=lambda *args: None, stop=lambda *args: None,
                                publish=lambda *args: None, armed_mode=lambda: 'explore')
                x, _, z = record['position']
                self.assertAlmostEqual(geometry.inflation_m, .4250805640305154)
                self.assertAlmostEqual(obstacle_clearance_m(snapshot, x, z), expected_clearance)
                diagnostic = start_clearance_diagnostics(snapshot, x, z, .1524)
                self.assertFalse(diagnostic['can_start'])
                self.assertEqual(diagnostic['reason'], 'start_blocked')
                self.assertAlmostEqual(diagnostic['nearest_occupied_m'], expected_clearance)
                self.assertAlmostEqual(diagnostic['footprint_bound_m'], .27268056403051544)
                self.assertFalse(snapshot.traversable(x, z))
                self.assertFalse(recoverable_start(snapshot, x, z, .1524))
                self.assertFalse(recovery_step_allowed(snapshot, x, z, x-.01, z, .1524))
                result = nav._plan(lambda: snapshot, (x, z), None, True, yaw=record['yaw_rad'])[-1]
                self.assertEqual(result.reason, 'start_blocked')
                self.assertEqual(result.points, [])
                # One-factor disconfirmation: an explicitly synthetic clear-map
                # counterfactual with identical frame/geometry does permit planning.
                # This is not permission to erase the recorded real returns.
                clear = OccupancySnapshot(('recorded', 1), 2, time.monotonic(), (), geometry.inflation_m,
                                          snapshot.origin, snapshot.cell_m, np.ones_like(cells), snapshot.floor_y, True)
                self.assertTrue(clear.traversable(x, z))
                self.assertTrue(nav._plan(lambda: clear, (x, z), None, True, yaw=record['yaw_rad'])[-1].ok)
