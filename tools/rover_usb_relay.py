"""Explicit manual TCP-to-Uno relay. Separate from the logging-only backend adapter."""
import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import time

STOP = b'{"H":"S","N":100}'
PROBE = b'{"H":"USBPROBE","N":22,"D1":1}'


def command(raw):
    """Allow only bounded timed motion, cached feedback and Stop."""
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError('Expected command object')
    if not isinstance(obj.get('H'), str) or not re.fullmatch(r'[A-Z0-9]{1,24}', obj['H']):
        raise ValueError('Invalid command ID')
    n = obj.get('N')
    if type(n) is not int:
        raise ValueError('Invalid command type')
    if n == 100:
        return STOP
    if n == 22 and type(obj.get('D1')) is int and obj['D1'] == 1:
        clean = dict(H=obj['H'], N=22, D1=1)
    elif n == 2 and all(type(obj.get(k)) is int for k in ('D1', 'D2', 'T')):
        if not (1 <= obj['D1'] <= 4 and 1 <= obj['D2'] <= 80 and obj['T'] == 200):
            raise ValueError('Motion outside allowed limits')
        clean = dict(H='M', N=2, D1=obj['D1'], D2=obj['D2'], T=200)
    else:
        raise ValueError('Command is not allowed')
    return json.dumps(clean, separators=(',', ':')).encode()


class Frames:
    def __init__(self):
        self.pending = bytearray()

    def feed(self, data):
        result = []
        for byte in data:
            if byte == 123:
                self.pending = bytearray([byte])
            elif self.pending:
                self.pending.append(byte)
                if len(self.pending) > 128:
                    self.pending.clear()
                    raise ValueError('Oversized frame')
                if byte == 125:
                    result.append(bytes(self.pending))
                    self.pending.clear()
        return result


class Queue:
    """Latest-only movement; Stop priority; no replay across connections."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.stop = True
        self.motion = None
        self.query = None
        self.last_drive = None

    def put(self, data, now):
        valid = command(data)
        kind = json.loads(valid)['N']
        if kind == 100:
            self.stop = True
            self.motion = None
        elif kind == 2:
            self.motion = (valid, now)
        else:
            self.query = valid

    def pop(self, now):
        if self.motion and now - self.motion[1] > 0.1:
            self.motion = None
            self.stop = True
        if self.last_drive is not None and now - self.last_drive >= 0.2:
            self.stop = True
        if self.stop:
            self.stop = False
            self.last_drive = None
            return STOP
        if self.motion:
            packet, _ = self.motion
            self.motion = None
            self.last_drive = now
            return packet
        if self.query:
            packet, self.query = self.query, None
            return packet
        return None


async def check_uno(port):
    # Opening an Uno normally resets it; allow boot/calibration to settle.
    await asyncio.sleep(4)
    port.reset_input_buffer()
    port.write(STOP)
    await asyncio.sleep(0.1)
    port.write(PROBE)
    frames = Frames()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        for reply in frames.feed(port.read(512)):
            match = re.fullmatch(rb'\{USBPROBE_(\d{1,4})\}', reply)
            if match and int(match[1]) <= 1023:
                print('Verified Uno reply over USB (non-motion query).', flush=True)
                return
        await asyncio.sleep(0.01)
    raise RuntimeError('No Uno reply over USB. Set Upload/Cam to Upload and check power/cable. No relay started.')


class Relay:
    def __init__(self, port, key):
        self.port, self.key = port, key
        self.queue = Queue()
        self.owner = None
        self.serial_frames = Frames()
        self.uart_free = 0
        self.closed = False

    async def client(self, reader, writer):
        owns = False
        try:
            line = await asyncio.wait_for(reader.readuntil(b'\n'), 3)
            auth = json.loads(line)
            supplied = auth.get('token') if isinstance(auth, dict) else None
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), self.key.encode()):
                return
            if self.owner is not None or self.closed:
                return
            self.owner = writer
            owns = True
            self.queue.reset()
            writer.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            writer.write(b'{Authenticated}')
            await writer.drain()
            frames = Frames()
            while not self.closed:
                # Idle clients still probe every second. Bound a partial frame's lifetime.
                data = await asyncio.wait_for(reader.read(256), 0.15 if frames.pending else 3)
                if not data:
                    break
                for frame in frames.feed(data):
                    self.queue.put(frame, time.monotonic())
        except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            if owns and self.owner is writer:
                self.owner = None
                self.queue.reset()
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def pump(self):
        try:
            while not self.closed:
                now = time.monotonic()
                if now >= self.uart_free:
                    packet = self.queue.pop(now)
                    if packet:
                        # The serial adapter must have a bounded write timeout.
                        written = self.port.write(packet)
                        if written != len(packet):
                            raise OSError('Incomplete serial write')
                        self.uart_free = now + len(packet) * 10 / 9600 + 0.002
                for reply in self.serial_frames.feed(self.port.read(512)):
                    owner = self.owner
                    if owner:
                        if owner.transport.get_write_buffer_size() > 4096:
                            owner.close()
                            self.owner = None
                            self.queue.reset()
                        else:
                            owner.write(reply)
                await asyncio.sleep(0.005)
        finally:
            self.closed = True
            if self.owner:
                self.owner.close()
            # Best-effort final Stop; Uno's per-command timer also expires.
            try:
                self.port.write(STOP)
            except OSError:
                pass


def pair_key(path):
    path = Path(path).expanduser()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(secrets.token_hex(8).upper() + '\n')
    key = path.read_text().strip()
    if not re.fullmatch(r'[A-Z0-9]{16,64}', key):
        raise ValueError('Pairing file must contain 16–64 uppercase letters/digits')
    return key


async def run(args):
    import serial
    with serial.Serial(args.device, 9600, timeout=0, write_timeout=0.05) as port:
        await check_uno(port)
        if args.check:
            return
        key = pair_key(args.key_file)
        relay = Relay(port, key)
        server = await asyncio.start_server(relay.client, args.listen, args.port, limit=256)
        print(f'Manual USB relay listening on {args.listen}:{args.port}. Pairing code: {key}', flush=True)
        print('Backend navigation remains logging-only. No movement until the phone explicitly enables and holds a direction.', flush=True)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: setattr(relay, 'closed', True))
        async with server:
            await relay.pump()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--key-file', default='~/.config/godseye/rover-pairing-key')
    parser.add_argument('--check', action='store_true', help='Only verify a non-motion Uno reply, then exit')
    try:
        asyncio.run(run(parser.parse_args()))
    except (RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
