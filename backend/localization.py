"""Pure bounding-box depth localization in ARKit world meters."""
from dataclasses import dataclass

import numpy as np

from .frame_bundle import FrameBundle


class LocalizationError(ValueError):
    """A detection lacks reliable depth or valid pixel geometry."""


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]  # JPEG xyxy, continuous pixel edges
    class_name: str
    confidence: float


@dataclass(frozen=True)
class LocalizedDetection:
    detection: Detection
    position: tuple[float, float, float]
    depth_m: float
    depth_samples: int
    session_id: str
    map_epoch: int
    frame_id: int
    t_capture: float


def localize_detection(frame: FrameBundle, detection: Detection, *,
                       min_samples: int = 3, min_high_fraction: float = .25,
                       min_detection_confidence: float = .25) -> LocalizedDetection:
    """Project box-center ray at median valid high-confidence (2) depth.

    Select depth pixels by their centers within the JPEG box, preserving the
    native orientation. Ignore NaN/Inf/nonpositive depth and confidence 0/1.
    Require both a sample count and coverage so isolated pixels cannot anchor
    a mostly unreliable box. This is a representative surface point, not an
    object centroid or association result.
    """
    if type(min_samples) is not int or min_samples < 1 or not 0 < min_high_fraction <= 1:
        raise ValueError('invalid depth quality thresholds')
    if not 0 <= min_detection_confidence <= 1:
        raise ValueError('invalid detection confidence threshold')
    box = np.asarray(detection.box, dtype=float)
    if box.shape != (4,) or not np.all(np.isfinite(box)):
        raise LocalizationError('box must contain four finite JPEG coordinates')
    if not np.isfinite(detection.confidence) or not min_detection_confidence <= detection.confidence <= 1:
        raise LocalizationError('detection confidence is too low or invalid')
    iw, ih = frame.image.size
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= iw and 0 <= y1 < y2 <= ih):
        raise LocalizationError('box is empty or outside JPEG bounds')
    dh, dw = frame.depth.shape
    # A depth sample at index i represents JPEG coordinate (i+.5)*scale.
    left, right = np.ceil(np.array([x1, x2]) * dw / iw - .5).astype(int)
    top, bottom = np.ceil(np.array([y1, y2]) * dh / ih - .5).astype(int)
    depths = frame.depth[top:bottom, left:right]
    confidences = frame.confidence[top:bottom, left:right]
    valid = (confidences == 2) & np.isfinite(depths) & (depths > 0)
    count = int(valid.sum())
    if count < min_samples or count < depths.size * min_high_fraction:
        raise LocalizationError('insufficient valid high-confidence depth')
    depth_m = float(np.median(depths[valid]))
    u, v = (x1 + x2) / 2, (y1 + y2) / 2
    k = frame.intrinsics
    # Optical +X right,+Y down,+Z forward -> ARKit +X right,+Y up,-Z forward.
    camera = np.array([(u - k[0, 2]) * depth_m / k[0, 0],
                       -(v - k[1, 2]) * depth_m / k[1, 1], -depth_m, 1])
    world = frame.transform @ camera
    if not np.all(np.isfinite(world)):
        raise LocalizationError('projection produced a nonfinite world point')
    return LocalizedDetection(detection, tuple(float(v) for v in world[:3]), depth_m,
                              count, frame.session_id, frame.map_epoch, frame.frame_id, frame.t_capture)


def view_status(frame: FrameBundle, position, *, margin_m: float = .15, max_depth_m: float = 5.,
                min_samples: int = 3) -> str:
    """How this frame's depth sees a remembered world point, with `localize_detection` geometry.

    'clear': the median high-confidence depth in the 3x3 cells around its pixel lies more than
    `margin_m` beyond it, so nothing solid stands there now (a surface point cannot be seen
    through). 'surface': depth ends within `margin_m` of it. 'occluded': something nearer
    blocks the view. 'out_of_view': behind the camera, off the image, too far, or too few
    reliable samples. Pose error larger than `margin_m` makes this unreliable.
    """
    camera = np.linalg.inv(frame.transform) @ np.array([*position, 1.])
    depth = -camera[2]  # ARKit camera looks along -Z
    if not .05 < depth <= max_depth_m:
        return 'out_of_view'
    k = frame.intrinsics
    u = k[0, 2] + camera[0] * k[0, 0] / depth
    v = k[1, 2] - camera[1] * k[1, 1] / depth
    iw, ih = frame.image.size
    if not (0 <= u < iw and 0 <= v < ih):
        return 'out_of_view'
    dh, dw = frame.depth.shape
    row, col = int(v * dh / ih), int(u * dw / iw)
    cells = np.s_[max(row - 1, 0):row + 2, max(col - 1, 0):col + 2]
    depths = frame.depth[cells]
    reliable = depths[(frame.confidence[cells] == 2) & np.isfinite(depths) & (depths > 0)]
    if reliable.size < min_samples:
        return 'out_of_view'
    measured = float(np.median(reliable))
    if measured < depth - margin_m:
        return 'occluded'
    return 'clear' if measured > depth + margin_m else 'surface'


def localize_all(frame: FrameBundle, detections, *, min_detection_confidence: float = .25,
                 **thresholds) -> list[LocalizedDetection]:
    """Localize each detection; skip boxes with inadequate depth, never fabricate positions."""
    localized = []
    for detection in detections:
        try:
            localized.append(localize_detection(frame, detection, **thresholds,
                                                min_detection_confidence=min_detection_confidence))
        except LocalizationError:
            continue
    return localized
