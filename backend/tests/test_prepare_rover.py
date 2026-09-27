import unittest
from fastapi import HTTPException
from backend.prepare_rover import prepare_rover
from backend.tests.test_device_relay import status


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_request_sets_up_capture_bluetooth_and_control_without_motion(self):
        class Device:
            phone = status()
            actions = []
            def snapshot(self): return dict(connected=True, phone=self.phone)
            async def action(self, body):
                self.actions.append(body.action)
                if body.action == 'capture_start': self.phone['capture_running'] = True
                elif body.action == 'rover_scan': self.phone['peers'] = [dict(id='rover', name='Test rover')]
                elif body.action == 'rover_select': self.phone.update(rover_connected=True, rover_verified=True)
                elif body.action == 'control_enable': self.phone['control_enabled'] = True
        device = Device()
        await prepare_rover(device, lambda: False)
        self.assertEqual(device.actions, ['capture_start', 'rover_scan', 'rover_select', 'control_enable'])
        before = list(device.actions)
        await prepare_rover(device, lambda: False)
        self.assertEqual(device.actions, before)
        with self.assertRaises(HTTPException): await prepare_rover(device, lambda: True)
