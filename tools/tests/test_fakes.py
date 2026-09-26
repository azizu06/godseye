import asyncio
import math
from unittest.mock import patch
from types import SimpleNamespace
import base64
import io
import json
import struct
import unittest

from PIL import Image
from tools.fake_phone import (delayed_bundle_stream, frame_bundle, look_pose, pose, synthetic_sensors,
                              world_sensors)
from tools.fake_live import BACKPACK_ID, messages


class FakeContractTests(unittest.TestCase):
    def test_frame_bundle_has_valid_sensors_and_matching_pose(self):
        expected = pose(42, 1.25, 123456, "test-session")
        sensors = synthetic_sensors()
        self.assertEqual(sensors, synthetic_sensors())
        bundle = frame_bundle(expected, sensors)
        header_len, = struct.unpack("<I", bundle[:4])
        header = json.loads(bundle[4:4 + header_len])
        offset = 4 + header_len
        jpeg_len = header["image"]["jpeg_len"]
        jpeg = bundle[offset:offset + jpeg_len]
        self.assertEqual(Image.open(io.BytesIO(jpeg)).size, (960, 720))
        offset += jpeg_len
        depth_len = header["depth"]["len"]
        self.assertEqual(depth_len, 256 * 192 * 4)
        self.assertEqual(struct.unpack("<f", bundle[offset:offset + 4])[0], 1.)
        offset += depth_len
        self.assertEqual(header["confidence"]["len"], 256 * 192)
        self.assertEqual(bundle[offset:], bytes([2]) * (256 * 192))
        self.assertEqual(len(bundle), offset + header["confidence"]["len"])
        for key in expected:
            if key != "type":
                self.assertEqual(header[key], expected[key])
        self.assertEqual(header["type"], "frame")
        self.assertEqual(header["image"]["intrinsics"], [720., 0., 0., 0., 720., 0., 480., 360., 1.])
        self.assertEqual(expected["transform"][12:15],
                         [math.sin(.25), 1., math.cos(.25)])

    def test_local_photo_replaces_the_gradient_at_contract_size(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png") as photo:
            Image.new("RGB", (64, 48), (10, 200, 30)).save(photo.name)
            jpeg, depth, confidence = synthetic_sensors(photo.name)
        with Image.open(io.BytesIO(jpeg)) as decoded:
            self.assertEqual((decoded.format, decoded.size), ("JPEG", (960, 720)))
            self.assertLess(abs(decoded.getpixel((480, 360))[1] - 200), 8)
        self.assertEqual((depth, confidence), synthetic_sensors()[1:])

    def test_world_depth_is_deterministic_and_sees_floor_and_boxes(self):
        import numpy as np
        transform = look_pose(0, 0., 0, "w")["transform"]
        first, second = world_sensors(transform), world_sensors(transform)
        self.assertEqual(first, second)
        depth = np.frombuffer(first[1], "<f4")
        confidence = np.frombuffer(first[2], "u1")
        self.assertEqual((depth.size, confidence.size), (256 * 192, 256 * 192))
        self.assertTrue(np.all(depth[confidence == 0] == 0))
        self.assertGreater((confidence == 2).mean(), .3)
        self.assertLessEqual(depth.max(), 5.)

    def test_delayed_bundle_fixture_keeps_encoder_latency_ordering(self):
        stream = delayed_bundle_stream("delay")
        poses = [(m, t) for kind, m, t in stream if kind == "pose"]
        frames = [(m, t) for kind, m, t in stream if kind == "frame"]
        self.assertTrue(frames)
        self.assertTrue(all(b[1] - a[1] > .03 for a, b in zip(poses, poses[1:])))
        seen_pose = -1.
        for kind, message, capture in stream:
            if kind == "pose":
                seen_pose = capture
            else:
                self.assertAlmostEqual(seen_pose - capture, .08 + 0., places=6)
                self.assertNotIn(capture, [t for _, t in poses])
                self.assertEqual(message["transform"], look_pose(0, 0., 0, "delay", yaw=.05 * math.sin(capture))["transform"])

    def test_live_types_shapes_and_synthetic_move(self):
        before = messages(0, 100, False)
        after = messages(5, 105, True)
        fields = {
            "health": {"phone", "car", "detector", "pose_age_ms", "mode", "armed", "stop_reason"},
            "pose": {"position", "yaw_rad", "tracking"},
            "points": {"chunk_id", "positions", "colors"},
            "occupancy": {"origin", "cell_m", "width", "height", "cells"},
            "path": {"points"}, "objects": {"objects"},
            "event": {"kind", "object_id", "old_position", "new_position", "displacement_m", "t"},
        }
        self.assertEqual(set(after), set(fields))
        for kind, required in fields.items():
            self.assertEqual(set(after[kind]), required | {"version", "type"})
            self.assertEqual(after[kind]["version"], 1)
            self.assertEqual(after[kind]["type"], kind)
            json.dumps(after[kind], allow_nan=False)
        self.assertNotEqual(before["pose"]["position"], after["pose"]["position"])
        grid = after["occupancy"]
        cells = base64.b64decode(grid["cells"], validate=True)
        self.assertEqual(len(cells), grid["width"] * grid["height"])
        self.assertLessEqual(set(cells), {0, 1, 2})
        obj = after["objects"]["objects"][0]
        self.assertEqual(set(obj), {"id", "class", "position", "confidence", "first_seen", "last_seen", "observations", "state"})
        self.assertEqual(obj["id"], BACKPACK_ID)
        self.assertEqual(obj["state"], "moved")
        self.assertEqual(obj["position"], after["event"]["new_position"])
        self.assertEqual(after["event"]["kind"], "moved")
        self.assertEqual(after["event"]["displacement_m"], 1.)
        self.assertIn("SYNTHETIC", after["health"]["stop_reason"])


class StopStream(Exception):
    pass


class FakeStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_hello_and_pacing_without_network(self):
        from tools.fake_phone import run
        sent = []

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, message):
                sent.append((asyncio.get_running_loop().time(), message))
                if len([m for _, m in sent if isinstance(m, str)]) >= 5:
                    raise StopStream()

        with patch("tools.fake_phone.connect", return_value=Socket()):
            with self.assertRaises(StopStream):
                await run("ws://example.invalid/phone", 5)
        text = [(t, json.loads(m)) for t, m in sent if isinstance(m, str)]
        hello = text[0][1]
        self.assertEqual(set(hello), {"version", "type", "device", "session_id", "map_epoch", "supports_scene_depth", "supports_mesh"})
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["version"], 1)
        poses = text[1:]
        self.assertTrue(all(m["session_id"] == hello["session_id"] for _, m in poses))
        for (a, _), (b, _) in zip(poses, poses[1:]):
            self.assertGreater(b - a, .02)
            self.assertLess(b - a, .15)
        self.assertTrue(any(isinstance(m, bytes) for _, m in sent))

    async def test_live_stream_delivers_all_types_without_network(self):
        from tools.fake_live import stream
        sent = []

        class Socket:
            request = SimpleNamespace(path="/live")

            async def send(self, message):
                sent.append(json.loads(message))
                if sent[-1]["type"] == "event":
                    raise StopStream()

        with self.assertRaises(StopStream):
            await stream(Socket(), 0)
        self.assertEqual({m["type"] for m in sent},
                         {"health", "pose", "points", "occupancy", "path", "objects", "event"})


if __name__ == "__main__":
    unittest.main()
