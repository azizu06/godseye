"""REST/phone drive safety through the real app with a fake car; nothing leaves the process.

A Mac-side timer here is not evidence of the car's own 300 ms stop (issue 6).
"""
from contextlib import contextmanager
import threading
import time
import unittest

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.drive import FakeCar
from backend.motion import DriveStop
from backend.tests.test_command_envelope import faults
from backend.tests.test_map_transport import frame, hello, wait_for
from backend.tests.test_objects import FakeDetector
from backend.tests.test_rich_capture import packet

HOLD = {'v_mps': .1, 'yaw_rate_rps': 0.}


class Flaky(FakeDetector):
    broken = False

    def localize(self, frame_bundle):
        if self.broken:
            raise RuntimeError('model lost')
        return super().localize(frame_bundle)


def sends(calls):
    return [call for call in calls if call[0] == 'send']


def after_zero(calls):
    """Calls from the first zero on; a safe stop never sends motion after it."""
    return calls[calls.index(('zero',)):]


class DriveSafetyTests(unittest.TestCase):
    @contextmanager
    def rig(self, car=None, detector=None):
        """Phone streaming fresh frames at 25 Hz and a fake detector, until phone and detector are ok."""
        car = FakeCar() if car is None else car
        app = create_app(':memory:', detector=detector or FakeDetector(), car=car)
        with TestClient(app) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            self.feeding = threading.Event()
            self.feeding.set()
            done = threading.Event()

            def feed():
                frame_id = 0
                while not done.is_set():
                    if self.feeding.is_set():
                        frame_id += 1
                        phone.send_bytes(frame(frame_id=frame_id, t_capture=float(frame_id)))
                    done.wait(.04)

            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
            try:
                wait_for(lambda: self.health(client)['phone'] == self.health(client)['detector'] == 'ok')
                yield client, car
            finally:
                done.set()
                feeder.join()

    def health(self, client):
        return client.get('/health').json()

    def hold(self, client, seconds):
        """Renew the held button every 100 ms, like the dashboard."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            client.post('/manual', json=HOLD)
            time.sleep(.1)

    def armed_and_moving(self, client, car):
        """Arm, hold, and return the call count once this hold's motion is being sent."""
        before = len(car.calls)
        self.assertEqual(client.post('/arm').status_code, 200)
        client.post('/manual', json=HOLD)
        wait_for(lambda: sends(car.calls[before:]))
        return len(car.calls)

    def test_default_logging_car_reports_down_so_arm_is_refused(self):
        with TestClient(create_app(':memory:', detector=FakeDetector())) as client:
            self.assertEqual(self.health(client)['car'], 'down')
            self.assertEqual(client.post('/arm').status_code, 409)

    def test_viewable_rich_capture_does_not_restore_stale_drive_authority(self):
        with self.rig() as (client, car):
            self.feeding.clear()
            wait_for(lambda: self.health(client)['phone'] != 'ok')
            payload = packet(session_id='map-session', capture=10000.)
            response = client.post('/capture/ingest', content=payload)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['recording_enabled'])
            self.assertEqual(client.get('/capture/rich/frame.bin').content, payload)
            self.assertNotEqual(self.health(client)['phone'], 'ok')
            self.assertFalse(self.health(client)['armed'])
            self.assertEqual(client.post('/arm').status_code, 409)
            self.assertEqual(client.post('/manual', json=HOLD).status_code, 409)
            self.assertEqual(sends(car.calls), [])

    def test_unhealthy_car_refuses_arm_even_with_phone_and_detector_ok(self):
        for state in ('stale', 'down'):
            with self.subTest(state=state), self.rig(FakeCar(state)) as (client, car):
                self.assertEqual(self.health(client)['car'], state)
                self.assertEqual(client.post('/arm').status_code, 409)
                self.assertEqual(client.post('/manual', json=HOLD).status_code, 409)
                time.sleep(.15)
                self.assertEqual(sends(car.calls), [])

    def test_held_manual_is_clamped_and_stop_zeroes_with_no_later_motion(self):
        with self.rig() as (client, car):
            self.assertEqual(client.post('/arm').status_code, 200)
            client.post('/manual', json={'v_mps': .2, 'yaw_rate_rps': -.5})
            wait_for(lambda: sends(car.calls))
            self.assertEqual(sends(car.calls)[0], ('send', .15, -.5))  # 0.15 m/s start cap
            before = len(car.calls)
            self.assertEqual(client.post('/stop').json()['stop_reason'], 'operator_stop')
            self.assertEqual(client.post('/manual', json=HOLD).status_code, 409)
            time.sleep(.3)
            stopped = car.calls[before:]
            self.assertEqual(sends(after_zero(stopped)), [])
            self.assertFalse(self.health(client)['armed'])

    def test_manual_lease_expires_when_renewals_stop(self):
        with self.rig() as (client, car):
            moving = self.armed_and_moving(client, car)
            time.sleep(.5)  # no renewal: the 250 ms lease lapses
            health = self.health(client)
        self.assertTrue(health['armed'])  # a released button stops the car, not the session
        self.assertLessEqual(len(sends(car.calls)), 6)  # at most 250 ms of 20 Hz dispatch
        self.assertEqual(sends(after_zero(car.calls[moving:])), [])

    def test_rearm_does_not_revive_an_earlier_hold(self):
        with self.rig() as (client, car):
            self.armed_and_moving(client, car)
            client.post('/stop')
            self.assertEqual(client.post('/arm').status_code, 200)
            before = len(car.calls)
            time.sleep(.3)
            self.assertEqual(sends(car.calls[before:]), [])

    def test_mode_switch_and_new_session_stop_before_changing(self):
        for path, body, reason in [('/mode', {'mode': 'navigate'}, 'mode_change'),
                                   ('/session', None, 'session_reset')]:
            with self.subTest(path=path), self.rig() as (client, car):
                self.armed_and_moving(client, car)
                before = len(car.calls)
                client.post(path, json=body)
                time.sleep(.2)
                self.assertEqual(sends(after_zero(car.calls[before:])), [])
                health = self.health(client)
                self.assertEqual((health['armed'], health['stop_reason']), (False, reason))

    def test_car_health_loss_disarms_even_while_idle(self):
        with self.rig() as (client, car):
            self.assertEqual(client.post('/arm').status_code, 200)
            before = len(car.calls)
            car.state = 'stale'  # no command held: the watchdog, not a send, must catch it
            wait_for(lambda: not self.health(client)['armed'])
            self.assertEqual(self.health(client)['stop_reason'], 'car_stale')
            self.assertEqual(car.calls[before:], [('zero',)])
            self.assertEqual(client.post('/manual', json=HOLD).status_code, 409)

    def test_detector_loss_while_driving_disarms(self):
        detector = Flaky()
        with self.rig(detector=detector) as (client, car):
            moving = self.armed_and_moving(client, car)
            detector.broken = True
            with self.assertLogs('backend.app', 'ERROR'):
                self.hold(client, 3.)  # detector health goes stale 2 s after its last result
            health = self.health(client)
        self.assertEqual((health['armed'], health['detector'], health['stop_reason']),
                         (False, 'stale', 'detector_stale'))
        self.assertEqual(sends(after_zero(car.calls[moving:])), [])

    def test_adapter_error_disarms(self):
        with self.rig() as (client, car):
            self.assertEqual(client.post('/arm').status_code, 200)
            car.error = OSError('link dropped')
            with self.assertLogs('backend.motion', 'ERROR'):
                client.post('/manual', json=HOLD)
                wait_for(lambda: not self.health(client)['armed'])
            self.assertEqual(self.health(client)['stop_reason'], 'car_error')
            self.assertEqual(client.post('/manual', json=HOLD).status_code, 409)
            self.assertEqual(client.post('/arm').status_code, 409)  # the zero is still unconfirmed
            car.error = None
            wait_for(lambda: car.calls[-1:] == [('zero',)])  # retried until the car accepted it
            self.assertEqual(client.post('/arm').status_code, 200)

    def test_each_arm_opens_a_command_session_that_a_stop_ends(self):
        with self.rig() as (client, car):
            self.armed_and_moving(client, car)
            client.post('/stop')
            self.armed_and_moving(client, car)
            car.error = OSError('link dropped')
            with self.assertLogs('backend.motion', 'ERROR'):
                client.post('/manual', json=HOLD)
                wait_for(lambda: not self.health(client)['armed'])
            self.assertEqual(client.post('/arm').status_code, 409)  # the Stop is still unconfirmed
            mark = len(car.envelopes)
            car.error = None
            wait_for(lambda: any(isinstance(envelope, DriveStop) for envelope in car.envelopes[mark:]))
            self.armed_and_moving(client, car)
            client.post('/stop')
            envelopes = list(car.envelopes)
        self.assertEqual(faults(envelopes), [])
        sessions = {envelope.session_id for envelope in envelopes if not isinstance(envelope, DriveStop)}
        self.assertEqual(len(sessions), 3)
        self.assertIsInstance(envelopes[-1], DriveStop)  # shutdown's Stop ends the last session too

    def test_pose_loss_while_held_stops_all_motion(self):
        with self.rig() as (client, car):
            moving = self.armed_and_moving(client, car)
            self.feeding.clear()  # the phone stays connected but sends nothing new
            self.hold(client, .6)
            health = self.health(client)
        self.assertEqual((health['armed'], health['stop_reason']), (False, 'pose_stale'))
        self.assertEqual(sends(after_zero(car.calls[moving:])), [])

    def test_shutdown_sends_a_final_zero(self):
        car = FakeCar()
        with TestClient(create_app(':memory:', car=car)):
            pass
        self.assertEqual(car.calls, [('zero',)])
        with self.rig() as (client, car):
            moving = self.armed_and_moving(client, car)
            client.post('/manual', json=HOLD)
        # TestClient closes the phone first (phone_disconnected zero), then shutdown zeroes again.
        self.assertEqual(car.calls[-2:], [('zero',), ('zero',)])
        self.assertEqual(sends(after_zero(car.calls[moving:])), [])


if __name__ == '__main__':
    unittest.main()
