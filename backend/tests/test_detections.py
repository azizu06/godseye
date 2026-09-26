"""Live detection overlay: phone frame -> detector -> /live boxes + exact JPEG; no weights.

The scene is test_mapping's: a wall 2 m ahead of a camera at (1, 2, 3) looking along
world -X, 80x60 JPEG. A box centred at column u localizes to (-1, 2, 3 - (u - 40) / 20).
"""
import threading
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.detections import DEMO_CLASSES, detections_message, overlay_classes
from backend.localization import Detection, localize_all
from backend.objects import FrameObjects, Sighting
from backend.tests.test_map_transport import fresh, hello, next_of, wait_for
from backend.tests.test_mapping import TRANSFORM, bundle, jpeg

PERSON = Detection((40., 10., 56., 50.), 'person', .87)  # centre u=48 -> world (-1, 2, 2.6)
BACKPACK = Detection((4., 24., 16., 36.), 'backpack', .71)  # centre u=10 -> world (-1, 2, 4.5)
TV = Detection((60., 20., 76., 40.), 'tv', .9)  # a real model class outside the staged set


class BoxDetector:
    """Stands in for MPSDetector: fixed 2D boxes, real depth localization."""

    confidence = .25

    def __init__(self, *boxes, gate=None):
        self.boxes = list(boxes)
        self.gate = gate
        self.calls = 0

    def detect(self, frame_bundle):
        self.calls += 1
        if self.gate is not None:
            self.gate()
        return list(self.boxes)

    def localize(self, frame_bundle):  # MPSDetector's contract; unused when detect exists
        return localize_all(frame_bundle, self.detect(frame_bundle))


def scene(session='room', frame_id=1, depth_right_half_only=False, image=None):
    """A fresh bundle; optionally only JPEG columns x >= 40 carry reliable depth."""
    depth = np.full((15, 20), 2, dtype='<f4')
    confidence = np.full((15, 20), 2, dtype='u1')
    if depth_right_half_only:
        confidence[:, :10] = 0
    return fresh(bundle(depth, confidence, jpeg_bytes=image, transform=TRANSFORM, session=session),
                 frame_id=frame_id, t_capture=float(frame_id))


class OverlayClassTests(unittest.TestCase):
    def test_staged_classes_are_limited_to_what_the_model_can_emit(self):
        names = {0: 'person', 24: 'backpack', 56: 'chair'}.values()
        self.assertEqual(overlay_classes(DEMO_CLASSES, names), ('person', 'backpack', 'chair'))
        self.assertEqual(overlay_classes(('person', 'unicorn'), None), ('person', 'unicorn'))

    def test_message_places_only_depth_supported_boxes_and_drops_unsupported_classes(self):
        located = localize_all(_bundle_frame(depth_right_half_only=True), [PERSON, BACKPACK, TV])
        self.assertEqual([item.detection for item in located], [PERSON, TV])
        result = FrameObjects('room', 1, 7, 7., 1000, tuple(located), boxes=(PERSON, BACKPACK, TV),
                              image_size=(80, 60))
        sightings = [Sighting('o1', 'person-1', 'person', located[0].position, True),
                     Sighting('o2', 'tv-1', 'tv', located[1].position, True)]
        message = detections_message(result, sightings, DEMO_CLASSES)
        self.assertEqual((message['type'], message['source'], message['frame_id'], message['image']),
                         ('detections', 'backend_detector', 7, dict(width=80, height=60)))
        person, backpack = message['detections']
        self.assertEqual((person['class'], person['object_id'], person['box']),
                         ('person', 'person-1', [40., 10., 56., 50.]))
        np.testing.assert_allclose(person['position'], [-1, 2, 2.6], atol=1e-6)
        self.assertEqual((backpack['class'], backpack['position'], backpack['depth_m'], backpack['object_id']),
                         ('backpack', None, None, None))


def _bundle_frame(**kwargs):
    from backend.frame_bundle import parse_frame_bundle
    return parse_frame_bundle(scene(**kwargs), session_id='room', map_epoch=1)


