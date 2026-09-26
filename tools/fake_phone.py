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


def synthetic_sensors(image_path=None):
    """Gradient JPEG, or a local photo resized to 960x720 so a detector has something to find."""
    if image_path:
        with Image.open(image_path) as photo:
            image = photo.convert("RGB").resize((960, 720))
    else:
        image = Image.new("RGB", (960, 720))
        image.putdata([(x * 255 // 959, y * 255 // 719, 96)
                       for y in range(720) for x in range(960)])
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=60)
    depth = b"".join(struct.pack("<f", 1. + x / 256 + y / 192)
                     for y in range(192) for x in range(256))
    confidence = bytes([2]) * (256 * 192)
    return output.getvalue(), depth, confidence


# Deterministic world for hardware-free rehearsals (tools/car_smoke.py): a flat floor and two
# axis-aligned boxes in ARKit world meters (+Y up, camera looks toward +Z at yaw 0). The phone
# starts at the origin 1.2 m above the floor. Boxes are (x0, x1, y0, y1, z0, z1).
WORLD_FLOOR_Y = -1.2
WORLD_LOW_BOX = (-.5, -.3, WORLD_FLOOR_Y, WORLD_FLOOR_Y + .10, 1.6, 1.8)  # 10 cm: above the 8 cm band
WORLD_BOX = (.3, .6, WORLD_FLOOR_Y, WORLD_FLOOR_Y + .50, 1.9, 2.2)  # ordinary 50 cm obstacle
WORLD_BOXES = (WORLD_LOW_BOX, WORLD_BOX)
WORLD_MAX_DEPTH_M = 5.
_INTRINSICS = (720., 720., 480., 360.)  # fx, fy, cx, cy at 960x720, as frame_bundle sends


def look_pose(frame_id, t_capture, t_wall_ms, session_id, position=(0., 0., 0.), yaw=0., pitch=.6,
              map_epoch=1):
    """A pose looking `pitch` rad below the horizon, yaw measured from +Z toward +X."""
    forward = (math.sin(yaw) * math.cos(pitch), -math.sin(pitch), math.cos(yaw) * math.cos(pitch))
    z_axis = (-forward[0], -forward[1], -forward[2])  # ARKit camera looks down its -Z
    x_axis = (z_axis[2], 0., -z_axis[0])  # up x z_axis
    norm = math.hypot(x_axis[0], x_axis[2])
    x_axis = (x_axis[0] / norm, 0., x_axis[2] / norm)
    y_axis = (z_axis[1] * x_axis[2] - z_axis[2] * x_axis[1], z_axis[2] * x_axis[0] - z_axis[0] * x_axis[2],
              z_axis[0] * x_axis[1] - z_axis[1] * x_axis[0])
    transform = [*x_axis, 0., *y_axis, 0., *z_axis, 0., *position, 1.]
    return dict(version=1, type="pose", session_id=session_id, map_epoch=map_epoch,
                frame_id=frame_id, t_capture=t_capture, t_wall_ms=t_wall_ms,
                transform=transform, tracking="normal")


def world_sensors(transform, base=None):
    """Ray-cast the world for `transform`: (jpeg, depth, confidence) for frame_bundle.

    Depth is optical-axis meters at the 256x192 grid; rays that miss everything or exceed
    5 m get confidence 0, like unreliable LiDAR pixels. `base` reuses one gradient JPEG.
    """
    import numpy as np
    jpeg = (base or synthetic_sensors())[0]
    fx, fy, cx, cy = _INTRINSICS
    cols, rows = np.meshgrid(np.arange(256), np.arange(192))
    u, v = (cols + .5) * 960 / 256, (rows + .5) * 720 / 192
    rays = np.stack([(u - cx) / fx, -(v - cy) / fy, -np.ones_like(u)], axis=-1).reshape(-1, 3)
    m = np.array(transform, dtype=np.float64).reshape(4, 4).T
    direction, origin = rays @ m[:3, :3].T, m[:3, 3]
    best = np.full(len(rays), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        floor = (WORLD_FLOOR_Y - origin[1]) / direction[:, 1]
        best = np.where(floor > 0, floor, best)
        for x0, x1, y0, y1, z0, z1 in WORLD_BOXES:
            lo, hi = np.array([x0, y0, z0]), np.array([x1, y1, z1])
            t1, t2 = (lo - origin) / direction, (hi - origin) / direction
            near = np.minimum(t1, t2).max(axis=1)
            far = np.maximum(t1, t2).min(axis=1)
            hit = (far >= near) & (near > 0)
            best = np.where(hit & (near < best), near, best)
    valid = np.isfinite(best) & (best <= WORLD_MAX_DEPTH_M)
    depth = np.where(valid, best, 0.).astype("<f4")
    confidence = np.where(valid, 2, 0).astype("u1")
    return jpeg, depth.tobytes(), confidence.tobytes()


def delayed_bundle_stream(session_id, poses=30, lag_s=.08, pose_dt=.033, frame_every=6, map_epoch=1):
    """Ordered (kind, message, capture) rehearsal of encoder latency, for pose-freshness work.

    Matches the unmerged iOS ordering: a fresh pose every `pose_dt`, and every
    `frame_every`th tick a binary bundle whose capture time is `lag_s` behind the
    newest pose. Each bundle carries the pose of its own ARFrame, not the newest one.
    `capture` is the message's t_capture; wall times are left 0 for the sender to stamp.
    """
    stream = []
    for tick in range(poses):
        t = tick * pose_dt
        stream.append(("pose", look_pose(tick, t, 0, session_id, yaw=.05 * math.sin(t), map_epoch=map_epoch), t))
        if tick >= 3 and tick % frame_every == 0:
            t_frame = t - lag_s
            stream.append(("frame", look_pose(1000 + tick, t_frame, 0, session_id, yaw=.05 * math.sin(t_frame),
                                              map_epoch=map_epoch), t_frame))
    return stream


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


async def run(url, frame_hz, image_path=None):
    sensors = synthetic_sensors(image_path)
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
    parser.add_argument("--image", help="local JPEG/PNG to send instead of the gradient (depth stays synthetic)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url, args.frame_hz, args.image))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
