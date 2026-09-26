"""God's Eye v1 transport skeleton; all motion is logging-only."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.drive import drive


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


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get('GODSEYE_DB', 'backend/godseye.db')

    @asynccontextmanager
    async def lifespan(app):
        db = sqlite3.connect(db_path)
        db.executescript(Path(__file__).with_name('schema.sql').read_text())
        app.state.db = db
        app.state.session = None
        app.state.phone = None
        app.state.pose = None
        app.state.pose_at = None
        app.state.armed = False
        app.state.mode = 'manual'
        app.state.stop_reason = 'startup_disarmed'
        app.state.listeners = set()
        task = asyncio.create_task(watchdog())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            stop('shutdown')
            db.close()

    app = FastAPI(title="God's Eye backend skeleton", version='1', lifespan=lifespan)

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    def stop(reason):
        app.state.armed = False
        app.state.stop_reason = reason
        drive(0.0, 0.0)
        session = app.state.session or (None, None)
        app.state.db.execute(
            'INSERT INTO health_events(t_wall_ms,session_id,map_epoch,component,reason,mode,armed) VALUES(?,?,?,?,?,?,?)',
            (int(time.time()*1000), *session, 'backend', reason, app.state.mode, 0))
        app.state.db.commit()

    def health():
        age = None if app.state.pose_at is None else (time.monotonic() - app.state.pose_at) * 1000
        phone = 'down' if app.state.phone is None else 'stale'
        if age is not None and age <= 250 and app.state.pose.tracking == 'normal':
            phone = 'ok'
        return dict(version=1, type='health', phone=phone, car='down', detector='down',
                    pose_age_ms=age, mode=app.state.mode, armed=app.state.armed,
                    stop_reason=app.state.stop_reason)

    def publish(message):
        for queue in app.state.listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    async def watchdog():
        ticks = 0
        while True:
            await asyncio.sleep(.05)
            if app.state.pose_at is not None and time.monotonic() - app.state.pose_at > .25:
                if app.state.stop_reason != 'pose_stale':
                    stop('pose_stale')
            ticks += 1
            if ticks % 10 == 0:
                publish(health())

    def set_session(session):
        stop('session_reset')
        app.state.session = session
        app.state.pose = app.state.pose_at = None
        app.state.db.execute('INSERT OR IGNORE INTO sessions(session_id,map_epoch,created_at_ms) VALUES(?,?,?)',
                             (*session, int(time.time()*1000)))
        app.state.db.commit()
        publish(dict(version=1, type='objects', objects=[]))
        publish(dict(version=1, type='path', points=[]))

    @app.get('/health')
    async def get_health():
        return health()

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
        raise HTTPException(501, 'Navigation is not implemented')

    @app.post('/rescan')
    async def rescan():
        raise HTTPException(501, 'Baseline/revisit is not implemented')

    @app.post('/ask')
    async def ask(body: Ask):
        raise HTTPException(501, 'Object queries are not implemented')

    @app.get('/objects')
    async def objects():
        return dict(version=1, objects=[])

    @app.get('/events')
    async def events():
        return dict(version=1, events=[])

    @app.websocket('/phone')
    async def phone(ws: WebSocket):
        await ws.accept()
        owner = object()
        try:
            hello = Hello.model_validate_json(await ws.receive_text())
            if app.state.phone is not None:
                await ws.close(code=1008, reason='Only one phone is supported')
                return
            set_session((hello.session_id, hello.map_epoch))
            app.state.phone = owner
            last_capture = -1.0
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
                if pose.tracking != 'normal':
                    stop('tracking_lost')
                # Do not let delayed data refresh the watchdog or overwrite newer poses.
                if pose.t_capture < last_capture or abs(time.time()*1000 - pose.t_wall_ms) > 250:
                    stop('pose_stale')
                    continue
                last_capture = pose.t_capture
                app.state.pose = pose
                app.state.pose_at = time.monotonic()
                app.state.db.execute(
                    'INSERT OR REPLACE INTO frames(session_id,map_epoch,frame_id,t_capture,t_wall_ms,transform_json,tracking,intrinsics_json) VALUES(?,?,?,?,?,?,?,?)',
                    (pose.session_id, pose.map_epoch, pose.frame_id, pose.t_capture, pose.t_wall_ms,
                     json.dumps(pose.transform), pose.tracking,
                     json.dumps(pose.image.intrinsics) if isinstance(pose, Frame) else None))
                app.state.db.commit()
                now = time.monotonic()
                if now - last_pose_publish >= 1/15:
                    last_pose_publish = now
                    transform = pose.transform
                    publish(dict(version=1, type='pose', position=transform[12:15],
                                 yaw_rad=math.atan2(-transform[8], -transform[10]), tracking=pose.tracking))
        except (ValueError, ValidationError, KeyError, TypeError, WebSocketDisconnect):
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1008, reason='Invalid v1 phone protocol')
        finally:
            if app.state.phone is owner:
                app.state.phone = None
                app.state.pose = app.state.pose_at = None
                stop('phone_disconnected')
                publish(health())

    @app.websocket('/live')
    async def live(ws: WebSocket):
        await ws.accept()
        queue = asyncio.Queue(maxsize=32)
        app.state.listeners.add(queue)
        async def send():
            await ws.send_json(health())
            await ws.send_json(dict(version=1, type='objects', objects=[]))
            await ws.send_json(dict(version=1, type='path', points=[]))
            while True:
                await ws.send_json(await queue.get())
        sender = asyncio.create_task(send())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.listeners.discard(queue)
            sender.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await sender

    return app


app = create_app()
