"""Rover calibration/readiness and the navigation occupancy snapshot; no hardware.

Every calibration value below is a TEST fixture, not a measurement of the real
rover. Real chassis, clearance and mount values come from Tomiwa's bench sheet.
"""
import json
import math
import os
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import app as backend_app
from backend.app import create_app
from backend.calibration import RoverCalibration, calibration_from_env, load_calibration
from backend.mapping import build_point_chunk
from backend.occupancy import OCCUPIED, OccupancyGrid
from backend.tests.test_map_transport import hello, next_of, wait_for
from backend.tests.test_occupancy import FLOOR_Y, box, feed, floor_frame, plane, send_frames
from backend.tests.test_pose_freshness import pose

SESSION = ('cal-session', 1)
# Measured-looking TEST values; they exercise the code, they do not describe the car.
TEST_CALIBRATION = dict(version=1, measured_by='TEST fixture, not a rover measurement',
                        obstacle_min_m=.065, footprint_length_m=.26, footprint_width_m=.18,
                        clearance_margin_m=.05, camera_forward_m=.08, camera_left_m=0.,
                        camera_yaw_rad=0.)
LOW_BOX = dict(x0=.6, x1=.8, z0=-.7, z1=-.5)  # asymmetric in x/z so a swap would fail


def low_box_scene(height):
    """Floor plus a box `height` m tall; 5 and 7 cm both sit below the generic 8 cm band."""
    b = LOW_BOX
    return np.concatenate([plane(-1, 1, -1, 1, FLOOR_Y),
                           box(b['x0'], b['x1'], b['z0'], b['z1'], FLOOR_Y, FLOOR_Y + height)])


class UncalibratedTests(unittest.TestCase):
    def test_uncalibrated_map_never_claims_traversability(self):
        grid = OccupancyGrid(SESSION)
        feed(grid, low_box_scene(.05))
        snapshot = grid.map_snapshot()
        self.assertFalse(snapshot.ready)
        self.assertIn('calibration_missing', snapshot.blockers)
        # The generic band still calls the 5 cm box free, which is why the
        # uncalibrated snapshot must not vouch for any cell, box or open floor.
        self.assertEqual(snapshot.cell(.7, -.6), 1)
        for x, z in [(.6, -.6), (.7, -.6), (0, 0), (-.6, .7)]:
            self.assertFalse(snapshot.traversable(x, z), (x, z))


