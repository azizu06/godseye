"""Real /phone -> motion-ready map -> /goal -> FakeCar rehearsal; no device or provider.

TEST geometry describes no physical rover. The detector is an offline empty stand-in.
"""
from contextlib import contextmanager
import threading
import time

import numpy as np

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.calibration import RoverCalibration
from backend.drive import FakeCar
from backend.navigator import NavSettings
from tools.fake_phone import frame_bundle, look_pose, synthetic_sensors, world_sensors

TEST_CALIBRATION = dict(version=1, measured_by='TEST synthetic rehearsal only; no real rover',
                        obstacle_min_m=.065, footprint_length_m=.26, footprint_width_m=.18,
                        clearance_margin_m=.05, camera_forward_m=.08, camera_left_m=0., camera_yaw_rad=0.)


class EmptyDetector:
    def localize(self, frame):
        return []


def wait_for(condition, timeout=5.):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('rehearsal condition timed out')
        time.sleep(.01)


class PhoneScene:
    def __init__(self, client, phone, session='rehearsal', hello_sent=False):
        self.client, self.phone, self.session = client, phone, session
        self.base = synthetic_sensors()
        self.index = 0
        self.position = (0., 0., 0.)
        self.frames = True
        self.done = threading.Event()
        self.error = None
        if not hello_sent:
            phone.send_json(dict(version=1, type='hello', device='TEST synthetic phone', session_id=session,
                             map_epoch=1, supports_scene_depth=True, supports_mesh=False))
        wait_for(lambda: client.app.state.session == (session, 1))

    def message(self, position=None, pitch=.6):
        self.index += 1
        return look_pose(self.index, float(self.index), time.time_ns() // 1_000_000, self.session,
                         position=position or self.position, yaw=0., pitch=pitch)

    def survey(self):
        # Survey from behind the starting pose so its entire footprint is observed.
        for x in (-.4, 0., .4):
            for _ in range(3):
                before = self.client.app.state.occupancy.revision
                message = self.message((x, 0., -1.3), pitch=.9)
                self.phone.send_bytes(frame_bundle(message, world_sensors(message['transform'], self.base)))
                wait_for(lambda: self.client.app.state.occupancy.revision > before)
                time.sleep(.27)
        self.phone.send_json(self.message())

    def start(self):
        def feed():
            next_frame = 0.
            try:
                while not self.done.is_set():
                    message = self.message()
                    self.phone.send_json(message)
                    if self.frames and time.monotonic() >= next_frame:
                        self.phone.send_bytes(frame_bundle(message, world_sensors(message['transform'], self.base)))
                        next_frame = time.monotonic() + .3
                    self.done.wait(.03)
            except Exception as error:
                self.error = error
        self.thread = threading.Thread(target=feed, daemon=True)
        self.thread.start()
        wait_for(lambda: self.client.get('/health').json()['phone'] ==
                 self.client.get('/health').json()['detector'] == 'ok')

    def close(self):
        self.done.set()
        if hasattr(self, 'thread'):
            self.thread.join(5)
            assert not self.thread.is_alive(), 'synthetic feeder did not stop'
        if self.error:
            raise self.error


@contextmanager
def scene(db=':memory:', calibration=TEST_CALIBRATION, **settings):
    car = FakeCar()
    measured = None if calibration is None else RoverCalibration.model_validate(calibration)
    app = create_app(db, car=car, detector=EmptyDetector(), calibration=measured,
                     capture_directory='', nav_settings=NavSettings(**settings))
    with TestClient(app) as client, client.websocket_connect('/phone') as phone:
        source = PhoneScene(client, phone)
        try:
            source.survey()
            source.start()
            yield client, car, source
        finally:
            source.close()


def arm(client, mode='navigate'):
    assert client.post('/mode', json={'mode': mode}).status_code == 200
    answer = client.post('/arm')
    assert answer.status_code == 200, answer.text


