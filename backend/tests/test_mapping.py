"""Pure depth-to-world proof; no sockets, weights, or hardware."""
import io
import json
import struct
import unittest

import numpy as np
from PIL import Image

from backend.frame_bundle import parse_frame_bundle
from backend.localization import Detection, localize_detection
from backend.mapping import MappingError, build_point_chunk, depth_to_points, points_message

# Camera rotated +90 degrees about world Y and translated (1, 2, 3), as in test_localization.
TRANSFORM = [0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 2, 3, 1]


def jpeg(color=(200, 30, 30), size=(80, 60)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='JPEG', quality=95)
    return output.getvalue()


def bundle(depth, confidence, jpeg_bytes=None, transform=TRANSFORM, session='s', epoch=1):
    jpeg_bytes = jpeg_bytes or jpeg()
    dh, dw = depth.shape
    header = dict(version=1, type='frame', session_id=session, map_epoch=epoch, frame_id=9,
                  t_capture=3.5, t_wall_ms=1000, tracking='normal', transform=transform,
                  image=dict(width=80, height=60, jpeg_len=len(jpeg_bytes),
                             intrinsics=[40, 0, 0, 0, 40, 0, 40, 30, 1], orientation='landscape_right'),
                  depth=dict(width=dw, height=dh, format='float32_m', len=depth.nbytes),
                  confidence=dict(width=dw, height=dh, format='uint8_0_2', len=confidence.size))
    encoded = json.dumps(header).encode()
    return (struct.pack('<I', len(encoded)) + encoded + jpeg_bytes
            + depth.astype('<f4').tobytes() + confidence.astype('u1').tobytes())


def grids(depth=2., confidence=2):
    return np.full((15, 20), depth, dtype='<f4'), np.full((15, 20), confidence, dtype='u1')


class KnownPointTests(unittest.TestCase):
    def test_single_depth_pixel_lands_at_independently_worked_world_point(self):
        depth, conf = grids(confidence=0)
        depth[7, 15], conf[7, 15] = 2, 2
        # Depth cell (row 7, col 15) is JPEG (62, 30) -> optical (1.1, 0, 2) -> ARKit camera
        # (1.1, 0, -2); camera-to-world columns (0,0,-1), (0,1,0), (1,0,0), t=(1,2,3) give
        # 1.1*(0,0,-1) + -2*(1,0,0) + (1,2,3) = (-1, 2, 1.9).
        chunk = build_point_chunk(bundle(depth, conf), 's', 1, min_points=1)
        self.assertEqual(chunk.positions.shape, (1, 3))
        np.testing.assert_allclose(chunk.positions[0], [-1, 2, 1.9], atol=1e-6)
        self.assertEqual((chunk.session_id, chunk.map_epoch, chunk.frame_id, chunk.t_capture), ('s', 1, 9, 3.5))

    def test_agrees_with_detection_localization_geometry(self):
        depth, conf = grids(confidence=0)
        depth[7, 15], conf[7, 15] = 2, 2
        frame = parse_frame_bundle(bundle(depth, conf), session_id='s', map_epoch=1)
        located = localize_detection(frame, Detection((60, 28, 64, 32), 'x', .9), min_samples=1)
        chunk = depth_to_points(frame, min_points=1)
        np.testing.assert_allclose(chunk.positions[0], located.position, atol=1e-9)

    def test_color_comes_from_the_jpeg_pixel_under_the_depth_pixel(self):
        image = Image.new('RGB', (80, 60), (255, 0, 0))
        image.paste((0, 0, 255), (40, 0, 80, 60))
        out = io.BytesIO()
        image.save(out, format='JPEG', quality=100, subsampling=0)
        depth, conf = grids()
        chunk = build_point_chunk(bundle(depth, conf, out.getvalue()), 's', 1, max_points=1000)
        left = chunk.positions[:, 2] > 3  # camera x < 0 -> world z = 3 - x_cam
        self.assertTrue(left.any() and (~left).any())
        self.assertGreater(chunk.colors[left, 0].min(), .9)
        self.assertGreater(chunk.colors[~left, 2].min(), .9)


