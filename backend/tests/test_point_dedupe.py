"""Voxel dedupe for live points: pure selection, bounded memory, env knobs, per-frame cost."""
from dataclasses import replace
import io
import json
import struct
import time
import unittest

import numpy as np
from PIL import Image

from backend.mapping import PointChunk, build_point_chunk, points_message
from backend.point_dedupe import (MAX_POINTS_PER_CHUNK, NoNewPoints, PointSettings, VoxelMemory,
                                  voxel_keys)
from backend.tests.test_mapping import TRANSFORM, bundle, grids

MOVED = TRANSFORM[:12] + [11, 2, 3, 1]  # same camera, ten meters along world +X
DASHBOARD_MAX_CHARS = 512_000  # the viewport rejects larger points messages


def candidates(transform=TRANSFORM, depth=2., shape=(15, 20), t_capture=3.5, samples=10_000):
    depth_grid = np.full(shape, depth, dtype='<f4')
    confidence = np.full(shape, 2, dtype='u1')
    chunk = build_point_chunk(bundle(depth_grid, confidence, transform=transform), 's', 1,
                              max_points=samples)
    return replace(chunk, t_capture=t_capture)


def full_depth_frame(t_capture=3.5, transform=TRANSFORM):
    """A 256x192 ARKit-sized depth map: every candidate a distinct pixel on a 2 m wall."""
    return candidates(transform=transform, shape=(192, 256), t_capture=t_capture)


def phone_sized_bundle():
    """A bundle shaped like the iPhone's: 960x720 JPEG, 256x192 depth and confidence."""
    image = Image.new('RGB', (960, 720), (90, 120, 150))
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=80)
    jpeg_bytes = out.getvalue()
    depth = np.full((192, 256), 2., dtype='<f4')
    confidence = np.full((192, 256), 2, dtype='u1')
    header = dict(version=1, type='frame', session_id='s', map_epoch=1, frame_id=9, t_capture=3.5,
                  t_wall_ms=1000, tracking='normal', transform=TRANSFORM,
                  image=dict(width=960, height=720, jpeg_len=len(jpeg_bytes),
                             intrinsics=[720, 0, 0, 0, 720, 0, 480, 360, 1], orientation='landscape_right'),
                  depth=dict(width=256, height=192, format='float32_m', len=depth.nbytes),
                  confidence=dict(width=256, height=192, format='uint8_0_2', len=confidence.size))
    encoded = json.dumps(header).encode()
    return struct.pack('<I', len(encoded)) + encoded + jpeg_bytes + depth.tobytes() + confidence.tobytes()


def sent(memory, chunk):
    """Select a chunk and record it as published, like the backend's accept step."""
    picked = memory.select(chunk)
    memory.commit(picked.voxel_keys, picked.t_capture)
    return picked


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = PointSettings.from_env({})
        self.assertEqual(settings, PointSettings())
        self.assertEqual((settings.voxel_m, settings.samples, settings.per_chunk, settings.refresh_s,
                          settings.max_voxels), (.02, 10_000, 2500, 60., 2_000_000))

    def test_env_values_are_parsed_and_empty_means_default(self):
        settings = PointSettings.from_env({
            'GODSEYE_POINT_VOXEL_M': '0.05', 'GODSEYE_POINT_SAMPLES': '20000',
            'GODSEYE_POINTS_PER_CHUNK': '1000', 'GODSEYE_POINT_REFRESH_S': '0',
            'GODSEYE_POINT_MAX_VOXELS': ''})
        self.assertEqual(settings, PointSettings(voxel_m=.05, samples=20_000, per_chunk=1000,
                                                 refresh_s=0., max_voxels=2_000_000))

    def test_zero_voxel_size_disables_dedupe(self):
        self.assertFalse(PointSettings.from_env({'GODSEYE_POINT_VOXEL_M': '0'}).dedupe)
        self.assertTrue(PointSettings.from_env({}).dedupe)

    def test_bad_values_fail_loudly_and_name_the_variable(self):
        for name, value in [
            ('GODSEYE_POINT_VOXEL_M', 'two cm'), ('GODSEYE_POINT_VOXEL_M', '-0.02'),
            ('GODSEYE_POINT_VOXEL_M', 'nan'), ('GODSEYE_POINT_VOXEL_M', '2'),
            ('GODSEYE_POINT_SAMPLES', '0'), ('GODSEYE_POINT_SAMPLES', '49153'),
            ('GODSEYE_POINT_SAMPLES', '1e4'), ('GODSEYE_POINTS_PER_CHUNK', '2501'),
            ('GODSEYE_POINTS_PER_CHUNK', '0'), ('GODSEYE_POINTS_PER_CHUNK', '2.5'),
            ('GODSEYE_POINT_REFRESH_S', '-1'), ('GODSEYE_POINT_REFRESH_S', 'inf'),
            ('GODSEYE_POINT_MAX_VOXELS', '0'), ('GODSEYE_POINT_MAX_VOXELS', '20000001'),
        ]:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, name):
                    PointSettings.from_env({name: value})

    def test_per_chunk_hard_max_matches_the_dashboard_limit(self):
        self.assertEqual(MAX_POINTS_PER_CHUNK, 2500)
        with self.assertRaises(ValueError):
            PointSettings(per_chunk=MAX_POINTS_PER_CHUNK + 1)


