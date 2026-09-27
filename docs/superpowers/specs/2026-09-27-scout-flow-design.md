# Scout: flowing detours and persistent exploration

The operator wants early, continuous steering around a single hallway obstacle,
and an Explore mission that remains selected until a dashboard admin stops it.
The existing prototype plans an early detour but collapses small yaw requests to
straight motion and larger requests to a fixed half/full wheel split. The
synthetic hallway run arrives without a pivot but switches steering 51 times.

Carry an optional `inner_power` from the prototype motor command through the
phone to the ESP's restricted forward arc. Existing arc packets without it retain
their half/full split. New arcs keep the outer wheel's requested cruise PWM and
vary the inner wheel continuously between half and full PWM. This is an
uncalibrated duty policy, not a physical speed or chassis measurement. Keep all
session, freshness, queue, Stop, and magnitude validation. The frozen sensor wire
is unchanged. Reject optional wheel power on non-arc commands and reject null,
non-integer or out-of-range values; accepted range is `power // 2` through `power`.

The prototype selects left/right arcs for nonzero representable steering; no
large steering deadband. A normalized yaw request of 0.5 selects half power;
smaller requests select proportionally less differential. Existing planning and
pure pursuit continue to choose the path and nominal cruise. Test the complete
packet-driven synthetic detour for early steering, continued translation, full
cruise request until goal approach, and substantially fewer direction switches.
Retain sensitivity checks for different invented wheel responses.

Persist the already explicitly selected Explore intent across recoverable
navigation endings and reconnects. Keep motor/session readiness separate from
mission intent. Retry exploration with bounded backoff, no busy loop, after
temporary no-path/no-frontier/no-progress conditions. Explicit dashboard Stop or
disable and explicit mode changes cancel the intent and prevent late rearming.
Do not initiate exploration without the operator's previous explicit request.

The operator has delegated implementation decisions and requested immediate
execution. Implement within the existing Scout worktree, review, integrate main,
then deploy phone/ESP updates when the corresponding device is connected.
