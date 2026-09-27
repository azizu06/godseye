"""Bounded next-view policy on measured test geometry, never real rover dimensions."""
import math
import unittest
import numpy as np
from backend.navigation import Grid, PlannerConfig
from backend.exploration import ExploreSettings, ViewLedger, select_view, visible_boundary, ScanEvidence
from backend.occupancy import ScanObservation


def observation(t=1., x=1., z=1., yaw=0., keys=(1, 2, 3)):
    # Camera forward +Z, camera height0.4m, 90degree TEST field of view.
    return ScanObservation(t, int(t*100), (x, .1, z), yaw, math.pi/2,
                           -.2, 0., tuple(keys) + tuple(range(1000,1016)), 20)


def room():
    cells = np.ones((40, 40), np.uint8)
    cells[[0, -1], :] = 2
    cells[:, [0, -1]] = 2
    grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.05)
    grid.cells.setflags(write=True)  # fixture owns its copy; production snapshots remain immutable
    return grid


CFG = PlannerConfig(robot_radius_m=.10, margin_m=0., unknown_traversable=False,
                    footprint_clearance=True, start_snap_radius_m=0., snap_radius_m=0.)


class ViewTests(unittest.TestCase):
    def test_wall_and_unknown_stop_visibility(self):
        grid = room()
        grid.cells[24, :] = 2
        grid.cells[25:, :] = 0
        self.assertFalse(visible_boundary(grid, (1., 1.), 0., observation(), 0., ExploreSettings()))
        grid.cells[24, :] = 0
        seen = visible_boundary(grid, (1., 1.), 0., observation(), 0., ExploreSettings())
        self.assertTrue(seen)
        self.assertTrue(all(z == 24 for x, z in seen))  # no gain behind firstunknown

    def test_view_is_reachable_and_no_room_percentage(self):
        grid = room()
        grid.cells[:, 26] = 2
        grid.cells[:, 27:] = 0
        result = select_view(grid, CFG, (1., 1.), 0., observation(), 0., ViewLedger(), ExploreSettings())
        self.assertIsNone(result.reason)
        self.assertLess(result.view.position[0], 1.3)
        self.assertLessEqual(result.view.path_length, .75 + 1e-6)

    def test_search_budget_is_not_completion(self):
        result = select_view(room(), CFG, (1., 1.), 0., observation(), 0., ViewLedger(),
                             ExploreSettings(max_expansions=2))
        self.assertEqual(result.reason, 'scan_search_limit')

    def test_ledger_stable_under_repeated_evidence_but_reopens_changed_view(self):
        ledger = ViewLedger()
        ledger.record((1., 1.), 0., frozenset({(5, 6)}))
        self.assertTrue(ledger.contains((1., 1.), 0., frozenset({(5, 6)})))
        self.assertFalse(ledger.contains((1., 1.), 0., frozenset({(5, 7)})))

    def test_downward_camera_is_unsupported_not_exhausted(self):
        from dataclasses import replace
        result = select_view(room(), CFG, (1., 1.), 0., replace(observation(), pitch=-math.pi/2),
                             0., ViewLedger(), ExploreSettings())
        self.assertEqual(result.reason, 'scan_view_unsupported')


class EvidenceTests(unittest.TestCase):
    def test_queued_replayed_and_wrong_pose_captures_do_not_count(self):
        evidence = ScanEvidence(10., (1., 1.), 0., {1, 2}, ExploreSettings())
        self.assertFalse(evidence.accept(observation(9., keys=(3,))))
        self.assertFalse(evidence.accept(observation(11., x=2., keys=(3,))))
        self.assertTrue(evidence.accept(observation(11., keys=(3,))))
        self.assertFalse(evidence.accept(observation(11., keys=(4,))))
        self.assertEqual(evidence.frames, 1)
        self.assertEqual(evidence.new_surface_keys, set())

    def test_blank_and_out_of_bounds_depth_cannot_prove_saturation(self):
        from dataclasses import replace
        evidence = ScanEvidence(10., (1., 1.), 0., set(), ExploreSettings())
        for t in (11.,12.,13.):
            self.assertFalse(evidence.accept(replace(observation(t), in_bounds_samples=0, surface_keys=())))
        self.assertFalse(evidence.ready)

    def test_new_upper_surface_counts_even_without_ground_change(self):
        evidence = ScanEvidence(10., (1., 1.), 0., {1,2}, ExploreSettings())
        for t in (11.,12.,13.):
            self.assertTrue(evidence.accept(observation(t, keys=(1,2,99,100))))
        self.assertTrue(evidence.ready)
        self.assertTrue({99,100}.issubset(evidence.new_surface_keys))

class FrameMetadataTests(unittest.TestCase):
    def test_calibration_comes_from_same_frame_and_lattice_support(self):
        from backend.exploration import observation_from_frame
        from backend.app import decode_frame
        from backend.mapping import build_point_chunk
        from backend.occupancy import frame_evidence
        from backend.tests.test_occupancy import floor_frame
        payload=floor_frame('TEST', 7, 12.)
        frame=decode_frame(payload)
        evidence=frame_evidence(build_point_chunk(payload,'TEST',1).positions)
        obs=observation_from_frame(frame,evidence)
        self.assertEqual((obs.frame_id,obs.t_capture),(7,12.))
        self.assertEqual(obs.surface_keys,tuple(evidence.keys.tolist()))
        self.assertEqual(obs.position,tuple(frame.transform[12:15]))
        self.assertAlmostEqual(obs.horizontal_fov,math.pi/2)
        self.assertLess(abs(obs.roll),1e-6)
