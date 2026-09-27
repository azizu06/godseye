"""God's Eye v1 transport skeleton; the default car adapter only logs motion."""
import asyncio
import base64
from collections import Counter, deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
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

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.approach import ASSUMPTIONS as APPROACH_ASSUMPTIONS, approach_route, route_still_clear, person_evidence_reason
from backend.audio import register_audio_routes
from backend.calibration import calibration_from_env
from backend.changes import ChangeTracker
from backend.drive import LoggingCar
from backend.capture import CaptureBuffer
from backend.capture_routes import register_capture_routes
from backend.rich_capture import RichCapture
from backend.frame_bundle import FrameValidationError, validate_rigid_transform, parse_frame_bundle
from backend.explore_obstacle_gate import ExploreObstacleGate, PathObservation
from backend.scan_pacing import CameraPose, ScanObservation, capture_observation
from backend.mapping import (InsufficientDepth, MappingError, PointChunk, build_point_chunk, depth_to_points,
                             points_message, points_binary, floor_plane_points, POINTS_PROTOCOL, DENSE_MAX_POINTS)
from backend.motion import CarAdapter, Motion, MotionLimits
from backend.manual_control import capabilities as manual_capabilities, clearance_problem
from backend.moves import MoveRunner, MoveSettings, register_move_routes
from backend.mission_entry import load_entry, record_entry
from backend.detections import classes_from_env, detections_message, overlay_classes
from backend.objects import ObjectMemory, detect_objects
from backend.labels import ObjectLabels, answer_from_objects, provider_from_env
from backend.voice import (DEFAULT_BUDGET, providers_from_env as voice_from_env, register_voice_routes, scene_extras,
                           scout_position)
from backend.occupancy import (CELL_M, PUBLISH_INTERVAL_S as OCCUPANCY_INTERVAL_S,
                               DepthView, Evidence, OccupancyGrid, depth_view, frame_evidence)
from backend.navigation import Grid, path_message
from backend.navigator import PLAN_STOP_REASONS, Navigator, NavSettings, RoverPose, pose_from_transform, map_problem, start_clearance_diagnostics
from backend.nav_actions import NavProposals, register_nav_action_routes
from backend.rover_relay import RelayCar, relay_from_env
from backend.device_relay import DeviceAction, DeviceRelay
from backend.prototype import PrototypeGeometry, filter_prototype_self_mesh
from backend.point_dedupe import NoNewPoints, PointSettings, VoxelMemory
from backend.prototype import PrototypeGeometry, filter_prototype_self_mesh

logger = logging.getLogger(__name__)
DENSE_MAP_INTERVAL_S = 1 / 30
MAP_INTERVAL_S = .25  # at most 4 Hz of point chunks
MAP_MAX_AGE_S = 1.  # discard chunks computed from frames older than this
POSE_HISTORY = 64  # recent captures kept to check a delayed bundle against its own pose
MAP_PENDING_POINTS = 2  # unsent point chunks kept per slow viewer
DETECT_INTERVAL_S = .5  # at most 2 Hz of object inference
DETECT_MAX_AGE_S = 2.  # discard detections finished this long after their frame arrived
DETECTOR_OK_S = 2.  # health reports the detector ok this long after a used result
EXPLORE_RETRY_S = 1.  # quiet backoff after a failed arm or a stopped Explore run


class Input(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)


class Mode(Input):
    mode: Literal['manual', 'navigate', 'explore']


class Manual(Input):
    takeover: bool = False
    release: bool = False
    expected_generation: int | None = Field(default=None, ge=0)
    v_mps: float = Field(ge=-.2, le=.2)
    yaw_rate_rps: float = Field(ge=-.5, le=.5)


class ExploreYield(Input):
    generation: int = Field(ge=0)
    reason: Literal['person_path_crossing', 'person_clearance_unknown', 'obstacle_wait']


class ExploreResume(Input):
    generation: int = Field(ge=0)


class Goal(Input):
    x: float
    z: float


class Ask(Input):
    question: str = Field(min_length=1, max_length=2000)


class Route(Input):
    purpose: Literal['selected', 'recon'] = 'selected'
    session_id: str = Field(min_length=1, max_length=256)
    map_epoch: int = Field(ge=1)
    object_id: str = Field(min_length=1, max_length=256)
    start: list[float] = Field(min_length=2, max_length=2)  # operator-selected entrance/start, world (x, z)


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
    version: Literal[1, 2, 3]
    floor: dict | None = None
    mesh_voxels: str | None = None
    type: Literal['frame']
    image: Image
    depth: Depth
    confidence: Confidence
    floor: dict | None = None
    mesh_voxels: str | None = None
    mesh_voxels: str | None = None


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

    def __init__(self, dense=False):
        self.dense = dense
        self.last_points = -1.0
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
    depth: DepthView | None = None
    retirement_keys: object = None
    retirement_cursor: int | None = None
    mesh_keys: object = None
    scan: ScanObservation | None = None


class LatestFrame:
    """Single-slot mailbox: a newer bundle replaces one still waiting."""

    def __init__(self):
        self.item = None
        self.ready = asyncio.Event()

    def put(self, payload, observed_at=None):
        replaced = self.item is not None
        self.item = (payload, time.monotonic(), observed_at)
        self.ready.set()
        return replaced

    def take(self):
        item, self.item = self.item, None
        self.ready.clear()
        return item


