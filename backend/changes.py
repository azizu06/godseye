"""Same-session rescan: a frozen baseline, independent revisit clusters, durable change events.

`start` freezes each remembered object's newest raw observations as a baseline cluster.
Observations from frames captured after that moment form separate revisit clusters, so
neither side is the objects table's running mean. Identity and movement rules are
deliberately conservative; see backend/README.md (Rescan and change events).
Callers run everything on one thread, after `ObjectMemory.record`.
"""
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import asdict, dataclass, field
from itertools import combinations
import json
import math
import statistics
from uuid import uuid4

CLUSTER_WINDOW = 20  # newest raw observations per object that form its cluster
CONSISTENT_M = .3  # members this close to the newest sightings' median agree with them
MIN_CONSISTENT = 3  # agreeing observations, one per frame, that confirm a cluster
DISPLACEMENT_M = .6  # smallest reportable move, raised by the two clusters' spread
ALIGN_TOL_M = .15  # reobserved static references must sit this close to their baseline;
# no looser than view_status's margin, so a verified map keeps its depth probes meaningful
MIN_REFERENCES = 2  # static references that must agree before a move is confirmed
ABSENCE_VIEWS = 3  # frames that must see through a remembered spot before it counts as empty
MAX_BASELINE = 256  # objects frozen per rescan, most recently seen first
MAX_TRACKED = 256  # revisit clusters kept in memory, least recently seen dropped first
MAX_EVENTS = 256  # events listed per session, newest kept


@dataclass(frozen=True)
class Cluster:
    position: tuple[float, float, float]  # coordinate-wise median of the agreeing members
    spread_m: float  # RMS distance of those members from `position`
    count: int  # agreeing members
    observation_id: str  # the member nearest `position`


@dataclass(frozen=True)
class Remembered(Cluster):
    object_id: str
    class_name: str


def _median(points):
    return tuple(statistics.median(axis) for axis in zip(*points))


def cluster(members):
    """Summarize `(observation_id, position)` members, newest first, around the newest sightings."""
    seed = _median([position for _, position in members[:MIN_CONSISTENT]])
    near = [(observation_id, position) for observation_id, position in members
            if math.dist(position, seed) <= CONSISTENT_M]
    if not near:
        return None
    center = _median([position for _, position in near])
    spread = math.sqrt(sum(math.dist(position, center) ** 2 for _, position in near) / len(near))
    nearest = min(near, key=lambda member: math.dist(member[1], center))[0]
    return Cluster(center, spread, len(near), nearest)


def confirmed(summary):
    return summary is not None and summary.count >= MIN_CONSISTENT


@dataclass
class Rescan:
    id: str
    session: tuple[str, int]
    after_t_capture: float  # frames captured later than this are revisit evidence
    baseline: dict[str, Remembered]
    revisit: OrderedDict = field(default_factory=OrderedDict)  # object_id -> (class, members)
    born: set = field(default_factory=set)  # objects first seen during the revisit
    reobserved: set = field(default_factory=set)  # baseline objects seen again
    views: defaultdict = field(default_factory=lambda: defaultdict(Counter))  # object_id -> statuses
    emitted: set = field(default_factory=set)  # (kind, object_id) already recorded


