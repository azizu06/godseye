# Synthetic development sources

The synthetic sources implement [the frozen v1 contract](../docs/INTERFACES.md). All data
is made up; neither tool drives hardware. Python 3.10+ is required.

```sh
$HOME/.venvs/godseye/bin/python -m pip install -r tools/requirements.txt
$HOME/.venvs/godseye/bin/python tools/fake_phone.py --url ws://localhost:8765/phone
$HOME/.venvs/godseye/bin/python tools/fake_live.py --port 8766
```

Point the dashboard WebSocket URL at `ws://127.0.0.1:8766/live` (or the host's
IP with `--host 0.0.0.0` for another device). Port 8766 keeps the mock separate
from the real backend on 8765. The mock serves only `/live`, not REST controls.
Each client starts its own timeline. After five seconds, `synthetic-backpack`
moves one meter and emits a `moved` event. `--move-after SECONDS` changes the
trigger. The health stop reason and startup banner explicitly label this demo
synthetic; `armed` stays false and `car` stays down.

The phone sends hello once, 30 Hz moving camera poses, and 5 Hz binary bundles
(`--frame-hz 5` through `10`). Its deterministic 960×720 JPEG gradient (or `--image PATH`, a local photo
resized to 960×720 for detector smoke tests), 256×192
little-endian float32 depth ramp, and high-confidence bytes need no assets.
Frame metadata comes from the same pose sent immediately before the bundle.
Camera translation occupies entries 12–14 of the column-major transform.
Capture times are elapsed monotonic seconds; wall times are Unix milliseconds.
Each connection gets a new session UUID. Slow sends skip missed pose ticks rather
than flooding the backend. Connection errors exit visibly; restart to reconnect.
Ctrl-C stops either tool.

## Separate manual USB rover relay

`rover_usb_relay.py` is a physical Uno manual-control tool, not a synthetic source or
the backend navigation adapter. It lets the phone keep normal Wi-Fi while a laptop
USB cable carries rover commands. It requires a verified non-motion Uno reply and
a pairing code before accepting a controller. Setup, the Upload/Cam switch,
timed-command limits and hardware test requirements are in [ios/ROVER.md](../ios/ROVER.md).
The optional dependency is `requirements-rover.txt`. Hardware-free checks:
`python3 -m unittest tools.tests.test_rover_usb_relay -v`.

`tools/probe_live.py` connects to a backend `/live` (default `ws://localhost:8765/live`) and prints message counts, the latest `points` chunk, the latest objects and any change events, so the phone-to-map path can be checked without the dashboard; see `backend/README.md`.

## Paired iPhone rover backend

`python3 -m tools.run_rover_backend --init` creates a private local pairing key and
all-null geometry/actuation profiles. Run without `--init` to serve the opt-in phone
relay; pass `--weights /absolute/path/to/yolo11n.pt` for the detector. Unknown
measurements prevent driving. See [autonomy setup](../docs/AUTONOMY.md).

