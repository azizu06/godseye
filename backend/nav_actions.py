"""Voice-suggested navigation as proposals that only a human confirmation can act on.

The answer model may suggest `propose_navigation`, `propose_exploration` or
`stop_navigation`. Nothing here trusts that output to move anything: an entry is
checked against a strict schema, a proposal is validated against the active map with
the rover planner, and only `/nav/confirm` (a human action in the dashboard) hands a
validated destination to the same goal path as `/goal`, or selects explore mode, which
still needs a deliberate `/arm`. See NAV_ACTIONS.md.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, replace
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

NAV_ACTION_NAMES = ('propose_navigation', 'propose_exploration', 'stop_navigation')
MAX_COORD_M = 50.  # the occupancy grid covers 10 m around the origin; this only rejects nonsense

# Instruction lines for the answer model (backend/labels.py). The model names an object only by
# its grounding `ref` or by a class with exactly one stored object; voice.resolve_actions turns
# that into the stored id before anything leaves the backend.
NAV_ACTION_PROMPT = (
    'Rover suggestions, only when the user asks the rover to go somewhere, explore or stop, and then '
    'as the only action: propose_navigation {"target": "object", "ref": "o3"} (or {"target": "object", '
    '"class": name} when exactly one exists), or {"target": "point", "x": meters, "z": meters} only for '
    'coordinates the user said; propose_exploration {}; stop_navigation {}. They only put a suggestion '
    'on screen for a person to confirm: never say the rover is moving, and never mention speed, arming '
    'or steering.')


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra='forbid')


class NavigationArgs(_Strict):
    """Resolved destination: a stored object by id (voice resolves the model's ref) or a map point."""
    target: Literal['object', 'point']
    object_id: str | None = Field(default=None, min_length=1, max_length=64)
    class_: str | None = Field(default=None, alias='class', min_length=1, max_length=64)
    x: float | None = Field(default=None, ge=-MAX_COORD_M, le=MAX_COORD_M)
    z: float | None = Field(default=None, ge=-MAX_COORD_M, le=MAX_COORD_M)

    @model_validator(mode='after')
    def one_target(self):
        point, item = (self.x, self.z), (self.object_id, self.class_)
        if self.target == 'object':
            if None in item or point != (None, None):
                raise ValueError('an object target needs object_id and class only')
        elif None in point or item != (None, None):
            raise ValueError('a point target needs x and z only')
        return self


class NoArgs(_Strict):
    pass


ARG_MODELS = {'propose_navigation': NavigationArgs, 'propose_exploration': NoArgs, 'stop_navigation': NoArgs}


class NavAction(_Strict):
    id: str = Field(min_length=1, max_length=64)
    name: Literal['propose_navigation', 'propose_exploration', 'stop_navigation']
    args: dict


def validate_nav_action(entry) -> dict | None:
    """The entry as `{id, name, args}` when it is exactly a reserved navigation action, else None.

    Nothing is coerced or repaired: a string number, a boolean coordinate, an extra key
    or a missing field drops the whole entry.
    """
    if not isinstance(entry, dict):
        return None
    try:
        action = NavAction.model_validate(entry)
        args = ARG_MODELS[action.name].model_validate(action.args)
    except ValidationError:
        return None
    if any(isinstance(v, bool) for v in action.args.values()):
        return None
    clean = args.model_dump(exclude_none=True, by_alias=True)
    if any(isinstance(v, float) and not math.isfinite(v) for v in clean.values()):
        return None
    return dict(id=action.id, name=action.name, args=clean)


# Proposals ---------------------------------------------------------------------------

PROPOSAL_TTL_S = 30.  # a confirmation card older than this must be asked for again
MAX_PENDING = 8
APPROACH_MAX_M = 1.  # how near counts as "at" an object; clearance comes only from the calibrated footprint
TARGET_MOVED_M = .25  # an object re-sighted farther than this from its proposal invalidates it
TARGET_STATES = ('present', 'moved', 'last_seen')

_UNVERIFIED = ('Autonomous driving is not physically validated: measured calibration, response '
               'curves and a supervised route are still missing.')
REASON_TEXT = {
    # map readiness (occupancy.OccupancySnapshot.blockers, navigator.map_problem)
    'map_unknown': 'No motion map exists for this scan yet.',
    'calibration_missing': 'Rover calibration has not been measured, so no route can be trusted.',
    'calibration_unverified': 'Rover calibration is not verified, so no route can be trusted.',
    'obstacle_min_unsupported': 'The rover\'s lowest obstacle is below what the depth map can see.',
    'no_floor': 'The floor has not been mapped yet.',
    'sensing_stale': 'Depth sensing is stale; the map is not current.',
    # pose
    'pose_stale': 'The rover position is not current.',
    'tracking_lost': 'Phone tracking is not normal, so the rover position is unreliable.',
    'start_blocked': 'The rover\'s own footprint is not on clear mapped floor.',
    # target resolution
    'target_not_found': 'That object is no longer in the current map.',
    'target_not_confirmed': 'That object was not found on the latest rescan.',
    'target_moved': 'The object moved since this was proposed.',
    'no_clear_approach': 'No reachable rover-clear floor was found within 1 m of the object.',
    # planner (navigator.PLAN_STOP_REASONS values)
    'destination_blocked': 'That spot is occupied or too close to an obstacle for the rover.',
    'destination_unknown': 'That spot has not been mapped.',
    'no_path': 'No rover-clear path reaches it on mapped floor.',
    'search_limit': 'Route search gave up before finding a path.',
    'out_of_bounds': 'That spot is outside the mapped area.',
    'explore_complete': 'No reachable unexplored boundary remains.',
    # execution readiness
    'arm_required': 'Arm the rover to confirm; the dashboard switches it to navigate mode.',
    'car_down': 'The car adapter reports down; the default adapter only logs. ' + _UNVERIFIED,
    'car_stale': 'The car adapter is stale. ' + _UNVERIFIED,
    'phone_down': 'The phone is not connected.',
    'phone_stale': 'The phone pose is stale or tracking is limited.',
    'detector_down': 'Object detection is not running.',
    'detector_stale': 'Object detection has no recent result.',
    'rover_arm_failed': 'The rover did not acknowledge the arm handshake.',
    # autonomous adapter readiness (GET /autonomy)
    'logging_adapter_only': 'This backend runs the logging car adapter; it cannot drive the rover. ' + _UNVERIFIED,
    'rover_relay_disconnected': 'The phone\'s rover relay is not connected.',
    'rover_feedback_stale': 'The rover relay has no recent car feedback.',
    'rover_capture_mismatch': 'The rover relay belongs to a different scan.',
    'rover_geometry_unmeasured': 'Rover geometry has not been measured.',
    'clearance_below_stopping_envelope': 'The clearance margin does not cover the measured stopping distance.',
}
MAP_SELECTION = 'Choose the destination yourself by clicking the floor on the map in Standard mode.'


def reason_text(reason: str | None) -> str | None:
    return None if reason is None else REASON_TEXT.get(reason, reason.replace('_', ' ').capitalize() + '.')


def approach_point(grid, config, start_xz, target_xz, *, min_m: float, max_m: float = APPROACH_MAX_M):
    """Nearest rover-clear, reachable floor point to an object, or None.

    Candidates are cell centers whose whole calibrated footprint is known free (the
    planner's own mask) and that are reachable from the rover over such cells, at
    least `min_m` from the object's center (so the footprint never covers it, even when
    its cell reads as floor) and at most `max_m` away. Nothing is inferred about the
    object's extent beyond what the occupancy grid already marks.
    """
    from backend.navigation import _snap, traversable_mask
    sx, sz = (float(v) for v in start_xz)
    tx, tz = (float(v) for v in target_xz)
    start_cell = grid.world_to_cell(sx, sz)
    if start_cell is None:
        return None
    mask = traversable_mask(grid, replace(config, unknown_traversable=False))
    start = _snap(grid, mask, start_cell, sx, sz, config.start_snap_radius_m)
    if start is None:
        return None
    h, w = mask.shape
    free = mask.ravel().tolist()
    seen = bytearray(h * w)
    s = start[0] * w + start[1]
    seen[s] = 1
    queue, head = [s], 0
    best, best_d2 = None, math.inf
    lo, hi = min_m * min_m, max_m * max_m
    moves = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    while head < len(queue) and head < config.max_expansions:
        cur = queue[head]
        head += 1
        r, c = divmod(cur, w)
        x, z = grid.cell_center(r, c)
        d2 = (x - tx) ** 2 + (z - tz) ** 2
        if lo <= d2 <= hi and d2 < best_d2:
            best, best_d2 = (x, z), d2
        for dr, dc in moves:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w:
                n = nr * w + nc
                if free[n] and not seen[n] and (not dr or not dc or (free[r * w + nc] and free[nr * w + c])):
                    seen[n] = 1
                    queue.append(n)
    return best


def _length(points):
    return round(sum(math.dist(a, b) for a, b in zip(points, points[1:])), 2)


@dataclass
class Proposal:
    id: str
    action_id: str
    kind: str  # 'destination' or 'exploration'
    session: tuple
    created: float
    destination: tuple | None = None
    target: dict | None = None


class NavProposals:
    """Pending proposals, each confirmable once, all cleared by any stop except a mode switch."""

    def __init__(self, settings, clock=time.monotonic):
        self.settings = settings
        self.clock = clock
        self._pending: dict[str, Proposal] = {}

    def add(self, proposal: Proposal) -> None:
        self._pending[proposal.id] = proposal
        while len(self._pending) > MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))

    def take(self, proposal_id: str) -> Proposal | None:
        """Remove and return it: a proposal is consumed before anything acts on it."""
        return self._pending.pop(proposal_id, None)

    def cancel(self, proposal_id: str) -> None:
        self._pending.pop(proposal_id, None)

    def invalidate(self) -> None:
        self._pending.clear()

    def expired(self, proposal: Proposal) -> bool:
        return self.clock() - proposal.created > PROPOSAL_TTL_S

    # Validation runs in a worker thread (it classifies the map and plans) -----------

    def validate(self, kind, args, snapshot, pose, objects):
        """(reason, details) for a destination or exploration against one map snapshot."""
        from backend.navigator import PLAN_STOP_REASONS, map_problem, planning_grid
        from backend.navigation import nearest_frontier, plan_path
        problem = map_problem(snapshot, self.settings.map_max_age_s)
        if problem:
            return problem, {}
        if pose is None:
            return 'pose_stale', {}
        if pose.tracking != 'normal':
            return 'tracking_lost', {}
        start = (pose.x, pose.z)
        if not snapshot.traversable(*start):
            return 'start_blocked', {}
        grid, config = planning_grid(snapshot, self.settings)
        if kind == 'exploration':
            frontier = nearest_frontier(grid, start, config)
            if frontier is None:
                return 'explore_complete', {}
            return None, dict(frontier=[round(frontier[0], 3), round(frontier[1], 3)])
        details = {}
        if args['target'] == 'point':
            destination = (args['x'], args['z'])
        else:
            item = next((o for o in objects if o['id'] == args['object_id']), None)
            if item is None or item['class'] != args['class']:
                return 'target_not_found', {}
            position = (item['position'][0], item['position'][2])
            details['target'] = dict(kind='object', object_id=item['id'], **{'class': item['class']},
                                     label=item.get('identity', {}).get('label')
                                     if item.get('identity', {}).get('status') == 'labeled' else None,
                                     position=list(position), state=item['state'], last_seen=item['last_seen'])
            if item['state'] not in TARGET_STATES:
                return 'target_not_confirmed', details
            destination = approach_point(grid, config, start, position, min_m=snapshot.inflation_m)
            if destination is None:
                return 'no_clear_approach', details
        if args['target'] == 'point':
            details['target'] = dict(kind='point', position=[destination[0], destination[1]])
        result = plan_path(grid, start, destination, config)
        if not result.ok:
            return PLAN_STOP_REASONS.get(result.reason, result.reason), details
        details.update(destination=[float(destination[0]), float(destination[1])],  # exactly the planned end
                       points=result.points, length_m=_length(result.points))
        return None, details


