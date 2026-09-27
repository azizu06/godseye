# Autonomous rover integration

The manual iPhone/Bluetooth implementation and opt-in autonomous prototype are on main.

## Current evidence

The iPhone's Bluetooth link to the real ESP32-S3 and Uno, manual movement, and
simultaneous RGB-D streaming over normal Wi-Fi are verified in [ios/ROVER.md](../ios/ROVER.md).
The opt-in autonomous relay now connects the laptop through that phone. The ordinary
backend command still defaults to a logging adapter that reports down.
On 2026-09-27, a supervised mounted-phone prototype run completed the dashboard's
Arm/setup path, followed a laptop-planned Explore route, and recorded about 0.243 m
of ARKit position change before the updated map triggered `path_blocked`. Explicit
Stop was acknowledged. This is a short physical demo, not measured motion calibration
or evidence of sustained navigation reliability.
After correcting the phone's upside-down mount, a three-second Explore run remained
armed, published routes and measured about 0.182 m of ARKit displacement before an
explicit Stop. A prior upright run exposed a delayed-pose disarm; prototype Explore
now discards that old pose, brakes on stale feedback, and resumes on fresh data.

The implementation provides:

- The measured adapter retains pivot-then-drive on stock timed `N=2` directions.
  The explicit prototype can instead request restricted forward arcs through
  the updated ESP bridge's differential-motor command.
- Navigation heading corrected by the measured camera mounting yaw. Map positions
  and the live display retain the v1 camera coordinates; footprint inflation already
  accounts for the measured camera offset.
- `backend/actuation.py`: validated measured power/rate curves, yaw-sign mapping,
  stopping evidence, and bounded interpolation to timed motor packets. Unknown
  response, out-of-range rates and simultaneous linear/angular commands are refused.
- Hardware-free regression in `backend/tests/test_actuation.py` plus the existing
  navigation, navigation-runner and geometry-calibration tests.
- An authenticated iPhone relay, mounted-phone dashboard controls, readiness display,
  and arm/stop barriers. Unknown calibration values prevent motion.
- Firmware with an independent permit/session gate and bounded input queue. Parsing
  runs on the main loop, avoiding the small Bluetooth callback stack.
- The S3 firmware was flashed and hash-verified. Five real Uno probes took 96.7–174.4 ms;
  unique Stop acknowledgement, fresh permits, arm acknowledgement, an idle zero and
  replay rejection passed over actual BLE. These tests sent no nonzero motor command.

## Control path

The laptop owns planning. A separate authenticated `/rover` WebSocket carries
small control messages to the iPhone while `/phone` continues carrying perception.
The phone remains the sole Bluetooth owner. Computer control is the default on
foreground launch: remembered pairing starts setup, capture and the selected BLE rover,
then enables the laptop link when sensing and Uno feedback are ready. It never arms.
The operator can instead enable the link from the paired dashboard. In the explicitly
armed uncalibrated prototype Explore mode, that choice stays latched until Stop,
mode change, or a dashboard control-disable action. A sensing or permit gap clears
pending movement and the ESP brakes after a full second without a new command; Explore waits for fresh data and
replans. A real phone/rover disconnect retires the physical drive session, then
foreground computer setup and the backend establish a fresh session when all live
readiness checks recover. Backgrounding cannot capture or drive; foregrounding
restarts setup. Measured navigation and manual control retain their stop behavior.

Before forwarding autonomous movement, the ESP must independently check a fresh
permit it generated, the active drive session and strictly increasing sequence.
The permit travels ESP → phone → laptop → phone → ESP. Its lifetime is checked
again when forwarding to UART, using the ESP's own clock. This avoids comparing
the Mac, iPhone and ESP uptime clocks or renewing an old command on receipt.
The backend also bounds the envelope's age before dispatch. Their combined
age budget, including UART serialization, must remain below the 500 ms contract.
The existing timed Uno command and ESP watchdog remain independent final stops.

