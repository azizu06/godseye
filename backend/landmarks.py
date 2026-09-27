"""Voice go-to for a named visual landmark the detector has no class for ("the door", "the red chair").

A small bounded ring keeps recent downscaled RGB-D frames of the active map only. On a spoken
`propose_landmark`, the answer model is asked where that open-vocabulary thing appears in a few of
them; the chosen pixel is unprojected with that frame's own depth and pose, and the nearest
rover-clear reachable floor short of it becomes an ordinary `propose_navigation` point. The result
is only a suggestion: the dashboard re-checks it at /nav/propose and only a person's confirmation
can act on it (NAV_ACTIONS.md). Nothing here is persisted or logged; frames live only in memory.
"""
from __future__ import annotations

import asyncio
import io
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

MAX_FRAMES = 24  # ring size per map; older frames are dropped
MIN_INTERVAL_S = 1.  # at most one kept frame per second
MAX_SIDE_PX = 640  # kept JPEGs are downscaled to this longest side
MAX_JPEG_BYTES = 200_000
MAX_DEPTH_PIXELS = 256 * 256  # ARKit scene depth is 256x192; larger depth is not kept
MAX_LOCATE_FRAMES = 8  # frames sent to the model per request, newest first
MAX_HITS = 8
MIN_HIT_CONFIDENCE = .5
PATCH = 2  # depth median over a (2*PATCH+1)^2 patch around the pixel
MIN_PATCH_SAMPLES = 5
MIN_DEPTH_M, MAX_DEPTH_M = .1, 8.
STANDOFF_M = .5  # stop at least this far from the landmark's visible surface (never below inflation)
APPROACH_WINDOW_M = 1.5  # search for clear floor up to this far from it
LOCATE_S = 12
NAME_RE = re.compile(r"[a-z0-9][a-z0-9 '-]{0,59}")
MAX_NAME_WORDS = 8


def landmark_name(value):
    """A short open-vocabulary noun phrase as said (qualifiers kept), lowercased; else None."""
    if not isinstance(value, str):
        return None
    name = ' '.join(value.lower().split())
    for article in ('the ', 'a ', 'an '):
        name = name.removeprefix(article)
    if not NAME_RE.fullmatch(name) or len(name.split()) > MAX_NAME_WORDS:
        return None
    return name


@dataclass(frozen=True)
class LandmarkFrame:
    frame_id: int
    t_wall_ms: int
    jpeg: bytes
    size: tuple[int, int]  # (width, height) of `jpeg`
    intrinsics: np.ndarray  # scaled to `jpeg`
    transform: np.ndarray  # camera-to-world, column-major decoded
    depth: np.ndarray
    confidence: np.ndarray


def keep_frame(frame) -> LandmarkFrame | None:
    """A downscaled copy of a parsed FrameBundle for the ring, or None when it is too large to keep."""
    dh, dw = frame.depth.shape
    if dh * dw > MAX_DEPTH_PIXELS:
        return None
    image = frame.image.copy()
    iw, ih = image.size
    image.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    w, h = image.size
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=75)
    jpeg = buffer.getvalue()
    if len(jpeg) > MAX_JPEG_BYTES:
        return None
    k = np.array(frame.intrinsics, dtype=float)
    k[0] *= w / iw
    k[1] *= h / ih
    k[2] = (0, 0, 1)
    arrays = [np.array(a) for a in (k, frame.transform, frame.depth, frame.confidence)]
    for array in arrays:
        array.setflags(write=False)
    return LandmarkFrame(frame.frame_id, frame.t_wall_ms, jpeg, (w, h), *arrays)


