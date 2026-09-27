"""Fake-only unit tests for the Explore obstacle debounce gate. No car/network."""
import unittest

from backend.explore_obstacle_gate import ExploreObstacleGate, GateConfig, PathObservation

BLOCKED = PathObservation(path_blocked=True, depth_known=True)
CLEAR = PathObservation(path_blocked=False, depth_known=True)
UNKNOWN_BLOCKED_LABEL = PathObservation(path_blocked=False, depth_known=False)
IMMINENT = PathObservation(path_blocked=True, depth_known=True, imminent=True)


class OffPathPosterUncertaintyTests(unittest.TestCase):
    def test_bare_label_without_depth_never_pauses(self):
        gate = ExploreObstacleGate()
        t = 0.0
        for _ in range(10):
            decision = gate.decide(UNKNOWN_BLOCKED_LABEL, t)
            self.assertFalse(decision.yielding)
            t += 0.1

    def test_off_corridor_detection_with_depth_is_ordinary_clear_evidence(self):
        gate = ExploreObstacleGate()
        decision = gate.decide(CLEAR, 0.0)
        self.assertFalse(decision.yielding)
        self.assertIsNone(decision.wait_reason)


class ConfirmedPathCrossingTests(unittest.TestCase):
    def test_pauses_only_after_configured_confirm_frames(self):
        gate = ExploreObstacleGate(GateConfig(pause_confirm_frames=3))
        self.assertFalse(gate.decide(BLOCKED, 0.0).yielding)
        self.assertFalse(gate.decide(BLOCKED, 0.1).yielding)
        third = gate.decide(BLOCKED, 0.2)
        self.assertTrue(third.yielding)
        self.assertEqual(third.wait_reason, "path_crossing")

    def test_a_clear_frame_resets_confirm_progress(self):
        gate = ExploreObstacleGate(GateConfig(pause_confirm_frames=3))
        gate.decide(BLOCKED, 0.0)
        gate.decide(BLOCKED, 0.1)
        gate.decide(CLEAR, 0.2)
        self.assertFalse(gate.decide(BLOCKED, 0.3).yielding)
        self.assertFalse(gate.decide(BLOCKED, 0.4).yielding)
        self.assertTrue(gate.decide(BLOCKED, 0.5).yielding)

    def test_imminent_evidence_bypasses_confirm_debounce(self):
        gate = ExploreObstacleGate(GateConfig(pause_confirm_frames=3))
        decision = gate.decide(IMMINENT, 0.0)
        self.assertTrue(decision.yielding)
        self.assertEqual(decision.wait_reason, "imminent")


class SustainedClearResumeTests(unittest.TestCase):
    def _yielding_gate(self, config=None):
        gate = ExploreObstacleGate(config or GateConfig())
        for i in range(3):
            gate.decide(BLOCKED, i * 0.1)
        return gate

    def test_resumes_after_clear_streak_plus_hold(self):
        gate = self._yielding_gate(GateConfig(resume_clear_frames=3, resume_hold_s=1.0))
        t = 1.0
        for _ in range(3):
            decision = gate.decide(CLEAR, t)
            self.assertTrue(decision.yielding)
            t += 0.1
        # Clear streak satisfied but hold not yet elapsed.
        still_yielding = gate.decide(CLEAR, t)
        self.assertTrue(still_yielding.yielding)
        self.assertFalse(still_yielding.resumed_this_tick)
        # Hold elapses.
        resumed = gate.decide(CLEAR, t + 1.0)
        self.assertFalse(resumed.yielding)
        self.assertTrue(resumed.resumed_this_tick)

    def test_hold_restarts_if_clear_streak_breaks(self):
        gate = self._yielding_gate(GateConfig(resume_clear_frames=2, resume_hold_s=1.0))
        gate.decide(CLEAR, 1.0)
        gate.decide(CLEAR, 1.1)  # clear streak of 2 reached, hold starts at 1.1
        gate.decide(BLOCKED, 1.2)  # breaks the streak, re-confirms block
        gate.decide(CLEAR, 1.3)
        still_yielding = gate.decide(CLEAR, 1.4)
        self.assertTrue(still_yielding.yielding)
        # Old hold start (1.1) would have elapsed by now; a fresh hold (from 1.4) must not have.
        not_yet = gate.decide(CLEAR, 2.2)
        self.assertTrue(not_yet.yielding)
        self.assertFalse(not_yet.resumed_this_tick)
        # The fresh hold elapses.
        resumed = gate.decide(CLEAR, 2.4)
        self.assertFalse(resumed.yielding)
        self.assertTrue(resumed.resumed_this_tick)