class Propose(_Strict):
    session_id: str = Field(min_length=1, max_length=256)
    map_epoch: int = Field(ge=1)
    action: dict


class Confirm(_Strict):
    proposal_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    map_epoch: int = Field(ge=1)


class Cancel(_Strict):
    proposal_id: str = Field(min_length=1, max_length=64)


def register_nav_action_routes(app, proposals: NavProposals, *, active_session, snapshot, pose, objects,
                               execution, readiness, begin_goal, select_explore):
    """Proposal routes over the app's own safeguards; none of them can arm.

    `snapshot()` (blocking) is the navigation map, `pose()` the fresh rover pose or None,
    `objects(session)` the stored objects. `execution(kind)` is why the proposal cannot be
    confirmed right now (a health hazard, or 'arm_required' for a destination), else None;
    `readiness()` lists the autonomous adapter's blockers (`GET /autonomy`), shown for context.
    `begin_goal(x, z)` is `/goal`'s own plan-and-follow path (409s and disarms on failure);
    `select_explore()` stops and selects explore mode, disarmed.
    """
    from fastapi import HTTPException

    def described(reason):
        blockers = list(readiness())
        return dict(available=reason is None, reason=reason, message=reason_text(reason),
                    autonomy_blockers=blockers, autonomy_message=reason_text(blockers[0]) if blockers else None)

    async def check(kind, args, session):
        found = await asyncio.to_thread(
            proposals.validate, kind, args, snapshot(), pose(), objects(session) if kind == 'destination' else [])
        if active_session() != session:
            raise HTTPException(409, 'Map reset while validating')
        return found

    @app.post('/nav/propose')
    async def propose(body: Propose):
        session = (body.session_id, body.map_epoch)
        if active_session() != session:
            raise HTTPException(409, 'Proposal is for a map that is not the active one')
        action = validate_nav_action(body.action)
        if action is None:
            raise HTTPException(422, 'Not a valid navigation action')
        kind = dict(propose_navigation='destination', propose_exploration='exploration',
                    stop_navigation='stop')[action['name']]
        response = dict(version=1, proposal_id=None, action_id=action['id'], name=action['name'], kind=kind,
                        session_id=session[0], map_epoch=session[1], hardware_verified=False)
        if kind == 'stop':  # stopping needs no validation; the dashboard's Stop is always available
            return dict(response, status='ready', reason=None, message='Stop the rover.',
                        execution=described(None))
        reason, details = await check(kind, action['args'], session)
        response.update(details, status='unavailable' if reason else 'ready', reason=reason,
                        message=reason_text(reason) if reason else None,
                        execution=described(execution(kind)))
        if reason in ('no_clear_approach', 'target_not_confirmed', 'target_not_found'):
            response['alternative'] = MAP_SELECTION
        if reason is None:
            proposal = Proposal(uuid4().hex, action['id'], kind, session, proposals.clock(),
                                destination=tuple(details['destination']) if kind == 'destination' else None,
                                target=details.get('target'))
            proposals.add(proposal)
            response.update(proposal_id=proposal.id, expires_in_s=PROPOSAL_TTL_S)
        return response

    @app.post('/nav/confirm')
    async def confirm(body: Confirm):
        proposal = proposals.take(body.proposal_id)  # consumed first: a retry can never replay it
        if proposal is None:
            raise HTTPException(404, 'Proposal unknown, already used, cancelled or invalidated by a stop')
        if proposals.expired(proposal):
            raise HTTPException(409, 'proposal_expired')
        if (body.session_id, body.map_epoch) != proposal.session or active_session() != proposal.session:
            raise HTTPException(409, 'map_changed')
        if (blocked := execution(proposal.kind)) is not None:
            raise HTTPException(409, blocked)
        if proposal.kind == 'exploration':
            reason, _ = await check('exploration', {}, proposal.session)
            if reason:
                raise HTTPException(409, reason)
            return dict(select_explore(), proposal_id=proposal.id, next='arm')
        target = proposal.target
        if target['kind'] == 'object':
            now = next((o for o in objects(proposal.session) if o['id'] == target['object_id']), None)
            if (now is None or now['state'] not in TARGET_STATES
                    or math.dist((now['position'][0], now['position'][2]), target['position']) > TARGET_MOVED_M):
                raise HTTPException(409, 'target_moved')
        return dict(await begin_goal(*proposal.destination), proposal_id=proposal.id)

    @app.post('/nav/cancel')
    async def cancel(body: Cancel):
        proposals.cancel(body.proposal_id)
        return dict(version=1, proposal_id=body.proposal_id, status='cancelled')
