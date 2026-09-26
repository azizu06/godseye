"""Rover calibration/readiness and the navigation occupancy snapshot; no hardware.

Every calibration value below is a TEST fixture, not a measurement of the real
rover. Real chassis, clearance and mount values come from Tomiwa's bench sheet.
"""
import json
import math
import os
import tempfile
import unittest

import numpy as np
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import create_app
from backend.calibration import RoverCalibration, calibration_from_env, load_calibration
from backend.occupancy import OCCUPIED, OccupancyGrid
from backend.tests.test_map_transport import hello, next_of, wait_for
from backend.tests.test_occupancy import FLOOR_Y, box, feed, plane, send_frames

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
        snapshot = grid.snapshot()
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
        self.assertEqual(generic.snapshot().cell(.7, -.6), 1)  # the generic band misses 7 cm
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        # Disc around the camera floor point that covers the footprint, plus the margin:
        # hypot(0.26 / 2 + 0.08, 0.18 / 2) + 0.05.
        self.assertAlmostEqual(calibration.inflation_m, .2785, delta=1e-4)
        grid = OccupancyGrid(SESSION, calibration=calibration)
        feed(grid, low_box_scene(.07))
        snapshot = grid.snapshot()
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

    def test_a_threshold_inside_the_floor_noise_band_is_unsupported_not_guessed(self):
        # Heights read up to one 2 cm slice low, so a 5 cm hazard can look like 4 cm
        # floor noise. A rover that cannot cross 4.5 cm is beyond this sensing.
        calibration = RoverCalibration.model_validate({**TEST_CALIBRATION, 'obstacle_min_m': .045})
        self.assertEqual(calibration.blockers, ('obstacle_min_unsupported',))
        grid = OccupancyGrid(SESSION, calibration=calibration)
        feed(grid, low_box_scene(.05))
        snapshot = grid.snapshot()
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
        snapshot = grid.snapshot()
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
        self.now = 100.
        self.grid = OccupancyGrid(SESSION, calibration=RoverCalibration.model_validate(TEST_CALIBRATION),
                                  clock=lambda: self.now)

    def test_unchanged_observations_advance_freshness_without_a_new_picture(self):
        empty = self.grid.snapshot()
        self.assertEqual((empty.revision, empty.sensed_at, empty.cells), (0, None, None))
        self.assertIn('no_floor', empty.blockers)
        feed(self.grid, low_box_scene(.07))
        first = self.grid.snapshot()
        self.assertEqual((first.revision, first.sensed_at, first.session), (1, 100., SESSION))
        self.assertIsNotNone(self.grid.message_if_due(0.))
        self.now = 107.5
        self.grid.add(low_box_scene(.07))  # the same view again
        self.assertIsNone(self.grid.message_if_due(5.))  # no new /live picture
        again = self.grid.snapshot()
        self.assertEqual((again.revision, again.sensed_at), (1, 107.5))
        self.assertEqual(first.sensed_at, 100.)  # snapshots are immutable

    def test_revision_advances_when_the_picture_or_floor_changes(self):
        feed(self.grid, plane(-1, 1, -1, 1, FLOOR_Y))
        floor_only = self.grid.snapshot()
        self.assertTrue(floor_only.traversable(.4, -.6))
        feed(self.grid, box(.6, .8, -.7, -.5, FLOOR_Y, FLOOR_Y + .07))  # an obstacle appears
        with_box = self.grid.snapshot()
        self.assertEqual(with_box.revision, floor_only.revision + 1)
        self.assertFalse(with_box.traversable(.4, -.6))
        self.assertEqual(self.grid.snapshot().revision, with_box.revision)  # recomputing is not a change
        # A lower floor, seen more often, shifts the floor estimate and every height with it.
        feed(self.grid, plane(-2, 2, -2, 2, FLOOR_Y - .3), frames=6)
        lower = self.grid.snapshot()
        self.assertNotAlmostEqual(lower.floor_y, with_box.floor_y, delta=.1)
        self.assertEqual(lower.revision, with_box.revision + 1)


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
                self.assertEqual(again.revision, first.revision)
                self.assertGreater(again.sensed_at, first.sensed_at)
                self.assertIsNone(state.occupancy.message_if_due(again.sensed_at + 60))
                self.assertIs(state.occupancy.last_message, published)
            wait_for(lambda: state.phone is None)
            # A map reset keeps the calibration and starts an empty, unsensed map.
            session = client.post('/session').json()
            reset = state.map_snapshot()
            self.assertEqual(reset.session, (session['session_id'], 1))
            self.assertEqual((reset.revision, reset.sensed_at, reset.blockers), (0, None, ('no_floor',)))
            self.assertEqual(first.session, ('cal-live', 1))  # the old snapshot is untouched

    def test_calibration_never_arms_or_drives(self):
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        with TestClient(create_app(':memory:', calibration=calibration)) as client:
            health = client.get('/health').json()
            self.assertEqual((health['armed'], health['stop_reason']), (False, 'startup_disarmed'))
            self.assertEqual(client.post('/arm').status_code, 409)


if __name__ == '__main__':
    unittest.main()
