"""Bounded next-view policy on measured test geometry, never real rover dimensions."""

import math
import unittest
import numpy as np
from backend.navigation import Grid, PlannerConfig
from backend.exploration import (
    ExploreSettings,
    ViewLedger,
    select_view,
    visible_boundary,
    ScanEvidence,
)
from backend.occupancy import ScanObservation


def observation(t=1.0, x=1.0, z=1.0, yaw=0.0, keys=(1, 2, 3)):
    # Camera forward +Z, camera height0.4m, 90degree TEST field of view.
    return ScanObservation(
        t,
        int(t * 100),
        (x, 0.1, z),
        yaw,
        math.pi / 2,
        -0.2,
        0.0,
        tuple(keys) + tuple(range(1000, 1016)),
        20,
    )


def room():
    cells = np.ones((40, 40), np.uint8)
    cells[[0, -1], :] = 2
    cells[:, [0, -1]] = 2
    grid = Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05)
    grid.cells.setflags(
        write=True
    )  # fixture owns its copy; production snapshots remain immutable
    return grid


CFG = PlannerConfig(
    robot_radius_m=0.10,
    margin_m=0.0,
    unknown_traversable=False,
    footprint_clearance=True,
    start_snap_radius_m=0.0,
    snap_radius_m=0.0,
)


class ViewTests(unittest.TestCase):
    def test_wall_and_unknown_stop_visibility(self):
        grid = room()
        grid.cells[24, :] = 2
        grid.cells[25:, :] = 0
        self.assertFalse(
            visible_boundary(
                grid, (1.0, 1.0), 0.0, observation(), 0.0, ExploreSettings()
            )
        )
        grid.cells[24, :] = 0
        seen = visible_boundary(
            grid, (1.0, 1.0), 0.0, observation(), 0.0, ExploreSettings()
        )
        self.assertTrue(seen)
        self.assertTrue(all(z == 24 for x, z in seen))  # no gain behind firstunknown

    def test_view_is_reachable_and_no_room_percentage(self):
        grid = room()
        grid.cells[:, 26] = 2
        grid.cells[:, 27:] = 0
        result = select_view(
            grid,
            CFG,
            (1.0, 1.0),
            0.0,
            observation(),
            0.0,
            ViewLedger(),
            ExploreSettings(),
        )
        self.assertIsNone(result.reason)
        self.assertLess(result.view.position[0], 1.3)
        self.assertLessEqual(result.view.path_length, 0.75 + 1e-6)

    def test_search_budget_is_not_completion(self):
        result = select_view(
            room(),
            CFG,
            (1.0, 1.0),
            0.0,
            observation(),
            0.0,
            ViewLedger(),
            ExploreSettings(max_expansions=2),
        )
        self.assertEqual(result.reason, "scan_search_limit")

    def test_ledger_stable_under_repeated_evidence_but_reopens_changed_view(self):
        ledger = ViewLedger()
        ledger.record((1.0, 1.0), 0.0, frozenset({(5, 6)}))
        self.assertTrue(ledger.contains((1.0, 1.0), 0.0, frozenset({(5, 6)})))
        self.assertFalse(ledger.contains((1.0, 1.0), 0.0, frozenset({(5, 7)})))

    def test_downward_camera_is_unsupported_not_exhausted(self):
        from dataclasses import replace

        result = select_view(
            room(),
            CFG,
            (1.0, 1.0),
            0.0,
            replace(observation(), pitch=-math.pi / 2),
            0.0,
            ViewLedger(),
            ExploreSettings(),
        )
        self.assertEqual(result.reason, "scan_view_unsupported")


class EvidenceTests(unittest.TestCase):
    def test_queued_replayed_and_wrong_pose_captures_do_not_count(self):
        evidence = ScanEvidence(10.0, (1.0, 1.0), 0.0, {1, 2}, ExploreSettings())
        self.assertFalse(evidence.accept(observation(9.0, keys=(3,))))
        self.assertFalse(evidence.accept(observation(11.0, x=2.0, keys=(3,))))
        self.assertTrue(evidence.accept(observation(11.0, keys=(3,))))
        self.assertFalse(evidence.accept(observation(11.0, keys=(4,))))
        self.assertEqual(evidence.frames, 1)
        self.assertEqual(evidence.new_surface_keys, set())

    def test_blank_and_out_of_bounds_depth_cannot_prove_saturation(self):
        from dataclasses import replace

        evidence = ScanEvidence(10.0, (1.0, 1.0), 0.0, set(), ExploreSettings())
        for t in (11.0, 12.0, 13.0):
            self.assertFalse(
                evidence.accept(
                    replace(observation(t), in_bounds_samples=0, surface_keys=())
                )
            )
        self.assertFalse(evidence.ready)

    def test_new_upper_surface_counts_even_without_ground_change(self):
        evidence = ScanEvidence(10.0, (1.0, 1.0), 0.0, {1, 2}, ExploreSettings())
        for t in (11.0, 12.0, 13.0):
            self.assertTrue(evidence.accept(observation(t, keys=(1, 2, 99, 100))))
        self.assertTrue(evidence.ready)
        self.assertTrue({99, 100}.issubset(evidence.new_surface_keys))