class SelectionTests(unittest.TestCase):
    def test_repeated_identical_frame_has_nothing_new(self):
        memory = VoxelMemory(PointSettings())
        first = sent(memory, candidates())
        self.assertGreater(len(first.positions), 0)
        with self.assertRaises(NoNewPoints):
            memory.select(candidates(t_capture=3.75))

    def test_unpublished_selection_is_not_remembered(self):
        # A chunk dropped as stale after computing never reached a viewer.
        memory = VoxelMemory(PointSettings())
        first = memory.select(candidates())
        again = memory.select(candidates(t_capture=3.75))
        np.testing.assert_array_equal(first.positions, again.positions)

    def test_camera_moving_to_a_new_area_emits_new_points(self):
        memory = VoxelMemory(PointSettings())
        sent(memory, candidates())
        moved = sent(memory, candidates(transform=MOVED, t_capture=3.75))
        self.assertGreater(len(moved.positions), 0)
        np.testing.assert_allclose(moved.positions[:, 0], 9, atol=1e-6)  # the wall at x = 11 - 2

    def test_partly_overlapping_view_sends_only_the_unseen_part(self):
        memory = VoxelMemory(PointSettings())
        sent(memory, candidates())
        shifted = TRANSFORM[:12] + [1, 2, 4, 1]  # one meter along the 4 m wide wall
        picked = sent(memory, candidates(transform=shifted, t_capture=3.75))
        # The first view covered world z in [1, 5]; only z > 5 is new.
        self.assertTrue(len(picked.positions) and (picked.positions[:, 2] > 5 - .02).all())

    def test_one_point_per_voxel_and_keys_match_positions(self):
        memory = VoxelMemory(PointSettings(voxel_m=.5))
        picked = memory.select(full_depth_frame())
        self.assertEqual(len(np.unique(picked.voxel_keys)), len(picked.positions))
        np.testing.assert_array_equal(picked.voxel_keys, voxel_keys(picked.positions, .5))

    def test_chunk_is_capped_spread_across_the_image_and_fits_the_dashboard(self):
        chunk = full_depth_frame()
        self.assertEqual(len(chunk.positions), 10_000)
        picked = VoxelMemory(PointSettings()).select(chunk)
        self.assertEqual(len(picked.positions), 2500)
        # Evenly spread, not first-N: the picks reach both ends of the wall's height.
        height = np.ptp(chunk.positions[:, 1])
        self.assertGreater(np.ptp(picked.positions[:, 1]), .95 * height)
        self.assertFalse(picked.positions.flags.writeable)
        self.assertLess(len(json.dumps(points_message(picked, 1))), DASHBOARD_MAX_CHARS)

    def test_worst_case_message_at_the_cap_fits_the_dashboard(self):
        far = np.full((MAX_POINTS_PER_CHUNK, 3), -1234.5678)
        chunk = PointChunk(far, np.full((MAX_POINTS_PER_CHUNK, 3), .1234), 's' * 36, 99, 10**9,
                           123456.789)
        message = json.dumps(points_message(chunk, 10**9))
        self.assertLess(len(message), DASHBOARD_MAX_CHARS)

    def test_later_frames_fill_in_what_the_cap_left_out(self):
        memory = VoxelMemory(PointSettings())
        first = sent(memory, full_depth_frame())
        second = sent(memory, full_depth_frame(t_capture=3.75))
        self.assertEqual(len(second.positions), 2500)
        self.assertFalse(np.isin(second.voxel_keys, first.voxel_keys).any())

    def test_per_chunk_setting_bounds_the_chunk(self):
        picked = VoxelMemory(PointSettings(per_chunk=100)).select(full_depth_frame())
        self.assertEqual(len(picked.positions), 100)

    def test_disabled_dedupe_keeps_resending_but_stays_capped(self):
        memory = VoxelMemory(PointSettings(voxel_m=0, per_chunk=500))
        for t in (3.5, 3.75):
            picked = sent(memory, full_depth_frame(t_capture=t))
            self.assertEqual(len(picked.positions), 500)
            self.assertIsNone(picked.voxel_keys)
        self.assertEqual(len(memory), 0)


