"""Suggested approach route to a localized person: observed-free only, visualization only."""
import math
import threading
import time
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from backend import approach as approach_module
from backend.app import create_app
from backend.approach import ASSUMPTIONS, PERSON_KEEP_OUT_M, WALKER_RADIUS_M, approach_route
from backend.localization import Detection, LocalizedDetection
from backend.navigation import FREE, OCCUPIED, UNKNOWN, Grid, PlannerConfig, plan_path
from backend.tests.test_detections import PERSON, BoxDetector, scene
from backend.tests.test_map_transport import hello, next_of, wait_for
from backend.tests.test_occupancy import FLOOR_Y, box, plane

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
        return client.app.state.objects.record(session, 1, time.time(), found)[0].object_id
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



class ApproachViewTests(unittest.TestCase):
    """`app.state.approach_view`: the current selected route for read-only consumers (voice)."""

    def route(self, client, session, person, start, **extra):
        return client.post('/route', json=dict(session_id=session[0], map_epoch=session[1], object_id=person,
                                               start=start, **extra))

    def test_selected_route_is_published_then_replaced_by_a_failed_or_missing_selection(self):
        with TestClient(create_app(':memory:')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, wall_with_gap(room(), 2., 1., .9))
            person = person_at(client, session, (2., .4, 3.))
            before = time.time() * 1000
            ok = self.route(client, session, person, [3., .5]).json()
            view = client.app.state.approach_view
            self.assertEqual({k: v for k, v in view.items() if k not in ('t_wall_ms', 'person_last_seen')}, ok)
            self.assertLessEqual(before, view['t_wall_ms'])
            self.assertEqual((view['status'], view['assumptions']['verified']), ('ok', False))
            self.assertEqual((client.app.state.nav.path, client.get('/health').json()['armed']), ([], False))
            # A new selection that fails replaces the earlier success.
            self.route(client, session, person, [3.9, 3.9])  # inside the wall's clearance band
            unavailable = client.app.state.approach_view
            self.assertEqual((unavailable['status'], unavailable['start']), ('unavailable', [3.9, 3.9]))
            self.assertNotIn('points', unavailable)
            self.route(client, session, person, [3., .5])
            self.assertEqual(self.route(client, session, 'missing', [3., .5]).status_code, 404)
            self.assertIsNone(client.app.state.approach_view)
            self.route(client, session, person, [3., .5])
            client.post('/session')
            self.assertIsNone(client.app.state.approach_view)

    def test_an_older_request_finishing_late_never_overwrites_a_newer_selection(self):
        started, release = threading.Event(), threading.Event()
        real = approach_module.approach_route

        def slow_then_real(grid, start, person):
            if start == [0.5, 0.5]:  # the first, older selection
                started.set()
                release.wait(5)
            return real(grid, start, person)

        with TestClient(create_app(':memory:')) as client, \
                mock.patch('backend.app.approach_route', slow_then_real):
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, room())
            person = person_at(client, session, (2., .4, 3.))
            older = []
            worker = threading.Thread(target=lambda: older.append(self.route(client, session, person, [.5, .5])))
            worker.start()
            self.assertTrue(started.wait(5))
            newer = self.route(client, session, person, [3., .5]).json()
            release.set()
            worker.join(5)
            view = client.app.state.approach_view
        self.assertEqual(older[0].json()['reason'], 'route_superseded')
        self.assertNotIn('points', older[0].json())
        self.assertEqual((view['start'], view['status']), ([3., .5], newer['status']))

    def test_a_changed_published_occupancy_picture_retires_the_route(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('route-map'))
            wait_for(lambda: client.app.state.session == ('route-map', 1))
            session, grid = ('route-map', 1), client.app.state.occupancy
            for now in (1., 2., 3.):
                grid.add(plane(-2., 2., -2., 2., FLOOR_Y), now=now)
            wait_for(lambda: grid.last_message is not None)
            person = person_at(client, session, (1., FLOOR_Y + 1., 1.))
            self.assertEqual(self.route(client, session, person, [-1., -1.]).json()['status'], 'ok')
            published = grid.last_message
            grid.add(plane(-2., 2., -2., 2., FLOOR_Y), now=4.)  # more of the same evidence
            time.sleep(.5)
            self.assertIs(grid.last_message, published)
            self.assertIsNotNone(client.app.state.approach_view)
            for now in (5., 6., 7.):  # a new obstacle changes the published picture
                grid.add(box(-.5, -.3, -.5, -.3, FLOOR_Y, FLOOR_Y + .3), now=now)
            wait_for(lambda: grid.last_message is not published)
            wait_for(lambda: client.app.state.approach_view is None)

    def test_a_sighting_that_moves_the_person_retires_the_route(self):
        detector = BoxDetector(PERSON)
        with TestClient(create_app(':memory:', detector=detector)) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('room'))
            phone.send_bytes(scene(frame_id=1))
            [person] = next_of(live, 'detections')['detections']
            self.route(client, ('room', 1), person['object_id'], [0., 0.])
            self.assertEqual(client.app.state.approach_view['reason'], 'no_observed_map')
            time.sleep(.55)
            phone.send_bytes(scene(frame_id=2))  # the same place: the route stands
            next_of(live, 'detections')
            self.assertIsNotNone(client.app.state.approach_view)
            detector.boxes = [Detection((44., 10., 60., 50.), 'person', .87)]  # 0.1 m further along z
            time.sleep(.55)
            phone.send_bytes(scene(frame_id=3))
            [moved] = next_of(live, 'detections')['detections']
            self.assertEqual(moved['object_id'], person['object_id'])
            self.assertIsNone(client.app.state.approach_view)

