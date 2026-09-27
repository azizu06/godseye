"""Pure stateful path-relevance/debounce policy for Explore obstacle yield.

Reimplements the debounced pause/resume idea assessed in
``reference-car-assessment.md`` section 9 (RDK-X5, commit 54151a45), adapted
to depth-localized swept-corridor evidence instead of a fixed 2D image ROI --
a bare image-space person label is not path-blocking evidence, and an
unresolved poster (no depth) must never count as clearance either. Nothing
here does detection, classification, depth sensing or corridor geometry: the
caller supplies already-localized evidence per tick. Nothing here owns the
operator Stop or sensor/relay/watchdog fault latches: those stay in app.py /
navigation.py, strictly higher priority, and this module never auto-rearms
past them.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PathObservation:
    """One control-tick's worth of path-relevance evidence.

    ``depth_known`` must be True only when this tick carries fresh, same-frame
    depth/occupancy evidence for the swept corridor. A detection with no such
    localization -- including an unresolved poster -- is ``depth_known=False``
    and must not also set ``path_blocked=True``: a bare label is not
    path-blocking evidence, and missing depth is not proof of clearance.
    ``imminent`` marks evidence severe enough (e.g. a very close in-corridor
    reading) to require an immediate pause, bypassing the confirm debounce.

    ``frame_id`` optionally identifies the underlying sensor sample this
    evidence came from (e.g. a capture sequence number or timestamp). When a
    caller polls faster than the sensor updates and re-presents the same
    ``frame_id`` on a later tick, that tick is a repeat, not a new distinct
    observation: it must not advance the confirm/clear streaks or the resume
    hold, and it must not itself complete a resume. Leave it ``None`` when no
    stable identity is available; every tick is then treated as distinct.
    """

    path_blocked: bool
    depth_known: bool
    imminent: bool = False
    frame_id: Optional[object] = None


@dataclass(frozen=True)
class GateConfig:
    pause_confirm_frames: int = 3
    resume_clear_frames: int = 3
    resume_hold_s: float = 1.0


@dataclass(frozen=True)
class GateDecision:
    yielding: bool
    wait_reason: Optional[str]
    resumed_this_tick: bool


class ExploreObstacleGate:
    """Debounced yield/resume state machine over path-relevance evidence."""

    def __init__(self, config: Optional[GateConfig] = None):
        self._config = config or GateConfig()
        self.reset()

    def reset(self) -> None:
        """Clear all state, for a new Explore session/generation."""
        self._yielding = False
        self._confirm_streak = 0
        self._clear_streak = 0
        self._clear_hold_started_at: Optional[float] = None
        self._last_frame_id: object = object()  # sentinel: never equals a real frame_id

    def decide(
        self,
        observation: PathObservation,
        now: float,
        external_hold: bool = False,
        external_reason: str = "external_hold",
    ) -> GateDecision:
        """Advance the debounce state machine by one control tick.

        ``external_hold`` stands in for an operator Stop or a sensor/relay/
        watchdog fault latch owned elsewhere: while True this forces a yield
        and leaves the debounce counters untouched, so it can never itself
        rearm a resume -- clearing it only lets ordinary clear-frame evidence
        and the resume hold resume driving on a later call.
        """
        if external_hold:
            return GateDecision(yielding=True, wait_reason=external_reason, resumed_this_tick=False)

        if observation.imminent:
            self._yielding = True
            self._confirm_streak = self._config.pause_confirm_frames
            self._clear_streak = 0
            self._clear_hold_started_at = None
            return GateDecision(yielding=True, wait_reason="imminent", resumed_this_tick=False)

        is_repeat_sample = (
            observation.frame_id is not None and observation.frame_id == self._last_frame_id
        )
        if observation.frame_id is not None:
            self._last_frame_id = observation.frame_id

        if is_repeat_sample:
            # Same sensor sample re-presented: not a new distinct observation.
            # Must not advance any streak or the hold, and must not itself
            # complete a resume -- freeze and report the unchanged state.
            reason = "path_crossing" if self._yielding else None
            return GateDecision(yielding=self._yielding, wait_reason=reason, resumed_this_tick=False)

        if observation.depth_known and observation.path_blocked:
            self._confirm_streak += 1
            self._clear_streak = 0
            self._clear_hold_started_at = None
        elif observation.depth_known and not observation.path_blocked:
            self._clear_streak += 1
            self._confirm_streak = 0
        else:
            # Missing evidence cannot spend a hold earned by an earlier clear
            # view. Keep the obstacle yield, but require a fresh clear streak
            # and hold before resuming once depth becomes usable again.
            self._clear_streak = 0
            self._clear_hold_started_at = None

        resumed = False
        if not self._yielding:
            if self._confirm_streak >= self._config.pause_confirm_frames:
                self._yielding = True
                self._clear_streak = 0
                self._clear_hold_started_at = None
        elif self._clear_streak >= self._config.resume_clear_frames:
            if self._clear_hold_started_at is None:
                self._clear_hold_started_at = now
            elif now - self._clear_hold_started_at >= self._config.resume_hold_s:
                self._yielding = False
                self._confirm_streak = 0
                self._clear_streak = 0
                self._clear_hold_started_at = None
                resumed = True
        else:
            self._clear_hold_started_at = None

        reason = "path_crossing" if self._yielding else None
        return GateDecision(yielding=self._yielding, wait_reason=reason, resumed_this_tick=resumed)
