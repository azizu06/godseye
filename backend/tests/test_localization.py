"""Public-seam accuracy proof; no YOLO weights or Apple hardware required."""
import io
import json
import struct
import unittest

import numpy as np
from PIL import Image

from backend.frame_bundle import parse_frame_bundle
from backend.localization import Detection, localize_detection


def fixture():
    jpeg = io.BytesIO()
    Image.new('RGB', (80, 60)).save(jpeg, format='JPEG')
    # Camera rotated +90 degrees about world Y, translated (1, 2, 3).
    header = dict(version=1, type='frame', session_id='synthetic', map_epoch=2,
                  frame_id=7, t_capture=12.5, t_wall_ms=1000, tracking='normal',
                  transform=[0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 2, 3, 1],
                  image=dict(width=80, height=60, jpeg_len=len(jpeg.getvalue()),
                             intrinsics=[40, 0, 0, 0, 40, 0, 40, 30, 1],
                             orientation='landscape_right'),
                  depth=dict(width=20, height=15, format='float32_m', len=1200),
                  confidence=dict(width=20, height=15, format='uint8_0_2', len=300))
    depth = np.full((15, 20), 50, dtype='<f4')
    confidence = np.zeros((15, 20), dtype='u1')
    depth[9:11, 14:16] = [[2, 2.01], [1.99, 25]]
    confidence[9:11, 14:16] = 2
    return header, jpeg.getvalue(), depth, confidence


def bundle(header, jpeg, depth, confidence):
    encoded = json.dumps(header).encode()
    return struct.pack('<I', len(encoded)) + encoded + jpeg + depth.tobytes() + confidence.tobytes()


class LocalizationTests(unittest.TestCase):
    def test_known_rotated_translated_point_with_outlier_within_five_cm(self):
        frame = parse_frame_bundle(bundle(*fixture()), session_id='synthetic', map_epoch=2)
        result = localize_detection(frame, Detection((56, 36, 64, 44), 'backpack', .9))
        # JPEG center (60,40), optical point (1,.5,2), ARKit (1,-.5,-2),
        # independently worked world point (-1,1.5,2).
        self.assertLess(np.linalg.norm(np.array(result.position) - [-1, 1.5, 2]), .05)
        self.assertEqual(result.depth_samples, 4)


class RejectionTests(unittest.TestCase):
    def test_malformed_and_misaligned_bundles_are_rejected(self):
        from backend.frame_bundle import FrameValidationError
        mutations = [
            ('version', lambda h: h.update(version=2)),
            ('boolean version', lambda h: h.update(version=True)),
            ('session', lambda h: h.update(session_id='other')),
            ('epoch', lambda h: h.update(map_epoch=3)),
            ('tracking', lambda h: h.update(tracking='limited')),
            ('transform length', lambda h: h.update(transform=[1])),
            ('transform axes', lambda h: h['transform'].__setitem__(0, 4)),
            ('intrinsics', lambda h: h['image']['intrinsics'].__setitem__(0, 0)),
            ('orientation', lambda h: h['image'].update(orientation='portrait')),
            ('JPEG dimensions', lambda h: h['image'].update(width=160, height=120)),
            ('depth length', lambda h: h['depth'].update(len=4)),
            ('depth format', lambda h: h['depth'].update(format='float16')),
            ('confidence shape', lambda h: h['confidence'].update(width=19)),
            ('missing field', lambda h: h.pop('frame_id')),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                h, jpeg, depth, conf = fixture()
                mutate(h)
                with self.assertRaises(FrameValidationError):
                    parse_frame_bundle(bundle(h, jpeg, depth, conf), session_id='synthetic', map_epoch=2)
        good = bundle(*fixture())
        for payload in (b'', b'\xff' * 4, b'\x01\x00\x00\x00{', good[:-1], good + b'x'):
            with self.subTest(payload_length=len(payload)), self.assertRaises(FrameValidationError):
                parse_frame_bundle(payload, session_id='synthetic', map_epoch=2)
        h, jpeg, depth, conf = fixture()
        for invalid_jpeg in (b'x' * len(jpeg), jpeg[:20] + b'x' * (len(jpeg) - 20)):
            with self.assertRaises(FrameValidationError):
                parse_frame_bundle(bundle(h, invalid_jpeg, depth, conf), session_id='synthetic', map_epoch=2)
        conf[0, 0] = 3
        with self.assertRaises(FrameValidationError):
            parse_frame_bundle(bundle(h, jpeg, depth, conf), session_id='synthetic', map_epoch=2)

    def test_optional_pose_must_match_capture_frame_and_transform(self):
        from backend.frame_bundle import FrameValidationError
        h, jpeg, depth, conf = fixture()
        pose = {**h, 'type': 'pose'}
        parse_frame_bundle(bundle(h, jpeg, depth, conf), session_id='synthetic', map_epoch=2, pose=pose)
        for key, value in [('frame_id', 8), ('t_capture', 12.6), ('map_epoch', 4), ('transform', [0] * 16)]:
            with self.subTest(key=key), self.assertRaises(FrameValidationError):
                parse_frame_bundle(bundle(h, jpeg, depth, conf), session_id='synthetic', map_epoch=2,
                                   pose={**pose, key: value})

    def test_unreliable_depth_and_bad_boxes_are_rejected(self):
        from backend.localization import LocalizationError
        for name in ('medium', 'invalid', 'sparse'):
            h, jpeg, depth, conf = fixture()
            if name == 'medium':
                conf[:] = 1
            elif name == 'invalid':
                depth[9:11, 14:16] = [[np.nan, np.inf], [0, -1]]
            else:
                conf[9:11, 14:16] = [[2, 0], [0, 0]]
            frame = parse_frame_bundle(bundle(h, jpeg, depth, conf), session_id='synthetic', map_epoch=2)
            with self.subTest(name=name), self.assertRaises(LocalizationError):
                localize_detection(frame, Detection((56, 36, 64, 44), 'backpack', .9))
        frame = parse_frame_bundle(bundle(*fixture()), session_id='synthetic', map_epoch=2)
        for box in ((0, 0, 80, 60), (56, 36, 56, 44), (-1, 36, 64, 44), (56, 36, 81, 44),
                    (56, 36, float('nan'), 44), (56, 36, 57, 37)):
            with self.subTest(box=box), self.assertRaises(LocalizationError):
                localize_detection(frame, Detection(box, 'backpack', .9))
        with self.assertRaises(LocalizationError):
            localize_detection(frame, Detection((56, 36, 64, 44), 'backpack', .1))

    def test_fractional_box_uses_depth_pixel_centers_and_row_major_layout(self):
        frame = parse_frame_bundle(bundle(*fixture()), session_id='synthetic', map_epoch=2)
        result = localize_detection(frame, Detection((57.9, 37.9, 62.1, 42.1), 'backpack', .9))
        self.assertEqual(result.depth_samples, 4)
        self.assertAlmostEqual(result.depth_m, 2.005, places=5)


    def test_invalid_depth_samples_do_not_contaminate_valid_median(self):
        h, jpeg, depth, conf = fixture()
        conf[9:12, 14:17] = 2
        depth[9:12, 14:17] = [[2, 2, 2.01], [np.nan, np.inf, 0], [-1, 50, 2]]
        frame = parse_frame_bundle(bundle(h, jpeg, depth, conf), session_id='synthetic', map_epoch=2)
        result = localize_detection(frame, Detection((56, 36, 68, 48), 'backpack', .9))
        self.assertEqual(result.depth_samples, 5)
        self.assertEqual(result.depth_m, 2)


if __name__ == '__main__':
    unittest.main()