class LiveDetectionTests(unittest.TestCase):
    def test_phone_frame_reaches_live_overlay_with_positions_only_where_depth_supports(self):
        detector = BoxDetector(PERSON, BACKPACK, TV)
        with TestClient(create_app(':memory:', detector=detector)) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('room'))
            phone.send_bytes(scene(depth_right_half_only=True))
            message = next_of(live, 'detections')
            objects = client.get('/objects').json()['objects']
        self.assertEqual((message['session_id'], message['map_epoch'], message['frame_id']), ('room', 1, 1))
        self.assertEqual(message['classes'], list(DEMO_CLASSES))
        by_class = {d['class']: d for d in message['detections']}
        self.assertEqual(set(by_class), {'person', 'backpack'})  # tv is not a staged class
        self.assertIsNone(by_class['backpack']['position'])  # no reliable depth: 2D evidence only
        np.testing.assert_allclose(by_class['person']['position'], [-1, 2, 2.6], atol=1e-6)
        # Object memory is unchanged: every localized sighting is remembered, none invented.
        self.assertEqual({o['class'] for o in objects}, {'person', 'tv'})
        person = next(o for o in objects if o['class'] == 'person')
        self.assertEqual(by_class['person']['object_id'], person['id'])

    def test_frame_without_depth_still_shows_boxes_but_places_nothing(self):
        detector = BoxDetector(PERSON)
        depth, confidence = np.full((15, 20), 2, dtype='<f4'), np.zeros((15, 20), dtype='u1')
        with TestClient(create_app(':memory:', detector=detector)) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('room'))
            phone.send_bytes(fresh(bundle(depth, confidence, transform=TRANSFORM, session='room'),
                                   frame_id=1, t_capture=1.))
            message = next_of(live, 'detections')
            objects = client.get('/objects').json()['objects']
        [person] = message['detections']
        self.assertEqual((person['position'], person['object_id']), (None, None))
        self.assertEqual(objects, [])

    def test_detection_image_is_that_exact_frame_and_other_frames_are_refused(self):
        image = jpeg((30, 200, 30))
        detector = BoxDetector(PERSON)
        with TestClient(create_app(':memory:', detector=detector)) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('room'))
            phone.send_bytes(scene(frame_id=4, image=image))
            message = next_of(live, 'detections')
            query = dict(session_id='room', map_epoch=1, frame_id=4)
            exact = client.get('/capture/detections.jpg', params=query)
            other_frame = client.get('/capture/detections.jpg', params=dict(query, frame_id=3))
            other_map = client.get('/capture/detections.jpg', params=dict(query, map_epoch=2))
        self.assertEqual(message['frame_id'], 4)
        self.assertEqual((exact.status_code, exact.headers['content-type'], exact.content),
                         (200, 'image/jpeg', image))
        self.assertEqual((exact.headers['x-frame-id'], exact.headers['cache-control']), ('4', 'no-store'))
        self.assertEqual((other_frame.status_code, other_map.status_code), (409, 409))

    def test_reset_retires_the_detection_image_and_late_results_are_never_published(self):
        started, release = threading.Event(), threading.Event()

        def gate():
            if detector.calls > 1:  # the first frame completes; the second is held
                started.set()
                release.wait(5)

        detector = BoxDetector(PERSON, gate=gate)
        with TestClient(create_app(':memory:', detector=detector)) as client, \
                client.websocket_connect('/live') as live:
            stats = client.app.state.detect_stats
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('room'))
                phone.send_bytes(scene(frame_id=1))
                next_of(live, 'detections')
                time.sleep(.55)  # past the 2 Hz detector cadence
                phone.send_bytes(scene(frame_id=2))
                self.assertTrue(started.wait(5))
                reset = client.post('/session').json()
                release.set()
                wait_for(lambda: stats['discarded_reset'] == 1)
            query = dict(session_id='room', map_epoch=1, frame_id=1)
            retired = client.get('/capture/detections.jpg', params=query)
            fresh_map = client.get('/capture/detections.jpg', params=dict(
                session_id=reset['session_id'], map_epoch=1, frame_id=2))
            time.sleep(.2)
            stored = client.app.state.detection_view
        self.assertEqual(stats['published'], 1)  # only frame 1 was ever accepted and overlaid
        self.assertIsNone(stored)
        self.assertEqual((retired.status_code, fresh_map.status_code), (409, 409))

    def test_localize_only_adapter_overlays_its_localized_boxes(self):
        class LocalizeOnly:
            def localize(self, frame_bundle):
                return localize_all(frame_bundle, [PERSON, BACKPACK])

        with TestClient(create_app(':memory:', detector=LocalizeOnly())) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(hello('room'))
            phone.send_bytes(scene(depth_right_half_only=True))
            message = next_of(live, 'detections')
        self.assertEqual([d['class'] for d in message['detections']], ['person'])


if __name__ == '__main__':
    unittest.main()
