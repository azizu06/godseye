import copy
import json
import math
import struct
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.tests.test_map_transport import frame, hello, wait_for
from backend.walls import wall_rectangles
from backend.mapping import build_point_chunk, WallOnlyFrame
from backend.tests.test_mapping import grids, bundle


def anchor(**changes):
    result = dict(id='wall-a', type='plane', classification='wall', is_wall=True, alignment=1,
                  center=[.5, 0, .25], extent=dict(width=2, height=2, rotation_y_rad=0),
                  transform=[1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 3, 1, -2, 1])
    return dict(result, **changes)


def geometry(anchors, capture=2.):
    header = dict(version=2, type='capture', kind='geometry', session_id='map-session', map_epoch=1,
                  frame_id=1, t_capture=capture, t_wall_ms=int(time.time() * 1000),
                  metadata=dict(anchors=anchors), sections=[])
    data = json.dumps(header).encode()
    return struct.pack('<I', len(data)) + data


class WallTests(unittest.TestCase):
    def test_stream_budget_goes_to_foreground_after_wall_filtering(self):
        depth, confidence = grids()
        depth[:, 10:] = 1  # Foreground surface one meter in front of the wall.
        walls = [dict(corners=[-1, .5, 1, -1, .5, 5, -1, 3.5, 5, -1, 3.5, 1])]
        result = build_point_chunk(bundle(depth, confidence), 's', 1, max_points=40, walls=walls)
        self.assertEqual(len(result.positions), 40)
        np.testing.assert_allclose(result.positions[:, 0], 0)
        with self.assertRaises(WallOnlyFrame):
            build_point_chunk(bundle(*grids()), 's', 1, walls=walls)
    def test_world_corners_include_local_center_and_anchor_transform(self):
        result = wall_rectangles(dict(anchors=[anchor()]))
        np.testing.assert_allclose(np.array(result[0]['corners']).reshape(4, 3),
                                   [[2.5, 1.75, -2], [4.5, 1.75, -2], [4.5, -.25, -2], [2.5, -.25, -2]])

    def test_ios16_extent_rotation_does_not_rotate_the_center(self):
        result = wall_rectangles(dict(anchors=[anchor(extent=dict(width=4, height=2, rotation_y_rad=math.pi/2))]))
        np.testing.assert_allclose(np.array(result[0]['corners']).reshape(4, 3),
                                   [[2.5, -1.25, -2], [2.5, 2.75, -2], [4.5, 2.75, -2], [4.5, -1.25, -2]])

    def test_only_classified_vertical_walls_survive(self):
        candidates = [anchor(is_wall=False, classification=c) for c in ['floor', 'table', 'window', 'door', 'none']]
        candidates += [anchor(alignment=0), anchor(type='mesh'),
                       anchor(extent=dict(width=0, height=2, rotation_y_rad=0)),
                       anchor(center=[0, float('nan'), 0]), anchor(transform=[1]*16),
                       anchor(extent=dict(width=1e20, height=2, rotation_y_rad=0))]
        self.assertEqual(wall_rectangles(dict(anchors=candidates)), [])
        legacy = anchor()
        del legacy['is_wall']
        self.assertEqual(len(wall_rectangles(dict(anchors=[legacy]))), 1)

    def test_anchor_ids_are_unique_and_snapshot_is_bounded(self):
        walls = wall_rectangles(dict(anchors=[anchor(), anchor()] + [anchor(id=f'wall-{i}') for i in range(200)]))
        self.assertEqual(len(walls), 128)
        self.assertEqual(len({w['id'] for w in walls}), 128)

    def test_endpoint_refines_removes_and_resets_walls_without_sending_mesh_buffers(self):
        with TestClient(create_app(':memory:')) as client:
            self.assertFalse(client.get('/capture/walls').json()['available'])
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                wait_for(lambda: client.app.state.pose is not None)
                self.assertEqual(client.post('/capture/ingest', content=geometry([anchor()])).status_code, 200)
                state = client.get('/capture/walls').json()
                self.assertTrue(state['available'])
                self.assertEqual(state['session_id'], 'map-session')
                self.assertEqual(len(state['walls']), 1)
                self.assertNotIn('sections', state)
                refined = copy.deepcopy(anchor())
                refined['extent']['width'] = 4
                client.post('/capture/ingest', content=geometry([refined], 3))
                self.assertNotEqual(client.get('/capture/walls').json()['walls'], state['walls'])
                client.post('/capture/ingest', content=geometry([], 4))
                self.assertEqual(client.get('/capture/walls').json()['walls'], [])
            wait_for(lambda: client.app.state.phone is None)
            self.assertFalse(client.get('/capture/walls').json()['available'])

    def test_old_geometry_cannot_return_after_tracking_loss(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            phone.send_bytes(frame())
            wait_for(lambda: client.app.state.pose is not None)
            client.post('/capture/ingest', content=geometry([anchor()]))
            client.app.state.tracking_lost_capture = 3
            self.assertFalse(client.get('/capture/walls').json()['available'])