class CalibratedTests(unittest.TestCase):
    def test_measured_threshold_marks_a_low_box_occupied_and_inflates_by_the_footprint(self):
        generic = OccupancyGrid(SESSION)
        feed(generic, low_box_scene(.07))
        self.assertEqual(generic.map_snapshot().cell(.7, -.6), 1)  # the generic band misses 7 cm
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        # Disc around the camera floor point that covers the footprint, plus the margin:
        # hypot(0.26 / 2 + 0.08, 0.18 / 2) + 0.05.
        self.assertAlmostEqual(calibration.inflation_m, .2785, delta=1e-4)
        grid = OccupancyGrid(SESSION, calibration=calibration)
        feed(grid, low_box_scene(.07))
        snapshot = grid.map_snapshot()
        self.assertEqual(snapshot.blockers, ())
        for x, z in [(.6, -.6), (.7, -.6), (.79, -.6), (.7, -.51)]:  # faces and top
            self.assertEqual(snapshot.cell(x, z), OCCUPIED, (x, z))
            self.assertFalse(snapshot.traversable(x, z), (x, z))
        # Free floor 0.2 m from the box face is inside the rover's inflated footprint.
        self.assertEqual(snapshot.cell(.4, -.6), 1)
        self.assertFalse(snapshot.traversable(.4, -.6))
        # Open floor, including the x/z mirror of the box, is traversable.
        for x, z in [(0, .3), (-.5, .5), (.7, .6), (-.6, -.6)]:
            self.assertTrue(snapshot.traversable(x, z), (x, z))
        # Unknown space and the known edge within a footprint of it are not.
        self.assertFalse(snapshot.traversable(3, 3))
        self.assertFalse(snapshot.traversable(.9, .9))

    def test_the_planner_grid_uses_the_same_calibrated_threshold(self):
        # backend.navigator plans on OccupancyGrid.snapshot(); it must see the low box too.
        grid = OccupancyGrid(SESSION, calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        feed(grid, low_box_scene(.07))
        _, origin, cells = grid.snapshot()
        mine = grid.map_snapshot()
        self.assertEqual(origin, mine.origin)
        np.testing.assert_array_equal(cells, mine.cells)
        self.assertEqual(mine.cell(.7, -.6), OCCUPIED)

    def test_narrow_passage_counts_the_whole_occupied_cell_not_just_its_center(self):
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)  # inflation 0.2785 m

        def passage(left_face, right_face):
            grid = OccupancyGrid(SESSION, calibration=calibration)
            feed(grid, np.concatenate([plane(-1, 1, -1, 1, FLOOR_Y),
                                       box(-.9, left_face, -.1, .1, FLOOR_Y, FLOOR_Y + .3),
                                       box(right_face, .9, -.1, .1, FLOOR_Y, FLOOR_Y + .3)]))
            return grid.map_snapshot()

        # Wall cells end at x = -0.25 and start at x = 0.30: 0.55 m of free floor. From the
        # middle cell (x = 0.025) the wall cell centers are 0.30 m away but their edges
        # only 0.275 m, inside the 0.2785 m disc, so the rover does not fit.
        narrow = passage(-.26, .30)
        self.assertEqual((narrow.cell(-.26, 0), narrow.cell(-.24, 0), narrow.cell(.30, 0)), (OCCUPIED, 1, OCCUPIED))
        for x in np.arange(-.225, .3, .05):
            self.assertFalse(narrow.traversable(x, 0), x)
        # One more free cell on each side leaves 0.325 m to either wall: it fits in the middle.
        wide = passage(-.31, .35)
        self.assertTrue(wide.traversable(.025, 0))
        self.assertFalse(wide.traversable(-.075, 0))  # 0.225 m from the left wall
        # Nonfinite queries are never traversable.
        for x, z in [(math.nan, 0), (0, math.inf)]:
            self.assertFalse(wide.traversable(x, z))
            self.assertEqual(wide.cell(x, z), 0)

    def test_a_threshold_inside_the_floor_noise_band_is_unsupported_not_guessed(self):
        # Heights read up to one 2 cm slice low, so a 5 cm hazard can look like 4 cm
        # floor noise. A rover that cannot cross 4.5 cm is beyond this sensing.
        calibration = RoverCalibration.model_validate({**TEST_CALIBRATION, 'obstacle_min_m': .045})
        self.assertEqual(calibration.blockers, ('obstacle_min_unsupported',))
        grid = OccupancyGrid(SESSION, calibration=calibration)
        feed(grid, low_box_scene(.05))
        snapshot = grid.map_snapshot()
        self.assertFalse(snapshot.ready)
        for x, z in [(.7, -.6), (0, .3), (-.5, .5)]:
            self.assertFalse(snapshot.traversable(x, z), (x, z))


# Tomiwa's measurements are still pending: this is the shape the file starts in.
PENDING = dict(version=1, measured_by=None, obstacle_min_m=None, footprint_length_m=None,
               footprint_width_m=None, clearance_margin_m=None, camera_forward_m=None,
               camera_left_m=None, camera_yaw_rad=None)


