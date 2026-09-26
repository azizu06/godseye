"""Voice grounding against the real #55 detection message and #56 /route response (fake providers)."""
import time
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.detections import detections_message
from backend.localization import Detection
from backend.tests.test_approach import Grid2D, person_at, room, wall_with_gap
from backend.tests.test_voice import CLIP, WEBM, providers


class CombinedVoiceGroundingTests(unittest.TestCase):
    def test_voice_sees_the_live_frame_and_selected_person_route_until_the_map_resets(self):
        voice = providers()
        with TestClient(create_app(':memory:', voice_providers=voice)) as client:
            reset = client.post('/session').json()
            session = (reset['session_id'], reset['map_epoch'])
            client.app.state.occupancy = Grid2D(session, wall_with_gap(room(), 2., 1., .9))
            person = person_at(client, session, (2., .4, 3.))
            box = Detection((10., 20., 60., 200.), 'person', .88)
            found = SimpleNamespace(detection=box, position=(2., .4, 3.), depth_m=2.4)
            result = SimpleNamespace(boxes=[box], found=[found], session_id=session[0], map_epoch=session[1],
                                     frame_id=9, t_capture=4., t_wall_ms=int(time.time() * 1000),
                                     image_size=(640, 480))
            message = detections_message(result, [SimpleNamespace(object_id=person)], ('person', 'chair'))
            client.app.state.detection_view = (message, b'jpeg')
            route = client.post('/route', json=dict(session_id=session[0], map_epoch=session[1],
                                                    object_id=person, start=[3., .5])).json()
            self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).status_code, 200)
            client.post('/session')
            self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).status_code, 200)
        context = voice.answerer.calls[0][1]
        self.assertIn('person', {o['class'] for o in context['objects']})
        frame = context['extras']['latest_frame']
        self.assertEqual(frame['detections'], [{'class': 'person', 'confidence': .88,
                                                'position_m': [2., .4, 3.], 'depth_m': 2.4}])
        approach = context['extras']['approach_route']
        self.assertEqual(route['status'], 'ok')
        self.assertEqual((approach['status'], approach['person_position_m'], approach['length_m']),
                         ('ok', route['person'], route['length_m']))
        self.assertFalse(approach['verified'])
        self.assertNotIn('route', context)  # the rover has no path; the walking route is separate
        self.assertNotIn('extras', voice.answerer.calls[1][1])  # map reset retires both


if __name__ == '__main__':
    unittest.main()
