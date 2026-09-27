#!/usr/bin/env python3
"""SIMULATED rover: rehearse the dashboard against the REAL backend with no phone or car.

One command starts the real prototype backend (the same objects tools.run_rover_backend
--prototype builds, plus an oracle detector) on a loopback port with a temporary database,
a temporary pairing key and the operator estimates 0.2413 m x 0.127 m, then plays three
roles over the backend's real sockets:

* the iPhone sensor stream (/phone): ~30 Hz poses and ~10 Hz v1 frame bundles whose RGB,
  depth and confidence are ray-cast from the simulated pose against a synthetic corridor
  (floor, 2.2 m wide walls, a 0.7 m box, a doorway into a room with a chair-like block);
* the iPhone setup link (/device): status, peers, capture/rover/control actions and acks;
* the iPhone autonomous relay (/rover): ESP-like 20 Hz status with fresh permits, the
  Stop/Arm acknowledgement barrier, session/sequence/permit checks and motor leases.

Motor packets drive a differential-drive pose with a SYNTHETIC PWM -> speed mapping (see
SPEED_* below). Nothing here touches Bluetooth, serial, cameras or paid APIs, and every
identifier and status string says SIMULATED. Default ports are 8775 (backend) and 5175
(dashboard); 8765 and 5173 are refused so the sim can never be mistaken for a live demo.
"""
import argparse
import asyncio
import io
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image
from websockets.asyncio.client import connect

from tools.fake_phone import frame_bundle, look_pose

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'SIMULATED'
RESERVED_PORTS = {8765, 5173}  # the live demo's backend and dashboard

# Synthetic scene in ARKit world meters: +Y up, the rover starts at the origin facing +Z,
# +X is to its left. Boxes are (x0, x1, y0, y1, z0, z1, (r, g, b)).
# The prototype inflates obstacles by ~0.43 m (footprint bound + 0.15 m margin), so the box
# leaves a ~1.5 m gap and the doorway is 1.4 m wide: narrower passages are correctly
# refused by the planner and would make every rehearsal stall.
FLOOR_Y = -.22  # phone lens 22 cm above the floor
PITCH_RAD = .3  # phone tilted 0.3 rad below the horizon
WALL_H = 1.0
WALL_RGB, BOX_RGB, CHAIR_RGB, DOOR_RGB = (200, 190, 170), (200, 60, 50), (50, 90, 200), (120, 80, 50)
SCENE = (
    (1.1, 1.2, FLOOR_Y, FLOOR_Y + WALL_H, -1.2, 6.0, WALL_RGB),     # corridor left wall (+X)
    (-1.2, -1.1, FLOOR_Y, FLOOR_Y + WALL_H, -1.2, 6.0, WALL_RGB),   # corridor right wall
    (-1.2, 1.2, FLOOR_Y, FLOOR_Y + WALL_H, -1.3, -1.2, WALL_RGB),   # back wall
    (.7, 1.2, FLOOR_Y, FLOOR_Y + WALL_H, 6.0, 6.1, DOOR_RGB),       # front wall, left of 1.4 m doorway
    (-1.2, -.7, FLOOR_Y, FLOOR_Y + WALL_H, 6.0, 6.1, DOOR_RGB),     # front wall, right of doorway
    (.4, 1.1, FLOOR_Y, FLOOR_Y + .7, 2.2, 2.9, BOX_RGB),            # 0.7 m box against the left wall
    (1.2, 2.3, FLOOR_Y, FLOOR_Y + WALL_H, 6.0, 6.1, WALL_RGB),      # room front wall pieces
    (-2.3, -1.2, FLOOR_Y, FLOOR_Y + WALL_H, 6.0, 6.1, WALL_RGB),
    (2.2, 2.3, FLOOR_Y, FLOOR_Y + WALL_H, 6.1, 9.5, WALL_RGB),      # room side walls
    (-2.3, -2.2, FLOOR_Y, FLOOR_Y + WALL_H, 6.1, 9.5, WALL_RGB),
    (-2.3, 2.3, FLOOR_Y, FLOOR_Y + WALL_H, 9.5, 9.6, WALL_RGB),     # room far wall
    (1.0, 1.45, FLOOR_Y, FLOOR_Y + .45, 7.4, 7.85, CHAIR_RGB),      # chair seat
    (1.0, 1.45, FLOOR_Y + .45, FLOOR_Y + .9, 7.8, 7.85, CHAIR_RGB), # chair back
)
MAX_DEPTH_M = 5.
FX, FY, CX, CY = 720., 720., 480., 360.  # tools.fake_phone.frame_bundle intrinsics at 960x720

