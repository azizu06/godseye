"""Real asynchronous replanning and motor packets; invented wheel response only."""
from dataclasses import replace
import math
import unittest

from backend.prototype import PrototypeActuation
from backend.tests.test_navigator import FakeOccupancy, Harness, Rover, initial_plan
from tools.scout_benchmark import Footprint, Pose, SyntheticResponse, collides, integrate, make_scenarios


class FlowingDetourTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_hallway_obstacle_triggers_an_early_moving_detour(self):
        scene = next(s for s in make_scenarios() if s.name == 'obstacle_detour')
        body, response, profile = Footprint(), SyntheticResponse(), PrototypeActuation(variable_arc_pwm=True)
        clear = scene.grid.cells.copy()
        clear[52:64, 26:38] = 1  # obstacle at x=1.3..1.9, z=2.6..3.2 is not observed yet

        class Occupancy(FakeOccupancy):
            def snapshot(self):
                return replace(super().snapshot(), inflation_m=body.radius_m + body.margin_m)

        occupancy = Occupancy(clear)
        trace, collisions = [], []
        discovered_at = []

        class PacketRover(Rover):
            def drive(self, v, w):
                self.commands.append((v, w))
                packet = profile.command(v, w)
                actual_v, actual_w = response.rates(packet)
                trace.append((self.z, v, w, actual_v, actual_w))
                pose = Pose(self.x, self.z, self.yaw)
                steps = max(1, math.ceil((abs(actual_v) + abs(actual_w) * body.radius_m) * .1 / .01))
                for _ in range(steps):
                    pose = integrate(pose, actual_v, actual_w, .1 / steps)
                    if collides(scene.grid, pose, body):
                        collisions.append(pose)
                self.x, self.z, self.yaw = pose.x, pose.z, pose.yaw
                if self.z >= .9 and not discovered_at:
                    discovered_at.append(self.z)
                    occupancy.set(scene.grid.cells)

        rover = PacketRover(scene.start.x, scene.start.z, scene.start.yaw)
        h = Harness(rover, occupancy, follower=profile.follower(), replan_s=4., blocked_check_s=.005)
        plan = initial_plan(clear, (rover.x, rover.z), scene.goal,
                            replace(h.nav.settings.planner, robot_radius_m=body.radius_m + body.margin_m))
        self.addAsyncCleanup(h.nav.aclose)
        h.nav.start_goal(scene.goal, plan, h.generation)
        await h.finished()
        self.assertEqual(h.stops, ['arrived'])
        self.assertEqual(collisions, [])
        self.assertTrue(discovered_at)
        first_turn = next(p for p in trace if abs(p[4]) > .01)
        self.assertLess(first_turn[0], 1.4, 'steer at least 1.2 m before the obstacle')
        travelling = [p for p in trace if .95 < p[0] < 5.]
        self.assertTrue(travelling)
        self.assertTrue(all(p[3] > 0 for p in travelling), 'no stop/pivot while passing the obstacle')
        self.assertTrue(all(abs(p[1] - .2) < 1e-9 for p in travelling), 'retain nominal cruise')


if __name__ == '__main__':
    unittest.main()