An explicit stopped/armed handshake must precede each new drive session; latest-only
command replacement cannot discard the session-opening handshake. A zero within a
live session means idle; an explicit Stop or failed moving lease retires the session.
Only a new arm handshake can start another; prototype Explore may repeat that
handshake under the operator's still-active Explore choice. Legacy manual control remains available.

Hardware-free integration covers synthetic RGB-D → calibrated occupancy → `/goal` →
real adapter wire commands → stale-sensing Stop. The actual Swift relay also runs
against a loopback WebSocket and fake BLE, including local tracking-loss Stop.
The phone build is installed; physical geometry and response curves remain unmeasured.

### ESP protocol

The bridge advertises its 64-bit permit as `{P<16 uppercase hex digits>}` every
50 ms on its existing notification characteristic. A permit is valid for at most
500 ms on the ESP clock. Notifications are transport feedback, not motor execution.

`N=201,H=<32 uppercase hex drive-session digits>,C=<permit>` queues Stop and opens
that new session. Its `{A<session>}` acknowledgement follows the UART Stop handoff;
all previous permits are invalidated. Wait for this acknowledgement and a subsequent
permit before sending movement. Retrying an arm with the same session is refused;
a missing acknowledgement requires Stop and a new explicit arm, not automatic retry.
During the explicit Stop/Arm acknowledgement barrier, a brief gap in ESP status
holds the pending Arm until a fresh permit arrives; the barrier still times out
after three seconds, and no drive command uses a stale permit.

`N=202,H=<session>,C=<permit>,S=<increasing uint32>,D1=<direction>,D2=<PWM>,T=1500`
becomes the bounded stock `N=2` packet for directions 1–4, or a restricted stock
`N=4` differential-speed forward arc for direction 5 (left) or 6 (right), only
when the session, sequence and permit are valid again at UART dispatch. Arcs run
the two forward motors at full/half PWM and rely on the ESP's 1 s command-loss
Stop because stock `N=4` has no Uno timer. `D1=0,D2=0` is an idle zero in the live session.
The legacy `N=100` Stop always retires it. After the UART Stop handoff,
`{Z<request H>}` acknowledges that specific Stop; use a unique request ID for an
arm barrier. `{X}` reports malformed input or a retired session. Manual movement while an autonomous session is
active stops it; a separate explicit Stop is required before manual takeover.
Other legacy commands retain their existing rules. No arbitrary UART passthrough
or unguarded indefinite motor command is added.

The 1 s bridge watchdog sends Stop on a sustained missed refresh. It keeps the already
armed session so a later command with a new permit and sequence may resume;
the late packet itself cannot revive the old motor lease. Straight and pivot `N=2`
commands also have the Uno's independent 1.5 s timer; forward arcs use the ESP
brake because `N=4` is untimed on the Uno. Invalid sessions, replay, explicit
Stop and disconnect retire the arm. Queue expiry, reconnect, malformed fragments and 32-bit clock wrap are
covered by `python3 firmware/elegoo-ble/test/run.py`. The server rejects movement older than 150 ms again at socket dispatch; the phone
rechecks permit receipt age before its bounded BLE write.
Expired movement samples are discarded without refreshing their timestamps or
motor leases. A fresh successor may continue the current session; without one,
the firmware sends Stop at 1 s. Arm, Stop, invalid sessions and lost
feedback retain their separate checks.

## Measurements before driving

Copy the all-null templates in [calibration](calibration/) to local files and fill
them only from measurements. The templates cannot enable motion.

