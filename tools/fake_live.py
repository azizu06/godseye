#!/usr/bin/env python3
"""SYNTHETIC /live server; never controls a car or contacts the backend."""
import argparse
import asyncio
import base64
import json
import math
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

BACKPACK_ID = "synthetic-backpack"


def messages(elapsed, wall_time, moved=False):
    old, new = [0.5, 0., -0.5], [1.5, 0., -0.5]
    position = new if moved else old
    payloads = {
        "health": dict(phone="ok", car="down", detector="ok", pose_age_ms=0,
                       mode="manual", armed=False, stop_reason="SYNTHETIC DEMO: no car"),
        "pose": dict(position=[math.sin(elapsed * .2), 1., math.cos(elapsed * .2)],
                     yaw_rad=elapsed * .2, tracking="normal"),
        "points": dict(chunk_id=int(elapsed * 2), positions=[0., 0., 0., 1., 0., -1.],
                       colors=[1., 0., 0., 0., 1., 0.]),
        "occupancy": dict(origin=[-1., -1.], cell_m=.05, width=4, height=4,
                          cells=base64.b64encode(bytes([0, 1, 1, 2] * 4)).decode()),
        "path": dict(points=[[0., 0.], [0.5, -0.5], [1., -1.]]),
        "objects": dict(objects=[dict(id=BACKPACK_ID, **{"class": "backpack"}, position=position,
                                     confidence=.95, first_seen=wall_time - elapsed,
                                     last_seen=wall_time, observations=max(1, int(elapsed * 2)),
                                     state="moved" if moved else "present")]),
        "event": dict(kind="moved", object_id=BACKPACK_ID, old_position=old,
                      new_position=new, displacement_m=1., t=wall_time),
    }
    return {kind: dict(version=1, type=kind, **payload) for kind, payload in payloads.items()}


async def stream(websocket, move_after):
    if websocket.request.path != "/live":
        await websocket.close(code=1008, reason="Use /live")
        return
    loop = asyncio.get_running_loop()
    start = loop.time()
    next_slow = next_grid = 0.
    tick = 0
    moved = False
    try:
        while True:
            await asyncio.sleep(max(0, start + tick / 15 - loop.time()))
            elapsed = loop.time() - start
            just_moved = not moved and elapsed >= move_after
            moved = moved or just_moved
            batch = messages(elapsed, time.time(), moved)
            kinds = ["pose"]
            if elapsed >= next_slow:
                kinds += ["health", "points", "objects"]
                next_slow = (math.floor(elapsed * 2) + 1) / 2
            if elapsed >= next_grid:
                kinds += ["occupancy"]
                next_grid = math.floor(elapsed) + 1.
            if tick == 0:
                kinds += ["path"]
            if just_moved:
                if "objects" not in kinds:
                    kinds += ["objects"]
                kinds += ["event"]
            for kind in kinds:
                await websocket.send(json.dumps(batch[kind]))
            tick = max(tick + 1, math.floor((loop.time() - start) * 15) + 1)
    except ConnectionClosed:
        return


async def run(host, port, move_after):
    async with serve(lambda ws: stream(ws, move_after), host, port):
        print(f"SYNTHETIC demo: ws://{host}:{port}/live; moved backpack after {move_after}s", flush=True)
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--move-after", type=float, default=5.)
    args = parser.parse_args()
    if args.move_after < 0 or not math.isfinite(args.move_after):
        parser.error("--move-after must be finite and nonnegative")
    try:
        asyncio.run(run(args.host, args.port, args.move_after))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
