# Autonomous rover integration

Work is on `codex/autonomous-driving`, based on GitHub main through `905b198`.
The working manual iPhone/Bluetooth implementation is preserved in `9539d8a`.

## Current evidence

The iPhone's Bluetooth link to the real ESP32-S3 and Uno, manual movement, and
simultaneous RGB-D streaming over normal Wi-Fi are verified in [ios/ROVER.md](../ios/ROVER.md).
The opt-in autonomous relay now connects the laptop through that phone. The ordinary
backend command still defaults to a logging adapter that reports down. Real autonomous
movement has not been validated: physical calibration is still missing.

The implementation provides:

- A pivot-then-drive mode for the existing follower, matching the stock Uno's
  timed `N=2` directions. It never pretends those commands can drive a curved arc.
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
The operator can instead enable the link from the paired dashboard. Stop, app inactivity, capture identity changes,
tracking loss or a lost transport revoke control; reconnect never resumes it.

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
Only a new explicit arm can start another. Legacy manual control remains available.

Hardware-free integration covers synthetic RGB-D → calibrated occupancy → `/goal` →
real adapter wire commands → stale-sensing Stop. The actual Swift relay also runs
against a loopback WebSocket and fake BLE, including local tracking-loss Stop.
The phone build is installed; physical geometry, response curves and a supervised
route remain unverified.

### ESP protocol

The bridge advertises its 64-bit permit as `{P<16 uppercase hex digits>}` every
50 ms on its existing notification characteristic. A permit is valid for at most
250 ms on the ESP clock. Notifications are transport feedback, not motor execution.

`N=201,H=<32 uppercase hex drive-session digits>,C=<permit>` queues Stop and opens
that new session. Its `{A<session>}` acknowledgement follows the UART Stop handoff;
all previous permits are invalidated. Wait for this acknowledgement and a subsequent
permit before sending movement. Retrying an arm with the same session is refused;
a missing acknowledgement requires Stop and a new explicit arm, not automatic retry.

`N=202,H=<session>,C=<permit>,S=<increasing uint32>,D1=<direction>,D2=<PWM>,T=200`
becomes the bounded stock `N=2` packet only when the session, sequence and permit
are valid again at UART dispatch. `D1=0,D2=0` is an idle zero in the live session.
The legacy `N=100` Stop always retires it. After the UART Stop handoff,
`{Z<request H>}` acknowledges that specific Stop; use a unique request ID for an
arm barrier. `{X}` reports watchdog expiry and invalid input. Manual movement while an autonomous session is
active stops it; a separate explicit Stop is required before manual takeover.
Other legacy commands retain their existing rules. No arbitrary UART passthrough
or indefinite motor command is added.

The 200 ms bridge watchdog retires a moving session on missed refresh; a late
packet cannot revive it. The Uno still receives its own independent 200 ms timed
command. Queue expiry, reconnect, malformed fragments and 32-bit clock wrap are
covered by `python3 firmware/elegoo-ble/test/run.py`. The server rejects movement older than 150 ms again at socket dispatch; the phone
rechecks permit receipt age before its bounded BLE write.

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
   A fresh foreground launch may prepare control again, but always needs a new arm.
4. Complete the measured profiles and restart the backend. Readiness must have no
   blockers. Select Navigate, explicitly Arm, then select a mapped destination.
   Explore starts planning upon explicit arm. Manual PWM driving stays on the phone.
5. **STOP ROVER** stops the backend and sends an independent phone-side Stop through
   the setup channel. Stop remains available without REST enable or a pairing key.

`/device` is a separate authenticated setup WebSocket with read-only `GET /device`
and authenticated `POST /device/action`. Its allowlist only includes capture start/stop,
Bluetooth scan/selection/disconnect, laptop-control enable/disable and Stop. It never
accepts motor packets or arms navigation. It supports one active phone, bounded
requests, explicit acknowledgements and discards pending actions on disconnect.
Setup can reconnect automatically after a network fault; it always stops the rover
and retires laptop control first. It never resumes motion. The independent `/rover`
link keeps its tighter freshness checks, even during camera startup.

The `/rover` sender schedules heartbeats from the last outbound message. Incoming
20 Hz ESP status must not reset that deadline; otherwise an idle, healthy phone
times out waiting for the laptop. `RelaySocketTests` covers continuous status and
independent disconnect on missing feedback. After this fix, the real phone stayed
enabled for approximately 11 seconds before ARKit tracking loss stopped control;
no movement was sent. This does not establish sustained network or driving readiness.

The phone allows up to three seconds for the initial laptop handshake, withholding
outbound feedback until the server responds and discarding permits accumulated
during startup. Once connected, the existing 200 ms send deadline and 500 ms
server-silence deadline apply. Capture and rover-feedback checks apply throughout.
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
rectangle plus 30 cm; the obstacle threshold is 6.5 cm, not measured traversability.
Actual motor speed, yaw sign and stopping distance remain unverified. This is only
for supervised tests in open space, not a claim of accurate autonomous driving.

The existing pose/map follower chooses forward or pivot direction, then the profile
uses PWM 60, matching the existing phone manual-control default, never synthetic
speed curves or reverse. Commands update continuously with no added pauses or run
duration limit. Each command retains the firmware's renewable 200 ms lease. Stop, tracking/depth freshness, authenticated pairing, unique arm sessions,
ESP permits, Uno feedback and link watchdogs remain enforced. No automatic arming.
The dashboard labels this profile **Uncalibrated prototype**. Select Explore or Navigate,
then explicitly Arm. Stop and reassess if the rover turns in the wrong direction.

Validation: `python -m unittest backend.tests.test_prototype -v` checks estimate
provenance, unchanged measured gates, API readiness, command bounds and continuous delivery without artificial pauses or run limits. No hardware motion is implied by those tests.

Prototype occupancy includes medium-confidence LiDAR samples (common on carpet),
with the existing repeated-frame free/obstacle evidence thresholds. Low-confidence
samples remain excluded; displayed point clouds retain high-confidence sampling.
