"""Fresh raw depth can clear disproved navigation obstacles, but not uncertain ones."""
from types import SimpleNamespace
import unittest

import numpy as np

from backend import occupancy
from backend.occupancy import OccupancyGrid, frame_evidence
from backend.tests.test_occupancy import FLOOR_Y, SESSION, plane


def view(capture, distance=3., confidence=2):
    transform = np.eye(4)
    transform[1, 3] = -.5
    return SimpleNamespace(
        t_capture=capture, session_id=SESSION[0], map_epoch=SESSION[1],
        transform=transform, intrinsics=np.array([[40., 0., 40.], [0., 40., 32.], [0., 0., 1.]]),
        image_size=(80, 64), depth=np.full((16, 20), distance, np.float32),
        confidence=np.full((16, 20), confidence, np.uint8))


def old_scene():
    floor = plane(-.5, .5, -1.5, -.5, FLOOR_Y)
    obstacle = np.array([[0., FLOOR_Y + y, -1.] for y in np.arange(.2, .5, .02)])
    return floor, obstacle


class DepthRetirementTests(unittest.TestCase):
    def test_navigation_cell_reopens_only_after_two_accepted_clear_views(self):
        grid = OccupancyGrid(SESSION)
        floor, obstacle = old_scene()
        for t in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate([floor, obstacle])), t,
                        depth_view=view(t, distance=.8))
        self.assertEqual(grid.map_snapshot().cell(0., -1.), occupancy.OCCUPIED)
        for t in (4., 5.):
            current = view(t)
            retired, cursor = grid.retirement_candidates(current)
            grid.commit(frame_evidence(floor), t, retirement_keys=retired,
                        retirement_cursor=cursor, depth_view=current)
            self.assertEqual(grid.map_snapshot().cell(0., -1.),
                             occupancy.OCCUPIED if t == 4. else occupancy.FREE)

    def test_tracking_break_cannot_combine_clear_views_across_the_gap(self):
        grid = OccupancyGrid(SESSION)
        floor, obstacle = old_scene()
        for t in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate([floor, obstacle])), t,
                        depth_view=view(t, distance=.8))
        first = view(4.)
        retired, cursor = grid.retirement_candidates(first)
        grid.commit(frame_evidence(floor), 4., retirement_keys=retired,
                    retirement_cursor=cursor, depth_view=first)
        grid.clear_depth_history()
        after_gap = view(5.)
        retired, cursor = grid.retirement_candidates(after_gap)
        grid.commit(frame_evidence(floor), 5., retirement_keys=retired,
                    retirement_cursor=cursor, depth_view=after_gap)
        self.assertEqual(grid.map_snapshot().cell(0., -1.), occupancy.OCCUPIED)
        final = view(6.)
        retired, cursor = grid.retirement_candidates(final)
        grid.commit(frame_evidence(floor), 6., retirement_keys=retired,
                    retirement_cursor=cursor, depth_view=final)
        self.assertEqual(grid.map_snapshot().cell(0., -1.), occupancy.FREE)

    def test_two_clear_views_remove_old_obstacle_but_keep_floor(self):
        grid = OccupancyGrid(SESSION)
        floor, obstacle = old_scene()
        for t in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate([floor, obstacle])), t)
        self.assertEqual(grid.map_snapshot().cell(0., -1.), occupancy.OCCUPIED)
        find = getattr(occupancy, 'contradicted_obstacle_keys', lambda *args: np.empty(0, np.int64))
        removed = find(grid._keys, grid._hits, grid.map_snapshot().floor_y,
                       grid.obstacle_from_m, view(4.), view(5.))
        self.assertGreater(len(removed), 0)
        self.assertLess(len(removed), grid.voxels)

    def test_medium_confidence_background_reopens_moving_person_space(self):
        grid = OccupancyGrid(SESSION)
        floor, obstacle = old_scene()
        for t in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate([floor, obstacle])), t,
                        depth_view=view(t, distance=.8))
        for t in (4., 5.):
            current = view(t, confidence=1)
            retired, cursor = grid.retirement_candidates(current)
            grid.commit(frame_evidence(floor), t, retirement_keys=retired,
                        retirement_cursor=cursor, depth_view=current)
        self.assertEqual(grid.map_snapshot().cell(0., -1.), occupancy.FREE)

    def test_uncertain_or_occluded_depth_cannot_clear_obstacle(self):
        grid = OccupancyGrid(SESSION)
        floor, obstacle = old_scene()
        for t in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate([floor, obstacle])), t)
        find = getattr(occupancy, 'contradicted_obstacle_keys', lambda *args: np.empty(0, np.int64))
        for first, second in ((view(4., confidence=0), view(5.)),
                              (view(4.), view(5., distance=.8)),
                              (view(5.), view(4.))):
            with self.subTest(first=first.t_capture, second=second.t_capture):
                removed = find(grid._keys, grid._hits, grid.map_snapshot().floor_y,
                               grid.obstacle_from_m, first, second)
                self.assertEqual(len(removed), 0)
