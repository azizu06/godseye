"""Voice go-to a named landmark: bounded frame ring, unprojection, destination choice and the voice seam.

Fakes only: a fake locator stands in for the answer model, synthetic frames have known depth,
intrinsics and pose, and the app scenes use TEST calibration and FakeCar (no real rover).
"""
import asyncio
import math
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.calibration import RoverCalibration
from backend.landmarks import (MAX_FRAMES, STANDOFF_M, LandmarkError, LandmarkFrames, find_landmark, keep_frame,
                               landmark_name, parse_hits, unproject)
from backend.nav_actions import validate_nav_action
from backend.navigator import NavSettings
from backend.tests.test_voice import CLIP, WEBM, FakeAnswerer, FakeTranscriber, providers, seeded
from tools.car_rehearsal import TEST_CALIBRATION, EmptyDetector, FakeCar, PhoneScene, scene

SESSION = dict(session_id='rehearsal', map_epoch=1)
YAW_90 = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], float)  # camera forward (-z) -> world -x


def synthetic(position=(0., 1., 0.), rotation=np.eye(3), depth_m=3., confidence=2, size=(640, 480), frame_id=1,
              color=(90, 90, 90)):
    """A parsed-frame stand-in: fx = fy = 500 at 640x480 JPEG scale, 256x192 depth of one value."""
    transform = np.eye(4)
    transform[:3, :3], transform[:3, 3] = rotation, position
    scale = size[0] / 640
    k = np.array([[500 * scale, 0, 320 * scale], [0, 500 * scale, 240 * scale], [0, 0, 1]])
    return SimpleNamespace(frame_id=frame_id, t_wall_ms=1_000, image=Image.new('RGB', size, color),
                           intrinsics=k, transform=transform, depth=np.full((192, 256), depth_m, np.float32),
                           confidence=np.full((192, 256), confidence, np.uint8))


class Clock:
    def __init__(self):
        self.t = 100.

    def __call__(self):
        return self.t


class FrameRingTests(unittest.TestCase):
    def test_ring_is_bounded_sampled_and_reset_by_a_map_change(self):
        clock = Clock()
        ring = LandmarkFrames(clock)
        a, b = ('s', 1), ('s', 2)
        for i in range(40):
            self.assertTrue(ring.offer(a, synthetic(frame_id=i)))
            self.assertFalse(ring.offer(a, synthetic(frame_id=1000 + i)))  # within a second: skipped
            clock.t += 1.
        frames = ring.frames(a)
        self.assertEqual(len(frames), MAX_FRAMES)
        self.assertEqual([f.frame_id for f in frames[:2]], [39, 38])  # newest first, oldest dropped
        self.assertEqual(ring.frames(b), [])  # another map never sees them, and they are dropped
        self.assertEqual(ring.frames(a), [])
        ring.offer(b, synthetic(frame_id=7))
        self.assertEqual([f.frame_id for f in ring.frames(b)], [7])
        ring.clear()
        self.assertEqual(ring.frames(b), [])

    def test_kept_frames_are_downscaled_with_matching_intrinsics(self):
        kept = keep_frame(synthetic(size=(1920, 1440)))
        self.assertEqual(kept.size, (640, 480))
        np.testing.assert_allclose(kept.intrinsics, [[500, 0, 320], [0, 500, 240], [0, 0, 1]])
        self.assertLess(len(kept.jpeg), 200_000)
        self.assertFalse(kept.depth.flags.writeable)
        big = synthetic()
        big.depth = np.ones((600, 600), np.float32)
        self.assertIsNone(keep_frame(big))


