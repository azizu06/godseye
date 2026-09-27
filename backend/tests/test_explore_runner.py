"""Ideal-visibility fixture for policy sequencing; does not model a real phone mount."""
import asyncio
from dataclasses import replace
import math
import time
import unittest
import numpy as np
from backend.navigator import Navigator, NavSettings, RoverPose
from backend.navigation import FollowerConfig
from backend.exploration import ExploreSettings
from backend.occupancy import OccupancySnapshot, ScanObservation


class ScanHarness:
    def __init__(self, *, capture=True, blanks=False, scope=('TEST',1), **settings):
        self.x=self.z=.75;self.yaw=0.;self.armed=True;self.capture=capture;self.blanks=blanks
        self.scope=scope;self.stops=[];self.commands=[];self.command_phases=[];self.trace=[];self.frozen=time.monotonic()-10
        self.cells=np.ones((30,30),np.uint8);self.cells[[0,-1],:]=2;self.cells[:,[0,-1]]=2
        self.config=ExploreSettings(**dict(dict(max_positions=1,headings=4,settle_s=.01,capture_timeout_s=.12,
                                    view_timeout_s=2.,max_runtime_s=8.,max_views=20), **settings))
        self.nav=Navigator(NavSettings(rate_hz=200.,replan_s=.02,blocked_check_s=.01,
                           exploration=self.config, follower=FollowerConfig(pivot_only=True)),
                           pose=self.pose,occupancy=self.snapshot,submit=self.submit,
                           stop=self.stop,publish=lambda msg:None,armed_mode=lambda:'explore' if self.armed else None)
    def pose(self):
        return RoverPose(self.x,self.z,self.yaw,0.,'normal',time.monotonic())
    def snapshot(self):
        now=time.monotonic();stamp=now if self.capture else self.frozen
        obs=ScanObservation(stamp,int(stamp*1e6),(self.x,.1,self.z),self.yaw,math.pi/2,-.2,0.,
                            () if self.blanks else tuple(range(20)),0 if self.blanks else 20)
        return OccupancySnapshot(self.scope,1,now,(),.1,(0.,0.),.05,self.cells,0.,
                                 np.full(self.cells.shape,now),obs,np.arange(20))
    def submit(self,generation,mode,v,w):
        if not self.armed:return False
        self.command_phases.append((self.nav.exploration_status['phase'],v,w))
        self.commands.append((v,w));self.yaw+=w*.1
        self.x+=v*math.sin(self.yaw)*.1;self.z+=v*math.cos(self.yaw)*.1
        phase=self.nav.exploration_status['phase']
        if not self.trace or self.trace[-1]!=phase:self.trace.append(phase)
        return True
    def stop(self,reason):
        self.stops.append(reason);self.armed=False;self.nav.halt(reason);self.commands.append((0.,0.))
    async def run(self):
        self.nav.start_explore(1)
        deadline=time.monotonic()+10
        while self.nav.active and time.monotonic()<deadline:await asyncio.sleep(.005)
        if self.nav.active:raise AssertionError('exploration did not finish')


class ExploreRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_requires_stable_distinct_captures_and_keeps_motion_zero_during_dwell(self):
        h=ScanHarness()
        await h.run()
        # Position candidate truncation cannot claim complete, even after genuine views.
        self.assertEqual(h.stops,['scan_candidate_limit'])
        self.assertGreaterEqual(h.nav.exploration_status['observed_views'],4)
        self.assertIn('settling',h.trace);self.assertIn('scanning',h.trace)
        self.assertEqual(h.nav.exploration_status['phase'],'blocked')
        self.assertTrue(all(not v and not w for phase,v,w in h.command_phases if phase in ('settling','scanning')))

    async def test_replayed_prearrival_frames_never_finish_a_scan(self):
        h=ScanHarness(capture=False)
        await h.run()
        self.assertEqual(h.stops,['scan_capture_unusable'])
        self.assertEqual(h.nav.exploration_status['observed_views'],0)
        self.assertFalse(any(v or w for v,w in h.commands))

    async def test_blank_capture_is_not_low_gain_success(self):
        h=ScanHarness(blanks=True)
        await h.run()
        self.assertEqual(h.stops,['scan_capture_unusable'])
        self.assertEqual(h.nav.exploration_status['observed_views'],0)

    async def test_operator_stop_during_dwell_never_restarts(self):
        h=ScanHarness();h.nav.start_explore(1)
        while h.nav.exploration_status['phase']!='scanning':await asyncio.sleep(.002)
        h.stop('operator_stop');count=len(h.commands)
        await asyncio.sleep(.1)
        self.assertEqual(len(h.commands),count)
        self.assertFalse(h.nav.active)
        self.assertEqual(h.nav.exploration_status['observed_views'],0)

    async def test_scope_change_during_dwell_stops_without_credit(self):
        h=ScanHarness();h.nav.start_explore(1)
        while h.nav.exploration_status['phase']!='scanning':await asyncio.sleep(.002)
        h.scope=('OTHER',2)
        deadline=time.monotonic()+1
        while h.nav.active and time.monotonic()<deadline:await asyncio.sleep(.005)
        self.assertEqual(h.stops,['map_reset'])
        self.assertEqual(h.nav.exploration_status['observed_views'],0)

    async def test_exhausted_local_views_with_repeated_low_surface_gain_complete(self):
        h=ScanHarness(view_spacing_m=10.)
        await h.run()
        self.assertEqual(h.stops,['scan_accessible_exhausted'])
        self.assertEqual(h.nav.exploration_status['phase'],'complete')
        self.assertEqual(h.nav.exploration_status['observed_views'],4)
        self.assertEqual(h.nav.exploration_status['last_gain_surface_voxels'],0)

    async def test_alignment_cannot_bypass_full_footprint_clearance(self):
        h=ScanHarness(view_spacing_m=10.)
        original=h.snapshot
        def blind():
            snapshot=original()
            if h.nav.exploration_status['observed_views']:
                return replace(snapshot,free_at=np.full(snapshot.cells.shape,-math.inf))
            return snapshot
        h.snapshot=blind;h.nav._occupancy=blind
        await h.run()
        self.assertEqual(h.stops,['sensing_clearance_unknown'])
        self.assertFalse(any(v or w for v,w in h.commands))

    async def test_late_selection_cannot_restart_after_stop(self):
        import threading
        from unittest.mock import patch
        import backend.explore_runner as runner
        entered,release=threading.Event(),threading.Event()
        select=runner.select_view
        def delayed(*args):
            entered.set();release.wait(2);return select(*args)
        h=ScanHarness(view_spacing_m=10.)
        with patch.object(runner,'select_view',delayed):
            h.nav.start_explore(1)
            while not entered.is_set():await asyncio.sleep(.005)
            h.stop('operator_stop');count=len(h.commands);release.set()
            await asyncio.sleep(.1)
            self.assertEqual(len(h.commands),count)
            self.assertEqual(h.nav.exploration_status['phase'],'blocked')

    async def test_moving_pose_during_scan_requires_new_settle(self):
        h=ScanHarness(view_spacing_m=10.)
        h.nav.start_explore(1)
        while h.nav.exploration_status['phase']!='scanning':await asyncio.sleep(.002)
        h.x+=.10
        await asyncio.sleep(.006)
        self.assertEqual(h.nav.exploration_status['observed_views'],0)
        h.stop('operator_stop')