# Synthetic motor model. NOT a measurement of any real rover.
SPEED_AT_60_MPS, SPEED_AT_180_MPS = .10, .35  # straight speed, linear in PWM between 60 and 180
PIVOT_RPS_AT_60 = .6  # pivot yaw rate at PWM 60, proportional to PWM
TRACK_M = .127  # wheel track used for arcs (operator-estimated width)
MAX_LEASE_S = 1.  # firmware brakes 1 s after the last fresh command even if lease_ms is longer
PERMIT_TTL_S = .5
BODY_RADIUS_M = .12  # collision disc around the simulated chassis centre


def speed(pwm):
    if pwm <= 0:
        return 0.
    if pwm < 60:
        return SPEED_AT_60_MPS * pwm / 60
    return SPEED_AT_60_MPS + (SPEED_AT_180_MPS - SPEED_AT_60_MPS) * (min(pwm, 180) - 60) / 120


def motor_twist(direction, power, inner_power=None):
    """(v_mps, yaw_rate_rps) for one ELEGOO-style direction code. + yaw turns left."""
    if direction == 3:
        return speed(power), 0.
    if direction == 4:
        return -speed(power), 0.
    if direction in (1, 2):
        rate = min(1.5, PIVOT_RPS_AT_60 * power / 60)
        return 0., rate if direction == 1 else -rate
    if direction in (5, 6):
        outer = speed(power)
        inner = speed(inner_power) if inner_power is not None else outer / 2  # legacy half power
        rate = min(1.5, (outer - inner) / TRACK_M)
        return (outer + inner) / 2, rate if direction == 5 else -rate
    return 0., 0.


def blocked(x, z):
    for x0, x1, y0, y1, z0, z1, _ in SCENE:
        if y1 - FLOOR_Y < .03:
            continue
        dx = max(x0 - x, 0., x - x1)
        dz = max(z0 - z, 0., z - z1)
        if dx * dx + dz * dz < BODY_RADIUS_M ** 2:
            return True
    return False


class Rover:
    """Simulated chassis pose and one motor slot with a lease."""

    def __init__(self):
        self.x, self.z, self.yaw = 0., 0., 0.
        self.motor = None  # (direction, power, inner_power)
        self.expires = 0.
        self.collisions = 0
        self.odometer = 0.

    def drive(self, direction, power, inner_power, lease_ms):
        if direction == 0 or power == 0:
            self.halt()
            return
        self.motor = (direction, power, inner_power)
        self.expires = time.monotonic() + min(lease_ms / 1000, MAX_LEASE_S)

    def halt(self):
        self.motor = None

    def step(self, dt):
        if self.motor is None:
            return
        if time.monotonic() > self.expires:
            self.motor = None  # lease expired: brake
            return
        v, w = motor_twist(*self.motor)
        yaw = self.yaw + w * dt
        x = self.x + v * math.sin(yaw) * dt
        z = self.z + v * math.cos(yaw) * dt
        self.yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        if v and blocked(x, z):
            self.collisions += 1
            if self.collisions % 25 == 1:
                print(f'[{LABEL}] COLLISION: chassis touching scene geometry at x={x:.2f} z={z:.2f}', flush=True)
            return
        self.odometer += abs(v) * dt
        self.x, self.z = x, z