def rehearsal(db=':memory:'):
    with scene(calibration=None) as (client, car, source):
        for mode in ('navigate', 'explore'):
            arm(client, mode)
            if mode == 'navigate':
                result = client.post('/goal', json={'x': 0., 'z': .5})
                assert result.status_code == 409 and result.json()['detail'] == 'calibration_missing'
            wait_for(lambda: not client.app.state.armed)
            assert client.app.state.stop_reason == 'calibration_missing'
        assert not any(call[0] == 'send' and (call[1] or call[2]) for call in car.calls)
    with scene(db, no_progress_s=.4) as (client, car, source):
        state = client.app.state
        snapshot = state.map_snapshot()
        assert snapshot.ready and snapshot.traversable(0., 0.), snapshot.blockers
        before = state.map_stats['no_new_points']
        revision = snapshot.revision
        wait_for(lambda: state.map_stats['no_new_points'] > before)
        repeated = state.map_snapshot()
        assert repeated.revision > revision and repeated.accepted_at > snapshot.accepted_at
        arm(client)
        result = client.post('/goal', json={'x': 0., 'z': .5})
        assert result.status_code == 200, result.text
        assert result.json()['points'][-1] == [0., .5]
        wait_for(lambda: any(call[0] == 'send' and call[1] > 0 for call in car.calls))
        wait_for(lambda: not state.armed)
        assert state.stop_reason == 'no_progress', state.stop_reason  # FakeCar deliberately never moves
        assert car.calls[-1] == ('zero',) and state.nav.path == []
        assert all(0 <= call[1] <= .15 and abs(call[2]) <= .5 for call in car.calls if call[0] == 'send')
        terminal = len(car.calls)
        time.sleep(.3)
        assert not any(call[0] == 'send' for call in car.calls[terminal:])
        row, col = np.argwhere(state.map_snapshot().cells == 0)[0]
        view = state.map_snapshot()
        unknown_goal = (view.origin[0] + (col + .5) * view.cell_m,
                        view.origin[1] + (row + .5) * view.cell_m)
        for goal, reason in [((0., 10.), 'out_of_bounds'), ((.45, 2.), 'destination_blocked'),
                             (unknown_goal, 'destination_unknown')]:
            arm(client)
            result = client.post('/goal', json={'x': goal[0], 'z': goal[1]})
            assert result.status_code == 409, result.text
            assert result.json()['detail'] == reason, result.text
            assert not state.armed and car.calls[-1] == ('zero',)
        # Poses continue while accepted depth sensing stops. Detector is still healthy at 1 s.
        source.frames = False
        wait_for(lambda: time.monotonic() - state.map_snapshot().accepted_at > 1.05)
        arm(client)
        result = client.post('/goal', json={'x': 0., 'z': .5})
        assert result.status_code == 409 and result.json()['detail'] == 'sensing_stale', result.text
        source.frames = True
        wait_for(lambda: time.monotonic() - state.map_snapshot().accepted_at < .5)
        arm(client)
        old_generation = state.motion.generation
        assert client.post('/goal', json={'x': 0., 'z': .5}).status_code == 200
        source.close()
        source.phone.close()
        wait_for(lambda: state.phone is None)
        assert not state.armed and not state.nav.active and car.calls[-1] == ('zero',)
        with client.websocket_connect('/phone') as reconnected:
            new_source = PhoneScene(client, reconnected)
            try:
                new_source.start()
                arm(client)
                mark = len(car.calls)
                assert state.motion.generation != old_generation
                assert not client.portal.call(state.motion.submit, old_generation, 'navigate', .1, 0.)
                time.sleep(.15)
                assert not any(call[0] == 'send' for call in car.calls[mark:])
                assert client.post('/stop').json()['stop_reason'] == 'operator_stop'
                arm(client, 'manual')
                assert client.post('/manual', json={'v_mps': .1, 'yaw_rate_rps': 0.}).status_code == 200
                wait_for(lambda: car.calls[-1][0] == 'send')
                time.sleep(.4)
                assert car.calls[-1] == ('zero',)  # stale manual lease, even while sensing is fresh
                client.post('/stop')
            finally:
                new_source.close()
        return len([call for call in car.calls if call[0] == 'send'])
