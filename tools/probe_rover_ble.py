"""Verify BLE -> ESP -> Uno using Stop and cached sensor queries only. No motion."""
import argparse
import asyncio
import json
import re
import secrets
import time

from tools.rover_usb_relay import Frames, STOP

SERVICE = '9e9e0001-3a17-4d2e-9a61-5c7d581f1800'
INPUT = '9e9e0002-3a17-4d2e-9a61-5c7d581f1800'
OUTPUT = '9e9e0003-3a17-4d2e-9a61-5c7d581f1800'


async def run(name, samples):
    from bleak import BleakClient, BleakScanner
    found = await BleakScanner.discover(timeout=8, return_adv=True, service_uuids=[SERVICE])
    matches = [device for device, advertisement in found.values()
               if (advertisement.local_name or device.name) == name]
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one {name!r}; found {len(matches)}. Check BLE firmware and power.')
    frames = Frames()
    replies = asyncio.Queue(maxsize=16)

    def notify(_, data):
        for frame in frames.feed(bytes(data)):
            if replies.full(): replies.get_nowait()
            replies.put_nowait(frame)

    async with BleakClient(matches[0], timeout=10) as client:
        await client.start_notify(OUTPUT, notify)

        async def write(packet):
            # Exercise the default ATT MTU fragmentation used by the phone.
            for start in range(0, len(packet), 20):
                await client.write_gatt_char(INPUT, packet[start:start + 20], response=True)

        await write(STOP)
        times = []
        prefix = 'BP' + secrets.token_hex(3).upper()
        try:
            for i in range(samples):
                tag = prefix + str(i)
                packet = json.dumps(dict(N=22, H=tag, D1=1), separators=(',', ':')).encode()
                start = time.monotonic()
                await write(packet)
                while True:
                    left = 2.5 - (time.monotonic() - start)
                    if left <= 0: raise TimeoutError('No matching Uno feedback over Bluetooth')
                    frame = await asyncio.wait_for(replies.get(), left)
                    match = re.fullmatch(rb'\{' + tag.encode() + rb'_(\d{1,4})\}', frame)
                    if match and int(match[1]) <= 1023:
                        times.append(round((time.monotonic() - start) * 1000, 1))
                        print(f'Uno reply {i + 1}/{samples}: {times[-1]} ms', flush=True)
                        break
                await asyncio.sleep(0.25)
        finally:
            if client.is_connected: await write(STOP)
        print(f'BLE + real Uno verified: {samples} replies, max {max(times)} ms. No motion sent.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='Exact advertised GodsEye-Rover-XXXX name')
    parser.add_argument('--samples', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100: parser.error('samples must be 1–100')
    asyncio.run(run(args.name, args.samples))