The geometry profile needs chassis length/width including bumpers, lowest obstacle
the rover cannot traverse, clearance margin, camera forward/left offset and yaw,
and measurement evidence. Existing map readiness rules remain authoritative in
[backend/README.md](../backend/README.md#rover-calibration-and-the-navigation-map).

The actuation profile records at least two increasing `{ "pwm": ..., "rate": ... }`
pairs for forward, left and right. Forward/reverse rates are m/s; turn rates are
rad/s magnitudes. Reverse may remain null for forward-only navigation. Measure
which vendor direction increases ARKit yaw and record `positive_yaw_direction`.
Use a fixed phone mount, representative battery charge and the intended floor.
Record stopping distance and command-loss stop time at the highest permitted power;
`measured_by` must identify the operator and evidence. Interpolation is an open-loop
estimate, not wheel-encoder feedback. No real measurement values ship as defaults.

Physical deployment must verify the autonomous relay, direction/yaw sign, low-speed
response, stop/release, Wi-Fi/Bluetooth loss, stale-command rejection, and a short
supervised route on mapped clear floor. The operator's successful manual test does
not replace these autonomy checks.


## Run and use the mounted-phone controls

The phone and dashboard use the same local pairing key. It is never committed,
placed in a URL, or exported with the map. The phone remembers it in the
[device-local Keychain](https://developer.apple.com/documentation/security/ksecattraccessiblewhenunlockedthisdeviceonly);
the dashboard keeps it in tab memory. **Forget laptop pairing** removes the saved
credential and disables computer startup. Capture preferences and the last verified
selected rover's identifier are remembered locally; a new nearby rover is never
selected automatically.
A debug launch can provision `GODSEYE_ROVER_PAIRING_KEY`, `GODSEYE_LAPTOP_ENDPOINT`,
and `GODSEYE_ROVER_IDENTIFIER` through devicectl environment variables for one-time
provisioning. Normal and Xcode launches then use the saved settings without phone
taps. Automatic setup never sends arm or movement commands.

```sh
python3 -m tools.run_rover_backend --init
python3 -m tools.run_rover_backend --weights /absolute/path/to/yolo11n.pt
```

Configuration lives in `~/.local/share/godseye/autonomy/`: `pairing-key`,
`geometry.json`, `actuation.json`. Init preserves existing files and creates null
measurement templates. The installed local detector runtime is
`~/.venvs/godseye/bin/python`; downloaded official YOLO11n weights and their source
manifest live in `~/.local/share/godseye/models/`. See the [official model docs](https://docs.ultralytics.com/models/yolo11/).
A missing detector stays down; the setup controls still work, but driving stays blocked.

1. Pair the laptop and select the Bluetooth rover once. **Computer control on launch**
   defaults on. Opening or foregrounding God's Eye restores dashboard remote, starts
   capture, finds only the saved rover, and enables laptop control when ready. Keep the
   app foregrounded; remote control keeps the display awake. A locked/background app
   cannot capture camera data.
2. Open the dashboard. Press **Escape → Connection settings**, enable REST commands,
   and enter the same key. This tab remembers the key and command permission across reloads for the same backend. Disable REST commands to forget them. Arming and movement are never restored.
3. Open **Rover controls → Mounted phone** to observe setup or change it. If no rover
   has been saved, select **GodsEye-Rover-D022** there. After Stop or a control loss,
   click **Enable laptop control** in this panel; recovery does not silently restore
   the link. Stop also cancels startup while it is waiting for tracking or Bluetooth.
   A fresh foreground launch may prepare control again; an active prototype Explore
   choice can rearm only after the phone, rover, camera and map recover.
4. Complete the measured profiles and restart the backend. Readiness must have no
   blockers. Select Navigate, explicitly Arm, then select a mapped destination.
   Explore starts planning upon explicit arm. Manual PWM driving stays on the phone;
   the laptop arms Standard only for one confirmed, pose-measured voice move
   ([backend/NAV_ACTIONS.md](../backend/NAV_ACTIONS.md), Bounded moves).
5. **STOP ROVER** stops the backend and sends an independent phone-side Stop through
   the setup channel. Stop remains available without REST enable or a pairing key.

`/device` is a separate authenticated setup WebSocket with read-only `GET /device`
and authenticated `POST /device/action`. Its allowlist only includes capture start/stop,
Bluetooth scan/selection/disconnect, laptop-control enable/disable and Stop. It never
accepts motor packets or arms navigation. It supports one active phone, bounded
requests, explicit acknowledgements and discards pending actions on disconnect.
Setup can reconnect automatically after a network fault; it stops the old motor
session first. The independent `/rover` link requires fresh capture and rover
feedback for every movement packet, even while Explore remains requested.

The `/rover` sender schedules heartbeats from the last outbound message. Incoming
20 Hz ESP status must not reset that deadline; otherwise an idle, healthy phone
times out waiting for the laptop. `RelaySocketTests` covers continuous status and
independent disconnect on missing feedback. After this fix, the real phone stayed
enabled for approximately 11 seconds before ARKit tracking loss stopped control;
no movement was sent. This does not establish sustained network or driving readiness.

The phone allows up to three seconds for the initial laptop handshake, withholding
outbound feedback until the server responds and discarding permits accumulated
during startup. While armed, a five-second WebSocket send/server-silence deadline
separates a broken link from a temporary permit gap; neither deadline authorizes
movement without a fresh permit, capture, map and command. Capture and rover-feedback
checks apply throughout.
The loopback Swift test covers an 800 ms delayed server, a completely silent
startup, and heartbeat loss after arming; those motor packets only reach fake BLE.
Phone status distinguishes handshake, send and heartbeat timeouts.

The REST motion endpoints require the pairing key when the iPhone adapter is enabled.
`GET /autonomy` explains readiness; its occupancy snapshot is computed off the event
loop. Geometry clearance must cover measured stopping distance plus the command-age
allowance. `ws://`/`http://` are for a trusted local demo network; use TLS for an
untrusted network. The BLE demo service still does not implement bonding.

## Validation

```sh
python3 -m unittest backend.tests.test_actuation backend.tests.test_rover_relay backend.tests.test_device_relay -v
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift test --package-path ios
python3 ios/Tests/check_rover_ble.py
python3 ios/Tests/check_rover_autonomy.py
python3 firmware/elegoo-ble/test/run.py
```

Physical non-movement firmware acceptance, while the iPhone is disconnected from BLE:

```sh
python3 -m tools.probe_rover_ble --name GodsEye-Rover-D022 --samples 5 --autonomy
```

The probe's only autonomous velocity is idle zero. A successful probe proves transport,
firmware gating and Uno feedback, not measured motor speed or stopping distance.

## Uncalibrated prototype option

At the operator's request, precision calibration can be deferred with a separate,
explicit prototype profile. Normal mode and the default logging adapter are unchanged.
No estimate is saved to the measured geometry or actuation files.

```sh
python -m tools.run_rover_backend --prototype \
  --estimated-length-m 0.2286 --estimated-width-m 0.127 \
  --weights /path/to/yolo11n.pt
```

Those dimensions are this operator's **9 × 5 inch estimate**, not project defaults.
Both dimensions are required. The profile assumes a forward-facing rear camera and
vendor left/right directions. Inflation covers a camera anywhere inside the estimated
rectangle plus the operator-requested 6 inch (0.1524 m) edge clearance;
the obstacle threshold is 6.5 cm, not measured traversability.
On the operator's flat-terrain assumption, this profile can traverse unknown floor
around the mounted camera's blind spot, including just outside the cropped map.
Explore still chooses an observed-floor frontier, and observed obstacles retain
the full footprint clearance. Measured mode still requires known-clear floor.
Actual motor speed, yaw sign and stopping distance remain unverified. This is only
for supervised tests in open space, not a claim of accurate autonomous driving.

The prototype pose/map follower chooses forward or moving left/right arcs, reserving
a pivot for turns above about 69°. In an open room, Explore favors reachable
frontiers with more unmapped area nearby, then covers the remaining frontiers
until the mapped room has none. When both hallway walls are observed, it first
moves toward their center and follows the far end. A straight path with at least 1 m of
clear route ahead requests the prototype's 0.2 m/s nominal command and PWM 180;
an observed corridor must also be at least 1.2 m wide and centered. Off-center
travel on a detour uses a differential forward arc at PWM 180; slow approaches
and rare pivots retain PWM 60. The
autonomous phone/ESP command path permits up to PWM 180; stock manual control
remains capped at 80. This is three times the former prototype duty setting,
not a measured threefold travel speed. The phone app and ESP firmware must both
be updated before using it. The occupancy planning window follows the rover
through consecutive hallways rather than ending at 10 m from the AR origin;
Explore holds a reachable forward destination across minor map updates and
extends that destination as fresh depth reveals more hallway, avoiding a stop at
each old frontier. It checks its route for new obstacles between full replans
every four seconds, or sooner when the destination is near or blocked. Medium-or-high-confidence depth can clear a departed
obstacle after two distinct views see through its former footprint.
If a wall newly overlaps only the prototype's extra six-inch clearance around
the camera point, Explore may snap a route toward nearby clear space and continue
only while each step maintains or increases obstacle clearance. An obstacle
inside the estimated chassis footprint still blocks this recovery.
The prototype never invents reverse motion or synthetic speed curves. Commands
update continuously with no added pauses or run duration limit. Each command
uses the firmware's 1 s command-loss brake; straight and pivot commands also
have the Uno's 1.5 s timed fallback, while forward arcs do not. Stop, tracking/depth freshness,
authenticated pairing, unique arm sessions, ESP permits, Uno feedback and link
watchdogs remain enforced. A latched Explore request may rearm after transient loss.
The dashboard labels this profile **Uncalibrated prototype**. Select Explore or Navigate,
then explicitly Arm. Stop and reassess if the rover turns in the wrong direction.

Validation: `python -m unittest backend.tests.test_prototype -v` checks estimate
provenance, unchanged measured gates, API readiness, command bounds and continuous delivery without artificial pauses or run limits. No hardware motion is implied by those tests.

Prototype occupancy includes medium-confidence LiDAR samples (common on carpet),
with the existing repeated-frame free/obstacle evidence thresholds. Low-confidence
samples remain excluded; displayed point clouds retain high-confidence sampling.

The idle rover WebSocket tolerates up to three seconds of silence; the armed link
tolerates up to five seconds before retiring a broken transport. Backend feedback
readiness remains 500 ms, independently of socket liveness. Prototype Explore
waits at zero through longer sensor gaps and retries a new arm after an actual
disconnect once fresh readiness returns; only explicit Stop clears that choice.

### One-click dashboard Arm

With server-side dashboard pairing configured (see `dashboard/VIEWER.md`), a fresh
browser needs no copied key or REST checkbox. Arm performs the existing capture,
Bluetooth and laptop-control setup via `/device/action`, then checks current
readiness and completes the normal arm barrier. This uses the installed phone
protocol and does not require a new phone build. Stop cancels pending startup;
setup never continues to arming after that cancellation.

### Explore recovery when new geometry blocks a route

Explore first zeroes and replans to its existing implicit frontier. If the newest
map proves no path to that still-free target, it can select another reachable
frontier on the same snapshot. It does not replace a target merely because a
search limit was reached. While stationary with no usable route, authoritative
map checks continue separately from the slower full-replan interval; a cached
snapshot aging out cannot by itself establish that incoming sensing stopped.
Actual sensing loss, invalid clearance, operator Stop and changed arm generations
retain their existing authority. The explicit prototype may still preserve its
requested Explore choice across recoverable input gaps under the rules above.
No unknown-space, footprint, turn or motor limit is relaxed by this recovery.

This policy is verified offline with real occupancy classification and a kinematic
command sink (`backend/tests/test_obstacle_replanning.py`), not a physical obstacle
avoidance demonstration. The active rover backend version and sensor/clearance
conditions must still be checked before interpreting a physical stop.