class DepthQualityTests(unittest.TestCase):
    def test_only_finite_positive_in_range_high_confidence_depth_survives(self):
        depth, conf = grids(confidence=0)
        good = [(r, c) for r in range(2) for c in range(10)]
        for r, c in good:
            depth[r, c], conf[r, c] = 2, 2
        bad = {'nan': np.nan, 'inf': np.inf, '-inf': -np.inf, 'zero': 0., 'negative': -1., 'far': 50.,
               'near': .001}
        for row, value in enumerate(bad.values(), start=4):
            depth[row, :], conf[row, :] = value, 2
        depth[12, :], conf[12, :] = 2, 1  # medium confidence
        depth[13, :], conf[13, :] = 2, 0  # low confidence
        chunk = build_point_chunk(bundle(depth, conf), 's', 1, max_points=10000)
        self.assertEqual(len(chunk.positions), len(good))
        self.assertTrue(np.all(np.isfinite(chunk.positions)))

    def test_unreliable_frames_are_rejected(self):
        for name, depth, conf in [
            ('low confidence', *grids(confidence=0)),
            ('medium confidence', *grids(confidence=1)),
            ('nan', *grids(depth=np.nan)),
            ('zero', *grids(depth=0.)),
            ('too far', *grids(depth=30.)),
        ]:
            with self.subTest(name), self.assertRaises(MappingError):
                build_point_chunk(bundle(depth, conf), 's', 1)

    def test_too_few_valid_pixels_are_rejected(self):
        depth, conf = grids(confidence=0)
        conf[0, :5] = 2
        with self.assertRaises(MappingError):
            build_point_chunk(bundle(depth, conf), 's', 1)  # 5 < default minimum

    def test_output_is_bounded_and_deterministic(self):
        depth, conf = grids()
        first = build_point_chunk(bundle(depth, conf), 's', 1, max_points=100)
        second = build_point_chunk(bundle(depth, conf), 's', 1, max_points=100)
        self.assertEqual(len(first.positions), 100)
        np.testing.assert_array_equal(first.positions, second.positions)
        self.assertTrue(((0 <= first.colors) & (first.colors <= 1)).all())
        self.assertFalse(first.positions.flags.writeable)

    def test_bundle_for_another_session_is_never_mapped(self):
        from backend.frame_bundle import FrameValidationError
        depth, conf = grids()
        with self.assertRaises(FrameValidationError):
            build_point_chunk(bundle(depth, conf, session='other'), 's', 1)
        with self.assertRaises(FrameValidationError):
            build_point_chunk(bundle(depth, conf, epoch=2), 's', 1)


class MessageTests(unittest.TestCase):
    def test_points_message_is_flat_rounded_and_versioned(self):
        depth, conf = grids()
        chunk = build_point_chunk(bundle(depth, conf), 's', 1, max_points=50)
        message = points_message(chunk, 7)
        self.assertEqual((message['version'], message['type'], message['chunk_id']), (1, 'points', 7))
        self.assertEqual(len(message['positions']), 150)
        self.assertEqual(len(message['colors']), 150)
        self.assertEqual((message['session_id'], message['map_epoch'], message['frame_id']), ('s', 1, 9))
        json.dumps(message, allow_nan=False)
        self.assertEqual(message['positions'][0], round(message['positions'][0], 3))


if __name__ == '__main__':
    unittest.main()


class NavigationConfidenceTests(unittest.TestCase):
    def test_medium_confidence_is_explicit_and_low_confidence_stays_excluded(self):
        depth, confidence = grids(confidence=1)
        confidence[0, :] = 0
        data = bundle(depth, confidence)
        with self.assertRaises(MappingError): build_point_chunk(data, 's', 1)
        chunk = build_point_chunk(data, 's', 1, min_confidence=1)
        self.assertEqual(len(chunk.positions), 14 * 20)
        with self.assertRaises(ValueError): build_point_chunk(data, 's', 1, min_confidence=0)
