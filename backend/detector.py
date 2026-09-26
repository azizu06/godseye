"""Explicit local-weights YOLO/MPS adapter; import does not load the model."""
from pathlib import Path

from .frame_bundle import FrameBundle
from .localization import Detection, LocalizedDetection, LocalizationError, localize_detection


class MPSDetector:
    """One model per worker; callers schedule inference off the ASGI event loop."""

    def __init__(self, weights: str | Path, *, confidence: float = .25):
        if not 0 <= confidence <= 1:
            raise ValueError('confidence must be in [0,1]')
        weights = Path(weights).expanduser()
        if not weights.is_file():
            raise FileNotFoundError(f'local YOLO weights missing: {weights}')
        import torch
        from ultralytics import YOLO

        if not torch.backends.mps.is_available():
            raise RuntimeError('MPS is unavailable; detector requires Apple GPU')
        self.confidence = confidence
        self.model = YOLO(str(weights))
        self.model.to('mps')

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
                for box, score, cls in zip(coordinates, scores, classes)]

    def localize(self, frame: FrameBundle) -> list[LocalizedDetection]:
        """Reject individual boxes with inadequate depth; never fabricate positions."""
        localized = []
        for detection in self.detect(frame):
            try:
                localized.append(localize_detection(frame, detection,
                                                   min_detection_confidence=self.confidence))
            except LocalizationError:
                continue
        return localized
