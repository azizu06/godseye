"""Paired dashboard setup controls for a mounted iPhone. No motor commands.

The phone must keep the app foregrounded. Capture/connection actions never arm
the backend; the calibrated /arm and /goal path remains separate.
"""
import asyncio
from contextlib import suppress
import time
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeviceAction(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    action: Literal['capture_start', 'capture_stop', 'rover_scan', 'rover_select',
                    'rover_disconnect', 'control_enable', 'control_disable', 'stop']
    peer_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode='after')
    def selection(self):
        if (self.action == 'rover_select') != (self.peer_id is not None):
            raise ValueError('Only rover_select requires peer_id')
        return self


class Peer(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)


class DeviceStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    version: Literal[1]
    type: Literal['status']
    seq: int = Field(ge=1)
    capture_running: bool
    capture_status: str = Field(max_length=512)
    tracking: str = Field(max_length=256)
    network: str = Field(max_length=512)
    rover_connected: bool
    rover_verified: bool
    rover_status: str = Field(max_length=512)
    control_enabled: bool
    control_status: str = Field(max_length=512)
    peers: list[Peer] = Field(max_length=12)


class DeviceRelay:
    def __init__(self, authorized):
        self.authorized = authorized
        self.connected = False
        self.phone = None
        self.received = 0.
        self.revision = 0
        self.pending = None
        self.reply = None
        self.wake = asyncio.Event()

    def snapshot(self):
        age = (time.monotonic() - self.received) * 1000 if self.phone else None
        fresh = self.connected and age is not None and age < 1500
        return dict(version=1, connected=fresh, age_ms=age,
                    phone=self.phone.model_dump() if fresh else None)

    def invalidate(self):
        self.revision += 1
        self.pending = None
        if self.reply is not None and not self.reply[1].done():
            self.reply[1].set_result(dict(ok=False, message='Phone action interrupted'))
        self.reply = None

    def emergency_stop(self):
        self.invalidate()
        if self.connected:
            self.pending = (dict(version=1, type='action', id=uuid4().hex, action='stop'), time.monotonic())
            self.wake.set()

    async def action(self, body):
        if not self.snapshot()['connected']:
            raise HTTPException(409, 'Open and pair the iPhone app first')
        if self.reply is not None:
            raise HTTPException(409, 'Another phone action is pending')
        ident = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.reply = ident, future
        self.pending = (dict(version=1, type='action', id=ident, **body.model_dump(exclude_none=True)),
                        time.monotonic())
        self.wake.set()
        try:
            answer = await asyncio.wait_for(future, 3.)
            if not answer['ok']:
                raise HTTPException(409, answer['message'])
            return answer
        except asyncio.TimeoutError as error:
            raise HTTPException(504, 'Phone did not acknowledge the action') from error
        finally:
            if self.reply is not None and self.reply[0] == ident:
                self.reply = None
                self.pending = None

    async def serve(self, ws):
        if self.connected or not self.authorized(ws.headers.get('authorization')):
            await ws.close(code=1008)
            return
        await ws.accept()
        self.invalidate()
        self.connected, self.phone = True, None

        async def sender():
            while True:
                try:
                    await asyncio.wait_for(self.wake.wait(), .2)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
                message = dict(version=1, type='heartbeat')
                if self.pending is not None:
                    candidate, queued = self.pending
                    self.pending = None
                    if candidate['action'] == 'stop' or time.monotonic() - queued < .5:
                        message = candidate
                await asyncio.wait_for(ws.send_json(message), .2)

        async def receiver():
            import json
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), 2.5)
                if len(raw) > 8192: raise ValueError('Oversized phone status')
                message = json.loads(raw)
                if not isinstance(message, dict): raise ValueError('Invalid phone status')
                if message.get('type') == 'status':
                    status = DeviceStatus.model_validate(message)
                    if self.phone is not None and status.seq <= self.phone.seq:
                        raise ValueError('Replayed phone status')
                    self.phone, self.received = status, time.monotonic()
                elif (message.get('version') == 1 and message.get('type') == 'ack'
                      and type(message.get('ok')) is bool and isinstance(message.get('message'), str)
                      and len(message['message']) <= 512):
                    if self.reply is not None and message.get('id') == self.reply[0] and not self.reply[1].done():
                        self.reply[1].set_result(dict(ok=message['ok'], message=message['message']))
                else:
                    raise ValueError('Invalid phone reply')

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
        except (WebSocketDisconnect, ValueError, asyncio.TimeoutError, RuntimeError):
            pass
        finally:
            self.connected, self.phone = False, None
            self.invalidate()
            for task in tasks: task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, WebSocketDisconnect, ValueError, asyncio.TimeoutError, RuntimeError):
                    await task
            with suppress(RuntimeError): await ws.close(code=1001)
