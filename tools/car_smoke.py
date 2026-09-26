#!/usr/bin/env python3
"""Hardware-free rehearsal: fake phone world -> real backend /phone pipeline -> /live occupancy.

Starts the real backend on a free loopback port with a task-local database, capture
recording off and no weights, streams the deterministic floor + low box + ordinary box world
of tools/fake_phone.py, and exits nonzero unless occupancy is reproducible and the system
stays disarmed with only zero drive stubs. No device, GPU, cloud, key or car is touched.
"""
import argparse
import asyncio
import base64
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect

from tools.fake_phone import (WORLD_BOX, WORLD_FLOOR_Y, WORLD_LOW_BOX, delayed_bundle_stream, frame_bundle,
                              look_pose, synthetic_sensors, world_sensors)

ROOT = Path(__file__).resolve().parent.parent
YAWS = (-.14, 0., .14, -.07, .07, 0.)  # radians
PITCH = .6
FRAME_PERIOD_S = .35  # above the backend's 250 ms mapping interval so every frame is mapped
QUIET_S = 2.5  # occupancy publishes at most 1 Hz; this long without a change is settled
DEADLINE_S = 90.
SERVER = ("import logging, sys, uvicorn; logging.basicConfig(level=logging.INFO); "
          "uvicorn.run('backend.app:app', host='127.0.0.1', port=int(sys.argv[1]), ws_max_size=8388608, "
          "log_level='warning')")


class Failure(Exception):
    pass


