"""Version-2 full sensor packets, exact-byte recording, and inspection previews."""
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import struct
import time
from uuid import uuid4

import numpy as np
from PIL import Image

MAX_PACKET = 32 * 1024 * 1024
MAX_HEADER = 2 * 1024 * 1024
KINDS = {'frame', 'telemetry', 'geometry', 'still'}
ITEM_BYTES = {'u8': 1, 'u16le': 2, 'u32le': 4, 'u64le': 8, 'f32le': 4}
SESSION_BUDGET = 8 * 1024**3


def integer(value, minimum=0, maximum=2**53):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid capture integer')
    return value


@dataclass(frozen=True)
class RichPacket:
    data: bytes
    header: dict
    body_offset: int
    token: str
    received_at: float

    def descriptor(self, name):
        return next((entry for entry in self.header['sections'] if entry['name'] == name), None)

    def section(self, name):
        item = self.descriptor(name)
        if item is None:
            return None
        start = self.body_offset + item['offset']
        return self.data[start:start + item['length']]


def decode_rich(data: bytes) -> RichPacket:
    if not 4 < len(data) <= MAX_PACKET:
        raise ValueError('full capture packet size is invalid')
    size = struct.unpack_from('<I', data)[0]
    if not 0 < size <= min(MAX_HEADER, len(data) - 4):
        raise ValueError('full capture header size is invalid')

    def reject_constant(value):
        raise ValueError('nonfinite JSON value')

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite JSON value')
        return result

    header = json.loads(data[4:4 + size], parse_constant=reject_constant, parse_float=finite_float)
    if not isinstance(header, dict) or type(header.get('version')) is not int or header.get('version') != 2 or header.get('type') != 'capture':
        raise ValueError('unsupported full capture version/type')
    if header.get('kind') not in KINDS:
        raise ValueError('unsupported full capture kind')
    if not isinstance(header.get('session_id'), str) or not 0 < len(header['session_id']) <= 128:
        raise ValueError('invalid session identity')
    integer(header['map_epoch'], 1)
    integer(header['frame_id'])
    integer(header['t_wall_ms'])
    capture = header['t_capture']
    if type(capture) not in (int, float) or not math.isfinite(capture) or capture < 0:
        raise ValueError('invalid capture timestamp')
    if not isinstance(header['metadata'], dict) or not isinstance(header['sections'], list) or len(header['sections']) > 8192:
        raise ValueError('invalid metadata or section collection')
    offset, names = 0, set()
    for item in header['sections']:
        name = item['name']
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', name) or name in names:
            raise ValueError('invalid or repeated section name')
        names.add(name)
        if integer(item['offset'], maximum=MAX_PACKET) != offset:
            raise ValueError('section offsets must be contiguous')
        length = integer(item['length'], maximum=MAX_PACKET)
        shape = item['shape']
        if not isinstance(shape, list) or not 1 <= len(shape) <= 3:
            raise ValueError('invalid section shape')
        for axis in shape:
            integer(axis, maximum=MAX_PACKET)
        count = math.prod(shape)
        if item['format'] == 'jpeg':
            if len(shape) != 2 or not 0 < count <= 64_000_000 or length == 0:
                raise ValueError('invalid image shape')
        elif item['format'] in ITEM_BYTES:
            if length != count * ITEM_BYTES[item['format']]:
                raise ValueError('section shape and bytes disagree')
        else:
            raise ValueError('unsupported section format')
        offset += length
    if 4 + size + offset != len(data):
        raise ValueError('full capture payload length mismatch')
    return RichPacket(data, header, 4 + size, uuid4().hex, time.monotonic())


