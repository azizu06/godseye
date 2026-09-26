"""God's Eye v1 transport skeleton; all motion is logging-only."""
import asyncio
from collections import Counter, deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.changes import ChangeTracker
from backend.drive import drive
from backend.capture import CaptureBuffer
from backend.capture_routes import register_capture_routes
from backend.rich_capture import RichCapture
from backend.frame_bundle import FrameValidationError, validate_rigid_transform
from backend.mapping import MappingError, PointChunk, build_point_chunk, points_message
from backend.objects import ObjectMemory, detect_objects
from backend.occupancy import PUBLISH_INTERVAL_S as OCCUPANCY_INTERVAL_S, Evidence, OccupancyGrid, frame_evidence
from backend.navigation import path_message
from backend.navigator import PLAN_STOP_REASONS, Navigator, NavSettings, RoverPose, pose_from_transform
from backend.point_dedupe import NoNewPoints, PointSettings, VoxelMemory

logger = logging.getLogger(__name__)
MAP_INTERVAL_S = .25  # at most 4 Hz of point chunks
MAP_MAX_AGE_S = 1.  # discard chunks computed from frames older than this
POSE_HISTORY = 64  # recent captures kept to check a delayed bundle against its own pose
MAP_PENDING_POINTS = 2  # unsent point chunks kept per slow viewer
DETECT_INTERVAL_S = .5  # at most 2 Hz of object inference
DETECT_MAX_AGE_S = 2.  # discard detections finished this long after their frame arrived
DETECTOR_OK_S = 2.  # health reports the detector ok this long after a used result


class Input(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)


class Mode(Input):
    mode: Literal['manual', 'navigate', 'explore']


class Manual(Input):
    v_mps: float = Field(ge=-.2, le=.2)
    yaw_rate_rps: float = Field(ge=-.5, le=.5)


class Goal(Input):
    x: float
    z: float


class Ask(Input):
    question: str = Field(min_length=1, max_length=2000)


class Hello(Input):
    version: Literal[1]
    type: Literal['hello']
    device: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    map_epoch: int = Field(ge=0)
    supports_scene_depth: bool
    supports_mesh: bool


class Pose(Input):
    version: Literal[1]
    type: Literal['pose', 'frame']
    session_id: str = Field(min_length=1)
    map_epoch: int = Field(ge=0)
    frame_id: int = Field(ge=0)
    t_capture: float = Field(ge=0)
    t_wall_ms: int = Field(ge=0)
    transform: list[float] = Field(min_length=16, max_length=16)
    tracking: Literal['normal', 'limited', 'not_available']

    @field_validator('transform')
    @classmethod
    def rigid(cls, value):
        validate_rigid_transform(value)  # same gate as mapping; zero/scaled/reflected never count as a pose
        return value


class Image(Input):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    jpeg_len: int = Field(gt=0)
    intrinsics: list[float] = Field(min_length=9, max_length=9)
    orientation: Literal['landscape_right']


class Depth(Input):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    format: Literal['float32_m']
    len: int = Field(gt=0)


class Confidence(Input):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    format: Literal['uint8_0_2']
    len: int = Field(gt=0)


class Frame(Pose):
    type: Literal['frame']
    image: Image
    depth: Depth
    confidence: Confidence


def decode_frame(data: bytes) -> Frame:
    if len(data) < 4 or len(data) > 8 * 1024 * 1024:
        raise ValueError('invalid bundle size')
    header_len = int.from_bytes(data[:4], 'little')
    if not 0 < header_len <= 65536 or 4 + header_len > len(data):
        raise ValueError('invalid header length')
    frame = Frame.model_validate(json.loads(data[4:4 + header_len]))
    if frame.depth.len != frame.depth.width * frame.depth.height * 4:
        raise ValueError('depth length mismatch')
    if frame.confidence.len != frame.confidence.width * frame.confidence.height:
        raise ValueError('confidence length mismatch')
    if (frame.depth.width, frame.depth.height) != (frame.confidence.width, frame.confidence.height):
        raise ValueError('confidence dimensions mismatch')
    if len(data) != 4 + header_len + frame.image.jpeg_len + frame.depth.len + frame.confidence.len:
        raise ValueError('payload length mismatch')
    return frame