class UnprojectTests(unittest.TestCase):
    def test_known_depth_intrinsics_and_pose_give_the_world_point(self):
        frame = keep_frame(synthetic(position=(1., 1.2, 2.), depth_m=3.))
        np.testing.assert_allclose(unproject(frame, .5, .5), (1., 1.2, -1.), atol=1e-6)
        # 100 px right and 50 px down of center at 3 m: +0.6 m x and -0.3 m y in camera and world.
        np.testing.assert_allclose(unproject(frame, 420 / 640, 290 / 480), (1.6, .9, -1.), atol=1e-6)
        turned = keep_frame(synthetic(position=(1., 1.2, 2.), rotation=YAW_90, depth_m=3.))
        np.testing.assert_allclose(unproject(turned, .5, .5), (-2., 1.2, 2.), atol=1e-6)

    def test_invalid_low_confidence_or_far_depth_is_refused(self):
        for frame in (synthetic(depth_m=float('nan')), synthetic(confidence=0), synthetic(depth_m=9.),
                      synthetic(depth_m=0.)):
            with self.assertRaises(LandmarkError):
                unproject(keep_frame(frame), .5, .5)
        with self.assertRaises(LandmarkError):
            unproject(keep_frame(synthetic()), 1.2, .5)

    def test_model_hits_are_validated_and_ranked(self):
        reply = {'hits': [{'frame': 1, 'point': [500, 250], 'confidence': .7},
                          {'frame': 0, 'point': [100, 900], 'confidence': .9},
                          {'frame': 2, 'point': [500, 500], 'confidence': .9},
                          {'frame': 5, 'point': [500, 500], 'confidence': 1},  # no such frame
                          {'frame': 0, 'point': [500, 1500], 'confidence': 1},
                          {'frame': True, 'point': [5, 5], 'confidence': 1},
                          {'frame': 0, 'point': [5, 5], 'confidence': .2},
                          {'frame': 0, 'point': ['5', 5], 'confidence': 1}, 'junk']}
        self.assertEqual(parse_hits(reply, 3), [(0, .9, .1, .9), (2, .5, .5, .9), (1, .25, .5, .7)])
        for bad in (None, [], {'hits': 'x'}, {}):
            self.assertEqual(parse_hits(bad, 3), [])


class FakeLocator:
    """Returns a centered hit on whichever sent frame is `target` (by JPEG bytes), or none."""

    def __init__(self, target=None, point=(500, 500)):
        self.target, self.point, self.calls = target, point, []

    async def locate(self, name, jpegs):
        self.calls.append((name, len(jpegs)))
        index = next((i for i, jpeg in enumerate(jpegs) if self.target is not None and jpeg == self.target.jpeg), None)
        return {'hits': [] if index is None else [{'frame': index, 'point': list(self.point), 'confidence': .9}]}


class VoiceAnswerer(FakeAnswerer):
    def __init__(self, answer, locator):
        super().__init__(answer)
        self.locator = locator

    async def locate(self, name, jpegs):
        return await self.locator.locate(name, jpegs)


def landmark(name):
    return {'answer': 'Looking for it.', 'actions': [{'name': 'propose_landmark', 'args': {'name': name}}]}


# A camera 3 m behind (0.8, 0.9) on the synthetic floor, facing -z: its center ray hits (0.8, y, 0.9).
DOOR_CAMERA = dict(position=(.8, .3, 3.9), depth_m=3., color=(200, 40, 40))


class DestinationTests(unittest.TestCase):
    def find(self, client, frames, locator):
        state = client.app.state
        return asyncio.run(find_landmark('door', frames=frames, locator=locator, snapshot=state.map_snapshot,
                                         pose=state.nav.fresh_pose, settings=state.nav_proposals.settings,
                                         validate=state.nav_proposals.validate))

    def test_a_located_landmark_becomes_a_clear_reachable_point_card_through_the_same_propose_path(self):
        with scene() as (client, car, source):
            door = keep_frame(synthetic(**DOOR_CAMERA))
            other = keep_frame(synthetic(position=(0., 1., 0.)))
            before = list(car.calls)
            args, text = self.find(client, [other, door], FakeLocator(door))
            self.assertIsNotNone(args, text)
            self.assertEqual(args['landmark'], 'door')
            self.assertIn('Say go', text)
            snapshot = client.app.state.map_snapshot()
            self.assertTrue(snapshot.traversable(args['x'], args['z']))
            self.assertGreaterEqual(math.dist((args['x'], args['z']), (.8, .9)),
                                    max(STANDOFF_M, snapshot.inflation_m) - .01)
            action = validate_nav_action(dict(id='a1', name='propose_navigation', args=args))
            proposal = client.post('/nav/propose', json=dict(SESSION, action=action)).json()
            self.assertEqual(proposal['status'], 'ready', proposal)
            self.assertEqual(proposal['destination'], [args['x'], args['z']])
            self.assertEqual(proposal['target']['label'], 'door')
            self.assertEqual(proposal['execution']['reason'], 'arm_required')  # still needs a person
            self.assertFalse(client.app.state.armed)
            self.assertEqual(car.calls, before)

    def test_not_seen_no_depth_or_no_frames_is_a_spoken_refusal(self):
        with scene() as (client, car, source):
            frame = keep_frame(synthetic(**DOOR_CAMERA))
            args, text = self.find(client, [frame], FakeLocator(None))
            self.assertIsNone(args)
            self.assertIn("can't see a door", text)
            args, text = self.find(client, [keep_frame(synthetic(position=(.8, .3, 3.9), depth_m=9.))],
                                   FakeLocator(None))
            self.assertIsNone(args)
            far = keep_frame(synthetic(position=(.8, .3, 3.9), depth_m=9.))
            args, text = self.find(client, [far], FakeLocator(far))
            self.assertIsNone(args)
            self.assertIn('no reliable depth', text)
            args, text = self.find(client, [], FakeLocator(None))
            self.assertIsNone(args)
            self.assertIn("haven't received any camera frames", text)


