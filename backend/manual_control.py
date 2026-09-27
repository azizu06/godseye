"""Dashboard gesture capabilities and class-independent swept motion checks."""
import math

from backend.navigator import map_problem, recovery_step_allowed


def capabilities(actuation, limits):
    if getattr(actuation, 'prototype', False):
        return dict(forward=[.05, min(.2, limits.max_speed_mps)], reverse=None,
                    yaw=[.05, min(.5, limits.max_yaw_rate_rps)], arcs=True)

    def rates(points, maximum):
        if not points or points[0].rate > maximum:
            return None
        return [points[0].rate, min(points[-1].rate, maximum)]

    forward = rates(actuation.forward, limits.max_speed_mps)
    reverse = rates(actuation.reverse, limits.max_speed_mps)
    left = rates(actuation.left, limits.max_yaw_rate_rps)
    right = rates(actuation.right, limits.max_yaw_rate_rps)
    yaw = None
    if left and right and max(left[0], right[0]) <= min(left[1], right[1]):
        yaw = [max(left[0], right[0]), min(left[1], right[1])]
    return dict(forward=forward, reverse=reverse, yaw=yaw, arcs=False)


def clearance_problem(snapshot, pose, session, settings, v, w):
    if (reason := map_problem(snapshot, settings.map_max_age_s)) is not None:
        return reason
    if snapshot.session != session:
        return 'map_unknown'
    if pose is None or pose.age_s > settings.pose_max_age_s:
        return 'pose_stale'
    if pose.tracking != 'normal':
        return 'tracking_lost'
    # Cover the maximum command lease plus transport allowance. Footprint
    # inflation retains the configured stopping envelope; no semantic labels.
    horizon = .5
    steps = max(1, math.ceil(abs(v) * horizon / (snapshot.cell_m / 2)))
    dt = horizon / steps
    x, z, yaw = pose.x, pose.z, pose.yaw_rad
    for _ in range(steps):
        heading = yaw + w * dt / 2
        nx, nz = x + v * math.sin(heading) * dt, z + v * math.cos(heading) * dt
        if not recovery_step_allowed(snapshot, x, z, nx, nz, settings.start_recovery_margin_m):
            return 'manual_path_blocked'
        x, z, yaw = nx, nz, yaw + w * dt
    return None
