"""Suggested approach route to a localized person: observed-free only, visualization only."""
import math
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.approach import ASSUMPTIONS, PERSON_KEEP_OUT_M, WALKER_RADIUS_M, approach_route
from backend.localization import Detection, LocalizedDetection
from backend.navigation import FREE, OCCUPIED, UNKNOWN, Grid, PlannerConfig, plan_path

CELL = .05


def room(width_m=4., depth_m=4.):
    """All observed free, origin (0, 0); cells[row=z, col=x]."""
    return np.full((round(depth_m / CELL), round(width_m / CELL)), FREE, dtype=np.uint8)


def wall_with_gap(cells, z, gap_x, gap_m):
    """An occupied wall across z with one opening of gap_m centred on gap_x."""
    row = round(z / CELL)
    xs = (np.arange(cells.shape[1]) + .5) * CELL
    cells[row - 1:row + 1, np.abs(xs - gap_x) > gap_m / 2] = OCCUPIED
    return cells


def grid(cells):
    return Grid.from_array(cells, origin=(0, 0), cell_m=CELL)


def clearance(cells, point):
    """Distance from a world point to the nearest non-free cell centre."""
    rows, cols = np.nonzero(cells != FREE)
    if rows.size == 0:
        return math.inf
    return float(np.min(np.hypot((cols + .5) * CELL - point[0], (rows + .5) * CELL - point[1])))


class ApproachRouteTests(unittest.TestCase):
    def test_route_reaches_an_observed_free_point_beside_the_person_through_a_door(self):
        cells = wall_with_gap(room(), 2., 1., .9)
        route = approach_route(grid(cells), (3., .5), (2., 3.))
        self.assertEqual(route['status'], 'ok')
        self.assertEqual(route['assumptions'], ASSUMPTIONS)
        self.assertFalse(route['assumptions']['verified'])
        points = route['points']
        self.assertLess(math.dist(points[0], (3., .5)), .1)
        # Ends near the person, never on the person's own footprint.
        distance = math.dist(route['approach'], (2., 3.))
        self.assertGreater(distance, PERSON_KEEP_OUT_M + WALKER_RADIUS_M)
        self.assertLessEqual(distance, 1.5)
        # The route crosses the wall through the opening, keeping walker clearance.
        self.assertTrue(any(abs(x - 1.) < .3 and abs(z - 2.) < .3 for x, z in points))
        self.assertTrue(all(clearance(cells, p) >= WALKER_RADIUS_M for p in points))

    def test_unknown_space_is_never_filled_in(self):
        cells = room()
        cells[36:44, :] = UNKNOWN  # an unobserved band between start and person
        route = approach_route(grid(cells), (2., .5), (2., 3.5))
        self.assertEqual((route['status'], route['reason']), ('unavailable', 'no_observed_free_route'))
        self.assertNotIn('points', route)

    def test_a_car_sized_gap_is_not_offered_as_a_walking_route(self):
        cells = wall_with_gap(room(), 2., 2., .45)
        car = plan_path(grid(cells), (2., .5), (2., 3.5),
                        PlannerConfig(unknown_traversable=False, footprint_clearance=True))
        self.assertTrue(car.ok)  # the default rover footprint fits
        route = approach_route(grid(cells), (2., .5), (2., 3.5))
        self.assertEqual((route['status'], route['reason']), ('unavailable', 'no_observed_free_route'))

    def test_unavailable_reasons_for_unsupported_starts_people_and_maps(self):
        cells = room()
        cells[:10, :10] = UNKNOWN
        self.assertEqual(approach_route(grid(cells), (.2, .2), (3., 3.))['reason'], 'start_not_observed_free')
        cells = room()
        cells[30:, 30:] = UNKNOWN  # the person stands deep in space nobody observed
        self.assertEqual(approach_route(grid(cells), (.5, .5), (3.5, 3.5))['reason'],
                         'no_observed_free_approach')
        self.assertEqual(approach_route(grid(room()), (.5, .5), (9., 9.))['reason'], 'start_or_person_off_map')
        self.assertEqual(approach_route(None, (.5, .5), (3., 3.))['reason'], 'no_observed_map')
        empty = np.full((10, 10), UNKNOWN, dtype=np.uint8)
        self.assertEqual(approach_route(grid(empty), (.1, .1), (.3, .3))['reason'], 'no_observed_map')


class Grid2D:
    """Stands in for OccupancyGrid: the active session's classified cells."""

    def __init__(self, session, cells):
        self.session = session
        self.cells = cells
        self.revision = 7

    def snapshot(self):
        return self.revision, (0., 0.), self.cells


def person_at(client, session, position):
    def record():
        db = client.app.state.db
        db.execute('INSERT OR IGNORE INTO frames(session_id,map_epoch,frame_id,t_capture,t_wall_ms,transform_json,'
                   "tracking) VALUES(?,?,1,1,1000,'[]','normal')", session)
        found = [LocalizedDetection(Detection((0, 0, 1, 1), 'person', .9), position, 2., 9, *session, 1, 1.)]
        return client.app.state.objects.record(session, 1, 1., found)[0].object_id
    return client.portal.call(record)


class RouteEndpointTests(unittest.TestCase):
    def test_route_is_planned_for_a_person_in_the_active_map_and_moves_nothing(self):
        with TestClient(create_app(':memory:')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, wall_with_gap(room(), 2., 1., .9))
            person = person_at(client, session, (2., .4, 3.))
            body = dict(session_id=session[0], map_epoch=session[1], object_id=person, start=[3., .5])
            route = client.post('/route', json=body).json()
            health = client.get('/health').json()
            nav_path = client.app.state.nav.path
            stale = client.post('/route', json=dict(body, map_epoch=2))
            chair = client.post('/route', json=dict(body, object_id='missing'))
        self.assertEqual(route['status'], 'ok')
        self.assertEqual((route['object_id'], route['person'], route['occupancy_revision']),
                         (person, [2., 3.], 7))
        self.assertEqual((route['session_id'], route['map_epoch']), session)
        self.assertEqual((health['armed'], nav_path), (False, []))  # visualization only
        self.assertEqual((stale.status_code, chair.status_code), (409, 404))

    def test_without_an_observed_map_the_route_is_unavailable(self):
        with TestClient(create_app(':memory:')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            person = person_at(client, session, (2., .4, 3.))
            route = client.post('/route', json=dict(session_id=session[0], map_epoch=session[1],
                                                    object_id=person, start=[3., .5])).json()
        self.assertEqual((route['status'], route['reason']), ('unavailable', 'no_observed_map'))


if __name__ == '__main__':
    unittest.main()
