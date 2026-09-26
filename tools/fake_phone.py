#!/usr/bin/env python3
"""Synthetic v1 phone source. No camera, recordings, or weights required."""
import argparse
import asyncio
import io
import json
import math
import struct
import time
import uuid

from PIL import Image
from websockets.asyncio.client import connect


def pose(frame_id, t_capture, t_wall_ms, session_id, map_epoch=1):
    yaw = t_capture * 0.2
    c, s = math.cos(yaw), math.sin(yaw)
    return dict(version=1, type="pose", session_id=session_id, map_epoch=map_epoch,
                frame_id=frame_id, t_capture=t_capture, t_wall_ms=t_wall_ms,
                transform=[c, 0., -s, 0., 0., 1., 0., 0., s, 0., c, 0.,
                           math.sin(yaw), 1., math.cos(yaw), 1.], tracking="normal")


def synthetic_sensors():
    image = Image.new("RGB", (960, 720))
    image.putdata([(x * 255 // 959, y * 255 // 719, 96)
                   for y in range(720) for x in range(960)])
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=60)
    depth = b"".join(struct.pack("<f", 1. + x / 256 + y / 192)
                     for y in range(192) for x in range(256))
    confidence = bytes([2]) * (256 * 192)
    return output.getvalue(), depth, confidence


def frame_bundle(frame_pose, sensors):
    jpeg, depth, confidence = sensors
    header = dict(frame_pose, type="frame",
                  image=dict(width=960, height=720, jpeg_len=len(jpeg),
                             intrinsics=[720., 0., 0., 0., 720., 0., 480., 360., 1.],
                             orientation="landscape_right"),
                  depth=dict(width=256, height=192, format="float32_m", len=len(depth)),
                  confidence=dict(width=256, height=192, format="uint8_0_2", len=len(confidence)))
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded + jpeg + depth + confidence


async def run(url, frame_hz):
    sensors = synthetic_sensors()
    session = str(uuid.uuid4())
    async with connect(url) as websocket:
        await websocket.send(json.dumps(dict(version=1, type="hello", device="synthetic-phone",
                                            session_id=session, map_epoch=1,
                                            supports_scene_depth=True, supports_mesh=False)))
        loop = asyncio.get_running_loop()
        start = loop.time()
        tick = 0
        next_frame = 0.
        while True:
            deadline = start + tick / 30
            await asyncio.sleep(max(0, deadline - loop.time()))
            capture = loop.time() - start
            message = pose(tick, capture, time.time_ns() // 1_000_000, session)
            await websocket.send(json.dumps(message))
            if capture >= next_frame:
                await websocket.send(frame_bundle(message, sensors))
                next_frame = (math.floor(capture * frame_hz) + 1) / frame_hz
            # Skip missed ticks rather than burst after a slow send.
            tick = max(tick + 1, math.floor((loop.time() - start) * 30) + 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8765/phone")
    parser.add_argument("--frame-hz", type=int, choices=range(5, 11), default=5)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url, args.frame_hz))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