class LandmarkFrames:
    """Recent frames of one map, at most MAX_FRAMES, one per MIN_INTERVAL_S; reset on any map change.

    Offered from the mapping worker thread and read from the event loop, so guarded by a lock.
    Worst case memory is about MAX_FRAMES * (MAX_JPEG_BYTES + 5 * MAX_DEPTH_PIXELS) bytes (~12 MB).
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._session = None
        self._frames: deque[LandmarkFrame] = deque(maxlen=MAX_FRAMES)
        self._last = -math.inf

    def _reset(self, session):
        self._session, self._last = session, -math.inf
        self._frames.clear()

    def due(self, session) -> bool:
        with self._lock:
            return session != self._session or self.clock() - self._last >= MIN_INTERVAL_S

    def offer(self, session, frame) -> bool:
        """Keep this parsed frame of `session` if one is due; True when kept."""
        if not self.due(session):
            return False
        kept = keep_frame(frame)
        with self._lock:
            if session != self._session:
                self._reset(session)
            if kept is None or self.clock() - self._last < MIN_INTERVAL_S:
                return False
            self._frames.append(kept)
            self._last = self.clock()
            return True

    def frames(self, session) -> list[LandmarkFrame]:
        """Newest first, only for the map they were captured in; another map's frames are dropped."""
        with self._lock:
            if session is None or session != self._session:
                self._reset(session)
                return []
            return list(reversed(self._frames))

    def clear(self):
        with self._lock:
            self._reset(None)


class LandmarkError(Exception):
    pass


def unproject(frame: LandmarkFrame, u: float, v: float):
    """World (x, y, z) of normalized JPEG point (u, v) in [0, 1], from the median of a small depth patch.

    Same conventions as mapping.depth_to_points: optical (x, y, z) -> ARKit camera (x, -y, -z) ->
    camera-to-world; intrinsics are scaled to the kept JPEG; depth pixel i covers JPEG (i + .5) * scale.
    Refuses invalid, low-confidence, too-near or too-far depth rather than guessing.
    """
    if not (0 <= u <= 1 and 0 <= v <= 1):
        raise LandmarkError('outside the image')
    iw, ih = frame.size
    dh, dw = frame.depth.shape
    col, row = min(int(u * dw), dw - 1), min(int(v * dh), dh - 1)
    rows = slice(max(0, row - PATCH), row + PATCH + 1)
    cols = slice(max(0, col - PATCH), col + PATCH + 1)
    depths, confidences = frame.depth[rows, cols], frame.confidence[rows, cols]
    with np.errstate(invalid='ignore'):
        valid = (confidences >= 1) & np.isfinite(depths) & (depths >= MIN_DEPTH_M) & (depths <= MAX_DEPTH_M)
    if int(valid.sum()) < MIN_PATCH_SAMPLES:
        raise LandmarkError('no reliable depth there')
    z = float(np.median(depths[valid]))
    k = frame.intrinsics
    px, py = u * iw, v * ih
    camera = np.array([(px - k[0, 2]) * z / k[0, 0], -(py - k[1, 2]) * z / k[1, 1], -z, 1.])
    world = (frame.transform @ camera)[:3]
    if not np.all(np.isfinite(world)):
        raise LandmarkError('nonfinite projection')
    return tuple(float(c) for c in world)


def parse_hits(reply, count):
    """Valid model hits as (frame index, u, v, confidence), best first; newest frame wins a tie.

    The model answers {"hits": [{"frame": i, "point": [y, x], "confidence": c}]} with points
    normalized to 0-1000 ([y, x], Gemini's own point convention). Anything malformed is dropped.
    """
    raw = reply.get('hits') if isinstance(reply, dict) else None
    if not isinstance(raw, list):
        return []
    hits = []
    for hit in raw[:MAX_HITS]:
        if not isinstance(hit, dict):
            continue
        index, point, confidence = hit.get('frame'), hit.get('point'), hit.get('confidence')
        if type(index) is not int or not 0 <= index < count:
            continue
        if not isinstance(point, list) or len(point) != 2 or \
                not all(type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1000 for p in point):
            continue
        if type(confidence) not in (int, float) or not MIN_HIT_CONFIDENCE <= confidence <= 1:
            continue
        hits.append((index, point[1] / 1000, point[0] / 1000, float(confidence)))
    return sorted(hits, key=lambda h: (-h[3], h[0]))


