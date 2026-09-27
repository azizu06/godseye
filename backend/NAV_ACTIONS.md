# Voice navigation proposals

Scout's voice answer may suggest driving the rover to a mapped object or point,
exploring, stopping, or one short measured move (see Bounded moves). A suggestion is only a **proposal**: the model's output never
arms, plans, selects a mode or sends a drive command. The backend validates it
against the current map with the existing rover planner, the dashboard shows it as a
confirmation card, and only a person's click starts the existing `/goal` or explore
machinery, with every safeguard those already enforce (see [README](README.md)
Navigation, Drive commands, Rover calibration, and [docs/AUTONOMY.md](../docs/AUTONOMY.md)).

The default `LoggingCar` reports the car down and the iPhone relay needs measured
geometry and actuation profiles, so real execution stays unavailable until those
exist; each card says why. Nothing here has been run on the physical rover.

## Action seam

`backend/labels.py` asks Gemini for these as the only action of a reply
(`nav_actions.NAV_ACTION_PROMPT`). `voice.resolve_actions` accepts one alone, turns an
object `ref` (or a class with exactly one stored object) into the stored id, gives it a
fresh server id, and checks the result with `nav_actions.validate_nav_action`. Anything
else refuses the whole reply with a fixed spoken reason ("Nothing was suggested"); two
matching objects get "Which one?".

| `name` | resolved `args` (no other keys) |
|---|---|
| `propose_navigation` | `{"target": "object", "object_id", "class"}` or `{"target": "point", "x", "z"}` (finite, at most 50 m) |
| `propose_exploration` | `{}` |
| `stop_navigation` | `{}` |
| `propose_move` | `{"direction": "forward", "amount", "unit": "cm" \| "m" \| "in"}` or `{"direction": "left" \| "right", "amount", "unit": "deg"}` |

The dashboard re-checks the entry (`dashboardActions.parseActions`, `navProposals.navAction`)
and `useDashboardActions` hands it to `offerForMap` only when the reply's map is still
the shown one; the spoken result says it is on screen and that nothing moves without
confirmation. `offerNavigationActions({session_id, map_epoch, actions})` and a
`godseye:voice-actions` window event are equivalent entry points. Each action id is
shown at most once.

## Routes

- `POST /nav/propose` `{session_id, map_epoch, action}`: 409 unless the map is the
  active one, 422 for an invalid action. Never moves anything. A ready proposal
  carries `proposal_id`, the planned `destination`, `points`, `length_m`,
  `expires_in_s` (30), and `execution` (`available`, `reason`, `message`, plus the
  `GET /autonomy` blockers as `autonomy_blockers`/`autonomy_message`). An unavailable
  one carries `reason`, `message` and, for object targets, a map-selection `alternative`.
- `POST /nav/confirm` `{proposal_id, session_id, map_epoch}` consumes the proposal
  before anything else (a retry gets 404; nothing is replayed), then requires the same
  map, no health hazard and, for a destination, a deliberate arm in navigate mode
  (the dashboard uses click-to-navigate's hand-off). An object that moved over 0.25 m
  or left the map is refused. A destination then goes through `/goal`'s own code;
  exploration only selects explore mode, disarmed, and a separate Arm starts the
  existing frontier planner. Needs the rover pairing key when the iPhone adapter is on.
- `POST /nav/cancel` `{proposal_id}` drops it. Stop (`/stop`) needs no proposal.

Any stop other than a mode switch (operator Stop, health or pose loss, session reset)
voids every pending proposal; the dashboard also voids a card on a map change, a
health drop, a new stop reason or expiry, and never re-enables a sent confirmation.

## Validation

- Object destinations are the nearest point within 1 m of the object that the
  planner's own calibrated footprint mask marks clear and reachable, at least the
  footprint radius from its center, never the occupied center. None gives
  `no_clear_approach` with the map-selection alternative.
- Refusals: map blockers (`calibration_missing`, `no_floor`, `sensing_stale`, ...),
  `pose_stale`, `tracking_lost`, `start_blocked`, `target_not_found`,
  `target_not_confirmed` (not found on rescan), planner reasons (`destination_unknown`,
  `destination_blocked`, `no_path`, `out_of_bounds`, ...), `explore_complete`.

## Bounded moves

An intermediate step before destination autonomy: "move forward 20 centimeters" or
"turn left 30 degrees" becomes one card. It needs no map, route, frontier or rover
geometry, only the manual prerequisites; it never falls back to a timed pulse.

- Speech: the transcript must contain the model's number and exactly its unit
  (`moves.heard_problem`); a missing unit ("go 100 forward"), a changed or converted
  unit or number, or a second segment gets a spoken clarification. 100 inches is
  2.54 m, never 100 cm. Reverse, speed and repeats are not offered.
- Limits (`moves.FORWARD_M`, `TURN_DEG`): forward 5-50 cm (0.05-0.5 m), turns 10-90
  degrees. Larger or smaller requests are refused (`move_out_of_range`), never clamped.
- Arming: the card's **Arm for this move** is a separate deliberate click that selects
  Standard and arms (`POST /arm?prepare=true&standard=true` with phone setup, or plain
  `/arm` in manual). On the iPhone adapter this is the only reason to arm in manual:
  `/manual` stays 409 there, and prepare without `standard` still defaults to Explore.
  Manual readiness is `RelayCar.blockers()` (link, fresh ESP feedback, capture identity
  and an actuation profile that turns a velocity into PWM) plus phone/car/detector
  health. An empty `actuation.json` reports the precise `actuation_*_unmeasured`
  blocker; the explicitly started, labeled uncalibrated prototype profile satisfies it
  with PWM 60.
- Execution (`moves.MoveRunner`, confirm via `/nav/confirm`): one move at a time
  (`move_in_progress`), submitted like held-button driving through `Motion.submit`
  (lease, pump, relay permits, Stop unchanged) at the slowest measured forward or
  turn rate (nominal on the prototype). Progress is the phone's fresh, normally tracked
  pose: forward distance along the start heading, or accumulated yaw toward the
  requested side. Chassis offsets do not matter: a translation moves the camera
  equally and a turn changes its yaw equally. It stops at the target (early by the
  measured stopping distance, if any) and fails with `move_diverging` (wrong way,
  more than 10 cm sideways, or a turn that drifts), `move_no_progress` (2 s),
  `move_timeout`, `pose_stale` or `tracking_lost`; every end disarms through `stop()`.
- Result: `GET /nav/move` gives the latest move (`running`, `completed`, `stopped`,
  `failed`), the pose-measured `achieved` meters or degrees (after 0.6 s of settling,
  so coasting is included) and a sentence. `POST /nav/move/speak {move_id}` speaks that
  backend sentence once, only after the move ended (409 while running, 503 without voice).

No obstacle check is made: a person watches the rover with Stop ready. Distances are
measured, not promised: on the prototype profile the PWM 60 speed, turn overshoot and
yaw sign are unmeasured, so results may overshoot or stop as diverging, and say so.
Nothing here has been run on the physical rover.

```sh
$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_moves backend.tests.test_nav_actions backend.tests.test_voice -v
$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_rover_relay.RelayHTTPTests.test_measured_manual_move_needs_no_map_or_geometry_but_unmeasured_power_blocks_it
cd dashboard && npx vitest run src/navProposals.test.ts
GODSEYE_DASHBOARD_TEST_PORT=<free port> npx playwright test tests/navProposals.spec.ts
```

These use a synthetic surveyed floor, TEST calibration, `FakeCar`, fake microphones and
mocked routes; they prove software gating only, not driving.
