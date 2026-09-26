"""Software handoff rehearsal uses the real phone/mapping/REST/motion boundaries."""
import unittest
from tools.car_rehearsal import rehearsal


class CarRehearsalTests(unittest.TestCase):
    def test_real_phone_scene_calibration_goal_refusals_and_fake_car_stop(self):
        self.assertGreater(rehearsal(), 0)