class StaleUnknownNeverProvesClearTests(unittest.TestCase):
    def test_unknown_depth_while_yielding_never_resumes(self):
        gate = ExploreObstacleGate()
        for i in range(3):
            gate.decide(BLOCKED, i * 0.1)
        t = 1.0
        for _ in range(50):
            decision = gate.decide(UNKNOWN_BLOCKED_LABEL, t)
            self.assertTrue(decision.yielding)
            self.assertFalse(decision.resumed_this_tick)
            t += 0.1


class SessionChangeTests(unittest.TestCase):
    def test_reset_clears_yield_state_and_streaks(self):
        gate = ExploreObstacleGate()
        for i in range(3):
            gate.decide(BLOCKED, i * 0.1)
        self.assertTrue(gate.decide(BLOCKED, 0.3).yielding)
        gate.reset()
        self.assertFalse(gate.decide(CLEAR, 1.0).yielding)
        # Fresh confirm progress required again after reset.
        self.assertFalse(gate.decide(BLOCKED, 1.1).yielding)
        self.assertFalse(gate.decide(BLOCKED, 1.2).yielding)
        self.assertTrue(gate.decide(BLOCKED, 1.3).yielding)


class UnknownBreaksResumeHoldRegressionTests(unittest.TestCase):
    """Regression for the integration owner's reproduced defect in dbf6097:
    3 CLEAR ticks starting the hold, then a single later UNKNOWN tick, must
    not let elapsed wall time alone complete the resume."""

    def test_unknown_after_hold_started_does_not_resume(self):
        gate = ExploreObstacleGate(GateConfig(resume_clear_frames=3, resume_hold_s=1.0))
        for i in range(3):
            gate.decide(BLOCKED, i * 0.1)
        self.assertTrue(gate.decide(BLOCKED, 0.3).yielding)

        gate.decide(CLEAR, 1.0)
        gate.decide(CLEAR, 1.1)
        gate.decide(CLEAR, 1.2)  # clear streak of 3 reached, hold starts at 1.2

        # A later UNKNOWN tick must break the streak/hold, not silently let
        # the already-elapsed 1.1s (2.3 - 1.2) complete the resume.
        broke = gate.decide(UNKNOWN_BLOCKED_LABEL, 2.3)
        self.assertTrue(broke.yielding)
        self.assertFalse(broke.resumed_this_tick)

        # A fresh, complete clear streak plus hold is still required to resume.
        t = 2.4
        for _ in range(3):
            gate.decide(CLEAR, t)
            t += 0.1
        still_holding = gate.decide(CLEAR, t)
        self.assertTrue(still_holding.yielding)
        resumed = gate.decide(CLEAR, t + 1.0)
        self.assertFalse(resumed.yielding)
        self.assertTrue(resumed.resumed_this_tick)


class FaultAndOperatorStopPrecedenceTests(unittest.TestCase):
    def test_external_hold_forces_yield_regardless_of_clear_evidence(self):
        gate = ExploreObstacleGate()
        decision = gate.decide(CLEAR, 0.0, external_hold=True, external_reason="operator_stop")
        self.assertTrue(decision.yielding)
        self.assertEqual(decision.wait_reason, "operator_stop")

    def test_external_hold_does_not_advance_or_corrupt_internal_debounce(self):
        gate = ExploreObstacleGate(GateConfig(resume_clear_frames=3, resume_hold_s=1.0))
        for i in range(3):
            gate.decide(BLOCKED, i * 0.1)
        self.assertTrue(gate.decide(BLOCKED, 0.3).yielding)

        # Fault latched: clear evidence arrives but must not be banked toward resume.
        for i in range(10):
            held = gate.decide(CLEAR, 1.0 + i * 0.1, external_hold=True, external_reason="fault")
            self.assertTrue(held.yielding)
            self.assertEqual(held.wait_reason, "fault")

        # Latch lifts: no auto-rearm from the mere absence of external_hold --
        # unresolved evidence still must not resume it.
        stale = gate.decide(UNKNOWN_BLOCKED_LABEL, 3.0)
        self.assertTrue(stale.yielding)
        self.assertFalse(stale.resumed_this_tick)

        # Only fresh, ordinary clear-frame evidence plus the hold can resume it.
        t = 3.1
        for _ in range(3):
            gate.decide(CLEAR, t)
            t += 0.1
        resumed = gate.decide(CLEAR, t + 1.0)
        self.assertTrue(resumed.resumed_this_tick)
        self.assertFalse(resumed.yielding)


if __name__ == "__main__":
    unittest.main()
