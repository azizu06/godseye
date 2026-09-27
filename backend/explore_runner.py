"""Stop–align–settle–observe loop; all nonzero commands retain Navigator's gates."""

import asyncio
import logging
import math
import time
from dataclasses import replace
import numpy as np
from backend.exploration import (
    ScanEvidence,
    ScanWitness,
    ViewLedger,
    untried_frontier,
    select_view,
    supported_view,
    usable,
    visible_boundary,
    wrap,
)
from backend.navigation import PurePursuit, path_blocked

logger = logging.getLogger(__name__)


def known_ground(snapshot):
    rows, cols = np.nonzero(snapshot.cells)
    # World-lattice identity survives cropping and expansion on either axis.
    cx, cz = round(snapshot.origin[0] / snapshot.cell_m), round(
        snapshot.origin[1] / snapshot.cell_m
    )
    return set(zip((cols + cx).tolist(), (rows + cz).tolist()))


def clear_motion(snapshot, previous, pose, v, w, period, now, max_age):
    start = previous or (pose.x, pose.z)
    distance = math.dist(start, (pose.x, pose.z))
    steps = max(1, math.ceil(distance / (snapshot.cell_m / 2)))
    if steps > 200:
        return False
    for i in range(steps + 1):
        x, z = (
            start[0] + (pose.x - start[0]) * i / steps,
            start[1] + (pose.z - start[1]) * i / steps,
        )
        if not snapshot.fresh_clearance(x, z, now, max_age):
            return False
    heading = pose.yaw_rad + w * period / 2
    return snapshot.fresh_clearance(
        pose.x + v * math.sin(heading) * period,
        pose.z + v * math.cos(heading) * period,
        now,
        max_age,
    )


