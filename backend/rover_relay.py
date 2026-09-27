"""Opt-in laptop → iPhone → BLE adapter. No radio ownership on the laptop.

Every movement echoes an ESP-generated, expiring permit unchanged. Local queue
age is bounded independently. Writes are never treated as Uno feedback. See
docs/AUTONOMY.md for the separate /rover protocol and physical prerequisites.
"""
import asyncio
from contextlib import suppress
import hmac
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from fastapi import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from backend.actuation import load_actuation
from backend.motion import DriveCommand, DriveStop

MAX_DISPATCH_AGE_MS = 150
STATUS_MAX_S = .2


class RoverStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    version: Literal[1]
    type: Literal['status']
    seq: int = Field(ge=1)
    session_id: str = Field(min_length=1, max_length=64)
    map_epoch: int = Field(ge=1)
    permit: str = Field(pattern=r'^[0-9A-F]{16}$')
    uno_age_ms: float = Field(ge=0, le=1500)
    enabled: Literal[True]


class RelayCar:
    """One opted-in phone, one non-buffering motion slot and a reliable arm barrier.

    All methods run on the application's asyncio loop. Synchronous adapter
    methods only replace local state; the WebSocket task owns I/O. Disconnect,
    stop and timeout invalidate pending work and never reopen a drive session.
    """
    def __init__(self, key, actuation, *, clock=time.monotonic):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', key):
            raise ValueError('Rover pairing key must contain 32–128 URL-safe characters')
        self.key, self.actuation, self.clock = key, actuation, clock
        self.identity = lambda: None
        self.on_loss = lambda reason: None
        self.connected = False
        self.status = None
        self.status_at = -float('inf')
        self.last_status_seq = 0
        self.last_permit = None
        self.revision = 0
        self.armed_session = None
        self.last_command_seq = 0
        self.control = None
        self.command = None
        self.wake = asyncio.Event()
        self.changed = asyncio.Event()
        self.ack = None
        self.stop_id = None
        self.awaiting = None

    def authorized(self, header):
        return isinstance(header, str) and hmac.compare_digest(header, 'Bearer ' + self.key)

    def blockers(self):
        result = list(self.actuation.blockers)
        if not self.connected:
            result.append('rover_relay_disconnected')
        elif not self.status or self.clock() - self.status_at > STATUS_MAX_S:
            result.append('rover_feedback_stale')
        elif (self.status.session_id, self.status.map_epoch) != self.identity():
            result.append('rover_capture_mismatch')
        return result

    def health(self):
        if not self.connected:
            return 'down'
        return 'stale' if self.blockers() else 'ok'

    def attach(self):
        if self.connected:
            raise ValueError('Another phone already owns the rover relay')
        self.connected = True
        self.status = None
        self.last_status_seq = 0
        self.last_permit = None
        self.zero(DriveStop(None, 0, 0))

    def detach(self):
        self.connected = False
        self.status = None
        self.zero(DriveStop(None, 0, 0))
        self.on_loss('rover_relay_lost')

    def receive(self, message):
        if not isinstance(message, dict) or message.get('version') != 1:
            raise ValueError('Invalid rover relay message')
        kind = message.get('type')
        if kind == 'status':
            status = RoverStatus.model_validate(message)
            if status.seq <= self.last_status_seq or status.permit == self.last_permit:
                raise ValueError('Replayed rover feedback')
            self.last_status_seq, self.last_permit = status.seq, status.permit
            self.status, self.status_at = status, self.clock()
        elif kind == 'ack':
            if set(message) != {'version', 'type', 'id'} or not isinstance(message['id'], str):
                raise ValueError('Invalid firmware acknowledgement')
            if self.awaiting is not None and message['id'] == self.awaiting:
                self.ack = (message['id'], self.last_status_seq)
                self.awaiting = None
        elif kind == 'retired':
            self.zero(DriveStop(None, 0, 0))
            self.on_loss('rover_session_retired')
        else:
            raise ValueError('Unknown rover relay message')
        self.changed.set()
        self.wake.set()

    def zero(self, envelope):
        if isinstance(envelope, DriveCommand):
            self.send(envelope)
            return
        self.revision += 1
        self.armed_session = None
        self.last_command_seq = 0
        self.command = None
        self.ack = None
        self.awaiting = None
        self.stop_id = uuid4().hex[:20].upper()
        self.control = dict(version=1, type='stop', id=self.stop_id)
        self.wake.set()
        self.changed.set()

    def send(self, command):
        if (self.health() != 'ok' or self.armed_session != command.session_id.upper()
                or not 0 < command.seq <= 0xFFFFFFFF or command.seq <= self.last_command_seq):
            raise ValueError('Rover is not armed for this command')
        age = round(self.clock() * 1000) - command.issued_at_ms
        if age < 0:
            raise ValueError('Rover command timestamp is in the future')
        if age > min(MAX_DISPATCH_AGE_MS, command.valid_for_ms):
            # Never forward or renew an expired sample. A newer desired value
            # may follow; the firmware still stops without timely fresh input.
            self.command = None
            self.last_command_seq = command.seq
            return
        motor = self.actuation.command(command.v_mps, command.yaw_rate_rps)
        self.command = (command, motor)
        self.last_command_seq = command.seq
        self.wake.set()

    async def prepare(self, session):
        """Called only by explicit /arm after Motion.begin has queued its Stop."""
        if not re.fullmatch(r'[0-9a-fA-F]{32}', session) or self.health() != 'ok':
            raise ValueError('Rover cannot arm')
        revision, stop_id = self.revision, self.stop_id
        deadline = self.clock() + 2.

        async def wait_for(predicate):
            while True:
                self.changed.clear()
                if revision != self.revision or self.health() != 'ok':
                    raise ValueError('Stopped or lost feedback during arm')
                if predicate():
                    return
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ValueError('Rover arm acknowledgement timed out')
                try:
                    await asyncio.wait_for(self.changed.wait(), min(.1, remaining))
                except asyncio.TimeoutError:
                    pass

        # Stop and Arm each invalidate ESP permits. A subsequent status is
        # essential; reusing the permit accompanying an acknowledgement fails.
        await wait_for(lambda: self.ack is not None and self.ack[0] == 'Z' + stop_id
                       and self.last_status_seq > self.ack[1])
        session = session.upper()
        self.ack = None
        self.control = dict(version=1, type='arm', session=session)
        self.wake.set()
        await wait_for(lambda: self.ack is not None and self.ack[0] == 'A' + session
                       and self.last_status_seq > self.ack[1])
        self.armed_session = session

    def next_message(self):
        """Recheck at actual dispatch, after any socket backpressure."""
        if self.control is not None:
            message, self.control = self.control, None
            if message['type'] == 'stop':
                self.awaiting = 'Z' + message['id']
            else:
                if self.health() != 'ok':
                    self.zero(DriveStop(None, 0, 0))
                    return self.next_message()
                message = dict(message, permit=self.status.permit)
                self.awaiting = 'A' + message['session']
            return message
        if self.command is None:
            return None
        command, motor = self.command
        self.command = None
        age = round(self.clock() * 1000) - command.issued_at_ms
        if (self.health() != 'ok' or self.armed_session != command.session_id.upper() or age < 0):
            self.zero(DriveStop(None, 0, 0))
            self.on_loss('rover_dispatch_stale')
            return self.next_message()
        if age > min(MAX_DISPATCH_AGE_MS, command.valid_for_ms):
            return None
        return dict(version=1, type='command', session=self.armed_session,
                    seq=command.seq, permit=self.status.permit,
                    direction=motor.direction if motor else 0, power=motor.pwm if motor else 0,
                    lease_ms=200)

    async def serve(self, ws):
        if not self.authorized(ws.headers.get('authorization')) or self.connected:
            await ws.close(code=1008)
            return
        await ws.accept()
        self.attach()

        async def sender():
            clock = asyncio.get_running_loop().time
            last_sent = clock()
            while True:
                remaining = .1 - (clock() - last_sent)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(self.wake.wait(), remaining)
                    except asyncio.TimeoutError:
                        pass
                self.wake.clear()
                while (message := self.next_message()) is not None:
                    await asyncio.wait_for(ws.send_json(message), .15)
                    last_sent = clock()
                # Incoming permits wake the sender at 20 Hz. They are not
                # outbound traffic and must never postpone the next heartbeat.
                if clock() - last_sent >= .1:
                    await asyncio.wait_for(ws.send_json(dict(version=1, type='heartbeat')), .15)
                    last_sent = clock()

        async def receiver():
            while True:
                # Idle transport may survive Wi-Fi jitter. Motion still requires
                # <200 ms feedback, fresh permits, and the existing arm barrier.
                timeout = .4 if self.armed_session is not None else 3.
                text = await asyncio.wait_for(ws.receive_text(), timeout)
                if len(text) > 2048:
                    raise ValueError('Rover message too large')
                import json
                self.receive(json.loads(text))

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, ValueError, asyncio.TimeoutError, RuntimeError):
            pass
        finally:
            self.detach()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, WebSocketDisconnect, ValueError,
                              asyncio.TimeoutError, RuntimeError):
                    await task
            with suppress(RuntimeError):
                await ws.close(code=1001)


def relay_from_env(environ=os.environ):
    """Explicit opt-in; absent configuration leaves the logging-only default."""
    if environ.get('GODSEYE_CAR_ADAPTER', 'logging') == 'logging':
        return None
    if environ['GODSEYE_CAR_ADAPTER'] != 'iphone':
        raise ValueError('GODSEYE_CAR_ADAPTER must be logging or iphone')
    key = Path(environ['GODSEYE_ROVER_KEY_FILE']).read_text().strip()
    return RelayCar(key, load_actuation(environ['GODSEYE_ACTUATION_CALIBRATION']))