def check(condition, message):
    if not condition:
        raise Failure(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rest(port, path, method="GET"):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None


def cell_at(grid, x, z):
    """Dashboard semantics: column grows along +x from origin[0], row along +z from origin[1]; 0 outside."""
    col = math.floor((x - grid["origin"][0]) / grid["cell_m"])
    row = math.floor((z - grid["origin"][1]) / grid["cell_m"])
    if not (0 <= col < grid["width"] and 0 <= row < grid["height"]):
        return 0
    return base64.b64decode(grid["cells"])[row * grid["width"] + col]


def centre(box):
    return (box[0] + box[1]) / 2, (box[4] + box[5]) / 2


async def read_live(port, sink):
    async with connect(f"ws://127.0.0.1:{port}/live", max_size=None) as live:
        async for raw in live:
            sink.append((time.monotonic(), json.loads(raw)))


async def stream_world(port, session):
    """One phone session over the real /phone socket; returns after the last frame plus a quiet period."""
    base = synthetic_sensors()
    async with connect(f"ws://127.0.0.1:{port}/phone", max_size=None) as phone:
        await phone.send(json.dumps(dict(version=1, type="hello", device="synthetic-phone", session_id=session,
                                        map_epoch=1, supports_scene_depth=True, supports_mesh=False)))
        for index, yaw in enumerate(YAWS):
            message = look_pose(index, index * FRAME_PERIOD_S, time.time_ns() // 1_000_000, session,
                                yaw=yaw, pitch=PITCH)
            await phone.send(json.dumps(message))
            await phone.send(frame_bundle(message, world_sensors(message["transform"], base)))
            await asyncio.sleep(FRAME_PERIOD_S)
        status, health = await asyncio.to_thread(rest, port, "/health")
        check(status == 200 and health["armed"] is False and health["car"] == "down",
              f"unsafe health while streaming: {health}")
        return status, health


async def one_session(port):
    """Stream the world, then the last occupancy grid once it stops changing."""
    seen = []
    reader = asyncio.create_task(read_live(port, seen))
    try:
        await stream_world(port, str(uuid.uuid4()))
        await asyncio.sleep(1.2)  # let the last mapped frame reach the 1 Hz publisher
        while True:
            grids = [m for _, m in seen if m["type"] == "occupancy"]
            if grids and time.monotonic() - max(t for t, m in seen if m["type"] == "occupancy") > QUIET_S:
                return grids[-1], [m for _, m in seen]
            await asyncio.sleep(.25)
    finally:
        reader.cancel()


def assert_world(grid):
    low, box = centre(WORLD_LOW_BOX), centre(WORLD_BOX)
    check(abs(grid["floor_y"] - WORLD_FLOOR_Y) < .03, f"floor_y {grid['floor_y']} not near {WORLD_FLOOR_Y}")
    check(cell_at(grid, *low) == 2, "low obstacle is not occupied")
    check(cell_at(grid, *box) == 2, "ordinary obstacle is not occupied")
    check(cell_at(grid, 0., 1.4) == 1, "open floor ahead is not free")
    check(cell_at(grid, box[0], box[1] + 1.0) == 0, "floor hidden behind the obstacle is not unknown")
    check(cell_at(grid, 0., -3.) == 0, "area behind the phone is not unknown")
    cells = np.frombuffer(base64.b64decode(grid["cells"]), np.uint8)
    check(cells.size == grid["width"] * grid["height"] and set(cells.tolist()) == {0, 1, 2},
          "grid must contain unknown, free and occupied cells")


def assert_disarmed(port, log):
    status, health = rest(port, "/health")
    check(status == 200 and health["armed"] is False and health["car"] == "down",
          f"system is not disarmed with car down: {health}")
    status, _ = rest(port, "/arm", "POST")
    check(status == 409, f"/arm must stay refused by default, got {status}")
    drives = re.findall(r"DRIVE STUB v_mps=(\S+) yaw_rate_rps=(\S+)", log)
    check(all(float(v) == 0. and float(w) == 0. for v, w in drives), f"nonzero drive stub: {drives}")
    source = (ROOT / "backend" / "drive.py").read_text()
    check(re.findall(r"^\s*(?:import|from)\s+(\w+)", source, re.M) == ["logging"],
          "backend/drive.py must stay logging-only")
    return len(drives)


async def delayed_probe(port):
    """Informational only: does this backend map bundles that arrive 80 ms behind newer poses?"""
    session = str(uuid.uuid4())
    base = synthetic_sensors()
    seen = []
    reader = asyncio.create_task(read_live(port, seen))
    try:
        async with connect(f"ws://127.0.0.1:{port}/phone", max_size=None) as phone:
            await phone.send(json.dumps(dict(version=1, type="hello", device="synthetic-phone", session_id=session,
                                            map_epoch=1, supports_scene_depth=True, supports_mesh=False)))
            start = time.monotonic()
            for kind, message, capture in delayed_bundle_stream(session):
                await asyncio.sleep(max(0., start + capture + (.08 if kind == "frame" else 0.) - time.monotonic()))
                message = dict(message, t_wall_ms=time.time_ns() // 1_000_000)
                await phone.send(json.dumps(message) if kind == "pose"
                                 else frame_bundle(message, world_sensors(message["transform"], base)))
            await asyncio.sleep(1.)
    finally:
        reader.cancel()
    health = [m for _, m in seen if m["type"] == "health"][-1]
    return sum(m["type"] == "points" for _, m in seen), health["stop_reason"]


def start_server(port, workdir):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GODSEYE_")}
    env.update(GODSEYE_DB=str(workdir / "smoke.db"), GODSEYE_CAPTURE_DIR="", PYTHONPATH=str(ROOT))
    log = open(workdir / "server.log", "w+")
    process = subprocess.Popen([sys.executable, "-c", SERVER, str(port)], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        if process.poll() is not None:
            raise Failure("backend exited at startup:\n" + (workdir / "server.log").read_text()[-800:])
        try:
            if rest(port, "/health")[0] == 200:
                return process
        except OSError:
            pass
        time.sleep(.1)
    raise Failure("backend did not start")


async def run(port, delayed):
    with tempfile.TemporaryDirectory(prefix="godseye-car-smoke-") as tmp:
        workdir = Path(tmp)
        process = start_server(port, workdir)
        try:
            first, _ = await asyncio.wait_for(one_session(port), DEADLINE_S)
            assert_world(first)
            second, _ = await asyncio.wait_for(one_session(port), DEADLINE_S)
            check((first["origin"], first["width"], first["height"], first["cells"]) ==
                  (second["origin"], second["width"], second["height"], second["cells"]),
                  "occupancy differs between two identical runs")
            notes = ""
            if delayed:
                points, reason = await asyncio.wait_for(delayed_probe(port), DEADLINE_S)
                notes = f"; delayed-bundle probe (not asserted): {points} points chunks, stop_reason={reason}"
            await asyncio.sleep(.5)
            drives = assert_disarmed(port, (workdir / "server.log").read_text())
            print(f"car smoke PASS: {first['width']}x{first['height']} grid reproducible, floor_y={first['floor_y']}, "
                  f"disarmed, {drives} zero drive stubs, no transport{notes}")
        finally:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, help="loopback port (default: a free one)")
    parser.add_argument("--delayed-bundles", action="store_true",
                        help="also report how the backend treats encoder-delayed bundles (informational)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.port or free_port(), args.delayed_bundles))
    except (Failure, asyncio.TimeoutError, OSError) as error:
        print(f"car smoke FAIL: {error!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