class InputTests(unittest.TestCase):
    def test_pending_measurements_load_but_block_motion_and_keep_the_generic_band(self):
        calibration = RoverCalibration.model_validate(PENDING)
        self.assertEqual(calibration.blockers, (
            'obstacle_min_m_unmeasured', 'footprint_length_m_unmeasured', 'footprint_width_m_unmeasured',
            'clearance_margin_m_unmeasured', 'camera_forward_m_unmeasured', 'camera_left_m_unmeasured',
            'camera_yaw_rad_unmeasured', 'calibration_unverified'))
        self.assertIsNone(calibration.inflation_m)
        grid = OccupancyGrid(SESSION, calibration=calibration)
        feed(grid, low_box_scene(.07))
        snapshot = grid.map_snapshot()
        self.assertEqual(snapshot.blockers, calibration.blockers)
        self.assertEqual(snapshot.cell(.7, -.6), 1)  # generic 8 cm band, unchanged
        self.assertFalse(snapshot.traversable(0, .3))

    def test_one_missing_value_or_signature_is_enough_to_block(self):
        self.assertEqual(RoverCalibration.model_validate({**TEST_CALIBRATION, 'camera_yaw_rad': None}).blockers,
                         ('camera_yaw_rad_unmeasured',))
        self.assertEqual(RoverCalibration.model_validate({**TEST_CALIBRATION, 'measured_by': None}).blockers,
                         ('calibration_unverified',))

    def test_malformed_values_are_rejected_rather_than_repaired(self):
        bad = [{'footprint_length_m': 26},  # centimeters typed into a meters field
               {'obstacle_min_m': 0}, {'obstacle_min_m': 1.5}, {'clearance_margin_m': -.01},
               {'camera_forward_m': 1.5}, {'camera_yaw_rad': 4.}, {'obstacle_min_m': math.nan},
               {'footprint_width_m': math.inf}, {'obstacle_min_m': '0.07'}, {'measured_by': ''},
               {'version': 2}, {'obstacle_min_cm': 7}]
        for change in bad:
            with self.assertRaises(ValidationError, msg=change):
                RoverCalibration.model_validate({**TEST_CALIBRATION, **change})
        missing = dict(TEST_CALIBRATION)
        del missing['camera_left_m']  # unmeasured must be written as null, not left out
        with self.assertRaises(ValidationError):
            RoverCalibration.model_validate(missing)
        with self.assertRaises(ValidationError):
            RoverCalibration.model_validate({**TEST_CALIBRATION, 'measured_by': '   '})

    def test_file_and_environment_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'rover.json')
            with open(path, 'w') as f:
                json.dump(TEST_CALIBRATION, f)
            self.assertEqual(load_calibration(path).inflation_m,
                             RoverCalibration.model_validate(TEST_CALIBRATION).inflation_m)
            self.assertEqual(calibration_from_env({'GODSEYE_ROVER_CALIBRATION': path}).blockers, ())
            with open(path, 'w') as f:
                f.write('{"version": 1,')
            with self.assertRaises(ValueError):
                load_calibration(path)
            with self.assertRaises(FileNotFoundError):
                calibration_from_env({'GODSEYE_ROVER_CALIBRATION': os.path.join(folder, 'missing.json')})
        self.assertIsNone(calibration_from_env({}))
        self.assertIsNone(calibration_from_env({'GODSEYE_ROVER_CALIBRATION': ''}))


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.grid = OccupancyGrid(SESSION, calibration=RoverCalibration.model_validate(TEST_CALIBRATION))

    def test_snapshot_carries_the_grids_accepted_revision_and_time(self):
        empty = self.grid.map_snapshot()
        self.assertEqual((empty.revision, empty.accepted_at, empty.cells), (0, None, None))
        self.assertIn('no_floor', empty.blockers)
        for now in (100., 100.25, 100.5):
            self.grid.add(low_box_scene(.07), now)
        first = self.grid.map_snapshot()
        self.assertEqual((first.revision, first.accepted_at, first.session), (3, 100.5, SESSION))
        self.assertEqual(self.grid.map_snapshot().revision, 3)  # reading is not sensing
        self.assertIsNotNone(self.grid.message_if_due(0.))
        self.grid.add(low_box_scene(.07), 107.5)  # the same view again
        self.assertIsNone(self.grid.message_if_due(5.))  # no new /live picture
        again = self.grid.map_snapshot()
        self.assertEqual((again.revision, again.accepted_at), (4, 107.5))
        np.testing.assert_array_equal(again.cells, first.cells)
        self.assertEqual((first.revision, first.accepted_at), (3, 100.5))  # snapshots are immutable

    def test_new_obstacles_and_a_shifted_floor_reach_the_next_snapshot(self):
        feed(self.grid, plane(-1, 1, -1, 1, FLOOR_Y))
        floor_only = self.grid.map_snapshot()
        self.assertTrue(floor_only.traversable(.4, -.6))
        feed(self.grid, box(.6, .8, -.7, -.5, FLOOR_Y, FLOOR_Y + .07))  # an obstacle appears
        with_box = self.grid.map_snapshot()
        self.assertGreater(with_box.revision, floor_only.revision)
        self.assertFalse(with_box.traversable(.4, -.6))
        # A lower floor, seen more often, shifts the floor estimate and every height with it.
        feed(self.grid, plane(-2, 2, -2, 2, FLOOR_Y - .3), frames=6)
        lower = self.grid.map_snapshot()
        self.assertNotAlmostEqual(lower.floor_y, with_box.floor_y, delta=.1)
        self.assertGreater(lower.revision, with_box.revision)


