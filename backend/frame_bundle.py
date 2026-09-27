"""Pure decoder for v1 RGB-D and optional ARKit floor/mesh frame extensions."""
import base64
import binascii
from dataclasses import dataclass
import io
import json
import struct
from typing import Mapping

import numpy as np
from PIL import Image, UnidentifiedImageError


class FrameValidationError(ValueError):
    """The bundle cannot safely be interpreted in the active map."""


@dataclass(frozen=True)
class FloorPlane:
    y: float
    polygon: np.ndarray  # world-space (x, z), counterclockwise or clockwise


@dataclass(frozen=True)
class FrameBundle:
    session_id: str
    map_epoch: int
    frame_id: int
    t_capture: float
    t_wall_ms: int
    transform: np.ndarray
    intrinsics: np.ndarray
    image: Image.Image
    depth: np.ndarray
    confidence: np.ndarray
    floor: FloorPlane | None = None
    mesh_points: np.ndarray | None = None  # A replaceable current mesh snapshot.


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise FrameValidationError(f'{name} must be an integer >= {minimum}')
    return value


def _number(value, name):
    if type(value) not in (int, float) or not np.isfinite(value):
        raise FrameValidationError(f'{name} must be finite')
    return value


def _matrix(values, size, name):
    if not isinstance(values, list) or len(values) != size * size:
        raise FrameValidationError(f'{name} has the wrong length')
    return np.array([_number(v, name) for v in values], dtype=float).reshape((size, size), order='F')


def validate_rigid_transform(values) -> np.ndarray:
    """Column-major camera-to-world 4x4: finite, affine, orthonormal, right-handed.

    Shared by bundle decoding and /phone health so an all-zero, scaled or
    reflected matrix can neither map points nor count as a live pose.
    """
    transform = _matrix(values, 4, 'transform')
    if (not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-5)
            or not np.allclose(transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-4)
            or not np.isclose(np.linalg.det(transform[:3, :3]), 1, atol=1e-4)):
        raise FrameValidationError('transform must be a rigid camera-to-world matrix')
    return transform


