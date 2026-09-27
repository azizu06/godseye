"""Bounded stationary RGB-D opportunities for Explore, never motion authority."""
from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class CameraPose:
    session: tuple[str, int]
    owner: object
    captured_at: float
    position: tuple[float, float, float]
    rotation: tuple[float, ...]

    @classmethod
    def from_transform(cls, session, owner, captured_at, transform):
        matrix = np.asarray(transform).reshape((4, 4), order='F')
        return cls(session, owner, captured_at, tuple(matrix[:3, 3]), tuple(matrix[:3, :3].ravel()))

    def angle(self, other):
        cosine = (sum(a * b for a, b in zip(self.rotation, other.rotation)) - 1.) / 2.
        return math.acos(max(-1., min(1., cosine)))

    def same_stream(self, other):
        return self.session == other.session and self.owner == other.owner

    def stable_with(self, other):
        return (self.same_stream(other) and math.dist(self.position, other.position) <= .03
                and self.angle(other) <= math.radians(5))


@dataclass(frozen=True)
class ScanObservation:
    pose: CameraPose
    frame_id: int
    observed_at: float  # monotonic receipt minus validated positive wall age, never commit time
    keys: frozenset[tuple[int, int, int]]
    usable: bool


def capture_observation(frame, candidates):
    """Worker-only bounded metric; excludes floor anchors, mesh and display dedup."""
    pose = CameraPose.from_transform((frame.session_id, frame.map_epoch), None,
                                     frame.t_capture, frame.transform)
    if frame.depth.size > 65_536:
        return ScanObservation(pose, frame.frame_id, -math.inf, frozenset(), False)
    valid = ((frame.confidence >= 2) & np.isfinite(frame.depth)
             & (frame.depth >= .05) & (frame.depth <= 5.))
    count = int(np.count_nonzero(valid))
    usable = count >= 64 and count / frame.depth.size >= .15
    points = candidates.positions if candidates is not None else np.empty((0, 3))
    if len(points) > 2048:
        points = points[np.linspace(0, len(points) - 1, 2048).astype(np.int64)]
    keys = frozenset(map(tuple, np.floor(points / .1).astype(np.int64).tolist()))
    return ScanObservation(pose, frame.frame_id, -math.inf, keys, usable and len(keys) >= 16)


class ScanPacer:
    """At most 2s per measured checkpoint; no claim of complete surfaces or sharp color."""
    def __init__(self):
        self.active = False
        self.attempt = None
        self.attempt_at = -math.inf
        self.anchor = None
        self.stable_since = 0.
        self.watermark = None
        self.last_capture = -math.inf
        self.seen = set()
        self.status = dict(phase='moving', result=None, stable_frames=0, checkpoints=0)

    def finish(self, reason):
        self.active = False
        self.seen.clear()
        self.status.update(phase='moving', result=reason)

    def step(self, now, pose, observation, allowed):
        """True requests idle-zero. False leaves all navigation decisions unchanged."""
        if self.active and (not allowed or pose is None or not pose.same_stream(self.anchor)):
            self.finish('interrupted')
            return False
        if not self.active:
            if not allowed or pose is None:
                return False
            if self.attempt is not None:
                progressed = (math.dist(pose.position, self.attempt.position) >= 1.
                              or pose.angle(self.attempt) >= math.radians(60))
                if now - self.attempt_at < 5. or not progressed:
                    return False
            self.active = True
            self.attempt = self.anchor = pose
            self.attempt_at = self.stable_since = now
            self.watermark = None
            self.last_capture = pose.captured_at
            self.seen.clear()
            self.status.update(phase='settling', result=None, stable_frames=0,
                               checkpoints=self.status['checkpoints'] + 1)
        if now - self.attempt_at >= 2.:
            self.finish('capture_limited')
            return False
        if not pose.stable_with(self.anchor):
            self.anchor = pose
            self.stable_since = now
            self.watermark = None
            self.seen.clear()
            self.status.update(phase='settling', stable_frames=0)
        if now - self.stable_since < .5 or pose.captured_at - self.anchor.captured_at < .5:
            return True
        if self.watermark is None:
            # This is the latest sensor capture after the measured settle interval.
            # A queued pre-stop image cannot earn credit merely by finishing later.
            self.watermark = pose.captured_at
            self.status['phase'] = 'capturing'
        if observation is None:
            return True
        captured = observation.pose.captured_at
        if (captured <= max(self.watermark, self.last_capture)
                or not observation.pose.stable_with(self.anchor)
                or now - observation.observed_at > 1. or now < observation.observed_at
                or not observation.usable):
            return True
        self.last_capture = captured
        gain = len(observation.keys - self.seen)
        if len(self.seen | observation.keys) > 8192:
            self.finish('support_limit')
            return False
        self.seen.update(observation.keys)
        self.status['stable_frames'] += 1
        if self.status['stable_frames'] >= 3 and gain <= max(8, len(observation.keys) * .03):
            self.finish('stable_support')
            return False
        return True