def render(transform):
    """(jpeg, depth, confidence) ray-cast from a camera-to-world transform."""
    cols, rows = np.meshgrid(np.arange(256), np.arange(192))
    u, v = (cols + .5) * 960 / 256, (rows + .5) * 720 / 192
    rays = np.stack([(u - CX) / FX, -(v - CY) / FY, -np.ones_like(u)], axis=-1).reshape(-1, 3)
    m = np.array(transform, dtype=np.float64).reshape(4, 4).T
    direction, origin = rays @ m[:3, :3].T, m[:3, 3]
    best = np.full(len(rays), np.inf)
    colour = np.zeros((len(rays), 3))
    with np.errstate(divide='ignore', invalid='ignore'):
        floor = (FLOOR_Y - origin[1]) / direction[:, 1]
        hit = floor > 0
        best = np.where(hit, floor, best)
        px = origin[0] + direction[:, 0] * floor
        pz = origin[2] + direction[:, 2] * floor
        checker = (np.floor(px / .5) + np.floor(pz / .5)) % 2
        shade = np.where(checker[:, None] > 0, [[150, 150, 150]], [[110, 115, 120]])
        colour = np.where(hit[:, None], shade, colour)
        for x0, x1, y0, y1, z0, z1, rgb in SCENE:
            lo, hi = np.array([x0, y0, z0]), np.array([x1, y1, z1])
            t1, t2 = (lo - origin) / direction, (hi - origin) / direction
            near = np.minimum(t1, t2).max(axis=1)
            far = np.maximum(t1, t2).min(axis=1)
            box_hit = (far >= near) & (near > 0) & (near < best)
            best = np.where(box_hit, near, best)
            colour = np.where(box_hit[:, None], [rgb], colour)
    fade = np.clip(1.1 - np.where(np.isfinite(best), best, MAX_DEPTH_M) / 12, .5, 1.)
    image = Image.fromarray((colour * fade[:, None]).reshape(192, 256, 3).astype(np.uint8)).resize((960, 720))
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=70)
    valid = np.isfinite(best) & (best <= MAX_DEPTH_M)
    depth = np.where(valid, best, 0.).astype('<f4')
    confidence = np.where(valid, 2, 0).astype('u1')
    return out.getvalue(), depth.tobytes(), confidence.tobytes()