class RouteEvidenceTests(unittest.TestCase):
    def request(self, client, session, person):
        return client.post('/route', json=dict(session_id=session[0], map_epoch=session[1],
                                             object_id=person, start=[.5, .5]))

    def delayed_request(self, client, session, person, change):
        started, release = threading.Event(), threading.Event()
        responses = []

        def delayed(*args):
            result = approach_route(*args)
            started.set()
            self.assertTrue(release.wait(5))
            return result

        with mock.patch('backend.app.approach_route', delayed):
            worker = threading.Thread(target=lambda: responses.append(self.request(client, session, person)))
            worker.start()
            self.assertTrue(started.wait(5))
            try:
                change()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        return responses[0]

    def test_new_obstacle_during_first_plan_cannot_publish_blocked_route(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            grid = Grid2D(session, room())
            client.app.state.occupancy = grid
            person = person_at(client, session, (3., .4, 3.))

            def changed():
                # The returned initial path is diagonal through this new obstacle.
                grid.cells = grid.cells.copy()
                grid.cells[25:45, 25:45] = OCCUPIED
                grid.revision += 1

            result = self.delayed_request(client, session, person, changed).json()
            self.assertEqual((result['status'], result['reason']), ('unavailable', 'map_changed'))
            self.assertNotIn('points', result)
            self.assertNotEqual((client.app.state.approach_view or {}).get('status'), 'ok')
            self.assertEqual(self.request(client, session, person).json()['status'], 'ok')

    def test_unrelated_obstacle_and_revision_change_does_not_starve_route(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            grid = Grid2D(session, room())
            client.app.state.occupancy = grid
            person = person_at(client, session, (3., .4, 3.))

            def changed():
                grid.cells = grid.cells.copy()
                grid.cells[10:15, 65:70] = OCCUPIED
                grid.revision += 1

            result = self.delayed_request(client, session, person, changed).json()
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['occupancy_revision'], grid.revision)
            self.assertEqual(client.app.state.approach_view['status'], 'ok')

    def test_person_moving_or_not_found_during_plan_invalidates_result(self):
        for state, position in [('present', '[3.5,0.4,3]'), ('not_found_on_rescan', '[3,0.4,3]')]:
            with self.subTest(state=state), TestClient(create_app(':memory:', capture_directory='')) as client:
                reset = client.post('/session').json()
                session = (reset['session_id'], reset['map_epoch'])
                client.app.state.occupancy = Grid2D(session, room())
                person = person_at(client, session, (3., .4, 3.))

                def changed():
                    client.portal.call(lambda: client.app.state.db.execute(
                        'UPDATE objects SET position_json=?,state=? WHERE id=?', (position, state, person)))

                result = self.delayed_request(client, session, person, changed).json()
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('points', result)
                self.assertNotEqual((client.app.state.approach_view or {}).get('status'), 'ok')
                if state == 'not_found_on_rescan':
                    self.assertEqual(self.request(client, session, person).json()['reason'], 'person_not_found')

    def test_phone_disconnect_retires_cached_and_pending_route(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('route-disconnect'))
            wait_for(lambda: client.app.state.session == ('route-disconnect', 1))
            session = ('route-disconnect', 1)
            grid = client.app.state.occupancy
            for now in (1., 2., 3.):
                grid.add(plane(-2., 2., -2., 2., FLOOR_Y), now=now)
            person = person_at(client, session, (1., FLOOR_Y + 1., 1.))
            self.assertEqual(self.request(client, session, person).json()['status'], 'ok')

            def disconnect():
                phone.close()
                wait_for(lambda: client.app.state.phone is None)
                self.assertIsNone(client.app.state.approach_view)

            result = self.delayed_request(client, session, person, disconnect).json()
            self.assertEqual((result['status'], result['reason']),
                             ('unavailable', 'route_evidence_changed'))
            self.assertIsNone(client.app.state.approach_view)

    def test_expired_person_is_unavailable_and_expired_cache_is_retired(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, room())
            person = person_at(client, session, (3., .4, 3.))
            self.assertEqual(self.request(client, session, person).json()['status'], 'ok')
            client.app.state.approach_view['person_last_seen'] = time.time() - 31
            wait_for(lambda: client.app.state.approach_view is None)
            client.portal.call(lambda: client.app.state.db.execute(
                'UPDATE objects SET last_seen=? WHERE id=?', (time.time() - 31, person)))
            result = self.request(client, session, person).json()
            self.assertEqual((result['status'], result['reason']), ('unavailable', 'person_stale'))

    def test_target_age_uses_wall_seconds_and_exact_existing_thirty_second_bound(self):
        from backend.approach import person_evidence_reason
        self.assertIsNone(person_evidence_reason(dict(state='present', last_seen=1700000000.), 1700000030.))
        self.assertEqual(person_evidence_reason(dict(state='present', last_seen=1700000000.),
                                              1700000030.001), 'person_stale')
        for value in (None, float('nan'), '1700000000'):
            self.assertEqual(person_evidence_reason(dict(state='present', last_seen=value),
                                                  1700000000.), 'person_stale')

    def test_reset_during_plan_never_repopulates_previous_map_route(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, room())
            person = person_at(client, session, (3., .4, 3.))
            result = self.delayed_request(client, session, person, lambda: client.post('/session'))
            self.assertEqual(result.status_code, 409)
            self.assertIsNone(client.app.state.approach_view)

    def test_change_after_second_snapshot_validation_cannot_publish(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            grid = Grid2D(session, room())
            client.app.state.occupancy = grid
            person = person_at(client, session, (3., .4, 3.))

            def first_change():
                grid.cells = grid.cells.copy()
                grid.cells[10:15, 65:70] = OCCUPIED
                grid.revision += 1

            def validate_then_change(*args):
                clear = approach_module.route_still_clear(*args)
                self.assertTrue(clear)
                # A third update races the final worker result after its snapshot.
                grid.cells = grid.cells.copy()
                grid.cells[25:45, 25:45] = OCCUPIED
                grid.revision += 1
                return clear

            with mock.patch('backend.app.route_still_clear', validate_then_change):
                result = self.delayed_request(client, session, person, first_change).json()
            self.assertEqual((result['status'], result['reason']), ('unavailable', 'map_changed'))
            self.assertNotIn('points', result)

    def test_rescan_immediately_retires_cache_and_unconfirms_person(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, room())
            person = person_at(client, session, (3., .4, 3.))
            self.assertEqual(self.request(client, session, person).json()['status'], 'ok')
            self.assertEqual(client.post('/rescan').status_code, 200)
            self.assertIsNone(client.app.state.approach_view)
            self.assertEqual(self.request(client, session, person).json()['reason'], 'person_unconfirmed')

    def test_identical_concurrent_viewers_share_valid_result_but_not_changed_evidence(self):
        for change in ('none', 'blocked', 'disconnect', 'reset'):
            with self.subTest(change=change), TestClient(create_app(':memory:', capture_directory='')) as client, \
                    client.websocket_connect('/phone') as phone:
                phone.send_json(hello('shared-route'))
                wait_for(lambda: client.app.state.session == ('shared-route', 1))
                session = ('shared-route', 1)
                grid = Grid2D(session, room())
                client.portal.call(lambda: setattr(client.app.state, 'occupancy', grid))
                person = person_at(client, session, (3., .4, 3.))
                started, release = threading.Event(), threading.Event()
                first = True

                def delayed_first(*args):
                    nonlocal first
                    delay = first
                    first = False
                    result = approach_route(*args)
                    if delay:
                        started.set()
                        self.assertTrue(release.wait(5))
                    return result

                responses = []
                with mock.patch('backend.app.approach_route', delayed_first):
                    worker = threading.Thread(target=lambda: responses.append(self.request(client, session, person)))
                    worker.start()
                    self.assertTrue(started.wait(5))
                    try:
                        newer = self.request(client, session, person).json()
                        self.assertEqual(newer['status'], 'ok')
                        cached = client.app.state.approach_view
                        if change == 'blocked':
                            grid.cells = grid.cells.copy()
                            grid.cells[25:45, 25:45] = OCCUPIED
                            grid.revision += 1
                        elif change == 'disconnect':
                            phone.close()
                            wait_for(lambda: client.app.state.phone is None)
                        elif change == 'reset':
                            client.post('/session')
                    finally:
                        release.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                if change == 'none':
                    self.assertEqual(responses[0].json()['status'], 'ok')
                    self.assertIs(client.app.state.approach_view, cached)
                elif change == 'reset':
                    self.assertEqual(responses[0].status_code, 409)
                else:
                    old = responses[0].json()
                    self.assertEqual(old['status'], 'unavailable')
                    self.assertEqual(old['reason'], 'map_changed' if change == 'blocked' else 'route_evidence_changed')
                    self.assertNotIn('points', old)


if __name__ == '__main__':
    unittest.main()
