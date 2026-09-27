import threading
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.rover_relay import RelayCar
from backend.tests.test_actuation import fixture
from tools.car_rehearsal import wait_for

KEY = 'ONLY_A_TEST_KEY_NOT_A_REAL_ROVER_KEY'
HEADERS = {'Authorization': 'Bearer ' + KEY}


def status(seq=1, **changes):
    values = dict(version=1, type='status', seq=seq, capture_running=False,
        capture_status='Ready', tracking='Not started', network='Offline', rover_connected=False,
        rover_verified=False, rover_status='Disconnected', control_enabled=False,
        control_status='Disabled', peers=[])
    values.update(changes)
    return values


def next_action(ws):
    for _ in range(10):
        message = ws.receive_json()
        if message['type'] == 'action': return message
    raise AssertionError('No phone action arrived')


class DeviceRelayTests(unittest.TestCase):
    def test_authenticated_capture_setup_without_map_or_motor_arming(self):
        app = create_app(db_path=':memory:', car=RelayCar(KEY, fixture()))
        with TestClient(app) as client:
            self.assertFalse(client.get('/device').json()['connected'])
            self.assertEqual(client.post('/device/action', json={'action': 'capture_start'}).status_code, 401)
            self.assertEqual(client.post('/device/action', json={'action': 'drive'}, headers=HEADERS).status_code, 422)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/device'): pass
            with client.websocket_connect('/device', headers=HEADERS) as ws:
                ws.send_json(status())
                wait_for(lambda: client.get('/device').json()['connected'])
                results = []
                worker = threading.Thread(target=lambda: results.append(client.post('/device/action',
                    json={'action': 'capture_start'}, headers=HEADERS)))
                worker.start()
                message = next_action(ws)
                self.assertEqual(message['action'], 'capture_start')
                self.assertNotIn('power', message)
                ws.send_json(dict(version=1, type='ack', id='wrong', ok=True, message='stale'))
                ws.send_json(dict(version=1, type='ack', id=message['id'], ok=True, message='Starting camera'))
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0].status_code, 200)
                self.assertFalse(app.state.armed)
                self.assertFalse(app.state.motion.active)
            self.assertFalse(client.get('/device').json()['connected'])

    def test_operator_stop_preempts_pending_phone_setup(self):
        app = create_app(db_path=':memory:', car=RelayCar(KEY, fixture()))
        with TestClient(app) as client, client.websocket_connect('/device', headers=HEADERS) as ws:
            ws.send_json(status())
            wait_for(lambda: client.get('/device').json()['connected'])
            results = []
            worker = threading.Thread(target=lambda: results.append(client.post('/device/action',
                json={'action': 'control_enable'}, headers=HEADERS)))
            worker.start()
            old = next_action(ws)
            self.assertEqual(client.post('/stop').status_code, 200)
            stop = next_action(ws)
            self.assertEqual(stop['action'], 'stop')
            ws.send_json(dict(version=1, type='ack', id=old['id'], ok=True, message='Late reply'))
            worker.join(2)
            self.assertEqual(results[0].status_code, 409)
            self.assertFalse(app.state.armed)

    def test_one_click_arm_prepares_phone_and_stop_cancels_startup(self):
        app = create_app(db_path=':memory:', car=RelayCar(KEY, fixture()))
        with TestClient(app) as client, client.websocket_connect('/device', headers=HEADERS) as ws:
            ws.send_json(status())
            wait_for(lambda: client.get('/device').json()['connected'])
            results = []
            worker = threading.Thread(target=lambda: results.append(client.post('/arm?prepare=true', headers=HEADERS)))
            worker.start()
            setup = next_action(ws)
            self.assertEqual(setup['action'], 'capture_start')
            ws.send_json(dict(version=1, type='ack', id=setup['id'], ok=True, message='Preparing'))
            self.assertEqual(client.post('/stop').status_code, 200)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0].status_code, 409)
            self.assertFalse(app.state.armed)
            self.assertIsNone(app.state.arm_request_token)

    def test_feedback_replay_disconnects_and_single_phone_owner(self):
        app = create_app(db_path=':memory:', car=RelayCar(KEY, fixture()))
        with TestClient(app) as client, client.websocket_connect('/device', headers=HEADERS) as ws:
            ws.send_json(status())
            wait_for(lambda: client.get('/device').json()['connected'])
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/device', headers=HEADERS): pass
            ws.send_json(status())
            wait_for(lambda: not client.get('/device').json()['connected'])


if __name__ == '__main__': unittest.main()