class LiveSnapshotTests(unittest.TestCase):
    def test_snapshot_is_session_scoped_and_repeats_advance_freshness_over_the_transport(self):
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        with TestClient(create_app(':memory:', calibration=calibration)) as client, \
                client.websocket_connect('/live') as live:
            state = client.app.state
            self.assertIsNone(state.map_snapshot())  # no active map yet
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('cal-live'))
                send_frames(client, phone, 'cal-live')
                next_of(live, 'occupancy', limit=200)
                first = state.map_snapshot()
                self.assertEqual((first.session, first.blockers), (('cal-live', 1), ()))
                published = state.occupancy.last_message
                # Two more frames of the same view: no new points, no new picture.
                send_frames(client, phone, 'cal-live', count=2, first=4)
                again = state.map_snapshot()
                self.assertEqual(state.map_stats['no_new_points'], 4)
                self.assertEqual(again.revision, first.revision + 2)
                self.assertGreater(again.accepted_at, first.accepted_at)
                np.testing.assert_array_equal(again.cells, first.cells)
                self.assertIsNone(state.occupancy.message_if_due(again.accepted_at + 60))
                self.assertIs(state.occupancy.last_message, published)
            wait_for(lambda: state.phone is None)
            # A map reset keeps the calibration and starts an empty, unsensed map.
            session = client.post('/session').json()
            reset = state.map_snapshot()
            self.assertEqual(reset.session, (session['session_id'], 1))
            self.assertEqual((reset.revision, reset.accepted_at, reset.blockers), (0, None, ('no_floor',)))
            self.assertEqual(first.session, ('cal-live', 1))  # the old snapshot is untouched

    def test_frames_dropped_as_stale_do_not_refresh_the_map(self):
        slow = threading.Event()

        def build(payload, session_id, map_epoch):
            if slow.is_set():
                threading.Event().wait(.8)  # finishes past the patched 0.5 s frame age
            return build_point_chunk(payload, session_id, map_epoch)

        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        with mock.patch.object(backend_app, 'MAP_MAX_AGE_S', .5), \
                TestClient(create_app(':memory:', build_points=build, calibration=calibration)) as client:
            state = client.app.state
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('cal-stale'))
                send_frames(client, phone, 'cal-stale')
                fresh_map = state.map_snapshot()
                self.assertIsNotNone(fresh_map.accepted_at)
                slow.set()
                phone.send_bytes(floor_frame('cal-stale', 4, 4.))  # same view: no new points either
                wait_for(lambda: state.map_stats['discarded_stale'] == 1)
                stale = state.map_snapshot()
                self.assertEqual((stale.revision, stale.accepted_at), (fresh_map.revision, fresh_map.accepted_at))
                self.assertEqual(state.map_stats['no_new_points'], 2)  # the stale one is not counted

    def test_a_repeat_captured_before_tracking_loss_does_not_refresh_the_map(self):
        started, release, gate = threading.Event(), threading.Event(), threading.Event()

        def build(payload, session_id, map_epoch):
            if gate.is_set():
                started.set()
                release.wait(5)
            return build_point_chunk(payload, session_id, map_epoch)

        with TestClient(create_app(':memory:', build_points=build)) as client:
            state = client.app.state
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())  # 'map-session', as the pose helper expects
                send_frames(client, phone, 'map-session')
                before = state.map_snapshot()
                gate.set()
                phone.send_bytes(floor_frame('map-session', 4, 4.))  # same view: no new points
                self.assertTrue(started.wait(5))
                phone.send_json(pose(4.1, 'not_available'))
                wait_for(lambda: state.tracking_lost_capture == 4.1)
                release.set()
                wait_for(lambda: state.map_stats['discarded_tracking'] == 1)
                after = state.map_snapshot()
                self.assertEqual((after.revision, after.accepted_at), (before.revision, before.accepted_at))
                self.assertEqual(state.map_stats['no_new_points'], 2)

    def test_calibration_never_arms_or_drives(self):
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        with TestClient(create_app(':memory:', calibration=calibration)) as client:
            health = client.get('/health').json()
            self.assertEqual((health['armed'], health['stop_reason']), (False, 'startup_disarmed'))
            self.assertEqual(client.post('/arm').status_code, 409)


if __name__ == '__main__':
    unittest.main()
