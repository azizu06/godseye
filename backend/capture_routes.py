"""Full sensor upload and inspection, separate from the frozen v1 drive transport."""
import asyncio
import time

from fastapi import HTTPException, Request
from fastapi.responses import Response

from backend.rich_capture import KINDS, MAX_PACKET, RichPacket, decode_rich, render_sensor
from backend.surface_preview import has_surface, surface_payload

IMAGE_SENSORS = {'rgb', 'raw_depth', 'raw_confidence', 'smoothed_depth',
                 'smoothed_confidence', 'person_mask', 'person_depth'}
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


def legacy_packet(frame):
    """Expose unchanged v1 buffers to the same inspection renderer."""
    meta = frame.metadata
    start = 4 + int.from_bytes(frame.payload[:4], 'little')
    offset, sections = 0, []
    for name, field, format, length_key in [('rgb', 'image', 'jpeg', 'jpeg_len'),
            ('raw_depth', 'depth', 'f32le', 'len'), ('raw_confidence', 'confidence', 'u8', 'len')]:
        value = meta[field]
        length = value[length_key]
        sections.append(dict(name=name, format=format, offset=offset, length=length,
                             shape=[value['height'], value['width']]))
        offset += length
    return RichPacket(frame.payload, dict(meta, sections=sections), start, frame.token, frame.received_at)


def register_capture_routes(app):
    @app.get('/capture/surface.bin')
    async def surface(request: Request):
        state = app.state

        def eligible(header, metadata, received_at):
            now = time.monotonic()
            return (state.phone is not None and state.pose is not None and
                    state.pose_at is not None and now - state.pose_at <= .25 and
                    state.pose.tracking == 'normal' and metadata.get('tracking') == 'normal' and
                    (header['session_id'], header['map_epoch']) == state.session and
                    header['t_capture'] > state.tracking_lost_capture and
                    header['t_capture'] >= state.pose.t_capture - 1 and
                    now - received_at <= 1)

        rich, legacy = state.rich_capture.latest.get('frame'), state.capture.latest
        if rich is not None and (not has_surface(rich) or not eligible(
                rich.header, rich.header['metadata'], rich.received_at)):
            rich = None
        if legacy is not None and not eligible(legacy.metadata, legacy.metadata, legacy.received_at):
            legacy = None
        use_rich = rich is not None and (legacy is None or
                                        rich.header['t_capture'] >= legacy.metadata['t_capture'] - .2)
        selected = rich if use_rich else legacy
        if selected is None:
            return Response(status_code=204, headers=HEADERS)
        tag = f'"surface-{selected.token}"'
        headers = dict(HEADERS, ETag=tag)
        if request.headers.get('if-none-match') == tag:
            return Response(status_code=304, headers=headers)
        owner = state.phone
        payload = await asyncio.to_thread(surface_payload, rich) if use_rich else legacy.payload
        header, metadata = (rich.header, rich.header['metadata']) if use_rich else (legacy.metadata, legacy.metadata)
        # A reset or tracking loss during packing must not publish retired geometry.
        if state.phone is not owner or not eligible(header, metadata, selected.received_at):
            return Response(status_code=204, headers=HEADERS)
        headers['X-Capture-Age-Ms'] = str((time.monotonic() - selected.received_at) * 1000)
        return Response(payload, media_type='application/octet-stream', headers=headers)

    @app.post('/capture/ingest')
    async def ingest(request: Request):
        owner, session = app.state.phone, app.state.session
        if owner is None or session is None:
            raise HTTPException(409, 'Connect the v1 /phone session before uploading capture data')
        # RichUploader sends one request at a time. Bound concurrent ingress and
        # disk work without delaying pose messages or admitting an unbounded queue.
        if app.state.capture_ingest_lock.locked():
            raise HTTPException(429, 'A capture upload is already being processed')
        async with app.state.capture_ingest_lock:
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_PACKET:
                    raise HTTPException(413, 'Capture packet exceeds 32 MiB')
                data.extend(chunk)
            try:
                packet = await asyncio.to_thread(decode_rich, bytes(data))
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
                raise HTTPException(400, f'Invalid capture packet: {error}') from error
            if (app.state.phone is not owner or app.state.session != session or
                    (packet.header['session_id'], packet.header['map_epoch']) != session):
                raise HTTPException(409, 'Capture belongs to an inactive session')
            if abs(time.time() * 1000 - packet.header['t_wall_ms']) > 15_000:
                raise HTTPException(409, 'Capture upload is more than 15 seconds old or clocks disagree')
            capture = app.state.rich_capture
            try:
                capture.accept(packet)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            used, folder, error = await asyncio.to_thread(capture.record, packet)
            if app.state.phone is not owner or app.state.session != session:
                raise HTTPException(409, 'Session changed during recording')
            capture.recorded_bytes, capture.recording_folder, capture.recording_error = used, folder, error
            return dict(kind=packet.header['kind'], bytes=len(packet.data), recorded_bytes=used,
                        recording_enabled=capture.directory is not None, recording_error=error)

    @app.get('/capture/rich/{kind}.bin')
    async def download(kind: str):
        packet = app.state.rich_capture.latest.get(kind) if kind in KINDS else None
        if packet is None:
            raise HTTPException(404, 'No such capture packet has arrived in this session')
        return Response(packet.data, media_type='application/octet-stream', headers=dict(HEADERS,
            **{'Content-Disposition': f'attachment; filename="godseye-{kind}-{packet.header["frame_id"]}.capture"'}))

    @app.get('/capture/sensor/{name}')
    async def sensor(name: str, request: Request):
        if name not in IMAGE_SENSORS:
            raise HTTPException(404, 'Unknown image sensor')
        packet = app.state.rich_capture.latest.get('frame')
        if packet is None and app.state.capture.latest is not None:
            packet = legacy_packet(app.state.capture.latest)
        if packet is None or packet.descriptor(name) is None:
            return Response(status_code=204, headers=HEADERS)
        tag = f'"{packet.token}-{name}"'
        headers = dict(HEADERS, ETag=tag, **{'X-Frame-Id': str(packet.header['frame_id'])})
        if request.headers.get('if-none-match') == tag:
            return Response(status_code=304, headers=headers)
        try:
            preview = await asyncio.to_thread(render_sensor, packet, name)
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error
        return Response(preview, media_type='image/jpeg' if name == 'rgb' else 'image/png', headers=headers)