def parse_frame_bundle(payload: bytes, *, session_id: str, map_epoch: int,
                       pose: Mapping | None = None) -> FrameBundle:
    """Decode against the hello's active session/epoch; optionally match a pose.

    The bundle's own transform is authoritative. If a matching pose is supplied,
    frame, capture time and transform must agree; a latest unrelated pose is unsafe.
    All arrays own their data and are read-only. No model or network access occurs.
    """
    if not isinstance(payload, bytes) or len(payload) < 4:
        raise FrameValidationError('missing binary length prefix')
    header_len = struct.unpack_from('<I', payload)[0]
    if not 0 < header_len <= min(65536, len(payload) - 4):
        raise FrameValidationError('invalid header length')
    try:
        header = json.loads(payload[4:4 + header_len].decode('utf-8'))
        if not isinstance(header, dict):
            raise FrameValidationError('header must be an object')
        if type(header['version']) is not int or header['version'] not in (1, 2, 3) or header['type'] != 'frame':
            raise FrameValidationError('unsupported frame version/type')
        if not isinstance(header['session_id'], str) or not header['session_id']:
            raise FrameValidationError('invalid session_id')
        epoch = _integer(header['map_epoch'], 'map_epoch', 1)
        if header['session_id'] != session_id or epoch != _integer(map_epoch, 'active map_epoch', 1):
            raise FrameValidationError('frame belongs to a different session/epoch')
        frame_id = _integer(header['frame_id'], 'frame_id')
        capture = _number(header['t_capture'], 't_capture')
        if capture < 0:
            raise FrameValidationError('negative capture timestamp')
        wall = _integer(header['t_wall_ms'], 't_wall_ms')
        if header['tracking'] != 'normal':
            raise FrameValidationError('tracking is not normal')
        transform = validate_rigid_transform(header['transform'])
        floor = None
        if header['version'] >= 2 and 'floor' in header:
            raw = header['floor']
            if not isinstance(raw, dict) or set(raw) != {'y', 'polygon'}:
                raise FrameValidationError('invalid floor plane')
            y = _number(raw['y'], 'floor y')
            vertices = raw['polygon']
            if not isinstance(vertices, list) or not 3 <= len(vertices) <= 64:
                raise FrameValidationError('floor polygon must have 3–64 vertices')
            if any(not isinstance(vertex, list) or len(vertex) != 2 for vertex in vertices):
                raise FrameValidationError('invalid floor polygon vertex')
            polygon = np.array([[_number(v, 'floor polygon coordinate') for v in vertex]
                                for vertex in vertices], dtype=float)
            span = polygon.max(axis=0) - polygon.min(axis=0)
            area = abs(np.dot(polygon[:, 0], np.roll(polygon[:, 1], -1))
                       - np.dot(polygon[:, 1], np.roll(polygon[:, 0], -1))) / 2
            if (not .05 <= transform[1, 3] - y <= 1.5 or np.any(np.abs(polygon) > 100_000)
                    or np.any(span > 20.) or area < .04):
                raise FrameValidationError('floor plane is outside the camera/room bounds')
            polygon.setflags(write=False)
            floor = FloorPlane(float(y), polygon)
        elif header['version'] == 2 or 'floor' in header:
            raise FrameValidationError('floor evidence requires a v2 or v3 frame')
        mesh_points = None
        if header['version'] == 3:
            encoded = header['mesh_voxels']
            if not isinstance(encoded, str):
                raise FrameValidationError('mesh voxels must be base64')
            raw_mesh = base64.b64decode(encoded, validate=True)
            if len(raw_mesh) % 6 or len(raw_mesh) > 4000 * 6:
                raise FrameValidationError('mesh snapshot exceeds 4000 xyz voxels')
            mesh_points = np.frombuffer(raw_mesh, dtype='<i2').reshape(-1, 3).astype(np.float64) * .05
            distance = np.linalg.norm(mesh_points[:, (0, 2)] - transform[(0, 2), 3], axis=1)
            if (np.any(distance > 5.5) or
                    np.any((mesh_points[:, 1] - transform[1, 3] < -.8) |
                           (mesh_points[:, 1] - transform[1, 3] > 1.6))):
                raise FrameValidationError('mesh snapshot is outside the current camera view')
            mesh_points.setflags(write=False)
        elif 'mesh_voxels' in header:
            raise FrameValidationError('mesh snapshot requires a v3 frame')
        if pose is not None:
            # A v1 pose precedes both v1 and v2 bundles from the same ARFrame.
            if pose.get('version') != 1:
                raise FrameValidationError('alignment reference must be a v1 pose')
            for key in ('session_id', 'map_epoch', 'frame_id', 't_capture', 'transform', 'tracking'):
                if pose.get(key) != header[key]:
                    raise FrameValidationError(f'pose/frame alignment mismatch: {key}')
            if pose.get('type') != 'pose':
                raise FrameValidationError('alignment reference is not a pose')
        image, depth, confidence = (header[k] for k in ('image', 'depth', 'confidence'))
        iw, ih = (_integer(image[k], f'image {k}', 1) for k in ('width', 'height'))
        dw, dh = (_integer(depth[k], f'depth {k}', 1) for k in ('width', 'height'))
        cw, ch = (_integer(confidence[k], f'confidence {k}', 1) for k in ('width', 'height'))
        if max(iw * ih, dw * dh) > 16_000_000:
            raise FrameValidationError('image/depth dimensions exceed decoder limit')
        if (cw, ch) != (dw, dh) or iw * dh != ih * dw:
            raise FrameValidationError('image/depth/confidence dimensions are not aligned')
        if image['orientation'] != 'landscape_right':
            raise FrameValidationError('unsupported image orientation')
        intrinsics = _matrix(image['intrinsics'], 3, 'intrinsics')
        fx, fy, cx, cy = intrinsics[0, 0], intrinsics[1, 1], intrinsics[0, 2], intrinsics[1, 2]
        if (fx <= 0 or fy <= 0 or not 0 <= cx < iw or not 0 <= cy < ih
                or not np.array_equal(intrinsics, [[fx, 0, cx], [0, fy, cy], [0, 0, 1]])):
            raise FrameValidationError('invalid JPEG-scaled intrinsics')
        jpeg_len = _integer(image['jpeg_len'], 'jpeg_len', 1)
        depth_len = _integer(depth['len'], 'depth len', 1)
        conf_len = _integer(confidence['len'], 'confidence len', 1)
        if (depth['format'] != 'float32_m' or confidence['format'] != 'uint8_0_2'
                or depth_len != dw * dh * 4 or conf_len != dw * dh):
            raise FrameValidationError('invalid depth/confidence format or length')
        start = 4 + header_len
        if len(payload) != start + jpeg_len + depth_len + conf_len:
            raise FrameValidationError('bundle length does not match header')
        jpeg = payload[start:start + jpeg_len]
        with Image.open(io.BytesIO(jpeg)) as decoded:
            if decoded.format != 'JPEG' or decoded.size != (iw, ih):
                raise FrameValidationError('JPEG format/dimensions do not match header')
            decoded.load()
            rgb = decoded.convert('RGB')
        start += jpeg_len
        depths = np.frombuffer(payload[start:start + depth_len], dtype='<f4').reshape(dh, dw).copy()
        confidences = np.frombuffer(payload[start + depth_len:], dtype='u1').reshape(dh, dw).copy()
        if np.any(confidences > 2):
            raise FrameValidationError('confidence values must be 0, 1, or 2')
        for array in (transform, intrinsics, depths, confidences):
            array.setflags(write=False)
        return FrameBundle(header['session_id'], epoch, frame_id, capture, wall,
                           transform, intrinsics, rgb, depths, confidences, floor, mesh_points)
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError, OSError, OverflowError, RecursionError) as exc:
        raise FrameValidationError(f'malformed frame bundle: {exc}') from exc