class Phone:
    """The iPhone app's three links, all synthetic."""

    def __init__(self, base, key, rover, auto_control):
        self.base, self.key, self.rover = base, key, rover
        self.session = str(uuid.uuid4())
        self.capture_running = True
        self.rover_connected = self.rover_verified = True
        self.control_enabled = False
        self.auto_control = auto_control
        self.relay_task = None
        self.relay_state = 'not connected'
        self.armed_session = None
        self.last_seq = 0
        self.permits = {}  # permit -> issued monotonic time
        self.status_seq = 0
        self.frames = 0

    @property
    def headers(self):
        return {'Authorization': 'Bearer ' + self.key}

    def ws(self, path):
        return self.base.replace('http', 'ws', 1) + path

    # ------------------------------------------------------------------ sensors
    async def sensors(self):
        while True:
            if not self.capture_running:
                await asyncio.sleep(.2)
                continue
            try:
                await self._stream()
            except Exception as error:  # reconnect like the app does
                print(f'[{LABEL} phone] /phone link dropped: {error!r}; reconnecting', flush=True)
                await asyncio.sleep(1.)

    async def _stream(self):
        async with connect(self.ws('/phone'), max_size=None) as ws:
            await ws.send(json.dumps(dict(version=1, type='hello', device='SIMULATED-iphone',
                                          session_id=self.session, map_epoch=1,
                                          supports_scene_depth=True, supports_mesh=False)))
            loop = asyncio.get_running_loop()
            start = loop.time()
            tick = 0
            frame_id = 0
            while self.capture_running:
                await asyncio.sleep(max(0., start + tick / 30 - loop.time()))
                capture = loop.time() - start
                rover = self.rover
                frame_id += 1
                message = look_pose(frame_id, round(capture, 6), time.time_ns() // 1_000_000, self.session,
                                    position=(rover.x, 0., rover.z), yaw=rover.yaw, pitch=PITCH_RAD)
                if tick % 3 == 0:
                    sensors = await asyncio.to_thread(render, message['transform'])
                    message['t_wall_ms'] = time.time_ns() // 1_000_000
                    await ws.send(json.dumps(message))
                    await ws.send(frame_bundle(message, sensors))
                    self.frames += 1
                else:
                    await ws.send(json.dumps(message))
                tick = max(tick + 1, math.floor((loop.time() - start) * 30) + 1)

    # ------------------------------------------------------------------ /device
    def device_status(self):
        self.status_seq += 1
        return dict(version=1, type='status', seq=self.status_seq,
                    capture_running=self.capture_running,
                    capture_status=f'{LABEL} capture: ray-cast corridor' if self.capture_running else f'{LABEL} capture stopped',
                    tracking='normal', network=f'{LABEL} loopback',
                    rover_connected=self.rover_connected, rover_verified=self.rover_verified,
                    rover_status=f'{LABEL} rover (no hardware): {self.relay_state}',
                    control_enabled=self.control_enabled,
                    control_status=f'{LABEL} laptop control ' + ('enabled' if self.control_enabled else 'off'),
                    peers=[dict(id='SIM-ROVER-0001', name='SIMULATED rover (not real)')])

    async def device(self):
        while True:
            try:
                async with connect(self.ws('/device'), additional_headers=self.headers, max_size=None) as ws:
                    print(f'[{LABEL} phone] /device setup link connected', flush=True)

                    async def status():
                        while True:
                            await ws.send(json.dumps(self.device_status()))
                            await asyncio.sleep(.25)

                    sender = asyncio.create_task(status())
                    try:
                        async for raw in ws:
                            message = json.loads(raw)
                            if message.get('type') == 'action':
                                ok, text = self.act(message)
                                await ws.send(json.dumps(dict(version=1, type='ack', id=message['id'],
                                                              ok=ok, message=text)))
                                await ws.send(json.dumps(self.device_status()))
                    finally:
                        sender.cancel()
            except Exception as error:
                print(f'[{LABEL} phone] /device link dropped: {error!r}; reconnecting', flush=True)
                await asyncio.sleep(1.)

    def act(self, message):
        action = message['action']
        print(f'[{LABEL} phone] device action: {action}', flush=True)
        if action == 'capture_start':
            self.capture_running = True
        elif action == 'capture_stop':
            self.capture_running = False
            self.set_control(False)
        elif action == 'rover_scan':
            pass
        elif action == 'rover_select':
            if message.get('peer_id') != 'SIM-ROVER-0001':
                return False, 'Unknown simulated peer'
            self.rover_connected = self.rover_verified = True
        elif action == 'rover_disconnect':
            self.rover_connected = self.rover_verified = False
            self.set_control(False)
        elif action == 'control_enable':
            if not (self.capture_running and self.rover_verified):
                return False, 'Start capture and connect the simulated rover first'
            self.set_control(True)
        elif action == 'control_disable':
            self.set_control(False)
        elif action == 'stop':
            self.rover.halt()
            self.armed_session = None
        return True, f'{LABEL}: {action} done'

    def set_control(self, enabled):
        self.control_enabled = enabled
        if enabled and (self.relay_task is None or self.relay_task.done()):
            self.relay_task = asyncio.create_task(self.relay())
        elif not enabled and self.relay_task is not None:
            self.relay_task.cancel()
            self.relay_task = None
            self.rover.halt()
            self.armed_session = None

    # ------------------------------------------------------------------ /rover
    async def relay(self):
        while self.control_enabled:
            try:
                await self._relay()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.relay_state = f'relay dropped ({type(error).__name__})'
                print(f'[{LABEL} relay] /rover link dropped: {error!r}; retrying', flush=True)
            self.rover.halt()
            self.armed_session = None
            await asyncio.sleep(1.)

    def fresh(self, permit):
        issued = self.permits.get(permit)
        return issued is not None and time.monotonic() - issued <= PERMIT_TTL_S

    async def _relay(self):
        async with connect(self.ws('/rover'), additional_headers=self.headers, max_size=None) as ws:
            self.relay_state = 'relay connected'
            print(f'[{LABEL} relay] /rover autonomous relay connected', flush=True)
            seq = 0

            async def status():
                nonlocal seq
                while True:
                    seq += 1
                    permit = secrets.token_hex(8).upper()
                    now = time.monotonic()
                    self.permits = {p: t for p, t in self.permits.items() if now - t <= PERMIT_TTL_S}
                    self.permits[permit] = now
                    await ws.send(json.dumps(dict(version=1, type='status', seq=seq, session_id=self.session,
                                                  map_epoch=1, permit=permit, uno_age_ms=40., enabled=True)))
                    await asyncio.sleep(.05)

            sender = asyncio.create_task(status())
            try:
                async for raw in ws:
                    reply = self.relay_message(json.loads(raw))
                    if reply is not None:
                        await ws.send(json.dumps(reply))
            finally:
                sender.cancel()

    def relay_message(self, message):
        kind = message.get('type')
        if kind == 'heartbeat':
            return None
        if kind == 'stop':
            self.rover.halt()
            self.armed_session = None
            self.last_seq = 0
            return dict(version=1, type='ack', id='Z' + message['id'])
        if kind == 'arm':
            if not self.fresh(message.get('permit')) or message.get('session') == self.armed_session:
                print(f'[{LABEL} relay] arm refused (stale permit or reused session)', flush=True)
                return dict(version=1, type='retired')
            self.rover.halt()
            self.armed_session, self.last_seq = message['session'], 0
            self.relay_state = 'armed'
            print(f'[{LABEL} relay] ARMED session {self.armed_session[:8]}...', flush=True)
            return dict(version=1, type='ack', id='A' + message['session'])
        if kind == 'command':
            if (message.get('session') != self.armed_session or not self.fresh(message.get('permit'))
                    or message.get('seq', 0) <= self.last_seq):
                print(f'[{LABEL} relay] command rejected: session/permit/seq invalid; retiring', flush=True)
                self.rover.halt()
                self.armed_session = None
                self.relay_state = 'session retired'
                return dict(version=1, type='retired')
            self.last_seq = message['seq']
            self.rover.drive(message['direction'], message['power'], message.get('inner_power'),
                             message.get('lease_ms', 1500))
            return None
        print(f'[{LABEL} relay] unknown message {message!r}', flush=True)
        return None

    # ------------------------------------------------------------------ physics
    async def physics(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(.02)
            now = time.monotonic()
            self.rover.step(now - last)
            last = now

    async def report(self):
        while True:
            await asyncio.sleep(2.)
            r = self.rover
            motor = 'idle' if r.motor is None else f'dir={r.motor[0]} pwm={r.motor[1]}' + (
                f' inner={r.motor[2]}' if r.motor[2] is not None else '')
            print(f'[{LABEL}] pose x={r.x:+.2f} z={r.z:+.2f} yaw={math.degrees(r.yaw):+.0f}deg '
                  f'odo={r.odometer:.2f}m motor={motor} armed={bool(self.armed_session)} '
                  f'control={self.control_enabled} frames={self.frames} collisions={r.collisions}', flush=True)

    async def run(self):
        tasks = [self.sensors(), self.device(), self.physics(), self.report()]
        if self.auto_control:
            async def enable_later():
                await asyncio.sleep(1.)
                self.set_control(True)
            tasks.append(enable_later())
        await asyncio.gather(*tasks)


def port_free(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore TIME_WAIT from a restart
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


# Scene objects the SIMULATED detector "sees" (class, SCENE index). Oracle boxes, not YOLO.
DETECTABLE = (('suitcase', 5), ('chair', 11))


class SimDetector:
    """Oracle stand-in for MPSDetector: projects visible scene objects into the frame.

    Boxes come from known geometry, so no weights, GPU or network are needed. Positions
    are still localized by the backend's own depth sampling (backend.localization).
    """
    confidence = .5
    class_names = tuple(name for name, _ in DETECTABLE)

    def detect(self, frame):
        from backend.localization import Detection
        world_to_camera = np.linalg.inv(frame.transform)
        fx, fy = frame.intrinsics[0, 0], frame.intrinsics[1, 1]
        cx, cy = frame.intrinsics[0, 2], frame.intrinsics[1, 2]
        width, height = frame.image.size
        found = []
        for name, index in DETECTABLE:
            x0, x1, y0, y1, z0, z1, _ = SCENE[index]
            corners = np.array([[x, y, z, 1.] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)])
            camera = corners @ world_to_camera.T
            optical = -camera[:, 2]
            if np.any(optical < .1) or optical.min() > MAX_DEPTH_M:
                continue
            u = fx * camera[:, 0] / optical + cx
            v = -fy * camera[:, 1] / optical + cy
            box = (max(0., u.min()), max(0., v.min()), min(width, u.max()), min(height, v.max()))
            if box[2] - box[0] < 12 or box[3] - box[1] < 12:
                continue
            # Visible only if the depth at the box centre lands on this object, not a nearer one.
            row = int((box[1] + box[3]) / 2 * frame.depth.shape[0] / height)
            col = int((box[0] + box[2]) / 2 * frame.depth.shape[1] / width)
            sample = frame.depth[min(row, frame.depth.shape[0] - 1), min(col, frame.depth.shape[1] - 1)]
            if not (optical.min() - .1 <= sample <= optical.max() + .1):
                continue
            found.append(Detection(tuple(float(c) for c in box), name, .9))
        return found


def serve_backend(args):
    """In-process equivalent of `tools.run_rover_backend --prototype` plus SimDetector."""
    from backend.app import create_app
    from backend.prototype import PrototypeActuation, prototype_geometry
    from backend.rover_relay import RelayCar
    import uvicorn
    print(f'{LABEL} BACKEND: UNCALIBRATED PROTOTYPE profile, oracle detector, fake world.', flush=True)
    actuation = PrototypeActuation(max_pwm=180, cruise_pwm=args.cruise_pwm)
    geometry = prototype_geometry(.2413, .127)
    car = RelayCar((args.workdir / 'pairing-key').read_text().strip(), actuation)
    app = create_app(car=car, calibration=geometry, detector=SimDetector(), voice_providers=None)
    uvicorn.run(app, host='127.0.0.1', port=args.port, ws_max_size=8388608)


def start_backend(workdir, port, cruise_pwm, log_path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('GODSEYE_', 'ELEVENLABS', 'GEMINI', 'GOOGLE', 'OPENAI', 'ANTHROPIC'))
           and 'API_KEY' not in k}
    env.update(GODSEYE_DB=str(workdir / 'sim.db'), GODSEYE_CAPTURE_DIR='', PYTHONPATH=str(ROOT))
    command = [sys.executable, '-m', 'tools.sim_rover', '--serve-backend', '--workdir', str(workdir),
               '--port', str(port)]
    if cruise_pwm:
        command += ['--cruise-pwm', str(cruise_pwm)]
    log = open(log_path, 'w')
    return subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_healthy(base, process, log_path):
    import urllib.request
    for _ in range(150):
        if process.poll() is not None:
            sys.exit(f'[{LABEL}] backend exited:\n' + Path(log_path).read_text()[-2000:])
        try:
            with urllib.request.urlopen(base + '/health', timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(.1)
    sys.exit(f'[{LABEL}] backend did not become healthy; see {log_path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8775, help='simulated backend port (default 8775)')
    parser.add_argument('--dashboard-port', type=int, default=5175)
    parser.add_argument('--dashboard', action='store_true', help='also start Vite for the dashboard')
    parser.add_argument('--auto-control', action='store_true',
                        help='enable laptop control at start instead of waiting for control_enable')
    parser.add_argument('--cruise-pwm', type=int, help='pass --prototype-cruise-pwm to the backend')
    parser.add_argument('--workdir', type=Path, help='keep DB/key/logs here (default: new temp dir)')
    parser.add_argument('--serve-backend', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_backend:
        serve_backend(args)
        return
    for port in (args.port, args.dashboard_port):
        if port in RESERVED_PORTS:
            parser.error(f'port {port} belongs to the live demo; the simulator refuses it')
    if not port_free(args.port):
        parser.error(f'port {args.port} is busy')
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix='godseye-sim-'))).resolve()
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file = workdir / 'pairing-key'
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(32) + '\n')
        key_file.chmod(0o600)
    key = key_file.read_text().strip()
    base = f'http://127.0.0.1:{args.port}'
    backend_log = workdir / 'backend.log'
    print('=' * 78)
    print(f'  {LABEL} ROVER - no phone, no car, no Bluetooth. Real backend code, fake world.')
    print('=' * 78, flush=True)
    backend = start_backend(workdir, args.port, args.cruise_pwm, backend_log)
    dashboard = None
    try:
        wait_healthy(base, backend, backend_log)
        dashboard_cmd = (f'cd {ROOT / "dashboard"} && GODSEYE_BACKEND_URL={base} '
                         f'GODSEYE_ROVER_KEY_FILE={key_file} VITE_LIVE_URL=/live '
                         f'npx vite --host 127.0.0.1 --port {args.dashboard_port} --strictPort')
        if args.dashboard:
            if not port_free(args.dashboard_port):
                parser.error(f'dashboard port {args.dashboard_port} is busy')
            env = dict(os.environ, GODSEYE_BACKEND_URL=base, GODSEYE_ROVER_KEY_FILE=str(key_file),
                       VITE_LIVE_URL='/live')
            dashboard = subprocess.Popen(
                [shutil.which('npx') or 'npx', 'vite', '--host', '127.0.0.1', '--port', str(args.dashboard_port),
                 '--strictPort'], cwd=ROOT / 'dashboard', env=env,
                stdout=open(workdir / 'dashboard.log', 'w'), stderr=subprocess.STDOUT)
        print(f'[{LABEL}] backend      {base}  (log {backend_log})')
        print(f'[{LABEL}] workdir      {workdir}  (temp DB, pairing key, logs)')
        print(f'[{LABEL}] dashboard    http://127.0.0.1:{args.dashboard_port}/'
              + ('  (started, log dashboard.log)' if dashboard else ''))
        if not dashboard:
            print(f'[{LABEL}] start it with:\n  {dashboard_cmd}')
        print(f'[{LABEL}] motor model  PWM60={SPEED_AT_60_MPS} m/s, PWM180={SPEED_AT_180_MPS} m/s, '
              f'pivot {PIVOT_RPS_AT_60} rad/s at PWM60 (synthetic, not measured)', flush=True)
        asyncio.run(Phone(base, key, Rover(), args.auto_control).run())
    except KeyboardInterrupt:
        pass
    finally:
        for process in (dashboard, backend):
            if process is not None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
        print(f'[{LABEL}] stopped; files kept in {workdir}', flush=True)


if __name__ == '__main__':
    main()