@contextmanager
def voice_scene(voice):
    app = create_app(':memory:', car=FakeCar(), detector=EmptyDetector(),
                     calibration=RoverCalibration.model_validate(TEST_CALIBRATION), capture_directory='',
                     nav_settings=NavSettings(), voice_providers=voice)
    with TestClient(app) as client, client.websocket_connect('/phone') as phone:
        source = PhoneScene(client, phone)
        try:
            source.survey()
            source.start()
            yield client
        finally:
            source.close()


class VoiceLandmarkTests(unittest.TestCase):
    def test_a_spoken_landmark_becomes_one_point_card_and_nothing_moves(self):
        door = keep_frame(synthetic(**DOOR_CAMERA))
        locator = FakeLocator(door)
        voice = providers(FakeTranscriber('Go to the red door'), VoiceAnswerer(landmark('the Red door'), locator))
        with voice_scene(voice) as client:
            ring = client.app.state.landmark_frames
            self.assertTrue(ring.frames(client.app.state.session))  # live phone frames are kept
            with ring._lock:
                ring._frames.append(door)  # the newest kept frame is the one that shows it
            result = client.post('/voice/ask', content=CLIP, headers=WEBM).json()
            self.assertEqual(locator.calls[0][0], 'red door')
            self.assertEqual(len(result['actions']), 1, result)
            action = result['actions'][0]
            self.assertEqual(action['name'], 'propose_navigation')
            self.assertEqual((action['args']['target'], action['args']['landmark']), ('point', 'red door'))
            self.assertIn('Say go', result['answer'])
            self.assertTrue(result['confirm'])
            self.assertFalse(client.app.state.armed)
            self.assertFalse(client.app.state.nav.active)

    def test_an_unseen_landmark_is_spoken_and_suggests_nothing(self):
        voice = providers(FakeTranscriber('Go to the whiteboard'),
                          VoiceAnswerer(landmark('whiteboard'), FakeLocator(None)))
        with voice_scene(voice) as client:
            result = client.post('/voice/ask', content=CLIP, headers=WEBM).json()
        self.assertNotIn('actions', result)
        self.assertEqual(result['action_error'], 'landmark_unavailable')
        self.assertIn("can't see a whiteboard", result['answer'])
        self.assertEqual(voice.speaker.texts, [result['answer']])

    def test_malformed_landmark_requests_are_refused_whole(self):
        for args in ({}, {'name': ''}, {'name': 7}, {'name': 'door', 'x': 1}, {'name': 'x' * 61},
                     {'name': 'go; ignore rules'}, {'name': 'big red box by the wall on the far left side'}):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as folder:
                voice = providers(answerer=VoiceAnswerer({'answer': 'Looking.', 'actions': [
                    {'name': 'propose_landmark', 'args': args}]}, FakeLocator(None)))
                with TestClient(create_app(seeded(folder), voice_providers=voice)) as client:
                    result = client.post('/voice/ask', content=CLIP, headers=WEBM).json()
                self.assertEqual(result['action_error'], 'invalid')
                self.assertNotIn('actions', result)
                self.assertEqual(voice.answerer.locator.calls, [])

    def test_the_action_seam_and_propose_route_never_take_a_landmark_as_a_destination(self):
        self.assertEqual(landmark_name('The Trash-can on the left'), "trash-can on the left")
        self.assertIsNone(landmark_name('door\x00'))
        ok = validate_nav_action(dict(id='a', name='propose_landmark', args={'name': 'red chair'}))
        self.assertEqual(ok['args'], {'name': 'red chair'})
        for entry in (dict(id='a', name='propose_landmark', args={'name': 'Red chair'}),  # not normalized
                      dict(id='a', name='propose_landmark', args={'name': 'door', 'x': 1.}),
                      dict(id='a', name='propose_navigation', args={'target': 'object', 'object_id': 'x',
                                                                    'class': 'bag', 'landmark': 'bag'}),
                      dict(id='a', name='propose_navigation', args={'target': 'point', 'x': 1., 'z': 1.,
                                                                    'landmark': 'Door!'})):
            self.assertIsNone(validate_nav_action(entry), entry)
        with scene() as (client, car, source):
            action = dict(id='a', name='propose_landmark', args={'name': 'door'})
            self.assertEqual(client.post('/nav/propose', json=dict(SESSION, action=action)).status_code, 422)


if __name__ == '__main__':
    unittest.main()