class FrameMetadataTests(unittest.TestCase):
    def test_calibration_comes_from_same_frame_and_lattice_support(self):
        from backend.exploration import observation_from_frame
        from backend.app import decode_frame
        from backend.mapping import build_point_chunk
        from backend.occupancy import frame_evidence
        from backend.tests.test_occupancy import floor_frame

        payload = floor_frame("TEST", 7, 12.0)
        frame = decode_frame(payload)
        evidence = frame_evidence(build_point_chunk(payload, "TEST", 1).positions)
        obs = observation_from_frame(frame, evidence)
        self.assertEqual((obs.frame_id, obs.t_capture), (7, 12.0))
        self.assertEqual(obs.surface_keys, tuple(evidence.keys.tolist()))
        self.assertEqual(obs.position, tuple(frame.transform[12:15]))
        self.assertAlmostEqual(obs.horizontal_fov, math.pi / 2)
        self.assertLess(abs(obs.roll), 1e-6)


class SaturationTests(unittest.TestCase):
    def test_diminishing_returns_requires_broad_translated_low_gain_evidence(self):
        from backend.exploration import ScanWitness

        witness = ScanWitness(ExploreSettings())
        for i in range(12):
            witness.record((1.0, 1.0), (i % 4) * math.pi / 2, 0, 0)
        self.assertFalse(witness.saturated)  # spinning in one spot is insufficient
        for point in ((1.6, 1.0), (2.2, 1.0)):
            witness.record(point, 0.0, 0, 0)
        self.assertTrue(witness.saturated)
        witness.record((2.2, 1.0), 0.0, 0, 30)  # real new wall geometry reopens work
        self.assertFalse(witness.saturated)

    def test_default_room_can_choose_before_visibility_budget_expires(self):
        cells = np.ones((120, 80), np.uint8)
        cells[[0, -1], :] = 2
        cells[:, [0, -1]] = 2
        grid = Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05)
        choice = select_view(
            grid,
            CFG,
            (0.8, 0.8),
            0.0,
            observation(x=0.8, z=0.8),
            0.0,
            ViewLedger(),
            ExploreSettings(),
        )
        self.assertIsNotNone(choice.view)
        self.assertIsNone(choice.reason)


class AdversarialViewTests(unittest.TestCase):
    def test_crop_expansion_preserves_world_boundary_identity(self):
        cells = np.ones((40, 40), np.uint8)
        cells[30:, :] = 0
        first = Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05)
        padded = Grid.from_array(
            np.pad(cells, ((5, 0), (7, 0)), constant_values=0),
            origin=(-0.35, -0.25),
            cell_m=0.05,
        )
        obs = observation()
        a = visible_boundary(first, (1.0, 1.0), 0.0, obs, 0.0, ExploreSettings())
        b = visible_boundary(padded, (1.0, 1.0), 0.0, obs, 0.0, ExploreSettings())
        self.assertTrue(a)
        self.assertEqual(a, b)
        ledger = ViewLedger()
        ledger.record((1.0, 1.0), 0.0, a)
        self.assertTrue(ledger.contains((1.0, 1.0), 0.0, b))

    def test_diagonal_corner_wall_blocks_unknown_gain(self):
        from dataclasses import replace

        cells = np.ones((40, 40), np.uint8)
        cells[21:, 21:] = 0
        cells[20, 21] = 2
        cells[21, 20] = 2
        grid = Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05)
        obs = replace(observation(x=1.025, z=1.025), horizontal_fov=0.1)
        self.assertFalse(
            visible_boundary(
                grid, (1.025, 1.025), math.pi / 4, obs, 0.0, ExploreSettings()
            )
        )

    def test_untried_frontier_outside_candidate_work_cannot_claim_saturation(self):
        from backend.exploration import untried_frontier

        cells = np.ones((100, 100), np.uint8)
        cells[90:, :] = 0
        grid = Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05)
        self.assertTrue(untried_frontier(grid, set()))

    def test_short_leg_signature_belongs_to_actual_endpoint(self):
        cells = np.ones((100, 100), np.uint8)
        cells[70:, :] = 0
        grid = Grid.from_array(cells, origin=(0.0, 0.0), cell_m=0.05)
        obs = observation()
        choice = select_view(
            grid, CFG, (1.0, 1.0), 0.0, obs, 0.0, ViewLedger(), ExploreSettings()
        )
        self.assertIsNotNone(choice.view)
        self.assertEqual(
            choice.view.signature,
            visible_boundary(
                grid, choice.view.position, choice.view.yaw, obs, 0.0, ExploreSettings()
            ),
        )
        self.assertLessEqual(choice.view.path_length, 0.75 + 1e-6)


class NoiseMetricTests(unittest.TestCase):
    def test_neighbor_jitter_is_quiet_but_distant_new_surface_counts(self):
        from backend.occupancy import frame_evidence
        from dataclasses import replace

        old = set(frame_evidence([[0.025, 0.15, 0.025]]).keys.tolist())
        nearby = tuple(frame_evidence([[0.051, 0.169, 0.051]]).keys.tolist())
        distant = tuple(frame_evidence([[0.25, 0.45, 0.25]]).keys.tolist())
        evidence = ScanEvidence(1.0, (1.0, 1.0), 0.0, old, ExploreSettings())
        for stamp in (2.0, 3.0, 4.0):
            obs = replace(
                observation(stamp),
                surface_keys=nearby + distant + tuple(range(1000, 1016)),
            )
            self.assertTrue(evidence.accept(obs))
        self.assertTrue(evidence.ready)
        self.assertTrue(set(distant).issubset(evidence.new_surface_keys))
        self.assertFalse(set(nearby) & evidence.new_surface_keys)
        self.assertIn(
            nearby[0], obs.surface_keys
        )  # actual captured geometry remains untouched
