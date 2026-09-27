"""Mounted-phone setup using existing non-movement phone actions."""
import asyncio
import time
from fastapi import HTTPException
from backend.device_relay import DeviceAction


async def prepare_rover(device, cancelled, timeout=10.):
    deadline = time.monotonic() + timeout
    requested = set()
    while True:
        if cancelled():
            raise HTTPException(409, 'Rover startup cancelled')
        snapshot = device.snapshot()
        phone = snapshot['phone'] if snapshot['connected'] else None
        if phone is None:
            raise HTTPException(409, 'The iPhone app is offline; keep it open to connect')
        if time.monotonic() >= deadline:
            raise HTTPException(409, 'Phone setup: ' + phone['rover_status'])
        action = peer = None
        if not phone['capture_running']:
            action = 'capture_start'
        elif not phone['rover_verified']:
            if not phone['rover_connected']:
                peers = phone['peers']
                if len(peers) == 1:
                    action, peer = 'rover_select', peers[0]['id']
                elif len(peers) > 1:
                    raise HTTPException(409, 'Multiple rovers found; select yours in Mounted phone')
                else:
                    action = 'rover_scan'
        elif not phone['control_enabled']:
            action = 'control_enable'
        else:
            return
        identity = (action, peer)
        if action and identity not in requested:
            try:
                await device.action(DeviceAction(action=action, peer_id=peer))
                requested.add(identity)
            except HTTPException as error:
                # ARKit/BLE readiness can settle between status and the action.
                # Retry setup within this same explicit click, never motor work.
                if error.status_code != 409 or action not in ('capture_start', 'control_enable'):
                    raise
        await asyncio.sleep(.05)