class ChangeTracker:
    def __init__(self, db, objects):
        self.db = db
        self.objects = objects
        self.session = None
        self.rescan = None
        self.watching = None  # (rescan_id, ((object_id, position), ...)): positions to depth-probe

    def activate(self, session):
        """Follow a session/epoch: resume its latest rescan from the database, if any."""
        if session == self.session:
            return
        self.session, self.rescan = session, None
        row = self.db.execute(
            'SELECT id, after_t_capture, baseline_json FROM rescans WHERE session_id=? AND map_epoch=? '
            'ORDER BY started_at_ms DESC, rowid DESC LIMIT 1', session).fetchone()
        if row is not None:
            rescan_id, after, baseline = row
            self.rescan = Rescan(rescan_id, session, after, {
                entry['object_id']: Remembered(**dict(entry, position=tuple(entry['position'])))
                for entry in json.loads(baseline)})
            self._load_revisit()
        self._watch()

    def start(self, session, started_at_ms):
        """Freeze a baseline from this session's stored objects; None before any frame arrived.

        The cut is the newest stored capture time, so work still in flight for
        earlier frames never counts as revisit evidence.
        """
        self.activate(session)
        (after,) = self.db.execute('SELECT MAX(t_capture) FROM frames WHERE session_id=? AND map_epoch=?',
                                   session).fetchone()
        members = defaultdict(list)
        names = {}
        for object_id, name, observation_id, position in self.db.execute(
                'SELECT object_id, class, id, position_json FROM ('
                ' SELECT o.object_id, o.class, o.id, o.position_json, ROW_NUMBER() OVER ('
                '  PARTITION BY o.object_id ORDER BY f.t_capture DESC, o.rowid DESC) AS n'
                ' FROM observations o JOIN frames f USING (session_id, map_epoch, frame_id)'
                ' WHERE o.session_id=? AND o.map_epoch=? AND o.object_id IN ('
                '  SELECT id FROM objects WHERE session_id=? AND map_epoch=? ORDER BY last_seen DESC, id LIMIT ?))'
                ' WHERE n<=? ORDER BY object_id, n', (*session, *session, MAX_BASELINE, CLUSTER_WINDOW)):
            members[object_id].append((observation_id, tuple(json.loads(position))))
            names[object_id] = name
        baseline = {}
        for object_id, rows in members.items():
            summary = cluster(rows)
            if summary is not None:
                baseline[object_id] = Remembered(**asdict(summary), object_id=object_id, class_name=names[object_id])
        if after is None:
            return None
        rescan = Rescan(str(uuid4()), session, after, baseline)
        with self.db:
            self.db.execute(
                'INSERT INTO rescans(id,session_id,map_epoch,started_at_ms,after_t_capture,baseline_json) '
                'VALUES(?,?,?,?,?,?)', (rescan.id, *session, started_at_ms, after,
                                        json.dumps([asdict(entry) for entry in baseline.values()])))
            self.objects.set_state(session, 'last_seen')  # until the revisit sees each one again
        self.rescan = rescan
        self._watch()
        return rescan

    def observe(self, t_capture, seen_at, sightings, views):
        """Take one detection frame; return (new event records, whether any object changed).

        Frames captured before the rescan, and depth probes made for another
        rescan, are ignored.
        """
        rescan = self.rescan
        if rescan is None:
            return [], False
        if t_capture <= rescan.after_t_capture:  # late inference for a frame from before the press
            with self.db:  # its sightings were recorded but do not count as seen again
                return [], self.objects.set_state(rescan.session, 'last_seen', [
                    s.object_id for s in sightings if s.object_id not in rescan.revisit])
        for sighting in sightings:
            self._remember(rescan, sighting.object_id, sighting.class_name,
                           (sighting.observation_id, sighting.position), sighting.created)
        if views is not None and views[0] == rescan.id:
            for object_id, status in views[1]:
                if object_id in rescan.baseline:
                    rescan.views[object_id][status] += 1
        try:
            with self.db:
                events, changed = self._evaluate(rescan, seen_at)
        except Exception:
            self.session = None
            self.activate(rescan.session)  # memory follows the rolled-back database
            raise
        self._watch()
        return events, changed

    def events(self, session, limit=MAX_EVENTS):
        """Stored events for one session/epoch, oldest first."""
        if session is None:
            return []
        rows = self.db.execute(
            'SELECT e.id, re.rescan_id, e.kind, e.object_id, n.object_id, e.old_position_json, '
            'e.new_position_json, e.displacement_m, e.t FROM events e '
            'JOIN rescan_events re ON re.event_id=e.id JOIN rescans r ON r.id=re.rescan_id '
            'LEFT JOIN observations n ON n.id=e.new_observation_id '
            'WHERE r.session_id=? AND r.map_epoch=? ORDER BY e.t DESC, e.rowid DESC LIMIT ?',
            (*session, limit)).fetchall()
        return [_event_record(*ids, old and json.loads(old), json.loads(new), displacement, t)
                for *ids, old, new, displacement, t in reversed(rows)]

    def _remember(self, rescan, object_id, name, member, created):
        if object_id in rescan.baseline:
            rescan.reobserved.add(object_id)
        elif created:
            rescan.born.add(object_id)
        _, members = rescan.revisit.pop(object_id, (name, deque(maxlen=CLUSTER_WINDOW)))
        members.appendleft(member)
        rescan.revisit[object_id] = (name, members)
        while len(rescan.revisit) > MAX_TRACKED:
            dropped, _ = rescan.revisit.popitem(last=False)
            rescan.born.discard(dropped)

    def _evaluate(self, rescan, seen_at):
        clusters = {object_id: cluster(list(members)) for object_id, (_, members) in rescan.revisit.items()}
        fresh = [(object_id, rescan.revisit[object_id][0], summary) for object_id, summary in clusters.items()
                 if object_id in rescan.born and confirmed(summary) and ('new', object_id) not in rescan.emitted]
        moved = {object_id for kind, object_id in rescan.emitted if kind == 'moved'}
        shifts = [math.dist(r.position, clusters[r.object_id].position) for r in rescan.baseline.values()
                  if r.object_id not in moved and confirmed(r) and confirmed(clusters.get(r.object_id))]
        seen = rescan.reobserved | moved
        unseen = [r for r in rescan.baseline.values() if r.object_id not in seen]
        # A surface where something was remembered contradicts "not found" whatever the alignment.
        changed = self.objects.set_state(rescan.session, 'last_seen', [
            r.object_id for r in unseen if rescan.views.get(r.object_id, {}).get('surface')])
        # Every non-remembered identity sighted since the press competes, confirmed or not.
        newcomers = Counter(name for object_id, (name, _) in rescan.revisit.items()
                            if object_id not in rescan.baseline)
        events, pairs = [], []
        for object_id, name, summary in fresh:
            rivals = [r for r in unseen if r.class_name == name]
            if not rivals:  # nothing of this class could have gone missing
                events.append(self._emit(rescan, 'new', object_id, object_id, seen_at, summary))
                continue
            # A nearby-only rule cannot re-identify an object across the room, so the nearest
            # unseen same-class memory is only a candidate until distinct evidence confirms it.
            old = min(rivals, key=lambda r: math.dist(r.position, summary.position))
            if math.dist(old.position, summary.position) >= max(
                    DISPLACEMENT_M, 3 * (old.spread_m + summary.spread_m)):
                unique = len(rivals) == 1 and newcomers[name] == 1
                pairs.append((old, object_id, summary, unique))
        alignment = _alignment(shifts, [(old.position, summary.position) for old, _, summary, _ in pairs])
        if alignment == 'drifted':  # suspend every movement claim until the map agrees again
            return events, changed
        aligned = alignment == 'ok'
        for old, object_id, summary, unique in pairs:
            if aligned and unique and confirmed(old) and self._empty(rescan, old.object_id):
                # Only then is this the remembered object at a new place: one identity, now moved.
                self.objects.merge(old.object_id, object_id, summary.position, 'moved')
                rescan.revisit.pop(object_id)
                rescan.born.discard(object_id)
                events.append(self._emit(rescan, 'moved', old.object_id, old.object_id, seen_at, summary, old))
                moved.add(old.object_id)
                changed = True
            elif ('possible_move', old.object_id) not in rescan.emitted:
                events.append(self._emit(rescan, 'possible_move', old.object_id, object_id, seen_at, summary, old))
        if aligned:  # seen through with the map verified: not found where it was, not "removed"
            missing = [r.object_id for r in unseen if r.object_id not in moved and self._empty(rescan, r.object_id)]
            changed |= self.objects.set_state(rescan.session, 'not_found_on_rescan', missing)
        return events, changed

    @staticmethod
    def _empty(rescan, object_id):
        views = rescan.views.get(object_id, {})
        return views.get('clear', 0) >= ABSENCE_VIEWS and not views.get('surface', 0)

    def _emit(self, rescan, kind, object_id, new_object_id, seen_at, new, old=None):
        """Store one event for this rescan, at most once per (kind, object)."""
        event_id = str(uuid4())
        displacement = None if old is None else math.dist(old.position, new.position)
        self.db.execute(
            'INSERT INTO events(id,object_id,kind,baseline_observation_id,new_observation_id,old_position_json,'
            'new_position_json,displacement_m,t) VALUES(?,?,?,?,?,?,?,?,?)',
            (event_id, object_id, kind, None if old is None else old.observation_id, new.observation_id,
             None if old is None else json.dumps(old.position), json.dumps(new.position), displacement, seen_at))
        self.db.execute('INSERT INTO rescan_events(rescan_id,event_id,kind,object_id) VALUES(?,?,?,?)',
                        (rescan.id, event_id, kind, object_id))
        rescan.emitted.add((kind, object_id))
        return _event_record(event_id, rescan.id, kind, object_id, new_object_id,
                             None if old is None else old.position, new.position, displacement, seen_at)

    def _load_revisit(self):
        rescan = self.rescan
        rows = self.db.execute(
            'SELECT object_id, class, id, position_json, born FROM ('
            ' SELECT o.object_id, o.class, o.id, o.position_json, o.rowid AS k, ROW_NUMBER() OVER ('
            '  PARTITION BY o.object_id ORDER BY f.t_capture DESC, o.rowid DESC) AS n,'
            '  NOT EXISTS (SELECT 1 FROM observations p JOIN frames g USING (session_id, map_epoch, frame_id)'
            '   WHERE p.object_id=o.object_id AND g.t_capture<=?) AS born'
            ' FROM observations o JOIN frames f USING (session_id, map_epoch, frame_id)'
            ' WHERE o.session_id=? AND o.map_epoch=? AND f.t_capture>?)'
            ' WHERE n<=? ORDER BY n DESC, k',
            (rescan.after_t_capture, *rescan.session, rescan.after_t_capture, CLUSTER_WINDOW)).fetchall()
        for object_id, name, observation_id, position, born in rows:  # oldest first
            self._remember(rescan, object_id, name, (observation_id, tuple(json.loads(position))), bool(born))
        rescan.emitted = set(self.db.execute('SELECT kind, object_id FROM rescan_events WHERE rescan_id=?',
                                             (rescan.id,)).fetchall())

    def _watch(self):
        rescan = self.rescan
        self.watching = None if rescan is None else (rescan.id, tuple(
            (object_id, remembered.position) for object_id, remembered in rescan.baseline.items()
            if object_id not in rescan.reobserved and ('moved', object_id) not in rescan.emitted))