The rover launcher loads server settings from the repository-root `.env` before
creating the app, including the [voice provider settings](../backend/VOICE.md#live-configuration).
Keep that gitignored file private (`chmod 600 .env`); provider keys belong only on
the backend. `--env-file /path/to/settings.env` selects another file. Existing
process environment values take precedence; `--weights` also overrides
`GODSEYE_YOLO_WEIGHTS` from either source. A missing file is optional, and `--init`
only creates local pairing/calibration files without loading provider settings.

`python3 -m tools.probe_rover_ble --name GodsEye-Rover-D022 --samples 5 --autonomy`
checks the real ESP/Uno with Stop, sensor queries, an arm barrier and idle zeros;
it sends no nonzero motor command. Disconnect the phone's BLE connection first.
`python3 ios/Tests/check_rover_autonomy.py` instead exercises the actual Swift relay
over a loopback WebSocket with fake BLE, without hardware.

## Car-ready smoke (no car)

```sh
$HOME/.venvs/godseye/bin/python -m pip install -r backend/requirements-test.txt
$HOME/.venvs/godseye/bin/python -m tools.car_smoke [--delayed-bundles]
```

`tools/car_smoke.py` starts the default backend on a free loopback port (task-local
DB, recording off, no weights) and streams the deterministic floor + 10 cm low box +
50 cm ordinary box through `/phone`. It asserts reproducible `/live` occupancy,
unknown shadow/behind-phone space, and that the default logging adapter stays down
and refuses `/arm`.

The same command then runs `tools/car_rehearsal.py` against the real app's test
WebSocket/REST transport with a healthy **FakeCar**, offline empty detector, and
explicit **TEST** calibration that describes no real rover. Survey frames cover the
starting footprint; `/goal` produces a path and bounded commands through the motion
pump. A stationary FakeCar ends in `no_progress`, an empty path and terminal zero.
The rehearsal also asserts uncalibrated goal/explore refusal, unknown/off-grid/blocked
goals, stale sensing despite fresh poses, accepted repeats without new published
points, expired manual lease zero, and phone disconnect/reconnect generation isolation.
It owns and cleans up its servers, feeder threads and temporary DBs. About 30 s;
90 s per socket mapping phase. `--delayed-bundles` retains the informational encoder
latency probe; it is not a physical capture or movement test.

**Remaining real-car checkpoint:** Tomiwa must supply measured chassis/bumpers,
clearance/lowest hazard height, phone mount offset and yaw alignment. Verify steering
sign, ability to turn in place, speed response/stopping distance, car-side independent
stop/watchdog and emergency stop on hardware. The Mac command lease is not that
watchdog. Connecting a physical adapter or live actuation needs a **separate explicit
live-actuation approval**. TEST calibration, passing backend tests and FakeCar motion
logs authorize none of those steps. No physical adapter exists in this rehearsal.

Offline validation (no sockets, camera, car, or weights):

```sh
$HOME/.venvs/godseye/bin/python -m unittest discover -s tools/tests -v
```

For an explicitly requested uncalibrated rover test, `run_rover_backend --prototype`
requires `--estimated-length-m` and `--estimated-width-m` with no dimension defaults.
See [prototype limits and setup](../docs/AUTONOMY.md#uncalibrated-prototype-option).

## Scout driving benchmark

```sh
$HOME/.venvs/godseye/bin/python -m tools.scout_benchmark --output-dir /tmp/scout-after
$HOME/.venvs/godseye/bin/python -m tools.scout_benchmark --revision 94d64f9 --output-dir /tmp/scout-before
$HOME/.venvs/godseye/bin/python -m unittest tools.tests.test_scout_benchmark -v
```

The benchmark uses the real live planner settings, path follower, replanning and
prototype packet conversion against deterministic invented differential-drive
response. It covers an off-center corridor, obstacle detour, right-angle corridor
and close goal. `benchmark.json` records source hashes, fixture dimensions,
response assumptions, metrics and complete traces. Open `replay.html` for
playback, scrubbing, route overlays and command inspection. Everything is labeled
synthetic; it opens no hardware connections and is not a video of the rover.

Use `--synthetic-wheel-speed-mps` and `--synthetic-track-width-m` to test different synthetic responses,
`--response-mode ideal` to compare an ideal velocity follower, and `--scenario`
to select a case. The actual async Navigator and changing-map behavior are
covered separately by `backend.tests.test_scout_navigation`,
`backend.tests.test_navigator`, and `backend.tests.test_flowing_detour`; the last
introduces a hallway obstacle after driving starts and checks early steering,
continued translation and nominal cruise through the pass. This benchmark does not emulate network timing
or raw sensor reconstruction.

## Offline nav-log replay (no rover needed)

```sh
$HOME/.venvs/godseye/bin/python -m tools.replay_nav_log /path/to/backend/captures/nav-logs/<run> --goal X Z
$HOME/.venvs/godseye/bin/python -m tools.replay_nav_log /path/to/<run> --json /tmp/replay.json --trace-jsonl /tmp/trace.jsonl
$HOME/.venvs/godseye/bin/python -m unittest tools.tests.test_replay_nav_log -v
```

`tools/replay_nav_log.py` replays whatever pose/occupancy a recorded nav-log run
captured (gitignored `backend/captures/nav-logs/<run>/{manifest.json,events.jsonl,
live.jsonl,summary.json}`) through the real `backend.navigation`/`backend.navigator`
planning code (`Navigator._plan`, `Navigator._check`, `PurePursuit`), so a teammate
without rover access can see what the planner would have decided at each recorded
moment. Point it at a run directory copied from whoever captured it; the run does
not need to live inside this checkout. Omit `--goal X Z` for an explore replay
(frontier-seeking); pass it for a goal replay to that point. `--inflation-m` and
`--unknown-traversable` set the assumed planner geometry when the log doesn't carry
real calibration (defaults match `backend.navigation.PlannerConfig`).

Hardware refusal by construction: it never imports `backend.app`, never opens a
socket/websocket/BLE/serial connection, reads no pairing key, and issues no live
command — it only calls pure planning functions with data already in the log.

It is a planner-decision replay, not a sensor or motion replay: the rover position
fed to the planner each tick is the log's own recorded ground-truth pose, never a
position integrated from planned commands (no motor-physics simulation), and raw
depth/camera/audio/GPS/motor-command/ESP-feedback data — never in the nav-log — is
never reconstructed. It also omits Navigator's pursuit feasibility downgrade, the
Explore person-yield/resume gate, and `ScanPacer` pacing; see the module docstring
and each report's `limitations` field. The CLI prints manifest/schema compatibility
notes before replaying and reports `end_of_recorded_log`/`pose_stale`/etc. distinctly
from a genuine planner stop, so a mismatched or partial log is never silently
misread as a full run.
