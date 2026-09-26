"""Lossless selection of the RGB-D bytes needed by the surface viewer."""
import json
import struct

from backend.rich_capture import RichPacket

SURFACE_SECTIONS = ('rgb', 'raw_depth', 'raw_confidence')


def has_surface(packet: RichPacket) -> bool:
    """Require same-frame raw depth/confidence; never mix different captures."""
    meta = packet.header['metadata']
    if meta.get('tracking') != 'normal' or not isinstance(meta.get('native_image'), dict):
        return False
    if not isinstance(meta.get('transform'), list) or len(meta['transform']) != 16:
        return False
    for key in ('session_id', 'map_epoch', 'frame_id', 't_capture'):
        if key in meta and meta[key] != packet.header[key]:
            return False
    rgb, depth, confidence = (packet.descriptor(name) for name in SURFACE_SECTIONS)
    return (rgb is not None and depth is not None and confidence is not None and
            rgb['format'] == 'jpeg' and depth['format'] == 'f32le' and confidence['format'] == 'u8' and
            len(depth['shape']) == 2 and depth['shape'] == confidence['shape'] and
            rgb['shape'] == [meta['native_image'].get('height'), meta['native_image'].get('width')])


def surface_payload(packet: RichPacket) -> bytes:
    """Repack sections without decoding, recompressing, or changing source bytes.

    The original packet remains available to recording and full-capture downloads.
    This deliberately does not copy motion, features, or lossless color planes.
    """
    header = {key: packet.header[key] for key in
              ('version', 'type', 'kind', 'session_id', 'map_epoch', 'frame_id', 't_capture', 't_wall_ms')}
    header['metadata'] = {key: packet.header['metadata'][key] for key in
                          ('native_image', 'transform', 'tracking')}
    sections, body, offset = [], [], 0
    for name in SURFACE_SECTIONS:
        descriptor = packet.descriptor(name)
        if descriptor is None:
            raise ValueError('Missing surface section')
        start = packet.body_offset + descriptor['offset']
        data = memoryview(packet.data)[start:start + descriptor['length']]
        sections.append(dict(descriptor, offset=offset))
        body.append(data)
        offset += len(data)
    header['sections'] = sections
    encoded = json.dumps(header, separators=(',', ':'), allow_nan=False).encode()
    return b''.join([struct.pack('<I', len(encoded)), encoded, *body])
