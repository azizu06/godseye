"""Pure depth-to-world point chunks for the live map; no I/O, models, or globals."""
from dataclasses import dataclass

import numpy as np

from .frame_bundle import FrameBundle, parse_frame_bundle


class MappingError(ValueError):
    """A frame carries too little reliable depth to contribute map points."""


@dataclass(frozen=True)
class PointChunk:
    positions: np.ndarray  # (N, 3) ARKit world meters, float64, read-only
    colors: np.ndarray  # (N, 3) float 0..1, read-only
    session_id: str
    map_epoch: int
    frame_id: int
    t_capture: float


def depth_to_points(frame: FrameBundle, *, max_points: int = 2500, min_points: int = 16,
                    min_depth_m: float = .05, max_depth_m: float = 5.) -> PointChunk:
    """Back-project high-confidence depth pixels to ARKit world meters.

    Keeps finite depth in [min_depth_m, max_depth_m] whose confidence is 2, then
    takes at most max_points evenly spaced samples (deterministic, bounded output).
    Depth pixel centers map to JPEG coordinates with independent width/height
    scales; intrinsics are already JPEG-scaled. Geometry matches
    localization.localize_detection: optical (x, y, z) -> ARKit camera (x, -y, -z)
    -> camera-to-world. Colors are the JPEG pixel under each depth pixel center.
    """
    if type(max_points) is not int or max_points < 1 or type(min_points) is not int or min_points < 1:
        raise ValueError('invalid point limits')
    if not 0 < min_depth_m < max_depth_m:
        raise ValueError('invalid depth range')
    depth, confidence = frame.depth, frame.confidence
    dh, dw = depth.shape
    with np.errstate(invalid='ignore'):  # NaN compares false, which is the point
        valid = (confidence == 2) & (depth >= min_depth_m) & (depth <= max_depth_m)
    indices = np.flatnonzero(valid)
    if indices.size < min_points:
        raise MappingError('insufficient valid high-confidence depth')
    if indices.size > max_points:
        indices = indices[np.linspace(0, indices.size - 1, max_points).astype(np.int64)]
    rows, cols = np.divmod(indices, dw)
    z = depth.ravel()[indices].astype(np.float64)
    iw, ih = frame.image.size
    u = (cols + .5) * iw / dw
    v = (rows + .5) * ih / dh
    k = frame.intrinsics
    camera = np.stack([(u - k[0, 2]) * z / k[0, 0], -(v - k[1, 2]) * z / k[1, 1], -z,
                       np.ones_like(z)])
    world = (frame.transform @ camera)[:3].T
    if not np.all(np.isfinite(world)):
        raise MappingError('projection produced nonfinite world points')
    pixels = np.asarray(frame.image)
    colors = pixels[np.minimum(v.astype(np.int64), ih - 1),
                    np.minimum(u.astype(np.int64), iw - 1)].astype(np.float64) / 255
    world.setflags(write=False)
    colors.setflags(write=False)
    return PointChunk(world, colors, frame.session_id, frame.map_epoch, frame.frame_id, frame.t_capture)


def build_point_chunk(payload: bytes, session_id: str, map_epoch: int, **limits) -> PointChunk:
    """Decode a v1 bundle against the active session/epoch, then back-project it.

    Raises FrameValidationError or MappingError; blocking (JPEG decode), so
    async callers run it in a worker thread.
    """
    return depth_to_points(parse_frame_bundle(payload, session_id=session_id, map_epoch=map_epoch),
                           **limits)


def points_message(chunk: PointChunk, chunk_id: int) -> dict:
    """The `/live` `points` message: mm-rounded flat positions, colors 0..1.

    session_id, map_epoch, frame_id and t_capture are additive fields so the
    dashboard can clear its cloud when the map identity changes.
    """
    return dict(version=1, type='points', chunk_id=chunk_id, session_id=chunk.session_id,
                map_epoch=chunk.map_epoch, frame_id=chunk.frame_id, t_capture=chunk.t_capture,
                positions=np.round(chunk.positions, 3).ravel().tolist(),
                colors=np.round(chunk.colors, 3).ravel().tolist())
