import asyncio
import json
import unittest

from tools.rover_usb_relay import Frames, Queue, Relay, STOP, command

FORWARD = b'{"N":2,"H":"M","D1":3,"D2":60,"T":200}'
PROBE = b'{"N":22,"H":"TEST01","D1":1}'
KEY = '0123456789ABCDEF'


class FakeSerial:
    def __init__(self):
        self.writes = []
        self.rx = bytearray()

    def write(self, data):
        self.writes.append(json.loads(data))
        obj = self.writes[-1]
        if obj['N'] == 22:
            self.rx.extend(('{' + obj['H'] + '_42}').encode())
        return len(data)

    def read(self, size):
        chunk = bytes(self.rx[:7])
        del self.rx[:7]
        return chunk


class QueueTests(unittest.TestCase):
    def test_limits_and_no_indefinite_or_ultrasound_commands(self):
        for n in [1, 3, 4, 21, True, 2.0]:
            with self.assertRaises(ValueError):
                command(json.dumps(dict(N=n, H='M', D1=3, D2=60, T=200)))
        for field, value in [('T', 0), ('T', 1000), ('D2', 81), ('D1', 5), ('D2', True), ('D2', 1.5)]:
            obj = json.loads(FORWARD)
            obj[field] = value
            with self.assertRaises(ValueError): command(json.dumps(obj))

    def test_latest_stop_expiry_watchdog_reset(self):
        q = Queue()
        self.assertEqual(q.pop(0), STOP)
        q.put(FORWARD, 0.01)
        q.put(FORWARD.replace(b'"D1":3', b'"D1":2'), 0.02)
        self.assertEqual(json.loads(q.pop(0.03))['D1'], 2)
        self.assertIsNone(q.pop(0.04))
        self.assertEqual(q.pop(0.24), STOP)
        q.put(FORWARD, 0.3)
        self.assertEqual(q.pop(0.41), STOP)
        q.put(FORWARD, 0.5)
        q.put(STOP, 0.51)
        self.assertEqual(q.pop(0.52), STOP)
        self.assertIsNone(q.pop(0.53))
        q.put(FORWARD, 0.6)
        q.reset()
        self.assertEqual(q.pop(0.61), STOP)
        self.assertIsNone(q.pop(0.62))

    def test_fragmented_and_oversized_frames(self):
        f = Frames()
        self.assertEqual(f.feed(b'boot\r\n{"N":'), [])
        self.assertEqual(f.feed(b'100}{ok}'), [b'{"N":100}', b'{ok}'])
        with self.assertRaises(ValueError): f.feed(b'{' + b'x' * 128)
        self.assertEqual(f.feed(b'{ok}'), [b'{ok}'])


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.serial = FakeSerial()
        self.relay = Relay(self.serial, KEY)
        self.server = await asyncio.start_server(self.relay.client, '127.0.0.1', 0, limit=256)
        self.port = self.server.sockets[0].getsockname()[1]
        self.pump = asyncio.create_task(self.relay.pump())
        self.clients = []

    async def asyncTearDown(self):
        for writer in self.clients:
            writer.close()
            await writer.wait_closed()
        self.relay.closed = True
        await self.pump
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, key=KEY):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.clients.append(writer)
        writer.write(json.dumps({'token': key}).encode() + b'\n')
        await writer.drain()
        return reader, writer

    async def frame(self, reader):
        return await asyncio.wait_for(reader.readuntil(b'}'), 1)

    async def test_auth_one_owner_feedback_and_disconnect_stop(self):
        bad, writer = await self.connect('WRONG')
        self.assertEqual(await asyncio.wait_for(bad.read(), 1), b'')
        reader, writer = await self.connect()
        self.assertEqual(await self.frame(reader), b'{Authenticated}')
        second, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(second.read(), 1), b'')
        writer.write(PROBE)
        self.assertEqual(await self.frame(reader), b'{TEST01_42}')
        writer.write(FORWARD)
        await asyncio.sleep(0.12)
        self.assertTrue(any(c['N'] == 2 for c in self.serial.writes))
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.08)
        self.assertEqual(self.serial.writes[-1]['N'], 100)
        count = sum(c['N'] == 2 for c in self.serial.writes)
        newer, _ = await self.connect()
        self.assertEqual(await self.frame(newer), b'{Authenticated}')
        await asyncio.sleep(0.1)
        self.assertEqual(sum(c['N'] == 2 for c in self.serial.writes), count)

    async def test_partial_frame_timeout_stops_connection(self):
        reader, writer = await self.connect()
        await self.frame(reader)
        writer.write(FORWARD + b'{"N":')
        await asyncio.sleep(0.24)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertEqual(self.serial.writes[-1]['N'], 100)


if __name__ == '__main__':
    unittest.main()