def create_app(db_path: str | None = None, build_points=None,
               detector=None, weights: str | None = None, capture_directory: str | None = None,
               point_settings: PointSettings | None = None, nav_settings: NavSettings | None = None,
               car: CarAdapter | None = None, motion_limits: MotionLimits | None = None,
               audio_provider=None, calibration=None, label_provider=None,
               label_timeout_s: float = 6., overlay: tuple[str, ...] | None = None,
               voice_providers=None, voice_budget: int = DEFAULT_BUDGET) -> FastAPI:
    """`build_points(payload, session_id, map_epoch)` runs in a worker thread.

    Its candidate points pass through a per-map voxel memory (`point_settings`,
    default from GODSEYE_POINT* variables) so chunks carry mostly new surface.

    `detector.localize(frame)` also runs in a worker thread, one call at a time.
    Without a detector, `weights` names existing local YOLO weights to load at
    startup; with neither, object detection is off and health reports it down.

    `overlay` names the classes shown as live detection boxes (default
    `GODSEYE_OVERLAY_CLASSES` or the staged demo set), limited to those the
    detector reports it can emit.

    `calibration` (backend.calibration.RoverCalibration) is the measured rover
    geometry, or an explicitly selected PrototypeGeometry with operator estimates.
    Missing geometry still prevents motion; prototype mode is labeled uncalibrated.

    `car` is the drive adapter (`backend.drive`); the default only logs and
    reports the car down, so /arm stays refused. Only the 20 Hz pump sends motion.
    """
    db_path = db_path or os.environ.get('GODSEYE_DB', 'backend/godseye.db')
    point_settings = point_settings or PointSettings.from_env()
    custom_builder = build_points
    build_points = build_points or partial(build_point_chunk, max_points=point_settings.samples)
    car = LoggingCar() if car is None else car
    relay = car if isinstance(car, RelayCar) else None
    device = DeviceRelay(relay.authorized) if relay is not None else None
    if relay is not None and not relay.actuation.blockers:
        nav_settings = replace(nav_settings or NavSettings(), follower=relay.actuation.follower(),
                               adaptive_explore=getattr(relay.actuation, 'prototype', False),
                               proximity_slowdown=getattr(relay.actuation, 'cruise_pwm', None) is not None,
                               replan_s=4. if getattr(relay.actuation, 'prototype', False)
                               else (nav_settings or NavSettings()).replan_s,
                               start_recovery_margin_m=.1524 if getattr(relay.actuation, 'prototype', False)
                               else (nav_settings or NavSettings()).start_recovery_margin_m,
                               pose_max_age_s=1. if getattr(relay.actuation, 'prototype', False)
                               else (nav_settings or NavSettings()).pose_max_age_s)
    detect_lock = threading.Lock()  # one inference at a time, even across phone reconnects
    nav_settings = nav_settings or NavSettings()
    proposals = NavProposals(nav_settings)  # voice navigation proposals awaiting a human confirmation

    @asynccontextmanager
    async def lifespan(app):
        db = sqlite3.connect(db_path)
        # Telemetry must not fsync a rollback journal on every 30 Hz pose.
        # WAL/NORMAL retains transactional consistency; recent telemetry may
        # be lost on power failure, but disk flushes no longer gate control.
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        db.executescript(Path(__file__).with_name('schema.sql').read_text())
        app.state.db = db
        app.state.objects = ObjectMemory(db)
        app.state.mission_entry = load_entry(db, app.state.objects.latest_session())
        # A pre-existing scan without metadata may already have moved. Never
        # manufacture its entry on a later arm after a restart/storage failure.
        app.state.mission_entry_attempted = app.state.objects.latest_session() is not None
        app.state.changes = ChangeTracker(db, app.state.objects)
        if detector is None and weights:
            from backend.detector import MPSDetector
            app.state.detector = await asyncio.to_thread(MPSDetector, weights)
        else:
            app.state.detector = detector
        app.state.detected_at = None
        app.state.detect_stats = Counter()
        app.state.overlay_classes = overlay_classes(overlay or classes_from_env(),
                                                    getattr(app.state.detector, 'class_names', None))
        app.state.detection_view = None  # (message, jpeg) of the newest accepted detection frame
        # The current selected-person /route response plus t_wall_ms, for read-only
        # consumers such as voice answers; None when absent or invalidated.
        app.state.approach_view = None
        app.state.approach_basis = None  # fingerprint of the occupancy cells it was planned on
        app.state.route_requests = 0
        app.state.route_selection = None
        app.state.route_lifecycle = 0
        app.state.session = None
        app.state.phone = None
        app.state.pose = None
        app.state.pose_at = None
        app.state.tracking_lost_capture = -1.0  # frames captured at or before this are untrusted
        app.state.armed = False
        app.state.auto_requested = False  # explicit active Explore; faults never auto-rearm
        app.state.explore_yield = None
        app.state.obstacle_gate = ExploreObstacleGate()
        app.state.yield_path = []
        app.state.input_gap_since = None
        app.state.auto_arm_task = None
        app.state.auto_retry_at = 0.
        app.state.arm_request_token = None
        app.state.mode = 'manual'
        app.state.manual_generation = None
        app.state.stop_reason = 'startup_disarmed'
        limits = motion_limits or (MotionLimits(max_speed_mps=.2)
                                   if relay is not None and getattr(relay.actuation, 'prototype', False)
                                   else MotionLimits())
        app.state.motion = Motion(car, motion_check, stop, limits=limits)
        if relay is not None:
            relay.identity = lambda: app.state.session
            relay.on_loss = stop
        app.state.listeners = set()
        app.state.capture = CaptureBuffer()
        app.state.rich_capture = RichCapture(capture_directory if capture_directory is not None else
            (None if db_path == ':memory:' else os.environ.get('GODSEYE_CAPTURE_DIR', 'backend/captures')))
        app.state.capture_ingest_lock = asyncio.Lock()
        app.state.chunk_id = 0
        app.state.point_memory = VoxelMemory(point_settings)
        app.state.map_stats = Counter()
        app.state.occupancy = None  # OccupancyGrid of the active session/epoch
        app.state.autonomy_map = None
        app.state.occupancy_stats = Counter()
        app.state.scan_observation = None
        app.state.nav = Navigator(nav_settings, pose=rover_pose,
                                  occupancy=map_snapshot, submit=app.state.motion.submit,
                                  stop=nav_stop, publish=publish,
                                  armed_mode=lambda: app.state.mode if app.state.armed else None,
                                  pause_reason=prototype_pause_reason,
                                  scan_observation=lambda: app.state.scan_observation)
        app.state.mover = MoveRunner(MoveSettings(), pose=rover_pose, submit=app.state.motion.submit, stop=nav_stop,
                                     armed_mode=lambda: app.state.mode if app.state.armed else None,
                                     speeds=move_speeds(), early_stop_m=getattr(
                                         relay.actuation if relay else None, 'stopping_distance_m', None) or 0.)
        proposals.invalidate()
        app.state.nav_proposals = proposals
        app.state.labels = ObjectLabels(db, label_provider,
            lambda session: publish(objects_message(session)) if session == shown_session() else None,
            timeout_s=label_timeout_s)
        label_task = asyncio.create_task(app.state.labels.run())
        task = asyncio.create_task(watchdog())
        readiness_task = asyncio.create_task(refresh_autonomy_map()) if relay is not None else None
        try:
            yield
        finally:
            stop('shutdown')
            if app.state.auto_arm_task is not None:
                app.state.auto_arm_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app.state.auto_arm_task
            label_task.cancel()
            with suppress(asyncio.CancelledError):
                await label_task
            task.cancel()
            if readiness_task is not None:
                readiness_task.cancel()
                with suppress(asyncio.CancelledError):
                    await readiness_task
            try:
                with suppress(asyncio.CancelledError):
                    await task
                await app.state.nav.aclose()
                await app.state.mover.aclose()
            finally:  # zero even if the watchdog or a navigation run died with an error
                stop('shutdown')
                db.close()

    app = FastAPI(title="God's Eye backend skeleton", version='1', lifespan=lifespan)
    register_capture_routes(app)
    register_audio_routes(app, audio_provider)
    # Live extras come from app.state.detection_view (newest detection frame) and
    # app.state.approach_view (last /route response); each is absent until its producer sets it.
    speak = register_voice_routes(app, voice_providers, voice_budget, lambda: dict(
        session=shown_session(), objects=app.state.objects.snapshot(shown_session(), limit=None),
        events=app.state.changes.events(shown_session()),
        live=app.state.phone is not None and app.state.session is not None,
        scout=scout_position(rover_pose()), route=app.state.nav.path, classes=app.state.overlay_classes,
        extras=lambda: scene_extras(app.state, shown_session(), time.time())))
    register_move_routes(app, lambda: app.state.mover, speak)

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["Content-Type", "If-None-Match", "Authorization"],
                       expose_headers=["ETag", "X-Capture-Age-Ms"])

    @app.middleware('http')
    async def authorize_rover_commands(request, call_next):
        # Reading the map and emergency Stop remain available without a key.
        if (relay is not None and request.url.path in {'/arm', '/mode', '/manual', '/goal', '/nav/confirm', '/device/action', '/explore/yield', '/explore/resume'}
                and request.method == 'POST' and not relay.authorized(request.headers.get('authorization'))):
            return Response('Rover pairing key required', status_code=401)
        return await call_next(request)

    @app.websocket('/rover')
    async def rover_socket(ws: WebSocket):
        if relay is None:
            await ws.close(code=1008)
        else:
            await relay.serve(ws)

    @app.websocket('/device')
    async def device_socket(ws: WebSocket):
        if device is None:
            await ws.close(code=1008)
        else:
            await device.serve(ws)

    @app.get('/device')
    async def device_status():
        return device.snapshot() if device else dict(version=1, connected=False, phone=None, age_ms=None)

    @app.post('/device/action')
    async def device_action(body: DeviceAction):
        if device is None:
            raise HTTPException(409, 'The iPhone adapter is not configured')
        if body.action in {'stop', 'control_disable', 'capture_stop', 'rover_disconnect'}:
            app.state.arm_request_token = None
            stop('operator_stop')
        return await device.action(body)

    def autonomy_blockers():
        if relay is None:
            return ['logging_adapter_only']
        reasons = list(relay.blockers())
        if calibration is None:
            reasons.append('rover_geometry_unmeasured')
        else:
            reasons.extend(calibration.blockers)
            distance = relay.actuation.stopping_distance_m
            margin = calibration.clearance_margin_m
            if distance is not None and margin is not None:
                # Cover measured stopping distance plus the full command-age
                # allowance at this adapter's maximum forward speed.
                speed = min(.15, relay.actuation.forward[-1].rate) if relay.actuation.forward else .15
                if margin < distance + speed * .5:
                    reasons.append('clearance_below_stopping_envelope')
        problem = map_problem(app.state.autonomy_map, (nav_settings or NavSettings()).map_max_age_s)
        if problem:
            reasons.append(problem)
        return list(dict.fromkeys(reasons))

    def manual_blockers():
        """What a bounded manual move needs from the adapter: its link, fresh car feedback and a
        profile that can turn a velocity into motor power. Map, route and footprint readiness are
        autonomy-only: a move follows no route and its progress comes from the phone's pose."""
        return list(dict.fromkeys(relay.blockers())) if relay is not None else []

    def move_speeds():
        """The slowest measured forward and turn rates, else the follower's (the prototype maps any
        nonzero request to its fixed PWM, so its rate is nominal, never a claimed speed)."""
        actuation = relay.actuation if relay is not None else None
        if getattr(actuation, 'forward', None) and actuation.left and actuation.right:
            return actuation.forward[0].rate, max(actuation.left[0].rate, actuation.right[0].rate)
        follower = (nav_settings or NavSettings()).follower
        return follower.min_mps, follower.max_yaw_rate_rps

    @app.get('/autonomy')
    async def autonomy_readiness():
        reasons = autonomy_blockers()
        current = health()
        snapshot, pose = map_snapshot(), rover_pose()
        start = None
        if (snapshot is not None and pose is not None and pose.tracking == 'normal'
                and pose.age_s <= app.state.nav.settings.pose_max_age_s
                and map_problem(snapshot, app.state.nav.settings.map_max_age_s) is None):
            start = start_clearance_diagnostics(snapshot, pose.x, pose.z,
                                               app.state.nav.settings.start_recovery_margin_m)
        reasons.extend(f'{part}_{current[part]}' for part in ('phone', 'detector') if current[part] != 'ok')
        return dict(version=1, adapter='iphone' if relay else 'logging',
                    profile='prototype' if relay and getattr(relay.actuation, 'prototype', False) else 'measured',
                    warnings=list(getattr(relay.actuation, 'warnings', ())) if relay else [],
                    prototype_max_pwm=getattr(relay.actuation, 'max_pwm', None) if relay else None,
                    prototype_cruise_pwm=getattr(relay.actuation, 'cruise_pwm', None) if relay else None,
                    prototype_variable_arcs=getattr(relay.actuation, 'variable_arc_pwm', False) if relay else False,
                    blockers=list(dict.fromkeys(reasons)), ready=not reasons,
                    auto_requested=app.state.auto_requested, scan_pacing=app.state.nav.scan_status,
                    generation=app.state.motion.generation,
                    exploration=app.state.nav.exploration.stats(),
                    navigation_wait_reason=current['navigation_wait_reason'],
                    explore_yield=app.state.explore_yield, navigation_start=start,
                    manual_control=manual_capabilities(relay.actuation, app.state.motion.limits) if relay else None,
                    armed=current['armed'], mode=current['mode'], stop_reason=current['stop_reason'],
                    car=current['car'], command_authorization_required=relay is not None)

    def stop(reason):
        # Delayed pose bursts must not continually replace the pending Stop
        # acknowledgement. A new arm generation is active even while awaiting
        # its handshake, so the same hazard still cancels a new arm.
        app.state.auto_requested = False
        app.state.arm_request_token = None
        pending = getattr(app.state, 'auto_arm_task', None)
        if pending is not None and pending is not asyncio.current_task():
            pending.cancel()
        app.state.explore_yield = None
        if hasattr(app.state, 'obstacle_gate'):
            app.state.obstacle_gate.reset()
            app.state.yield_path = []
        app.state.input_gap_since = None
        if not app.state.motion.active and app.state.stop_reason == reason:
            return
        if app.state.auto_requested:
            app.state.auto_retry_at = time.monotonic() + EXPLORE_RETRY_S
        app.state.armed = False
        app.state.manual_generation = None
        app.state.stop_reason = reason
        if getattr(app.state, 'nav', None) is not None:
            app.state.nav.halt()  # end any goal/explore run and clear its path first
        if getattr(app.state, 'mover', None) is not None:
            app.state.mover.halt(reason)  # a running voice move reports this reason and its measured result
        if reason != 'mode_change' and getattr(app.state, 'nav_proposals', None) is not None:
            app.state.nav_proposals.invalidate()  # a stop, loss or reset voids every pending confirmation
        app.state.motion.halt()  # drops every held command, then an explicit zero
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
        mount_yaw = calibration.camera_yaw_rad if calibration is not None else None
        x, z, yaw = pose_from_transform(pose.transform, mount_yaw if mount_yaw is not None else 0.)
        camera = CameraPose.from_transform(app.state.session, app.state.phone, pose.t_capture, pose.transform)
        return RoverPose(x, z, yaw, time.monotonic() - app.state.pose_at, pose.tracking, camera)

    def health():
        age = None if app.state.pose_at is None else (time.monotonic() - app.state.pose_at) * 1000
        phone = 'down' if app.state.phone is None else 'stale'
        if (age is not None and age <= nav_settings.pose_max_age_s * 1000
                and app.state.pose.tracking == 'normal'):
            phone = 'ok'
        detector = 'down'
        if app.state.detector is not None:
            detector = 'stale'
            if app.state.detected_at is not None and time.monotonic() - app.state.detected_at <= DETECTOR_OK_S:
                detector = 'ok'
        return dict(version=1, type='health', phone=phone, car=car_health(), detector=detector,
                    pose_age_ms=age, mode=app.state.mode, armed=app.state.armed,
                    stop_reason=app.state.stop_reason, mission_entry=app.state.mission_entry,
                    motion_generation=app.state.motion.generation,
                    navigation_wait_reason=app.state.nav.waiting_reason)

    def car_health():
        try:
            state = car.health()
        except Exception:
            logger.exception('car health check failed')
            return 'down'
        return state if state in ('ok', 'stale', 'down') else 'down'

    def hazard(mode=None):
        """Why the rover must not move now in `mode` (default the current one), e.g. 'car_stale';
        None when phone, car and detector are ok and the adapter is ready for that mode."""
        current = health()
        manual = (mode or app.state.mode) == 'manual'
        for key in ('phone', 'car', 'detector'):
            if current[key] != 'ok':
                if key == 'car' and manual and (reasons := manual_blockers()):
                    return reasons[0]  # name the relay's precise blocker, e.g. an unmeasured profile
                return f'{key}_{current[key]}'
        if relay is not None and (reasons := manual_blockers() if manual else autonomy_blockers()):
            return reasons[0]
        return None

    def persistent_explore():
        return (relay is not None and getattr(relay.actuation, 'prototype', False)
                and app.state.armed and app.state.mode == 'explore')

    def prototype_pause_reason():
        """Ordinary yield is in-session; critical input gaps are bounded, never rearmed."""
        if not app.state.armed or app.state.mode != 'explore':
            return None
        reason = hazard()
        if reason is None:
            app.state.input_gap_since = None
            return app.state.explore_yield
        if app.state.pose is not None and app.state.pose.tracking != 'normal':
            return None  # invalid tracking is a fault, not an ordinary obstruction
        recoverable = persistent_explore() and (
            reason in {'phone_stale', 'detector_stale', 'sensing_stale'} or
            (reason == 'car_stale' and relay.blockers() == ['rover_feedback_stale']))
        if recoverable:
            if app.state.input_gap_since is None:
                app.state.input_gap_since = time.monotonic()
            if time.monotonic() - app.state.input_gap_since <= 1.:
                return reason
        return None

    def motion_check(command):
        """Rechecked before every send; stop, reset, loss and shutdown already cut the generation."""
        if not app.state.armed:
            return 'disarmed'
        if command.mode != app.state.mode:
            return 'mode_change'
        if (reason := hazard()) is not None:
            return reason
        if (relay is not None and app.state.manual_generation == command.generation
                and (command.v_mps or command.yaw_rate_rps)):
            return clearance_problem(app.state.autonomy_map, rover_pose(), app.state.session,
                                     nav_settings, command.v_mps, command.yaw_rate_rps)
        return None

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
        last_start = -1.0
        last_capture = -1.0
        while True:
            await mailbox.ready.wait()
            cadence = interval() if callable(interval) else interval
            await asyncio.sleep(max(0., last_start + cadence - time.monotonic()))
            payload, received, observed_at = mailbox.take()  # newest wins; older ones were replaced
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
                if isinstance(result, MapUpdate) and result.scan is not None:
                    result = replace(result, scan=replace(result.scan,
                        pose=replace(result.scan.pose, owner=owner),
                        observed_at=received if observed_at is None else observed_at))
                try:
                    outcome = accept(result) or 'published'
                except Exception:
                    stats['failed'] += 1
                    logger.exception('frame result could not be applied')
                    continue
                stats[outcome] += 1

    def accept_points(chunk):
        app.state.chunk_id += 1
        now = time.monotonic()
        binary = legacy = None
        for listener in app.state.listeners:
            if listener.dense:
                if binary is None:
                    binary = points_binary(chunk, app.state.chunk_id)
                listener.points.append(binary)
            elif not any(v.dense for v in app.state.listeners) or now - listener.last_points >= MAP_INTERVAL_S:
                if legacy is None:
                    legacy = points_message(chunk, app.state.chunk_id)
                listener.points.append(legacy)
                listener.last_points = now
            else:
                continue
            listener.wake.set()
        app.state.point_memory.commit(chunk.voxel_keys, chunk.t_capture)

    def map_update(payload, session_id, map_epoch):
        """Worker-thread half of mapping: the frame's occupancy evidence and deduped chunk.

        The grid gets every candidate point; only the published chunk is deduped,
        so occupancy evidence keeps accumulating when nothing new is published.
        """
        dense = any(listener.dense for listener in tuple(app.state.listeners))
        view = None
        mesh_keys = None
        if custom_builder is None:
            frame = parse_frame_bundle(payload, session_id=session_id, map_epoch=map_epoch)
            view = depth_view(frame)
            if frame.mesh_points is not None:
                mesh_points = (filter_prototype_self_mesh(frame.mesh_points, frame.transform, calibration,
                                                          intrinsics=frame.intrinsics,
                                                          image_size=frame.image.size)
                               if isinstance(calibration, PrototypeGeometry) else frame.mesh_points)
                mesh_keys = frame_evidence(mesh_points).keys
                grid = app.state.occupancy
                if grid is not None:
                    mesh_keys = grid.mesh_keys_consistent_with_depth(mesh_keys, view)
            samples = max(point_settings.samples, DENSE_MAX_POINTS) if dense else point_settings.samples
            prototype_depth = getattr(calibration, 'depth_confidence', 2) == 1
            try:
                candidates = depth_to_points(frame, max_points=samples)
            except InsufficientDepth:
                if not prototype_depth:
                    raise
                candidates = None  # display may be empty; navigation still needs valid medium depth
            # Carpet commonly has medium LiDAR confidence. Prototype navigation
            # accumulates that evidence across frames; displayed points retain
            # their existing high-confidence policy. Decode the JPEG only once.
            evidence_points = (depth_to_points(frame, max_points=max(samples, 6000), min_confidence=1).positions
                               if prototype_depth else candidates.positions)
            # Preserve the depth sample minimum: a floor polygon by itself
            # cannot refresh collision sensing. Observed obstacle voxels win
            # over these bounded same-frame free-floor samples.
            if frame.floor is not None and frame.frame_id % 5 == 0:
                evidence_points = np.concatenate((evidence_points, floor_plane_points(frame)))
        else:
            candidates = build_points(payload, session_id, map_epoch)
            evidence_points = candidates.positions
        evidence = None
        try:
            evidence = frame_evidence(evidence_points,
                                      camera_y=frame.transform[1, 3] if custom_builder is None else None,
                                      camera_xz=(frame.transform[0, 3], frame.transform[2, 3])
                                      if custom_builder is None else None,
                                      floor_y=frame.floor.y if custom_builder is None and frame.floor else None)
        except Exception:  # an occupancy bug must not cost the live points
            app.state.occupancy_stats['failed'] += 1
            logger.exception('occupancy evidence failed')
        if evidence is not None and view is not None:
            accepted = ((view.confidence >= getattr(calibration, 'depth_confidence', 2))
                        & (view.depth >= .05) & (view.depth <= 5.) & np.isfinite(view.depth))
            confidence = float(np.mean(view.confidence[accepted]) / 2.) if np.any(accepted) else 0.
            evidence = replace(evidence, sensing_confidence=confidence)
        retired = cursor = None
        if evidence is not None and view is not None:
            try:
                retired, cursor = app.state.occupancy.retirement_candidates(view)
            except Exception:
                app.state.occupancy_stats['failed'] += 1
                logger.exception('occupancy depth retirement failed')
        chunk = None
        if candidates is not None:
            try:
                chunk = app.state.point_memory.select(candidates, limit=DENSE_MAX_POINTS if dense else None)
            except NoNewPoints:
                pass
        return MapUpdate(frame.t_capture if custom_builder is None else candidates.t_capture,
                         evidence, chunk, view, retired, cursor, mesh_keys,
                         capture_observation(frame, candidates) if custom_builder is None else None)

    def accept_map(grid):
        """Event-loop half: commit a current frame to its map's grid, then publish its points.

        A phone rejoining the same map resumes this grid, so evidence from a frame
        that did not pass the worker's checks must never reach it.
        """
        def accept(update):
            if update.evidence is not None:
                try:
                    grid.commit(update.evidence, time.monotonic(),
                                retirement_keys=update.retirement_keys,
                                retirement_cursor=update.retirement_cursor,
                                depth_view=update.depth, mesh_keys=update.mesh_keys)
                    # Only accepted, successfully integrated same-frame evidence
                    # can inform pacing, even when display dedup emitted no chunk.
                    app.state.scan_observation = update.scan
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
            if app.state.approach_basis is not None and app.state.approach_basis != cells_fingerprint(
                    message['origin'], (message['height'], message['width']), base64.b64decode(message['cells'])):
                retire_route()

    def retire_route(*, invalidate_pending=False):
        app.state.approach_view = app.state.approach_basis = None
        if invalidate_pending:
            app.state.route_lifecycle += 1

    def cells_fingerprint(origin, shape, cells: bytes):
        return (tuple(float(v) for v in origin), tuple(int(v) for v in shape), hash(bytes(cells)))

    def active_grid():
        grid = app.state.occupancy
        return grid if grid is not None and grid.session == app.state.session else None

    def occupancy_snapshot():
        grid = active_grid()
        return None if grid is None else grid.last_message

    def map_snapshot():
        """The active map's OccupancySnapshot for navigation, or None without one.

        Blocking (it classifies all evidence), so async callers use a worker thread.
        """
        grid = active_grid()
        return None if grid is None else grid.map_snapshot()

    async def refresh_autonomy_map():
        # Classification/copying must never block the 20 Hz motion pump or
        # incoming permit acknowledgements. Freshness uses accepted sensing time,
        # not the time this cached snapshot was produced.
        while True:
            session, grid = app.state.session, active_grid()
            try:
                snapshot = await asyncio.to_thread(map_snapshot)
            except Exception:
                logger.exception('autonomy readiness map failed')
                snapshot = None
            if app.state.session == session and active_grid() is grid:
                app.state.autonomy_map = snapshot
            await asyncio.sleep(.2)

    app.state.map_snapshot = map_snapshot

    def accept_objects(result):
        session = (result.session_id, result.map_epoch)
        seen_at = result.t_wall_ms / 1000
        sightings = app.state.objects.record(session, result.frame_id, seen_at, result.found)
        app.state.labels.enqueue(session, sightings, result.crops)
        app.state.detected_at = time.monotonic()
        overlay = detections_message(result, sightings, app.state.overlay_classes)
        app.state.detection_view = (overlay, result.jpeg)
        publish(overlay)
        events, changed = app.state.changes.observe(result.t_capture, seen_at, sightings, result.views)
        if (view := app.state.approach_view) is not None and (sightings or changed):
            person = next((o for o in app.state.objects.snapshot(session, limit=None)
                           if o['id'] == view['object_id']), None)
            if (person is None or person_evidence_reason(person, time.time()) is not None
                    or [person['position'][0], person['position'][2]] != view['person']):
                retire_route()
            else:
                view['person_last_seen'] = person['last_seen']
        if sightings or changed:
            publish(objects_message(session))
        for record in events:
            publish(event_message(session, record))

    async def watchdog():
        ticks = 0
        while True:
            await asyncio.sleep(.05)
            if (app.state.pose_at is not None
                    and time.monotonic() - app.state.pose_at > nav_settings.pose_max_age_s):
                if not prototype_pause_reason() and app.state.stop_reason != 'pose_stale':
                    stop('pose_stale')
            pause_reason = prototype_pause_reason()
            if app.state.armed and not pause_reason and (reason := hazard()) is not None:
                stop(reason)
            if pause_reason:
                # Drop every queued movement before the 20 Hz pump can dispatch it.
                # Fresh relay feedback allows an in-session zero; otherwise the ESP
                # independently brakes when its command-loss timer expires.
                app.state.motion.desired = None
                if relay is not None:
                    relay.command = None
                if car_health() == 'ok' and not app.state.motion.zero():
                    stop('car_error')
            # Explore runs whenever the rover is armed in explore mode, under the current generation.
            if app.state.armed and app.state.mode == 'explore' and not app.state.nav.active:
                app.state.nav.start_explore(app.state.motion.generation)
            if not pause_reason:
                app.state.motion.tick()  # the 20 Hz dispatch pump
            ticks += 1
            if ticks % 10 == 0:
                view = app.state.approach_view
                if view is not None and person_evidence_reason(
                        dict(state='present', last_seen=view.get('person_last_seen')), time.time()):
                    retire_route()
                publish(health())

    def set_session(session):
        previous_session = app.state.session
        stop('session_reset')
        app.state.autonomy_map = None
        if app.state.session != session:
            app.state.chunk_id = 0
            app.state.occupancy = OccupancyGrid(session, calibration=calibration)
            app.state.point_memory.reset()
        app.state.session = session
        app.state.pose = app.state.pose_at = app.state.detected_at = None
        app.state.scan_observation = None
        app.state.detection_view = None
        retire_route(invalidate_pending=True)
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
        inserted = app.state.db.execute(
            'INSERT OR IGNORE INTO sessions(session_id,map_epoch,created_at_ms) VALUES(?,?,?)',
            (*session, int(time.time()*1000))).rowcount
        app.state.db.commit()
        app.state.mission_entry = load_entry(app.state.db, session)
        if previous_session != session:
            app.state.mission_entry_attempted = not inserted
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

    @app.get('/capture/detections.jpg')
    async def detection_jpeg(session_id: str, map_epoch: int, frame_id: int):
        """The exact JPEG a live `detections` message describes, while it is the newest one.

        Boxes are only valid on their own frame, so any other frame, map or a
        retired (reset/disconnected) view is refused rather than substituted.
        """
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        view = app.state.detection_view
        if view is None or (session_id, map_epoch, frame_id) != (
                view[0]['session_id'], view[0]['map_epoch'], view[0]['frame_id']):
            return Response(status_code=409, headers=headers)
        headers['X-Frame-Id'] = str(frame_id)
        return Response(view[1], media_type='image/jpeg', headers=headers)

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
        app.state.arm_request_token = None
        # Revoke the old phone before changing map identity.
        app.state.phone = None
        set_session((str(uuid4()), 1))
        return dict(version=1, session_id=app.state.session[0], map_epoch=1)

    @app.post('/stop')
    async def operator_stop():
        app.state.arm_request_token = None
        if device is not None:
            device.emergency_stop()
        stop('operator_stop')
        publish(health())
        return health()

    @app.post('/arm')
    async def arm(prepare: bool = False, standard: bool = False):
        """`standard` keeps manual mode for dashboard gestures or a confirmed voice move instead of
        prepare's Explore default; it needs only the manual prerequisites, never a route or map."""
        if standard and app.state.mode != 'manual':
            raise HTTPException(409, 'Select Standard before arming for a move')
        if app.state.arm_request_token is not None:
            raise HTTPException(409, 'Rover startup is already in progress')
        if prepare and relay is not None and app.state.mode not in ('navigate', 'explore') and not standard:
            app.state.mode = 'explore'
        # Only an explicit arm request selects the mission. A readiness gap or
        # failed handshake may stop motors without discarding that choice.
        if relay is not None and getattr(relay.actuation, 'prototype', False) and app.state.mode == 'explore':
            app.state.auto_requested = True
        return await arm_once(prepare, standard)

    async def arm_once(prepare=False, standard=False):
        if app.state.arm_request_token is not None:
            raise HTTPException(409, 'Rover startup is already in progress')
        token = object()
        app.state.arm_request_token = token
        try:
            return await prepare_and_arm(prepare, standard, token)
        except Exception:
            if app.state.auto_requested:
                app.state.auto_retry_at = time.monotonic() + EXPLORE_RETRY_S
            raise
        finally:
            if app.state.arm_request_token is token:
                app.state.arm_request_token = None

    async def prepare_and_arm(prepare, standard, token):
        if prepare and relay is not None:
            if hazard() is not None:
                if not device.snapshot()['connected']:
                    raise HTTPException(409, 'The iPhone app is offline; keep it open to connect')
                from backend.prepare_rover import prepare_rover
                deadline = time.monotonic() + 12.
                await prepare_rover(device, lambda: app.state.arm_request_token is not token)
                while hazard() is not None:
                    if app.state.arm_request_token is not token:
                        raise HTTPException(409, 'Rover startup cancelled by Stop or a mode change')
                    if time.monotonic() >= deadline:
                        raise HTTPException(409, 'Rover startup: ' + ', '.join(
                            manual_blockers() if standard else autonomy_blockers()))
                    await asyncio.sleep(.05)
            if app.state.arm_request_token is not token:
                raise HTTPException(409, 'Rover startup cancelled')
        if (reason := hazard()) is not None:
            raise HTTPException(409, reason)
        entry_pose = app.state.pose  # the exact pose accepted by this readiness check
        app.state.armed = False
        app.state.nav.halt()  # a run from before this arm must not continue under it
        generation = app.state.motion.begin()
        if generation is None:  # a fresh generation: nothing from before this arm can move
            raise HTTPException(409, 'The car did not accept a zero command')
        if relay is not None:
            try:
                await relay.prepare(app.state.motion.session_id)
                if (app.state.arm_request_token is not token
                        or generation != app.state.motion.generation or hazard() is not None):
                    raise ValueError('Stopped or lost readiness while arming')
                entry_pose = app.state.pose  # readiness was rechecked after the await
            except (ValueError, asyncio.TimeoutError) as error:
                if generation == app.state.motion.generation:
                    stop('rover_arm_failed')
                raise HTTPException(409, str(error)) from error
        # All arm awaits and generation/readiness checks have succeeded. Capture
        # the actual camera-floor projection before the watchdog can start Explore.
        # This does not add readiness or motion authority: absent metadata remains
        # unavailable, and Stop/rearm never substitutes a later entry point.
        if app.state.mode == 'explore' and not app.state.mission_entry_attempted:
            app.state.mission_entry_attempted = True
            entry = None
            if entry_pose is not None and entry_pose.tracking == 'normal':
                entry = dict(session_id=app.state.session[0], map_epoch=app.state.session[1],
                             start=[entry_pose.transform[12], entry_pose.transform[14]],
                             frame_id=entry_pose.frame_id, t_capture=entry_pose.t_capture,
                             started_at_ms=int(time.time() * 1000), basis='explore_start')
            try:
                app.state.mission_entry = record_entry(app.state.db, app.state.session, entry)
            except sqlite3.Error:
                logger.warning('Explore entry metadata could not be persisted')
        app.state.armed = True
        app.state.stop_reason = None
        return health()

    def check_explore_generation(generation):
        if (not app.state.armed or app.state.mode != 'explore'
                or generation != app.state.motion.generation):
            raise HTTPException(409, 'Explore stopped or generation changed; resume cannot arm')

    def yield_observation():
        """Fresh metric proof along the held route; labels without depth cannot clear it."""
        snapshot, pose = map_snapshot(), rover_pose()
        if snapshot is None or pose is None or map_problem(snapshot, nav_settings.map_max_age_s):
            return PathObservation(False, False, frame_id=('invalid', time.monotonic()))
        view = app.state.detection_view
        message = view[0] if view is not None else None
        frame = (snapshot.session, snapshot.accepted_at)
        if message is not None:
            frame = (snapshot.session, message.get('frame_id'))
            if ((message.get('session_id'), message.get('map_epoch')) != app.state.session
                    or not isinstance(message.get('t_wall_ms'), (int, float))
                    or not 0 <= time.time()*1000-message['t_wall_ms'] <= DETECTOR_OK_S*1000):
                return PathObservation(False, False, frame_id=frame)
        # Keep a bounded prefix of the route that existed when yield began.
        # If no plan existed yet, require fresh known floor in the facing corridor.
        points = [(pose.x, pose.z)]
        route = app.state.yield_path
        if route:
            closest = min(range(len(route)), key=lambda i: math.dist(route[i], points[0]))
            route = route[closest:]
        else:
            route = [(pose.x + .8*math.sin(pose.yaw_rad), pose.z + .8*math.cos(pose.yaw_rad))]
        remaining = .8
        for target in route:
            origin = points[-1]
            length = math.dist(origin, target)
            if length <= 1e-9:
                continue
            travel = min(length, remaining)
            for distance in np.arange(snapshot.cell_m, travel + snapshot.cell_m, snapshot.cell_m):
                fraction = min(distance, travel)/length
                points.append((origin[0]+fraction*(target[0]-origin[0]), origin[1]+fraction*(target[1]-origin[1])))
            remaining -= travel
            if remaining <= 1e-9:
                break
        known = all(snapshot.cell(*point) == 1 for point in points)
        blocked = any(not snapshot.traversable(*point) for point in points)
        for detection in message.get('detections', ()) if message else ():
            if detection.get('class') not in {'person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck'}:
                continue
            position = detection.get('position')
            if position is None:
                known = False  # unresolved image label is not a proven poster or clearance
            elif min(math.dist((position[0], position[2]), point) for point in points) <= snapshot.inflation_m:
                blocked = True
        return PathObservation(blocked, known, frame_id=frame)

    @app.post('/explore/yield')
    async def yield_explore(body: ExploreYield):
        check_explore_generation(body.generation)
        app.state.explore_yield = body.reason
        app.state.yield_path = list(app.state.nav.path)
        app.state.obstacle_gate.reset()
        # Explicit yield means brake immediately; no image threshold is invented.
        app.state.obstacle_gate.decide(PathObservation(True, True, imminent=True), time.monotonic())
        app.state.motion.desired = None
        if relay is not None:
            relay.command = None
        if not app.state.motion.zero():
            stop('car_error')
            raise HTTPException(409, 'car_error')
        publish(health())
        return health()

    @app.post('/explore/resume')
    async def release_explore(body: ExploreResume):
        check_explore_generation(body.generation)
        if (reason := hazard()) is not None:
            app.state.obstacle_gate.decide(PathObservation(False, False), time.monotonic(),
                                           external_hold=True, external_reason=reason)
            raise HTTPException(409, reason)
        decision = app.state.obstacle_gate.decide(yield_observation(), time.monotonic())
        if not decision.yielding:
            app.state.explore_yield = None
            app.state.yield_path = []
        # 200 is an accepted observation, not permission to drive: read yielding.
        return dict(health(), yielding=decision.yielding, resumed=decision.resumed_this_tick)

    @app.post('/mode')
    async def mode(body: Mode):
        app.state.arm_request_token = None
        stop('mode_change')
        app.state.mode = body.mode
        return health()

    @app.post('/manual')
    async def manual(body: Manual):
        motion = app.state.motion
        if body.takeover or body.release or body.expected_generation is not None or relay is not None:
            if (not app.state.armed or not motion.active
                    or body.expected_generation != motion.generation
                    or app.state.arm_request_token is not None):
                raise HTTPException(409, 'Manual control generation is no longer armed')
            if body.takeover:
                if body.v_mps or body.yaw_rate_rps or body.release:
                    raise HTTPException(409, 'Acquire manual control with zero first')
                if (reason := hazard()) is not None:
                    raise HTTPException(409, reason)
                app.state.auto_requested = False
                pending = app.state.auto_arm_task
                if pending is not None and pending is not asyncio.current_task():
                    pending.cancel()
                app.state.nav.halt()
                app.state.mover.halt('manual_takeover')
                app.state.nav_proposals.invalidate()
                generation = motion.takeover(body.expected_generation)
                if generation is None:
                    raise HTTPException(409, 'Manual takeover could not zero the rover')
                app.state.mode = 'manual'
                app.state.manual_generation = generation
                publish(health())
                return health()
            if app.state.mode != 'manual' or app.state.manual_generation != motion.generation:
                raise HTTPException(409, 'Acquire manual control before driving')
            if body.release:
                if body.v_mps or body.yaw_rate_rps:
                    raise HTTPException(409, 'Release manual control with zero')
                # A release retires every delayed command from this gesture.
                if motion.takeover(body.expected_generation) is None:
                    raise HTTPException(409, 'Manual release could not zero the rover')
                app.state.manual_generation = None
                publish(health())
                return health()
            reason = hazard()
            if relay is not None and reason is None and (body.v_mps or body.yaw_rate_rps):
                try:
                    relay.actuation.command(body.v_mps, body.yaw_rate_rps)
                except ValueError as error:
                    reason = str(error)
                if reason is None:
                    reason = clearance_problem(app.state.autonomy_map, rover_pose(), app.state.session,
                                               nav_settings, body.v_mps, body.yaw_rate_rps)
            if reason is not None:
                motion.desired = None
                if not motion.zero():
                    stop('car_error')
                raise HTTPException(409, reason)
        if (not app.state.armed or app.state.mode != 'manual'
                or not motion.submit(motion.generation, 'manual', body.v_mps, body.yaw_rate_rps)):
            raise HTTPException(409, 'Disarmed or not in manual mode')
        return health()

    @app.post('/goal')
    async def goal(body: Goal):
        return await begin_goal(body.x, body.z)

    async def begin_goal(x, z):
        """/goal's plan-and-follow path, shared by confirmed voice proposals."""
        # No disarmed preview: a drawn path must mean the rover is about to follow it.
        if not app.state.armed or app.state.mode != 'navigate':
            raise HTTPException(409, 'Arm in navigate mode before choosing a goal')
        session = app.state.session
        generation = app.state.motion.generation  # before the await: a stop and re-arm meanwhile must not revive it
        result = await app.state.nav.plan_once((x, z))
        if (not app.state.armed or app.state.mode != 'navigate' or app.state.session != session
                or app.state.motion.generation != generation):
            raise HTTPException(409, 'Stopped while planning')
        if result is None:
            nav_stop('pose_stale')
            raise HTTPException(409, 'No current rover pose')
        if not result.ok:
            reason = PLAN_STOP_REASONS.get(result.reason, result.reason)
            nav_stop(reason)
            raise HTTPException(409, reason)
        app.state.nav.start_goal((x, z), result, generation)
        return dict(version=1, goal=[x, z], points=result.points)

    def execution(kind):
        """Why a proposal cannot be confirmed now: a health hazard, or no deliberate arm for a destination
        or move. A move is checked against the manual prerequisites only."""
        if (reason := hazard('manual' if kind == 'move' else None)) is not None:
            return reason
        if kind == 'move' and not app.state.armed:
            return 'move_arm_required'
        return 'arm_required' if kind == 'destination' and not app.state.armed else None

    def begin_move(request, proposal_id):
        """Start one confirmed move under the current arm; the dashboard's hand-off armed it in manual."""
        if not app.state.armed or app.state.mode != 'manual':
            raise HTTPException(409, 'move_arm_required')
        if app.state.mover.active:
            raise HTTPException(409, 'move_in_progress')
        pose = app.state.nav.fresh_pose()
        if pose is None or pose.tracking != 'normal':
            raise HTTPException(409, 'pose_stale' if pose is None else 'tracking_lost')
        if app.state.manual_generation is not None:
            if app.state.motion.takeover(app.state.motion.generation) is None:
                raise HTTPException(409, 'Manual control could not be released')
            app.state.manual_generation = None
        return app.state.mover.start(request, app.state.motion.generation, proposal_id)

    def select_explore():
        stop('mode_change')
        app.state.mode = 'explore'
        publish(health())
        return health()

    register_nav_action_routes(
        app, proposals, active_session=lambda: app.state.session,
        snapshot=map_snapshot, pose=lambda: app.state.nav.fresh_pose(),
        objects=lambda session: app.state.objects.snapshot(session, limit=None),
        execution=execution, readiness=lambda kind: manual_blockers() if kind == 'move' else autonomy_blockers(),
        warnings=lambda: list(getattr(relay.actuation, 'warnings', ())) if relay else [], begin_goal=begin_goal,
        select_explore=select_explore, begin_move=begin_move)

    @app.post('/rescan')
    async def rescan():
        session = app.state.session
        if session is None:
            raise HTTPException(409, 'No active map; connect the phone or create a session first')
        started = app.state.changes.start(session, int(time.time()*1000))
        if started is None:
            raise HTTPException(409, 'Nothing observed in this map yet')
        retire_route(invalidate_pending=True)
        publish(objects_message(session))
        return dict(version=1, session_id=session[0], map_epoch=session[1], rescan_id=started.id,
                    baseline_objects=len(started.baseline))

    @app.post('/route')
    async def route(body: Route):
        """Suggested walking approach to a remembered person; visualization only.

        Never sets a rover goal, path or motion. The dashboard re-requests it when the
        map or the person's evidence changes and drops it on a map reset.
        """
        session = (body.session_id, body.map_epoch)
        if session != app.state.session:
            raise HTTPException(409, 'Route requested for a map that is no longer active')
        selected = body.purpose == 'selected'
        request = None
        selection = (*session, body.object_id, tuple(body.start))
        if selected:
            app.state.route_requests += 1
            request = app.state.route_requests
            app.state.route_selection = selection
        person = next((o for o in app.state.objects.snapshot(session, limit=None)
                       if o['id'] == body.object_id and o['class'] == 'person'), None)
        if person is None:
            if selected:
                retire_route()  # a failed selection never leaves an older route standing
            raise HTTPException(404, 'No localized person with that id in the active map')
        target = (person['position'][0], person['position'][2])
        lifecycle = app.state.route_lifecycle
        grid = active_grid()

        def unavailable(reason):
            return dict(status='unavailable', reason=reason, assumptions=APPROACH_ASSUMPTIONS)

        def snapshot():
            revision, origin, cells = (None, None, None) if grid is None else grid.snapshot()
            basis = (('no_map',) if cells is None else
                     cells_fingerprint(origin, cells.shape, cells.tobytes()))
            nav_grid = None if cells is None else Grid.from_array(cells, origin=origin, cell_m=CELL_M)
            return revision, nav_grid, basis

        def plan():
            revision, nav_grid, basis = snapshot()
            reason = person_evidence_reason(person, time.time())
            planned = unavailable(reason) if reason else approach_route(nav_grid, body.start, target)
            # Planning is off-loop: a new obstacle can arrive before it finishes.
            # Revalidate the actual path against the newest snapshot, accepting
            # unrelated map changes rather than restarting an expensive search.
            current_revision, current_grid, current_basis = snapshot()
            if current_basis != basis:
                if planned['status'] != 'ok' or not route_still_clear(current_grid, target, planned['points']):
                    planned = unavailable('map_changed')
            return current_revision, planned, current_basis

        revision, planned, basis = await asyncio.to_thread(plan)
        if app.state.session != session:
            raise HTTPException(409, 'The map changed while planning')
        current_person = next((o for o in app.state.objects.snapshot(session, limit=None)
                               if o['id'] == body.object_id and o['class'] == 'person'), None)
        if selected and request != app.state.route_requests and selection != app.state.route_selection:
            planned = unavailable('route_superseded')
        elif lifecycle != app.state.route_lifecycle:
            planned = unavailable('route_evidence_changed')
        elif current_person is None:
            planned = unavailable('person_not_in_map')
        elif (reason := person_evidence_reason(current_person, time.time())) is not None:
            planned = unavailable(reason)
        elif current_person['position'] != person['position']:
            planned = unavailable('person_changed')
        elif active_grid() is not grid or (grid is not None and grid.revision != revision):
            # Evidence changed again after validation. Fail closed without an
            # unbounded retry loop; the next explicit/new-evidence request can plan.
            planned = unavailable('map_changed')
        response = dict(version=1, session_id=session[0], map_epoch=session[1], object_id=body.object_id,
                        person=list(target), start=list(body.start), occupancy_revision=revision, **planned)
        if selected and request == app.state.route_requests and lifecycle == app.state.route_lifecycle:
            # A failed selection replaces an earlier success. A disconnected or
            # reset lifecycle cannot repopulate the read-only voice route cache.
            app.state.approach_view = dict(response, t_wall_ms=int(time.time() * 1000),
                                           person_last_seen=current_person['last_seen'] if current_person else None)
            app.state.approach_basis = basis
        return response

    @app.post('/ask')
    async def ask(body: Ask):
        session = shown_session()
        return answer_from_objects(body.question, app.state.objects.snapshot(session, limit=None), session)

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
                lambda: DENSE_MAP_INTERVAL_S if any(v.dense for v in app.state.listeners) else MAP_INTERVAL_S,
                MAP_MAX_AGE_S)))
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
                received_at, received_wall = time.monotonic(), time.time()
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
                    elif not persistent_explore():
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
                    if not persistent_explore():
                        stop('pose_stale')  # a rewound pose is not progress
                    continue
                elif pose.t_capture == last_capture:
                    continue  # repeat, or the pose half of a bundle already counted
                if pose.tracking != 'normal':
                    app.state.tracking_lost_capture = max(app.state.tracking_lost_capture, pose.t_capture)
                    grid.clear_depth_history()
                    app.state.scan_observation = None
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
                    observed_at = received_at - max(0., received_wall - pose.t_wall_ms / 1000)
                    if mailbox.put(message['bytes'], observed_at):
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
                app.state.scan_observation = None
                grid.clear_depth_history()
                app.state.phone = None
                retire_route(invalidate_pending=True)
                app.state.pose = app.state.pose_at = app.state.detected_at = None
                app.state.detection_view = None
                app.state.tracking_lost_capture = -1.0
                app.state.capture.clear()
                app.state.rich_capture.reset()
                stop('phone_disconnected')
                publish(health())

    @app.websocket('/live')
    async def live(ws: WebSocket):
        dense = POINTS_PROTOCOL in ws.scope.get('subprotocols', [])
        await ws.accept(subprotocol=POINTS_PROTOCOL if dense else None)
        listener = Listener(dense)
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
                        message = listener.points.popleft()
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)
                        else:
                            await ws.send_json(message)
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


app = create_app(weights=os.environ.get('GODSEYE_YOLO_WEIGHTS'), calibration=calibration_from_env(),
                 car=relay_from_env(), label_provider=provider_from_env(), voice_providers=voice_from_env())
