"""Rescan baselines and change events on synthetic scenes; real-scene accuracy is unproven."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.changes import ChangeTracker
from backend.frame_bundle import parse_frame_bundle
from backend.localization import Detection, view_status
from backend.objects import ObjectMemory
from backend.tests.test_map_transport import fresh, hello, next_of, wait_for
from backend.tests.test_mapping import TRANSFORM, bundle, grids
from backend.tests.test_objects import FAR_CHAIR, LEFT_CUP, SCHEMA, FakeDetector, located

SESSION = ('s', 1)
CUP, CHAIR, BACKPACK = (0., 0., 0.), (2., 0., 0.), (1., 0., 1.)
FAR = (1., 0., 3.)  # 2 m from BACKPACK: far outside the 0.5 m association radius
REFS = (('cup', CUP), ('chair', CHAIR))  # static references that verify map alignment

# test_mapping.TRANSFORM puts the camera at (1, 2, 3) looking along world -X at a flat wall
# 2 m away. A JPEG box centred on column u, row 30 at depth d lands at world
# (1 - d, 2, 3 - (u - 40) * d / 40): OLD_SPOT is u=20 and NEW_SPOT u=60, both 1.5 m out.
OLD_SPOT = (-.5, 2., 3.75)
NEW_SPOT = (-.5, 2., 2.25)
OLD_CELLS = np.s_[6:9, 4:6]  # depth cells under JPEG box (16, 24, 24, 36)
NEW_CELLS = np.s_[6:9, 14:16]  # depth cells under JPEG box (56, 24, 64, 36)


def scene_depth(*cells, depth=1.5):
    """The 2 m wall with a 1.5 m object standing in front of it at each of `cells`."""
    depths, confidence = grids()
    for where in cells:
        depths[where] = depth
    return depths, confidence



class Scene:
    """ObjectMemory plus ChangeTracker over one SQLite file, fed one synthetic frame at a time.

    Frame n is captured at t=n and phone wall second n. `see` records placed objects
    (class, position) and passes depth-probe results keyed by class of the baseline object.
    """

    def __init__(self, path=':memory:', session=SESSION):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.execute('INSERT OR IGNORE INTO sessions(session_id,map_epoch,created_at_ms) VALUES(?,?,1)', session)
        self.session = session
        self.objects = ObjectMemory(self.db)
        self.changes = ChangeTracker(self.db, self.objects)
        self.changes.activate(session)
        self.frame = self.db.execute('SELECT COALESCE(MAX(frame_id), 0) FROM frames').fetchone()[0]

    def see(self, *placed, views=None, t_capture=None, rescan_id=None):
        self.frame += 1
        t = float(self.frame) if t_capture is None else t_capture
        self.db.execute('INSERT INTO frames(session_id,map_epoch,frame_id,t_capture,t_wall_ms,transform_json,'
                        "tracking) VALUES(?,?,?,?,?,'[]','normal')", (*self.session, self.frame, t, self.frame * 1000))
        found = [located(Detection((0, 0, 1, 1), name, .9), position, self.frame, self.session)
                 for name, position in placed]
        sightings = self.objects.record(self.session, self.frame, float(self.frame), found)
        watching = self.changes.watching
        probes = None if watching is None or views is None else (
            rescan_id or watching[0], tuple((object_id, views[self.ids[object_id]]) for object_id, _ in watching[1]
                               if self.ids[object_id] in views))
        events, _ = self.changes.observe(t, float(self.frame), sightings, probes)
        return events

    def remember(self, *placed, frames=3):
        """Build a baseline map, then press Rescan."""
        for _ in range(frames):
            self.see(*placed)
        rescan = self.changes.start(self.session, 0)
        self.ids = {o['id']: o['class'] for o in self.objects.snapshot(self.session)}
        return rescan

    def state(self):
        return {o['class']: o['state'] for o in self.objects.snapshot(self.session)}


class RescanTests(unittest.TestCase):
    def scene(self, path=':memory:'):
        scene = Scene(path)
        self.addCleanup(scene.db.close)
        return scene

    def test_unchanged_or_out_of_view_objects_produce_no_events(self):
        scene = self.scene()
        scene.remember(('cup', CUP), ('chair', CHAIR), ('backpack', BACKPACK))
        self.assertEqual(set(scene.state().values()), {'last_seen'})
        # The revisit only looks at the cup and chair: the backpack is out of view, not missing.
        for _ in range(5):
            self.assertEqual(scene.see(('cup', CUP), ('chair', CHAIR), views={}), [])
        self.assertEqual(scene.state(), {'cup': 'present', 'chair': 'present', 'backpack': 'last_seen'})
        for _ in range(3):
            self.assertEqual(scene.see(('cup', CUP), ('chair', CHAIR), ('backpack', (1.05, 0, 1)),
                                       views={'backpack': 'surface'}), [])
        self.assertEqual(set(scene.state().values()), {'present'})
        self.assertEqual(scene.changes.events(SESSION), [])

    def test_one_new_object_is_reported_once_after_three_consistent_sightings(self):
        scene = self.scene()
        first = scene.remember(('cup', CUP), ('backpack', BACKPACK))
        bottle = (3., 0., 0.)
        self.assertEqual(scene.see(('cup', CUP), ('bottle', bottle)), [])
        self.assertEqual(scene.see(('cup', CUP), ('bottle', (3.1, 0, 0))), [])
        [event] = scene.see(('cup', CUP), ('bottle', (2.9, 0, 0)))
        [bottle_id] = [i for i, o in {o['id']: o for o in scene.objects.snapshot(SESSION)}.items()
                       if o['class'] == 'bottle']
        self.assertEqual({k: event[k] for k in ('rescan_id', 'kind', 'object_id', 'new_object_id')},
                         dict(rescan_id=first.id, kind='new', object_id=bottle_id, new_object_id=bottle_id))
        self.assertEqual((event['old_position'], event['new_position'], event['displacement_m'], event['t']),
                         (None, [3., 0., 0.], None, float(scene.frame)))
        for _ in range(3):  # still there: no duplicate
            self.assertEqual(scene.see(('cup', CUP), ('bottle', bottle)), [])
        scene.remember(('cup', CUP), ('bottle', bottle), frames=0)  # a second rescan remembers it
        for _ in range(3):
            self.assertEqual(scene.see(('cup', CUP), ('bottle', bottle)), [])
        self.assertEqual(scene.changes.events(SESSION), [event])

    def test_far_same_class_sighting_without_distinct_evidence_is_only_a_possible_move(self):
        clear = {'backpack': 'clear'}
        cases = [  # name, extra remembered objects, revisit references, per-frame probes, new spot, kinds
            ('old spot never seen again', (), REFS, [{}] * 4, FAR, ['possible_move']),
            ('one reference cannot verify alignment', (), REFS[:1], [clear] * 4, FAR, ['possible_move']),
            ('old spot still showed a surface', (), REFS, [{'backpack': 'surface'}] + [clear] * 3, FAR,
             ['possible_move']),
            ('two remembered backpacks compete', (('backpack', (4., 0., 1.)),), REFS, [clear] * 4, FAR,
             ['possible_move']),
            ('within the displacement threshold', (), REFS, [{}] * 4, (1., 0., 1.55), []),
        ]
        for name, extra, seen, probes, spot, kinds in cases:
            with self.subTest(name):
                scene = self.scene()
                scene.remember(*REFS, ('backpack', BACKPACK), *extra)
                [remembered] = [i for i, c in scene.ids.items() if c == 'backpack' and
                                scene.changes.rescan.baseline[i].position == BACKPACK]
                events = [e for views in probes for e in scene.see(*seen, ('backpack', spot), views=views)]
                self.assertEqual([e['kind'] for e in events], kinds)
                backpacks = [o for o in scene.objects.snapshot(SESSION) if o['class'] == 'backpack']
                self.assertEqual(len(backpacks), 2 + len(extra))  # identities stay separate
                if events:
                    [new] = [o['id'] for o in backpacks if o['id'] not in scene.ids]
                    self.assertEqual((events[0]['object_id'], events[0]['new_object_id']), (remembered, new))
                    self.assertEqual((events[0]['old_position'], events[0]['new_position'],
                                      events[0]['displacement_m']), (list(BACKPACK), list(FAR), 2.))

    def test_move_is_confirmed_once_and_folds_the_identities_together(self):
        # Distinct evidence: the only remembered backpack's spot is seen through, two static
        # references agree with the baseline, and a new backpack cluster confirms 2 m away.
        clear = {'backpack': 'clear'}
        cases = [  # name, frames of (backpack seen?, probes), expected kinds, state before the move
            ('found elsewhere first, then the old spot is empty', [(True, {})] * 3 + [(True, clear)] * 3,
             ['possible_move', 'moved'], 'last_seen'),
            ('old spot empty first, then found elsewhere', [(False, clear)] * 3 + [(True, {})] * 3,
             ['moved'], 'not_found_on_rescan'),
        ]
        for name, frames, kinds, before in cases:
            with self.subTest(name):
                scene = self.scene()
                scene.remember(*REFS, ('backpack', BACKPACK))
                [remembered] = [i for i, c in scene.ids.items() if c == 'backpack']
                events = []
                for i, (visible, views) in enumerate(frames):
                    events += scene.see(*REFS, *[('backpack', FAR)] * visible, views=views)
                    if i == 2:
                        self.assertEqual(scene.state()['backpack'], before)
                self.assertEqual([e['kind'] for e in events], kinds)
                moved = events[-1]
                self.assertEqual((moved['object_id'], moved['new_object_id'], moved['old_position'],
                                  moved['new_position'], moved['displacement_m']),
                                 (remembered, remembered, list(BACKPACK), list(FAR), 2.))
                [backpack] = [o for o in scene.objects.snapshot(SESSION) if o['class'] == 'backpack']
                self.assertEqual((backpack['id'], backpack['position'], backpack['state']),
                                 (remembered, list(FAR), 'moved'))
                self.assertEqual(backpack['observations'], 3 + sum(visible for visible, _ in frames))
                for _ in range(3):  # seen again at its new place: still moved, nothing repeats
                    self.assertEqual(scene.see(*REFS, ('backpack', FAR), views=clear), [])
                self.assertEqual(scene.state()['backpack'], 'moved')
                # Stored events now name the one surviving identity as the new sighting too.
                self.assertEqual(scene.changes.events(SESSION), [dict(e, new_object_id=remembered) for e in events])

    def test_not_found_needs_verified_alignment_and_yields_to_a_surface(self):
        clear, surface = {'backpack': 'clear'}, {'backpack': 'surface'}
        cases = [  # name, revisit references, per-frame probes, backpack state after each frame
            ('seen through with two references', REFS, [clear] * 3 + [surface],
             ['last_seen'] * 2 + ['not_found_on_rescan', 'last_seen']),
            ('one reference cannot verify alignment', REFS[:1], [clear] * 4, ['last_seen'] * 4),
        ]
        for name, seen, probes, states in cases:
            with self.subTest(name):
                scene = self.scene()
                scene.remember(*REFS, ('backpack', BACKPACK))
                observed = []
                for views in probes:
                    self.assertEqual(scene.see(*seen, views=views), [])
                    observed.append(scene.state()['backpack'])
                self.assertEqual(observed, states)

    def test_map_drift_suspends_movement_claims(self):
        def shifted(position, dz):
            return (position[0], position[1], position[2] + dz)

        remembered = (('cup', CUP), ('chair', CHAIR), ('backpack', BACKPACK))
        cases = [  # name, what each revisit frame sees
            ('references all shifted 0.3 m', (('cup', shifted(CUP, .3)), ('chair', shifted(CHAIR, .3)),
                                              ('backpack', FAR))),
            ('everything "moved" 1 m the same way', tuple((name, shifted(p, 1.)) for name, p in remembered)),
        ]
        for name, seen in cases:
            with self.subTest(name):
                scene = self.scene()
                scene.remember(*remembered)
                for _ in range(4):
                    self.assertEqual(scene.see(*seen, views={'backpack': 'clear'}), [])
                self.assertNotIn('not_found_on_rescan', scene.state().values())

    def test_stale_or_foreign_evidence_cannot_change_the_baseline_or_publish(self):
        scene = self.scene()
        scene.remember(*REFS, ('backpack', BACKPACK))
        for _ in range(3):  # inference for frames captured before the Rescan press lands late
            self.assertEqual(scene.see(*REFS, ('backpack', (1., 0., 1.4)), t_capture=.5,
                                       views={'backpack': 'clear'}), [])
        for _ in range(3):  # probes made for some other rescan
            self.assertEqual(scene.see(*REFS, views={'backpack': 'clear'}, rescan_id='other-rescan'), [])
        events = [e for _ in range(3) for e in scene.see(*REFS, ('backpack', FAR))]
        # Neither the late sightings nor foreign probes count: still only a possible move,
        # measured from the frozen baseline rather than the running mean they shifted.
        self.assertEqual([(e['kind'], e['old_position']) for e in events], [('possible_move', list(BACKPACK))])
        scene.changes.activate(('other-session', 1))
        self.assertEqual(scene.changes.observe(99., 99., [], None), ([], False))
        self.assertEqual(scene.changes.events(('other-session', 1)), [])
        self.assertIsNone(scene.changes.watching)
        self.assertEqual(scene.changes.events(SESSION), events)

    def test_rescan_evidence_and_events_survive_a_restart_without_duplicates(self):
        bottle = (3., 0., 0.)
        path = os.path.join(self.enterContext(tempfile.TemporaryDirectory()), 'godseye.db')
        before = self.scene(path)
        before.remember(*REFS, ('backpack', BACKPACK))
        events = [e for _ in range(3) for e in before.see(*REFS, ('bottle', bottle))]
        for _ in range(2):  # not yet enough to confirm the far backpack
            self.assertEqual(before.see(*REFS, ('bottle', bottle), ('backpack', FAR)), [])
        before.db.close()
        after = self.scene(path)  # restarted backend, same AR session
        self.assertEqual(after.changes.events(SESSION), events)
        events += after.see(*REFS, ('bottle', bottle), ('backpack', FAR))
        events += after.see(*REFS, ('bottle', bottle), ('backpack', FAR))
        self.assertEqual([e['kind'] for e in events], ['new', 'possible_move'])
        self.assertEqual(after.changes.events(SESSION), events)


class LiveRescanTests(unittest.TestCase):
    def test_phone_rescan_publishes_one_evidenced_move_then_resets_with_the_map(self):
        detector = FakeDetector()
        old_bag, new_bag = Detection((16, 24, 24, 36), 'backpack', .9), Detection((56, 24, 64, 36), 'backpack', .9)

        def show(phone, frame_ids, *detections, cells):
            detector.detections = [LEFT_CUP, FAR_CHAIR, *detections]
            stats = client.app.state.detect_stats
            for frame_id in frame_ids:
                done = stats['published']
                phone.send_bytes(fresh(bundle(*scene_depth(cells), transform=TRANSFORM, session='map-session'),
                                       frame_id=frame_id, t_capture=float(frame_id)))
                wait_for(lambda: stats['published'] == done + 1)

        with mock.patch('backend.app.DETECT_INTERVAL_S', .01), \
                TestClient(create_app(':memory:', detector=detector)) as client, \
                client.websocket_connect('/live') as live:
            self.assertEqual(client.post('/rescan').status_code, 409)  # no map yet
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                show(phone, [1, 2, 3], old_bag, cells=OLD_CELLS)
                started = client.post('/rescan').json()
                [bag_id] = [o['id'] for o in client.get('/objects').json()['objects'] if o['class'] == 'backpack']
                self.assertEqual({o['state'] for o in client.get('/objects').json()['objects']}, {'last_seen'})
                # The bag now stands 1.5 m away; the wall shows through where it was.
                show(phone, [4, 5, 6, 7], new_bag, cells=NEW_CELLS)
                event = next_of(live, 'event', limit=500)
                listed = client.get('/events').json()
                objects = client.get('/objects').json()['objects']
            client.post('/session')
            reset = client.get('/events').json()
            refused = client.post('/rescan').status_code
        self.assertEqual((started['version'], started['session_id'], started['map_epoch'],
                          started['baseline_objects']), (1, 'map-session', 1, 3))
        self.assertEqual({k: event[k] for k in ('version', 'session_id', 'map_epoch', 'rescan_id', 'kind', 'object_id')},
                         dict(version=1, session_id='map-session', map_epoch=1, rescan_id=started['rescan_id'],
                              kind='moved', object_id=bag_id))
        np.testing.assert_allclose(event['old_position'], OLD_SPOT, atol=1e-3)
        np.testing.assert_allclose(event['new_position'], NEW_SPOT, atol=1e-3)
        self.assertAlmostEqual(event['displacement_m'], 1.5, places=3)
        self.assertEqual(listed, dict(version=1, session_id='map-session', map_epoch=1, events=[
            {k: v for k, v in event.items() if k not in ('version', 'type', 'session_id', 'map_epoch')}]))
        self.assertEqual({o['class']: o['state'] for o in objects},
                         {'cup': 'present', 'chair': 'present', 'backpack': 'moved'})
        [bag] = [o for o in objects if o['class'] == 'backpack']
        self.assertEqual(bag['id'], bag_id)
        self.assertEqual((reset['events'], refused), ([], 409))


class ViewStatusTests(unittest.TestCase):
    def test_depth_ray_classifies_a_remembered_position(self):
        low = scene_depth(OLD_CELLS)
        low[1][:] = 1
        cases = [
            ('object still standing there', scene_depth(OLD_CELLS), OLD_SPOT, 'surface'),
            ('wall seen through the old spot', scene_depth(NEW_CELLS), OLD_SPOT, 'clear'),
            ('something nearer blocks it', scene_depth(OLD_CELLS, depth=1.), OLD_SPOT, 'occluded'),
            ('behind the camera', scene_depth(), (3., 2., 3.), 'out_of_view'),
            ('outside the image', scene_depth(), (-.5, 2., 6.), 'out_of_view'),
            ('no high-confidence depth', low, OLD_SPOT, 'out_of_view'),
        ]
        for name, grid, position, expected in cases:
            with self.subTest(name):
                frame = parse_frame_bundle(bundle(*grid), session_id='s', map_epoch=1)
                self.assertEqual(view_status(frame, position), expected)


if __name__ == '__main__':
    unittest.main()