def sample_frames(frames, limit=MAX_LOCATE_FRAMES):
    """At most `limit` frames spread over the ring, newest first, always keeping the newest."""
    n = len(frames)
    if n <= limit:
        return list(frames)
    return [frames[i] for i in sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})]


def not_seen(name):
    return f"I can't see a {name} in what I've mapped so far. Nothing was suggested."


def _meters(distance):
    return 'less than a meter' if distance < 1 else 'about 1 meter' if distance < 1.5 else \
        f'about {round(distance)} meters'


def _lower(text):
    return text[:1].lower() + text[1:]


def choose_destination(name, target_xz, snapshot, pose, settings, validate):
    """(args, None) for a rover-clear, planner-reachable floor point short of the landmark, else (None, text).

    Worker-thread half: the same map snapshot and pose checks as /nav/propose, then the nearest
    clear floor at least STANDOFF_M (and never less than the rover inflation) from the landmark,
    re-validated through NavProposals.validate exactly as the dashboard's card will be.
    """
    from backend.nav_actions import approach_point, reason_text
    from backend.navigator import map_problem, planning_grid
    problem = map_problem(snapshot, settings.map_max_age_s)
    if problem is None and pose is None:
        problem = 'pose_stale'
    if problem is None and pose.tracking != 'normal':
        problem = 'tracking_lost'
    if problem:
        return None, f'I can see the {name}, but {_lower(reason_text(problem))} Nothing was suggested.'
    start = (pose.x, pose.z)
    grid, config = planning_grid(snapshot, settings)
    point = approach_point(grid, config, start, target_xz, min_m=max(STANDOFF_M, snapshot.inflation_m or 0.),
                           max_m=APPROACH_WINDOW_M)
    if point is None:
        return None, (f'I can see the {name}, but no rover-clear mapped floor reaches near it. '
                      'Nothing was suggested.')
    args = {'target': 'point', 'x': round(float(point[0]), 3), 'z': round(float(point[1]), 3), 'landmark': name}
    reason, _ = validate('destination', args, snapshot, pose, [])
    if reason:
        return None, f'I can see the {name}, but {_lower(reason_text(reason))} Nothing was suggested.'
    distance = math.dist(start, target_xz)
    return args, f'The {name} is {_meters(distance)} away. Say go to drive there.'


async def find_landmark(name, *, frames, locator, snapshot, pose, settings, validate):
    """(propose_navigation args, spoken text) or (None, spoken refusal) for one named landmark.

    `frames` are this map's ring frames newest first; `locator.locate(name, jpegs)` is the answer
    model's bounded image query; `snapshot()` is the blocking navigation map; `pose()` the fresh
    rover pose; `validate` is NavProposals.validate. Every failure is a spoken reason, never a guess.
    """
    frames = sample_frames(frames)
    if not frames:
        return None, f"I haven't received any camera frames for this map yet, so I can't look for the {name}."
    if locator is None or not hasattr(locator, 'locate'):
        return None, 'Looking for things in camera frames is unavailable. Nothing was suggested.'
    try:
        reply = await asyncio.wait_for(locator.locate(name, [f.jpeg for f in frames]), LOCATE_S)
    except Exception:
        return None, f"I couldn't look for the {name} right now. Nothing was suggested."
    hits = parse_hits(reply, len(frames))
    target = None
    for index, u, v, _ in hits:
        try:
            x, _, z = unproject(frames[index], u, v)
        except LandmarkError:
            continue
        target = (x, z)
        break
    if target is None:
        return None, (not_seen(name) if not hits else
                      f'I may see the {name}, but I have no reliable depth for it. Get the camera closer '
                      'and ask again. Nothing was suggested.')
    return await asyncio.to_thread(lambda: choose_destination(name, target, snapshot(), pose(), settings, validate))