async def run_exploration(nav, generation):
    from backend.navigator import map_problem, planning_grid

    s, cfg = nav.settings, nav.settings.exploration
    status = nav.exploration_status
    status["phase"] = "settling"
    period = 1 / s.rate_hz
    deadline = time.monotonic() + cfg.max_runtime_s
    snapshot_job = selection_job = None
    snapshot = scope = follower = evidence = anchor = previous = None
    target_yaw = stable_since = view_since = scan_since = None
    known_surfaces = ground = None
    ledger = ViewLedger()
    witness = ScanWitness(cfg)
    attempted_frontiers = set()
    low_gain = 0
    last_snapshot = -math.inf
    progress = None
    try:
        while True:
            now = time.monotonic()
            if nav._armed_mode() != "explore":
                return nav._finish("disarmed")
            pose = nav._pose()
            if pose is None or pose.age_s > s.pose_max_age_s:
                return nav._finish("pose_stale")
            if pose.tracking != "normal":
                return nav._finish("tracking_lost")
            if now > deadline or status["observed_views"] >= cfg.max_views:
                return nav._finish("scan_budget")
            if snapshot_job is not None and snapshot_job.done():
                snapshot = snapshot_job.result()
                snapshot_job = None
                if snapshot is None:
                    return nav._finish("map_unknown")
            if snapshot_job is None and now - last_snapshot >= s.blocked_check_s:
                last_snapshot = now
                snapshot_job = asyncio.create_task(asyncio.to_thread(nav._occupancy))
            if snapshot is None:
                if not nav._submit(generation, "explore", 0.0, 0.0):
                    return nav._finish("command_stale")
                await asyncio.sleep(period)
                continue
            if problem := map_problem(snapshot, s.map_max_age_s):
                return nav._finish(problem)
            if scope is None:
                scope = snapshot.session
                status.update(session_id=scope[0], map_epoch=scope[1])
                known_surfaces = (
                    set(snapshot.observation.surface_keys)
                    if snapshot.observation
                    else set()
                )
                ground = known_ground(snapshot)
            elif snapshot.session != scope:
                return nav._finish("map_reset")
            if pose.t_capture is None:
                return nav._finish("scan_capture_unusable")
            obs = snapshot.observation
            if not usable(obs, cfg):
                return nav._finish("scan_capture_unusable")
            if not supported_view(obs):
                return nav._finish("scan_view_unsupported")
            if len(known_surfaces) > cfg.max_surface_keys:
                return nav._finish("scan_budget")
            camera_yaw = wrap(pose.yaw_rad + pose.camera_yaw_offset)
            if target_yaw is None:
                target_yaw = camera_yaw
                anchor = (pose.x, pose.z, pose.yaw_rad)
                stable_since = view_since = now
            v = w = 0.0
            phase = status["phase"]
            if phase == "selecting":
                if selection_job is None:
                    grid, config = planning_grid(snapshot, s)
                    selection_job = asyncio.create_task(
                        asyncio.to_thread(
                            select_view,
                            grid,
                            config,
                            (pose.x, pose.z),
                            camera_yaw,
                            obs,
                            snapshot.floor_y,
                            ledger,
                            cfg,
                        )
                    )
                elif selection_job.done():
                    selection = selection_job.result()
                    selection_job = None
                    if selection.reason:
                        if selection.reason == "scan_views_exhausted":
                            reason = (
                                "scan_accessible_exhausted"
                                if low_gain >= cfg.low_gain_views
                                else "scan_gain_unsettled"
                            )
                            return nav._finish(reason)
                        return nav._finish(selection.reason)
                    view = selection.view
                    nav.goal = view.position
                    nav._show(view.points)
                    status.update(phase="moving", target=list(view.position))
                    target_yaw = view.yaw
                    follower = PurePursuit(
                        view.points,
                        replace(
                            s.follower,
                            arrive_tolerance_m=min(
                                s.follower.arrive_tolerance_m, cfg.stable_position_m / 2
                            ),
                        ),
                    )
            elif phase == "moving":
                grid, config = planning_grid(snapshot, s)
                if path_blocked(grid, follower.path, config, follower.segment):
                    return nav._finish("path_blocked")
                command = follower.step(pose.x, pose.z, pose.yaw_rad)
                if command.arrived:
                    nav._show([])
                    status["phase"] = "aligning"
                    view_since = now
                else:
                    v, w = command.v_mps, command.yaw_rate_rps
            elif phase == "aligning":
                error = wrap(target_yaw - camera_yaw)
                if abs(error) <= cfg.stable_yaw_rad:
                    status["phase"] = "settling"
                    anchor = (pose.x, pose.z, pose.yaw_rad)
                    stable_since = now
                else:
                    maximum = min(0.5, s.follower.max_yaw_rate_rps)
                    w = (
                        math.copysign(maximum, error)
                        if s.follower.pivot_only
                        else max(-maximum, min(maximum, error * 2))
                    )
            elif phase in ("settling", "scanning"):
                drift = (
                    math.dist((pose.x, pose.z), anchor[:2]) > cfg.stable_position_m
                    or abs(wrap(pose.yaw_rad - anchor[2])) > cfg.stable_yaw_rad
                )
                if drift:
                    status["phase"] = (
                        "aligning"
                        if abs(wrap(target_yaw - camera_yaw)) > cfg.stable_yaw_rad
                        else "settling"
                    )
                    anchor = (pose.x, pose.z, pose.yaw_rad)
                    stable_since = now
                    evidence = None
                elif phase == "settling" and now - stable_since >= cfg.settle_s:
                    # The newest settled POSE capture watermark, not integration
                    # time, excludes even a queued pre-arrival frame accepted late.
                    evidence = ScanEvidence(
                        pose.t_capture, anchor[:2], target_yaw, known_surfaces, cfg
                    )
                    scan_since = now
                    status["phase"] = "scanning"
                elif phase == "scanning":
                    if not evidence.ready:
                        if now - scan_since > cfg.capture_timeout_s:
                            return nav._finish("scan_capture_unusable")
                        # Dense captures can contain 20k samples. Run novelty
                        # membership off-loop while explicitly holding zero.
                        # Re-enter the loop before crediting: fresh pose, drift,
                        # scope and arm checks must still pass after this await.
                        if not nav._submit(generation, "explore", 0.0, 0.0):
                            return nav._finish("command_stale")
                        await asyncio.to_thread(evidence.accept, obs)
                        await asyncio.sleep(period)
                        continue
                    if evidence.ready:
                        new_surfaces = evidence.new_surface_keys
                        if (
                            len(known_surfaces) + len(new_surfaces)
                            > cfg.max_surface_keys
                        ):
                            return nav._finish("scan_budget")
                        current_ground = known_ground(snapshot)
                        gain = len(current_ground - ground)
                        surface_gain = len(new_surfaces)
                        ground |= current_ground
                        known_surfaces |= new_surfaces
                        status.update(
                            observed_views=status["observed_views"] + 1,
                            gained_cells=status["gained_cells"] + gain,
                            last_gain_cells=gain,
                            gained_surface_voxels=status["gained_surface_voxels"]
                            + surface_gain,
                            last_gain_surface_voxels=surface_gain,
                        )
                        low_gain = (
                            low_gain + 1
                            if gain < cfg.low_gain_cells
                            and surface_gain < cfg.low_gain_surface_voxels
                            else 0
                        )
                        grid, _ = planning_grid(snapshot, s)
                        signature = visible_boundary(
                            grid, anchor[:2], target_yaw, obs, snapshot.floor_y, cfg
                        )
                        ledger.record(anchor[:2], target_yaw, signature)
                        witness.record(anchor[:2], target_yaw, gain, surface_gain)
                        if (
                            gain < cfg.low_gain_cells
                            and surface_gain < cfg.low_gain_surface_voxels
                        ):
                            attempted_frontiers.update(signature)
                        if witness.saturated and not untried_frontier(
                            grid, attempted_frontiers
                        ):
                            return nav._finish("scan_diminishing_returns")
                        status["phase"] = "selecting"
                        status["target"] = None
                        evidence = None
                    elif now - scan_since > cfg.capture_timeout_s:
                        return nav._finish("scan_capture_unusable")
            if (
                status["phase"] in ("aligning", "settling", "scanning")
                and now - view_since > cfg.view_timeout_s
            ):
                return nav._finish("scan_timeout")
            if v or w:
                if not clear_motion(
                    snapshot, previous, pose, v, w, period, now, s.map_max_age_s
                ):
                    return nav._finish("sensing_clearance_unknown")
                previous = (pose.x, pose.z)
                if (
                    progress is None
                    or math.dist((pose.x, pose.z), progress[:2]) >= s.progress_m
                    or abs(wrap(pose.yaw_rad - progress[2])) >= s.progress_rad
                ):
                    progress = (pose.x, pose.z, pose.yaw_rad, now)
                elif now - progress[3] >= s.no_progress_s:
                    return nav._finish("no_progress")
            else:
                progress = None
            if not nav._submit(generation, "explore", v, w):
                return nav._finish("command_stale")
            await asyncio.sleep(period)
    except Exception:
        logger.exception("exploration failed")
        return nav._finish("nav_error")
    finally:
        for job in (snapshot_job, selection_job):
            if job is not None:
                job.cancel()
