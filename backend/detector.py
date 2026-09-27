"""Explicit local-weights YOLO/MPS adapter; import does not load the model."""
import os
from pathlib import Path

from .frame_bundle import FrameBundle
from .localization import Detection, LocalizedDetection, localize_all

# COCO classes plausible inside a school building. Everything else the stock weights can
# emit (animals, food, vehicles, outdoor fixtures) is a false positive indoors.
INDOOR_CLASSES = frozenset((
    'person', 'backpack', 'handbag', 'suitcase', 'umbrella', 'bottle', 'cup', 'chair', 'couch',
    'potted plant', 'dining table', 'bench', 'tv', 'laptop', 'mouse', 'remote', 'keyboard',
    'cell phone', 'book', 'clock', 'scissors', 'refrigerator', 'microwave', 'sink', 'sports ball'))


def detector_classes_from_env():
    """`GODSEYE_DETECTOR_CLASSES`: comma list, `all` for every class; default the indoor set."""
    raw = (os.environ.get('GODSEYE_DETECTOR_CLASSES') or '').strip()
    if not raw:
        return INDOOR_CLASSES
    if raw.lower() == 'all':
        return None
    return frozenset(name.strip() for name in raw.split(',') if name.strip())


class MPSDetector:
    """One model per worker; callers schedule inference off the ASGI event loop."""

    def __init__(self, weights: str | Path, *, confidence: float = .25, classes=INDOOR_CLASSES):
        if not 0 <= confidence <= 1:
            raise ValueError('confidence must be in [0,1]')
        weights = Path(weights).expanduser()
        if not weights.is_file():
            raise FileNotFoundError(f'local YOLO weights missing: {weights}')
        # Ultralytics skips its online checks, asset downloads and telemetry when offline.
        os.environ.setdefault('YOLO_OFFLINE', '1')
        import torch
        from ultralytics import YOLO

        if not torch.backends.mps.is_available():
            raise RuntimeError('MPS is unavailable; detector requires Apple GPU')
        self.confidence = confidence
        self.classes = classes  # None keeps every class the weights emit
        self.model = YOLO(str(weights))
        self.model.to('mps')

    @property
    def class_names(self) -> tuple[str, ...]:
        """Every class these weights can emit."""
        return tuple(self.model.names.values())

    def detect(self, frame: FrameBundle) -> list[Detection]:
        """Ultralytics xyxy boxes are in original JPEG pixels, after letterbox undo."""
        result = self.model.predict(source=frame.image, device='mps', conf=self.confidence,
                                    verbose=False, save=False)[0]
        if result.orig_shape != (frame.image.height, frame.image.width):
            raise RuntimeError('detector output is not aligned with the JPEG')
        if result.boxes is None:
            return []
        coordinates = result.boxes.xyxy.cpu().tolist()
        scores = result.boxes.conf.cpu().tolist()
        classes = result.boxes.cls.cpu().tolist()
        return [Detection(tuple(box), result.names[int(cls)], float(score))
                for box, score, cls in zip(coordinates, scores, classes)
                if self.classes is None or result.names[int(cls)] in self.classes]

    def localize(self, frame: FrameBundle) -> list[LocalizedDetection]:
        """Reject individual boxes with inadequate depth; never fabricate positions."""
        return localize_all(frame, self.detect(frame), min_detection_confidence=self.confidence)
