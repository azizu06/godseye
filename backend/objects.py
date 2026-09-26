"""Session-scoped object memory over the existing SQLite schema.

`associate` is pure; `ObjectMemory` keeps every detection as its own
`observations` row and folds it into one stable `objects` record. No models,
network, or threads: callers run it on the event loop after inference.
"""
from dataclasses import dataclass
import json
import math
from uuid import uuid4

from .frame_bundle import parse_frame_bundle
from .localization import LocalizedDetection

MATCH_RADIUS_M = .5  # same-class sightings closer than this are the same object
MAX_SNAPSHOT = 256  # objects per snapshot, most recently seen first


@dataclass(frozen=True)
class FrameObjects:
    session_id: str
    map_epoch: int
    frame_id: int
    t_capture: float
    t_wall_ms: int
    found: tuple[LocalizedDetection, ...]


def detect_objects(detector, payload: bytes, session_id: str, map_epoch: int) -> FrameObjects:
    """Decode a v1 bundle against the active session/epoch, then run `detector.localize`.

    Raises FrameValidationError for bad bundles. Blocking (JPEG decode and
    inference), so async callers run it in a worker thread.
    """
    frame = parse_frame_bundle(payload, session_id=session_id, map_epoch=map_epoch)
    return FrameObjects(frame.session_id, frame.map_epoch, frame.frame_id, frame.t_capture,
                        frame.t_wall_ms, tuple(detector.localize(frame)))


def associate(tracks, found, radius_m=MATCH_RADIUS_M):
    """Match one frame's detections to known objects; return an object id or None each.

    `tracks` is `(object_id, class_name, position)`. Closest same-class pairs
    within `radius_m` win, and each object takes at most one detection per
    frame, so two same-class items seen together never merge.
    """
    pairs = sorted((distance, i, object_id)
                   for i, located in enumerate(found)
                   for object_id, class_name, position in tracks
                   if class_name == located.detection.class_name
                   and (distance := math.dist(position, located.position)) <= radius_m)
    matches, taken = [None] * len(found), set()
    for _, i, object_id in pairs:
        if matches[i] is None and object_id not in taken:
            matches[i] = object_id
            taken.add(object_id)
    return matches


class ObjectMemory:
    def __init__(self, db, radius_m=MATCH_RADIUS_M):
        self.db = db
        self.radius_m = radius_m

    def record(self, session, frame_id, seen_at, found) -> bool:
        """Persist one frame's detections; return whether any object changed.

        `session` is `(session_id, map_epoch)`, which must already have a
        `frames` row for `frame_id`. `seen_at` is phone wall-clock seconds.
        """
        if not found:
            return False
        rows = self.db.execute(
            'SELECT id, class, position_json, identity_confidence, observations, first_seen, last_seen '
            'FROM objects WHERE session_id=? AND map_epoch=?', session).fetchall()
        known = {row[0]: row for row in rows}
        matches = associate([(row[0], row[1], json.loads(row[2])) for row in rows],
                            found, self.radius_m)
        with self.db:
            for located, object_id in zip(found, matches):
                score = located.detection.confidence
                if object_id is None:
                    object_id = str(uuid4())
                    self.db.execute(
                        'INSERT INTO objects(id,session_id,map_epoch,class,position_json,identity_confidence,'
                        'first_seen,last_seen,observations,state) VALUES(?,?,?,?,?,?,?,?,1,?)',
                        (object_id, *session, located.detection.class_name, json.dumps(located.position),
                         score, seen_at, seen_at, 'present'))
                else:  # associate() hands each object at most one detection per frame
                    _, _, position, confidence, count, first, last = known[object_id]
                    # Running means over all assigned observations; raw rows keep each sighting.
                    position = [(p * count + q) / (count + 1)
                                for p, q in zip(json.loads(position), located.position)]
                    self.db.execute(
                        'UPDATE objects SET position_json=?, identity_confidence=?, first_seen=?, last_seen=?, '
                        "observations=?, state='present' WHERE id=?",
                        (json.dumps(position), (confidence * count + score) / (count + 1),
                         min(first, seen_at), max(last, seen_at), count + 1, object_id))
                self.db.execute(
                    'INSERT INTO observations(id,session_id,map_epoch,frame_id,class,box_mask_json,position_json,'
                    'uncertainty_json,detector_confidence,object_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (str(uuid4()), *session, frame_id, located.detection.class_name,
                     json.dumps(dict(box=list(located.detection.box))), json.dumps(located.position),
                     json.dumps(dict(depth_m=located.depth_m, depth_samples=located.depth_samples)),
                     score, object_id))
        return True

    def snapshot(self, session) -> list[dict]:
        """The v1 `objects` list for one session/epoch, newest sighting first."""
        if session is None:
            return []
        rows = self.db.execute(
            'SELECT id, class, position_json, identity_confidence, first_seen, last_seen, observations, state '
            'FROM objects WHERE session_id=? AND map_epoch=? ORDER BY last_seen DESC, id LIMIT ?',
            (*session, MAX_SNAPSHOT)).fetchall()
        return [dict(id=object_id, **{'class': name}, position=[round(v, 3) for v in json.loads(position)],
                     confidence=round(confidence, 3), first_seen=first, last_seen=last,
                     observations=count, state=state)
                for object_id, name, position, confidence, first, last, count, state in rows]

    def latest_session(self):
        """The most recently created session/epoch, so reads survive a backend restart."""
        row = self.db.execute('SELECT session_id, map_epoch FROM sessions '
                              'ORDER BY created_at_ms DESC, rowid DESC LIMIT 1').fetchone()
        return None if row is None else tuple(row)
