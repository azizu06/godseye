"""Session-scoped object memory over the existing SQLite schema.

`associate` is pure; `ObjectMemory` keeps every detection as its own
`observations` row and folds it into one stable `objects` record. No models,
network, or threads: callers run it on the event loop after inference.
"""
from dataclasses import dataclass
import json
import math
from typing import NamedTuple
from uuid import uuid4

from .frame_bundle import parse_frame_bundle
from .localization import Detection, LocalizedDetection, localize_all, view_status
from .labels import crop_jpeg

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
    views: tuple | None = None  # (rescan_id, ((object_id, view_status), ...)) for watched points
    crops: tuple[bytes, ...] = ()
    boxes: tuple[Detection, ...] = ()  # every 2D detection, localized or not
    image_size: tuple[int, int] = (0, 0)
    jpeg: bytes = b''  # this frame's own JPEG, for the live overlay


class Sighting(NamedTuple):
    """One stored observation and the object it was folded into."""
    observation_id: str
    object_id: str
    class_name: str
    position: tuple[float, float, float]
    created: bool  # this observation started a new object


def detect_objects(detector, payload: bytes, session_id: str, map_epoch: int, watch=None) -> FrameObjects:
    """Decode a v1 bundle against the active session/epoch, then detect and localize.

    A detector with `detect(frame)` and `confidence` (MPSDetector) yields every 2D box;
    only those with reliable same-frame depth are localized. Otherwise `localize(frame)`.

    `watch` is `(rescan_id, ((object_id, position), ...))`; each position is probed
    with `view_status` against this frame's depth. Raises FrameValidationError for
    bad bundles. Blocking (JPEG decode and inference), so async callers run it in a
    worker thread.
    """
    frame = parse_frame_bundle(payload, session_id=session_id, map_epoch=map_epoch)
    views = None if watch is None else (
        watch[0], tuple((object_id, view_status(frame, position)) for object_id, position in watch[1]))
    detect = getattr(detector, 'detect', None)
    if detect is None:  # a localize-only adapter reports just the boxes it could place
        found = tuple(detector.localize(frame))
        boxes = tuple(item.detection for item in found)
    else:
        boxes = tuple(detect(frame))
        found = tuple(localize_all(frame, boxes, min_detection_confidence=detector.confidence))
    crops = tuple(crop_jpeg(frame.image, item.detection.box) if i < 32 else b''
                  for i, item in enumerate(found))
    start = 4 + int.from_bytes(payload[:4], 'little')
    jpeg_len = json.loads(payload[4:start])['image']['jpeg_len']  # validated by the parse above
    return FrameObjects(frame.session_id, frame.map_epoch, frame.frame_id, frame.t_capture,
                        frame.t_wall_ms, found, views=views, crops=crops, boxes=boxes,
                        image_size=frame.image.size, jpeg=payload[start:start + jpeg_len])


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

    def record(self, session, frame_id, seen_at, found) -> list[Sighting]:
        """Persist one frame's detections; return one Sighting each (empty: nothing changed).

        `session` is `(session_id, map_epoch)`, which must already have a
        `frames` row for `frame_id`. `seen_at` is phone wall-clock seconds.
        A sighting marks its object `present`, except that `moved` is kept.
        """
        sightings = []
        if not found:
            return sightings
        rows = self.db.execute(
            'SELECT id, class, position_json, identity_confidence, observations, first_seen, last_seen '
            'FROM objects WHERE session_id=? AND map_epoch=?', session).fetchall()
        known = {row[0]: row for row in rows}
        matches = associate([(row[0], row[1], json.loads(row[2])) for row in rows],
                            found, self.radius_m)
        with self.db:
            for located, object_id in zip(found, matches):
                score = located.detection.confidence
                created = object_id is None
                if created:
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
                        "observations=?, state=CASE state WHEN 'moved' THEN 'moved' ELSE 'present' END WHERE id=?",
                        (json.dumps(position), (confidence * count + score) / (count + 1),
                         min(first, seen_at), max(last, seen_at), count + 1, object_id))
                observation_id = str(uuid4())
                self.db.execute(
                    'INSERT INTO observations(id,session_id,map_epoch,frame_id,class,box_mask_json,position_json,'
                    'uncertainty_json,detector_confidence,object_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (observation_id, *session, frame_id, located.detection.class_name,
                     json.dumps(dict(box=list(located.detection.box))), json.dumps(located.position),
                     json.dumps(dict(depth_m=located.depth_m, depth_samples=located.depth_samples)),
                     score, object_id))
                sightings.append(Sighting(observation_id, object_id, located.detection.class_name,
                                          tuple(located.position), created))
        return sightings

    def set_state(self, session, state, object_ids=None) -> bool:
        """Set `state` on some (default: all) objects of a session; return whether any changed.

        Runs in the caller's transaction.
        """
        query = 'UPDATE objects SET state=? WHERE session_id=? AND map_epoch=? AND state!=?'
        if object_ids is None:
            return self.db.execute(query, (state, *session, state)).rowcount > 0
        return sum(self.db.execute(query + ' AND id=?', (state, *session, state, object_id)).rowcount
                   for object_id in object_ids) > 0

    def merge(self, keep, drop, position, state):
        """Fold object `drop` into `keep`: one identity at `position` with both histories.

        Raw observations stay untouched except for their object link. Runs in the
        caller's transaction.
        """
        rows = {object_id: self.db.execute(
                    'SELECT identity_confidence, observations, first_seen, last_seen FROM objects WHERE id=?',
                    (object_id,)).fetchone() for object_id in (keep, drop)}
        (kc, kn, kf, kl), (dc, dn, df, dl) = rows[keep], rows[drop]
        self.db.execute('UPDATE observations SET object_id=? WHERE object_id=?', (keep, drop))
        self.db.execute('DELETE FROM objects WHERE id=?', (drop,))
        self.db.execute(
            'UPDATE objects SET position_json=?, identity_confidence=?, first_seen=?, last_seen=?, '
            'observations=?, state=? WHERE id=?',
            (json.dumps(list(position)), (kc * kn + dc * dn) / (kn + dn), min(kf, df), max(kl, dl),
             kn + dn, state, keep))

    def snapshot(self, session, limit=MAX_SNAPSHOT) -> list[dict]:
        """The v1 object facts, newest first; `limit=None` searches the complete map."""
        if session is None:
            return []
        rows = self.db.execute(
            'SELECT id, class, position_json, identity_confidence, first_seen, last_seen, observations, state '
            'FROM objects WHERE session_id=? AND map_epoch=? ORDER BY last_seen DESC, id' +
            (' LIMIT ?' if limit is not None else ''),
            (*session, limit) if limit is not None else session).fetchall()
        identities = {row[0]: dict(label=row[1], status=row[2], reason=row[3], source='gemini')
                      for row in self.db.execute(
                          'SELECT l.object_id,l.label,l.status,l.reason FROM object_labels l '
                          'JOIN objects o ON o.id=l.object_id WHERE o.session_id=? AND o.map_epoch=?', session)}
        return [dict(id=object_id, **{'class': name}, position=[round(v, 3) for v in json.loads(position)],
                     confidence=round(confidence, 3), first_seen=first, last_seen=last,
                     observations=count, state=state, identity=identities.get(object_id,
                         dict(label=None, status='unavailable', reason='not_requested', source=None)))
                for object_id, name, position, confidence, first, last, count, state in rows]

    def latest_session(self):
        """The most recently created session/epoch, so reads survive a backend restart."""
        row = self.db.execute('SELECT session_id, map_epoch FROM sessions '
                              'ORDER BY created_at_ms DESC, rowid DESC LIMIT 1').fetchone()
        return None if row is None else tuple(row)
