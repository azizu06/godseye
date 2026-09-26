#!/usr/bin/env python3
"""Print what a dashboard would receive on /live: message counts, points chunk shape, objects."""
import argparse
import asyncio
import json
import time

from websockets.asyncio.client import connect


async def run(url, seconds):
    counts = {}
    last_points = None
    last_objects = None
    async with connect(url) as websocket:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5)
            message = json.loads(raw)
            counts[message["type"]] = counts.get(message["type"], 0) + 1
            if message["type"] == "points":
                last_points = (message, len(raw))
            elif message["type"] == "objects":
                last_objects = message
    print("messages:", counts)
    if last_objects is not None:
        print("latest objects:", [(o["class"], o["id"][:8], o["observations"], o["confidence"])
                                  for o in last_objects["objects"]])
    if last_points is None:
        raise SystemExit("no points chunk received: is a phone (or tools/fake_phone.py) connected?")
    message, size = last_points
    print("last points chunk:", {key: value for key, value in message.items()
                                 if key not in ("positions", "colors")},
          f"points={len(message['positions']) // 3}", f"json_bytes={size}")
    print("first point xyz (ARKit world m):", message["positions"][:3], "rgb (0..1):", message["colors"][:3])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8765/live")
    parser.add_argument("--seconds", type=float, default=5.)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.seconds))


if __name__ == "__main__":
    main()
