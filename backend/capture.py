"""Bounded, in-memory preview of accepted phone frames; no recording or inference."""
from collections import deque
from dataclasses import dataclass
import time
from uuid import uuid4


@dataclass(frozen=True)
class CaptureFrame:
    token: str
    payload: bytes
    metadata: dict
    received_at: float

    @property
    def jpeg(self) -> bytes:
        start = 4 + int.from_bytes(self.payload[:4], 'little')
        return self.payload[start:start + self.metadata['image']['jpeg_len']]


class CaptureBuffer:
    def __init__(self):
        self.clear()

    def clear(self):
        self.latest: CaptureFrame | None = None
        self.receipts = deque(maxlen=120)
        self.count = 0

    def update(self, payload: bytes, metadata: dict):
        now = time.monotonic()
        self.latest = CaptureFrame(uuid4().hex, payload, metadata, now)
        self.receipts.append(now)
        self.count += 1

    def status(self) -> dict:
        now = time.monotonic()
        recent = [stamp for stamp in self.receipts if now - stamp <= 2]
        hz = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 1 and recent[-1] > recent[0] else 0.0
        frame = self.latest
        return dict(frame=None if frame is None else dict(
            capture_id=frame.token, age_ms=(now - frame.received_at) * 1000,
            metadata=frame.metadata), received_frames=self.count, frame_hz=hz)