class RefreshAndMemoryTests(unittest.TestCase):
    def test_voxel_is_resent_after_the_refresh_age(self):
        memory = VoxelMemory(PointSettings(refresh_s=60))
        sent(memory, candidates(t_capture=100.))
        with self.assertRaises(NoNewPoints):
            memory.select(candidates(t_capture=159.))
        refreshed = sent(memory, candidates(t_capture=161.))
        self.assertGreater(len(refreshed.positions), 0)
        with self.assertRaises(NoNewPoints):  # the resend restarts its age
            memory.select(candidates(t_capture=200.))

    def test_zero_refresh_never_resends(self):
        memory = VoxelMemory(PointSettings(refresh_s=0))
        sent(memory, candidates(t_capture=1.))
        with self.assertRaises(NoNewPoints):
            memory.select(candidates(t_capture=10_000.))

    def test_capture_clock_going_backwards_counts_as_old(self):
        memory = VoxelMemory(PointSettings(refresh_s=60))
        sent(memory, candidates(t_capture=1000.))
        self.assertGreater(len(memory.select(candidates(t_capture=10.)).positions), 0)

    def test_memory_cap_evicts_oldest_voxels_first(self):
        memory = VoxelMemory(PointSettings(max_voxels=1000, refresh_s=0))
        batches = []
        for t in range(1, 5):  # 4 x 400 distinct voxels, oldest first
            keys = np.arange(t * 10_000, t * 10_000 + 400, dtype=np.int64)
            memory.commit(keys, float(t))
            batches.append(keys)
            memory.select(candidates(t_capture=float(t)))  # a frame folds and trims the memory
        self.assertLessEqual(len(memory), 1000)
        remembered = memory.remembered()
        self.assertFalse(np.isin(batches[0], remembered).any())  # oldest evicted
        self.assertTrue(np.isin(batches[-1], remembered).all())  # newest kept

    def test_reset_forgets_everything(self):
        memory = VoxelMemory(PointSettings())
        sent(memory, candidates())
        self.assertGreater(len(memory), 0)
        memory.reset()
        self.assertEqual(len(memory), 0)
        self.assertGreater(len(memory.select(candidates(t_capture=3.75)).positions), 0)


class PerFrameCostTests(unittest.TestCase):
    """Worker-thread cost per frame must stay far below the 250 ms chunk interval."""

    def test_ten_thousand_candidates_against_a_full_memory(self):
        settings = PointSettings()
        memory = VoxelMemory(settings)
        rng = np.random.default_rng(1)
        memory.commit(np.unique(rng.integers(0, 1 << 62, settings.max_voxels)), 1.)
        memory.select(candidates(t_capture=1.))  # fold the prefill outside the timed loop
        timings = []
        for step in range(5):  # each frame a new wall, so every one has new voxels to insert
            chunk = full_depth_frame(t_capture=2. + step, transform=TRANSFORM[:12] + [1 + 10 * step, 2, 3, 1])
            start = time.perf_counter()
            picked = memory.select(chunk)
            memory.commit(picked.voxel_keys, picked.t_capture)
            timings.append(time.perf_counter() - start)
        self.assertGreaterEqual(len(memory), settings.max_voxels * .9)
        self.assertLess(sorted(timings)[len(timings) // 2], .25)

    def test_full_frame_parse_backproject_and_select(self):
        payload = phone_sized_bundle()
        memory = VoxelMemory(PointSettings())
        timings = []
        for _ in range(5):
            start = time.perf_counter()
            memory.select(build_point_chunk(payload, 's', 1, max_points=10_000))
            timings.append(time.perf_counter() - start)
        self.assertLess(sorted(timings)[len(timings) // 2], .25)


if __name__ == '__main__':
    unittest.main()