class RichCapture:
    def __init__(self, directory: str | Path | None):
        self.directory = Path(directory) if directory else None
        self.reset()

    def reset(self):
        self.latest = {}
        self.received = 0
        self.bytes_received = 0
        self.png_cache = {}
        self.recording_error = None
        self.recorded_bytes = 0
        self.recording_folder = None

    def accept(self, packet: RichPacket):
        kind = packet.header['kind']
        previous = self.latest.get(kind)
        if previous and packet.header['t_capture'] <= previous.header['t_capture']:
            raise ValueError('duplicate or out-of-order full capture sample')
        self.latest[kind] = packet
        self.received += 1
        self.bytes_received += len(packet.data)
        if kind == 'frame':
            self.png_cache.clear()

    def status(self):
        now = time.monotonic()
        result = {}
        for kind, packet in self.latest.items():
            metadata = dict(packet.header['metadata'])
            # High-rate batches and geometry are downloadable; avoid copying them
            # into every UI status poll. Keep last measured values and counts.
            samples = metadata.pop('samples', [])
            metadata.pop('anchors', None)
            metadata['sample_count'] = len(samples)
            result[kind] = dict(token=packet.token, frame_id=packet.header['frame_id'],
                t_capture=packet.header['t_capture'], age_ms=(now - packet.received_at) * 1000,
                metadata=metadata, section_count=len(packet.header['sections']),
                sections=[] if kind == 'geometry' else packet.header['sections'], bytes=len(packet.data))
        return dict(packets=result, received_packets=self.received, bytes_received=self.bytes_received,
                    recording_enabled=self.directory is not None, recording_error=self.recording_error,
                    recorded_bytes=self.recorded_bytes, session_budget_bytes=SESSION_BUDGET,
                    recording_folder=self.recording_folder)

    def record(self, packet: RichPacket):
        """Run off the event loop, one write at a time per active phone upload."""
        if self.directory is None:
            return 0, None, None
        identity = f"{packet.header['session_id']}:{packet.header['map_epoch']}"
        name = hashlib.sha256(identity.encode()).hexdigest()[:24]
        folder = self.directory / name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            # The persisted count survives a server restart/reconnect. No image
            # values or caller paths are used as filesystem paths.
            sizes = folder / 'recording.json'
            used = json.loads(sizes.read_text())['bytes'] if sizes.exists() else sum(p.stat().st_size for p in folder.glob('*.capture'))
            if used + len(packet.data) > SESSION_BUDGET:
                return used, name, 'Laptop recording reached its 8 GiB session limit'
            if shutil.disk_usage(folder).free < len(packet.data) + 1024**3:
                return used, name, 'Laptop recording stopped: less than 1 GiB free space'
            target = folder / f"{packet.header['kind']}-{packet.header['frame_id']}-{packet.token}.capture"
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(packet.data)
            temporary.replace(target)
            used += len(packet.data)
            summary = dict(bytes=used, session_id=packet.header['session_id'], map_epoch=packet.header['map_epoch'],
                           last_packet=target.name, updated_at_ms=int(time.time() * 1000))
            temporary = sizes.with_suffix('.tmp')
            temporary.write_text(json.dumps(summary))
            temporary.replace(sizes)
            return used, name, None
        except (OSError, ValueError, KeyError) as error:
            return 0, name, f'Laptop recording failed: {error}'


def render_sensor(packet: RichPacket, name: str) -> bytes:
    item = packet.descriptor(name)
    data = packet.section(name)
    if item is None or data is None or len(item['shape']) != 2:
        raise ValueError('requested image sensor is unavailable')
    if item['format'] == 'jpeg':
        return data
    height, width = item['shape']
    if not 0 < width * height <= 4_000_000:
        raise ValueError('preview dimensions exceed limit')
    if item['format'] == 'f32le':
        values = np.frombuffer(data, dtype='<f4').reshape(height, width)
        valid = np.isfinite(values) & (values > 0)
        t = np.clip(np.where(valid, values, 0) / 5, 0, 1)
        rgb = np.stack([255 * (1-t), 255 * (1-np.abs(2*t-1)), 255*t], axis=-1).astype('uint8')
        rgb[~valid] = 0
    elif name.endswith('confidence'):
        values = np.frombuffer(data, dtype='u1').reshape(height, width)
        palette = np.array([[65, 65, 65], [245, 178, 66], [91, 230, 161]], dtype='uint8')
        rgb = palette[np.minimum(values, 2)]
        rgb[values > 2] = 0
    else:
        values = np.frombuffer(data, dtype='u1').reshape(height, width)
        rgb = (values > 0)[..., None] * np.array([140, 185, 255], dtype='uint8')
    output = io.BytesIO()
    Image.fromarray(rgb).save(output, format='PNG')
    return output.getvalue()