def _alignment(shifts, moves):
    """'ok' when enough static references agree, 'drifted' when the map itself seems to shift.

    `shifts` are reobserved references' distances from their baseline; `moves` are
    candidate (old, new) positions. Several candidates displaced the same way also
    point at the map, not the scene.
    """
    if len(shifts) >= MIN_REFERENCES and statistics.median(shifts) > ALIGN_TOL_M:
        return 'drifted'
    vectors = [[n - o for o, n in zip(old, new)] for old, new in moves]
    if any(math.dist(a, b) <= ALIGN_TOL_M for a, b in combinations(vectors, 2)):
        return 'drifted'
    return 'ok' if len(shifts) >= MIN_REFERENCES and max(shifts) <= ALIGN_TOL_M else 'unverified'


def _event_record(event_id, rescan_id, kind, object_id, new_object_id, old, new, displacement, t):
    """The v1 `event` payload plus additive ids; `old`/`displacement` are None for `new`."""
    def rounded(value):
        return None if value is None else [round(v, 3) for v in value]
    return dict(id=event_id, rescan_id=rescan_id, kind=kind, object_id=object_id, new_object_id=new_object_id,
                old_position=rounded(old), new_position=rounded(new),
                displacement_m=None if displacement is None else round(displacement, 3), t=t)