class Listener:
    """One /live viewer: control messages plus a small, separate points backlog."""

    def __init__(self):
        self.queue = asyncio.Queue(maxsize=32)
        self.points = deque(maxlen=MAP_PENDING_POINTS)
        self.occupancy = None  # full-grid snapshots: only the newest is worth sending
        self.wake = asyncio.Event()


@dataclass(frozen=True)
class MapUpdate:
    """One frame's mapping, computed off the event loop and applied only once accepted."""
    t_capture: float
    evidence: Evidence | None  # None when the occupancy computation failed
    chunk: PointChunk | None  # None when every point was sent recently


class LatestFrame:
    """Single-slot mailbox: a newer bundle replaces one still waiting."""

    def __init__(self):
        self.item = None
        self.ready = asyncio.Event()

    def put(self, payload):
        replaced = self.item is not None
        self.item = (payload, time.monotonic())
        self.ready.set()
        return replaced

    def take(self):
        item, self.item = self.item, None
        self.ready.clear()
        return item


def create_app(db_path: str | None = None, build_points=None,
               detector=None, weights: str | None = None, capture_directory: str | None = None,
               point_settings: PointSettings | None = None, nav_settings: NavSettings | None = None) -> FastAPI:
    """`build_points(payload, session_id, map_epoch)` runs in a worker thread.

    Its candidate points pass through a per-map voxel memory (`point_settings`,
    default from GODSEYE_POINT* variables) so chunks carry mostly new surface.

    `detector.localize(frame)` also runs in a worker thread, one call at a time.
    Without a detector, `weights` names existing local YOLO weights to load at
    startup; with neither, object detection is off and health reports it down.
    """
    db_path = db_path or os.environ.get('GODSEYE_DB', 'backend/godseye.db')
    point_settings = point_settings or PointSettings.from_env()
    build_points = build_points or partial(build_point_chunk, max_points=point_settings.samples)
    detect_lock = threading.Lock()  # one inference at a time, even across phone reconnects

    @asynccontextmanager
    async def lifespan(app):
        db = sqlite3.connect(db_path)
        db.executescript(Path(__file__).with_name('schema.sql').read_text())
        app.state.db = db
        app.state.objects = ObjectMemory(db)
        app.state.changes = ChangeTracker(db, app.state.objects)
        if detector is None and weights:
            from backend.detector import MPSDetector
            app.state.detector = await asyncio.to_thread(MPSDetector, weights)
        else:
            app.state.detector = detector
        app.state.detected_at = None
        app.state.detect_stats = Counter()
        app.state.session = None
        app.state.phone = None
        app.state.pose = None
        app.state.pose_at = None
        app.state.tracking_lost_capture = -1.0  # frames captured at or before this are untrusted
        app.state.armed = False
        app.state.mode = 'manual'
        app.state.stop_reason = 'startup_disarmed'
        app.state.listeners = set()
        app.state.capture = CaptureBuffer()
        app.state.rich_capture = RichCapture(capture_directory if capture_directory is not None else
            (None if db_path == ':memory:' else os.environ.get('GODSEYE_CAPTURE_DIR', 'backend/captures')))
        app.state.capture_ingest_lock = asyncio.Lock()
        app.state.chunk_id = 0
        app.state.point_memory = VoxelMemory(point_settings)
        app.state.map_stats = Counter()
        app.state.occupancy = None  # OccupancyGrid of the active session/epoch
        app.state.occupancy_stats = Counter()
        app.state.nav = Navigator(nav_settings or NavSettings(), pose=rover_pose,
                                  occupancy=lambda: app.state.occupancy, drive=lambda v, w: drive(v, w),
                                  stop=nav_stop, publish=publish,
                                  armed_mode=lambda: app.state.mode if app.state.armed else None)
        task = asyncio.create_task(watchdog())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await app.state.nav.aclose()
            stop('shutdown')
            db.close()

    app = FastAPI(title="God's Eye backend skeleton", version='1', lifespan=lifespan)
    register_capture_routes(app)

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    def stop(reason):
        app.state.armed = False
        app.state.stop_reason = reason
        if getattr(app.state, 'nav', None) is not None:
            app.state.nav.halt()  # end any goal/explore run and clear its path first
        drive(0.0, 0.0)
        session = app.state.session or (None, None)
        app.state.db.execute(
            'INSERT INTO health_events(t_wall_ms,session_id,map_epoch,component,reason,mode,armed) VALUES(?,?,?,?,?,?,?)',
            (int(time.time()*1000), *session, 'backend', reason, app.state.mode, 0))
        app.state.db.commit()

    def nav_stop(reason):
        stop(reason)
        publish(health())

    def rover_pose():
        pose = app.state.pose
        if pose is None or app.state.pose_at is None:
            return None
        x, z, yaw = pose_from_transform(pose.transform)
        return RoverPose(x, z, yaw, time.monotonic() - app.state.pose_at, pose.tracking)

    def health():
        age = None if app.state.pose_at is None else (time.monotonic() - app.state.pose_at) * 1000
        phone = 'down' if app.state.phone is None else 'stale'
        if age is not None and age <= 250 and app.state.pose.tracking == 'normal':
            phone = 'ok'
        detector = 'down'
        if app.state.detector is not None:
            detector = 'stale'
            if app.state.detected_at is not None and time.monotonic() - app.state.detected_at <= DETECTOR_OK_S:
                detector = 'ok'
        return dict(version=1, type='health', phone=phone, car='down', detector=detector,
                    pose_age_ms=age, mode=app.state.mode, armed=app.state.armed,
                    stop_reason=app.state.stop_reason)

    def publish(message):
        for listener in app.state.listeners:
            if message['type'] == 'points':
                listener.points.append(message)  # oldest chunk drops when full
            elif message['type'] == 'occupancy':
                listener.occupancy = message
            else:
                if listener.queue.full():
                    listener.queue.get_nowait()
                listener.queue.put_nowait(message)
            listener.wake.set()

    def objects_message(session):
        session_id, map_epoch = session or (None, None)
        return dict(version=1, type='objects', session_id=session_id, map_epoch=map_epoch,
                    objects=app.state.objects.snapshot(session))

    def event_message(session, record):
        session_id, map_epoch = session
        return dict(version=1, type='event', session_id=session_id, map_epoch=map_epoch, **record)

    def shown_session():
        """The active map, else the newest stored one (restart-safe reads)."""
        return app.state.session or app.state.objects.latest_session()

    def object_locator(owner):
        def locate(payload, session_id, map_epoch):
            with detect_lock:
                # Work queued behind a slow inference is skipped once its phone is gone;
                # the worker then counts the None as discarded_reset.
                if app.state.phone is not owner:
                    return None
                # Depth-probe the active rescan's unseen remembered positions in the same frame.
                return detect_objects(app.state.detector, payload, session_id, map_epoch,
                                      app.state.changes.watching)
        return locate

    async def frame_worker(owner, session, mailbox, compute, accept, stats, interval, max_age):
        """Run `compute` on the newest bundle, one at a time, off the event loop.

        `compute` must not change map state: its thread keeps running after the worker
        is cancelled.
        `accept` runs on the event loop only for results that are still current, in the
        same step as the checks, so no reset or disconnect can come between them. It may
        return the stats key to count instead of 'published'.
        """
        last_start = -interval
        last_capture = -1.0
        while True:
            await mailbox.ready.wait()
            await asyncio.sleep(max(0., last_start + interval - time.monotonic()))
            payload, received = mailbox.take()  # newest wins; older ones were replaced
            last_start = time.monotonic()
            try:
                result = await asyncio.to_thread(compute, payload, *session)
            except (FrameValidationError, MappingError):
                stats['rejected'] += 1
                continue
            except Exception:  # a mapping or model bug must not end the phone session
                stats['failed'] += 1
                logger.exception('frame computation failed')
                continue
            # The map may have reset or the phone left while this was computing.
            if app.state.phone is not owner or app.state.session != session:
                stats['discarded_reset'] += 1
            elif result.t_capture <= app.state.tracking_lost_capture:
                stats['discarded_tracking'] += 1
            elif result.t_capture <= last_capture or time.monotonic() - received > max_age:
                stats['discarded_stale'] += 1
            else:
                last_capture = result.t_capture
                try:
                    outcome = accept(result) or 'published'
                except Exception:
                    stats['failed'] += 1
                    logger.exception('frame result could not be applied')
                    continue
                stats[outcome] += 1

    def accept_points(chunk):
        app.state.chunk_id += 1
        publish(points_message(chunk, app.state.chunk_id))
        app.state.point_memory.commit(chunk.voxel_keys, chunk.t_capture)

    def map_update(payload, session_id, map_epoch):
        """Worker-thread half of mapping: the frame's occupancy evidence and deduped chunk.

        The grid gets every candidate point; only the published chunk is deduped,
        so occupancy evidence keeps accumulating when nothing new is published.
        """
        candidates = build_points(payload, session_id, map_epoch)
        evidence = None
        try:
            evidence = frame_evidence(candidates.positions)
        except Exception:  # an occupancy bug must not cost the live points
            app.state.occupancy_stats['failed'] += 1
            logger.exception('occupancy evidence failed')
        try:
            chunk = app.state.point_memory.select(candidates)
        except NoNewPoints:
            chunk = None
        return MapUpdate(candidates.t_capture, evidence, chunk)

    def accept_map(grid):
        """Event-loop half: commit a current frame to its map's grid, then publish its points.

        A phone rejoining the same map resumes this grid, so evidence from a frame
        that did not pass the worker's checks must never reach it.
        """
        def accept(update):
            if update.evidence is not None:
                try:
                    grid.commit(update.evidence, time.monotonic())
                except Exception:  # an occupancy bug must not cost the live points
                    app.state.occupancy_stats['failed'] += 1
                    logger.exception('occupancy update failed')
            if update.chunk is None:
                return 'no_new_points'
            accept_points(update.chunk)
        return accept

    async def occupancy_worker(owner, session, grid):
        """Publish the grid at most once per interval, when it changed, off the event loop."""
        stats = app.state.occupancy_stats
        while True:
            await asyncio.sleep(OCCUPANCY_INTERVAL_S)
            try:
                message = await asyncio.to_thread(grid.message_if_due, time.monotonic())
            except Exception:
                stats['failed'] += 1
                logger.exception('occupancy snapshot failed')
                continue
            if message is None:
                continue
            # The map may have reset or the phone left while this was computing.
            if app.state.phone is not owner or app.state.session != session or app.state.occupancy is not grid:
                stats['discarded_reset'] += 1
                continue
            publish(message)
            stats['published'] += 1

    def occupancy_snapshot():
        grid = app.state.occupancy
        if grid is not None and grid.session == app.state.session:
            return grid.last_message
        return None

    def accept_objects(result):
        session = (result.session_id, result.map_epoch)
        seen_at = result.t_wall_ms / 1000
        sightings = app.state.objects.record(session, result.frame_id, seen_at, result.found)
        app.state.detected_at = time.monotonic()
        events, changed = app.state.changes.observe(result.t_capture, seen_at, sightings, result.views)
        if sightings or changed:
            publish(objects_message(session))
        for record in events:
            publish(event_message(session, record))

    async def watchdog():
        ticks = 0
        while True:
            await asyncio.sleep(.05)
            if app.state.pose_at is not None and time.monotonic() - app.state.pose_at > .25:
                if app.state.stop_reason != 'pose_stale':
                    stop('pose_stale')
            # Explore runs whenever the rover is armed in explore mode; arming is unchanged.
            if app.state.armed and app.state.mode == 'explore' and not app.state.nav.active:
                app.state.nav.start_explore()
            ticks += 1
            if ticks % 10 == 0:
                publish(health())

    def set_session(session):
        stop('session_reset')
        if app.state.session != session:
            app.state.chunk_id = 0
            app.state.occupancy = OccupancyGrid(session)
            app.state.point_memory.reset()
        app.state.session = session
        app.state.pose = app.state.pose_at = app.state.detected_at = None
        app.state.tracking_lost_capture = -1.0
        app.state.capture.clear()
        app.state.rich_capture.reset()
        for listener in app.state.listeners:
            listener.points.clear()
            listener.occupancy = None
            # Pose/path/health are unscoped v1 messages. Do not let a slow
            # viewer receive a previous map's queued state beside new points.
            while not listener.queue.empty():
                listener.queue.get_nowait()
        app.state.db.execute('INSERT OR IGNORE INTO sessions(session_id,map_epoch,created_at_ms) VALUES(?,?,?)',
                             (*session, int(time.time()*1000)))
        app.state.db.commit()
        app.state.changes.activate(session)
        publish(health())
        publish(objects_message(session))
        publish(dict(version=1, type='path', points=[]))
        if (grid_message := occupancy_snapshot()) is not None:  # a phone rejoining the same map keeps its grid
            publish(grid_message)

    @app.get('/health')
    async def get_health():
        return health()

    @app.get('/capture', response_class=HTMLResponse)
    async def capture_page():
        return HTMLResponse(Path(__file__).with_name('static').joinpath('capture.html').read_text(),
                            headers={'Cache-Control': 'no-store'})

    @app.get('/capture/status')
    async def capture_status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        pose = app.state.pose
        return dict(version=1, **app.state.capture.status(), health=health(),
                    rich=app.state.rich_capture.status(), mapping=dict(app.state.map_stats),
                    tracking=None if pose is None else pose.tracking,
                    position=None if pose is None else pose.transform[12:15])

    @app.get('/capture/frame.jpg')
    async def capture_jpeg(request: Request):
        frame = app.state.capture.latest
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if frame is None:
            return Response(status_code=204, headers=headers)
        headers.update({'ETag': f'"{frame.token}"', 'X-Frame-Id': str(frame.metadata['frame_id'])})
        if request.headers.get('if-none-match') == headers['ETag']:
            return Response(status_code=304, headers=headers)
        return Response(frame.jpeg, media_type='image/jpeg', headers=headers)

    @app.get('/capture/frame.bin')
    async def capture_bundle():
        frame = app.state.capture.latest
        if frame is None:
            raise HTTPException(404, 'No camera frame has arrived in this session')
        return Response(frame.payload, media_type='application/octet-stream', headers={
            'Cache-Control': 'no-store',
            'Content-Disposition': f'attachment; filename="godseye-frame-{frame.metadata["frame_id"]}.bin"'})

    @app.post('/session')
    async def new_session():
        # Revoke the old phone before changing map identity.
        app.state.phone = None
        set_session((str(uuid4()), 1))
        return dict(version=1, session_id=app.state.session[0], map_epoch=1)

    @app.post('/stop')
    async def operator_stop():
        stop('operator_stop')
        publish(health())
        return health()

    @app.post('/arm')
    async def arm():
        if any(health()[key] != 'ok' for key in ('phone', 'car', 'detector')):
            raise HTTPException(409, 'Health must all be ok; car and detector are unavailable in this skeleton')
        app.state.armed = True
        app.state.stop_reason = None
        return health()

    @app.post('/mode')
    async def mode(body: Mode):
        stop('mode_change')
        app.state.mode = body.mode
        return health()

    @app.post('/manual')
    async def manual(body: Manual):
        if not app.state.armed or app.state.mode != 'manual':
            raise HTTPException(409, 'Disarmed or not in manual mode')
        raise HTTPException(501, 'Motion is not implemented; drive adapter only logs')

    @app.post('/goal')
    async def goal(body: Goal):
        # No disarmed preview: a drawn path must mean the rover is about to follow it.
        if not app.state.armed or app.state.mode != 'navigate':
            raise HTTPException(409, 'Arm in navigate mode before choosing a goal')
        session = app.state.session
        result = await app.state.nav.plan_once((body.x, body.z))
        if not app.state.armed or app.state.mode != 'navigate' or app.state.session != session:
            raise HTTPException(409, 'Stopped while planning')
        if result is None:
            nav_stop('pose_stale')
            raise HTTPException(409, 'No current rover pose')
        if not result.ok:
            reason = PLAN_STOP_REASONS.get(result.reason, 'no_path')
            nav_stop(reason)
            raise HTTPException(409, reason)
        app.state.nav.start_goal((body.x, body.z), result)
        return dict(version=1, goal=[body.x, body.z], points=result.points)

    @app.post('/rescan')
    async def rescan():
        session = app.state.session
        if session is None:
            raise HTTPException(409, 'No active map; connect the phone or create a session first')
        started = app.state.changes.start(session, int(time.time()*1000))
        if started is None:
            raise HTTPException(409, 'Nothing observed in this map yet')
        publish(objects_message(session))
        return dict(version=1, session_id=session[0], map_epoch=session[1], rescan_id=started.id,
                    baseline_objects=len(started.baseline))

    @app.post('/ask')
    async def ask(body: Ask):
        raise HTTPException(501, 'Object queries are not implemented')

    @app.get('/objects')
    async def objects():
        message = objects_message(shown_session())
        del message['type']
        return message

    @app.get('/events')
    async def events():
        session = shown_session()
        session_id, map_epoch = session or (None, None)
        return dict(version=1, session_id=session_id, map_epoch=map_epoch,
                    events=app.state.changes.events(session))

    @app.websocket('/phone')
    async def phone(ws: WebSocket):
        await ws.accept()
        owner = object()
        workers = []
        try:
            hello = Hello.model_validate_json(await ws.receive_text())
            if app.state.phone is not None:
                await ws.close(code=1008, reason='Only one phone is supported')
                return
            set_session((hello.session_id, hello.map_epoch))
            app.state.phone = owner
            session = (hello.session_id, hello.map_epoch)
            mailbox = LatestFrame()
            grid = app.state.occupancy
            workers.append(asyncio.create_task(frame_worker(
                owner, session, mailbox, map_update, accept_map(grid), app.state.map_stats,
                MAP_INTERVAL_S, MAP_MAX_AGE_S)))
            workers.append(asyncio.create_task(occupancy_worker(owner, session, grid)))
            detections = LatestFrame()
            if app.state.detector is not None:
                workers.append(asyncio.create_task(frame_worker(
                    owner, session, detections, object_locator(owner), accept_objects, app.state.detect_stats,
                    DETECT_INTERVAL_S, DETECT_MAX_AGE_S)))
            # Receipt is not progress. Pose and bundle streams are ordered independently:
            # an encoded bundle may follow newer poses, but only a strictly newer capture
            # advances the pose watchdog. All of this is per connection, so a reconnect
            # (whose phone clock may restart) begins clean.
            last_capture = -1.0  # newest pose progress, from either stream
            last_frame_capture = -1.0
            seen = {}  # t_capture -> (frame_id, transform, tracking) of recent messages
            last_pose_publish = -1.0
            while True:
                message = await ws.receive()
                if message['type'] == 'websocket.disconnect':
                    break
                if app.state.phone is not owner:
                    await ws.close(code=1008, reason='Session revoked; reconnect with hello')
                    break
                if message.get('bytes') is not None:
                    pose = decode_frame(message['bytes'])
                else:
                    pose = Pose.model_validate_json(message['text'])
                    if pose.type != 'pose':
                        raise ValueError('frame must be binary')
                if (pose.session_id, pose.map_epoch) != app.state.session:
                    raise ValueError('session/epoch mismatch; reconnect with hello')
                is_frame = isinstance(pose, Frame)
                identity = (pose.frame_id, pose.transform, pose.tracking)
                if seen.setdefault(pose.t_capture, identity) != identity:
                    raise ValueError('pose/frame disagree about the same capture')
                if len(seen) > POSE_HISTORY:
                    del seen[next(iter(seen))]
                if abs(time.time()*1000 - pose.t_wall_ms) > 250:
                    if is_frame:
                        app.state.map_stats['discarded_wall_time'] += 1
                    else:
                        stop('pose_stale')
                    continue
                if is_frame:
                    if (pose.t_capture <= last_frame_capture or
                            pose.t_capture < last_capture - MAP_MAX_AGE_S):
                        app.state.map_stats['discarded_order'] += 1
                        continue
                    last_frame_capture = pose.t_capture
                    app.state.map_stats['received'] += 1
                elif pose.t_capture < last_capture:
                    stop('pose_stale')  # a rewound pose is not progress
                    continue
                elif pose.t_capture == last_capture:
                    continue  # repeat, or the pose half of a bundle already counted
                if pose.tracking != 'normal':
                    app.state.tracking_lost_capture = max(app.state.tracking_lost_capture, pose.t_capture)
                newest_pose = pose.t_capture > last_capture
                if newest_pose:
                    last_capture = pose.t_capture
                    app.state.pose = pose
                    app.state.pose_at = time.monotonic()
                    if pose.tracking != 'normal':
                        stop('tracking_lost')
                app.state.db.execute(
                    'INSERT OR REPLACE INTO frames(session_id,map_epoch,frame_id,t_capture,t_wall_ms,transform_json,tracking,intrinsics_json) VALUES(?,?,?,?,?,?,?,?)',
                    (pose.session_id, pose.map_epoch, pose.frame_id, pose.t_capture, pose.t_wall_ms,
                     json.dumps(pose.transform), pose.tracking,
                     json.dumps(pose.image.intrinsics) if isinstance(pose, Frame) else None))
                app.state.db.commit()
                if is_frame:
                    app.state.capture.update(message['bytes'], pose.model_dump())
                if is_frame and pose.tracking == 'normal' and pose.t_capture > app.state.tracking_lost_capture:
                    if mailbox.put(message['bytes']):
                        app.state.map_stats['replaced'] += 1
                    if app.state.detector is not None and detections.put(message['bytes']):
                        app.state.detect_stats['replaced'] += 1
                elif is_frame:
                    app.state.map_stats['discarded_tracking'] += 1
                now = time.monotonic()
                if newest_pose and now - last_pose_publish >= 1/15:
                    last_pose_publish = now
                    transform = pose.transform
                    publish(dict(version=1, type='pose', position=transform[12:15],
                                 yaw_rad=math.atan2(-transform[8], -transform[10]), tracking=pose.tracking))
        except (ValueError, ValidationError, KeyError, TypeError, WebSocketDisconnect):
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1008, reason='Invalid v1 phone protocol')
        finally:
            for worker in workers:
                worker.cancel()
            if app.state.phone is owner:
                app.state.phone = None
                app.state.pose = app.state.pose_at = app.state.detected_at = None
                app.state.tracking_lost_capture = -1.0
                app.state.capture.clear()
                app.state.rich_capture.reset()
                stop('phone_disconnected')
                publish(health())

    @app.websocket('/live')
    async def live(ws: WebSocket):
        await ws.accept()
        listener = Listener()
        app.state.listeners.add(listener)
        async def send():
            await ws.send_json(health())
            await ws.send_json(objects_message(shown_session()))
            await ws.send_json(path_message(app.state.nav.path))
            if (grid_message := occupancy_snapshot()) is not None:
                await ws.send_json(grid_message)
            while True:
                await listener.wake.wait()
                listener.wake.clear()
                while listener.queue.qsize() or listener.points or listener.occupancy:
                    if listener.queue.qsize():
                        await ws.send_json(listener.queue.get_nowait())
                    if listener.points:
                        await ws.send_json(listener.points.popleft())
                    if listener.occupancy:
                        message, listener.occupancy = listener.occupancy, None
                        await ws.send_json(message)
        sender = asyncio.create_task(send())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.listeners.discard(listener)
            sender.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await sender

    return app


app = create_app(weights=os.environ.get('GODSEYE_YOLO_WEIGHTS'))
