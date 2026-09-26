# Synthetic development sources

These tools implement [the frozen v1 contract](../docs/INTERFACES.md). All data
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

`tools/probe_live.py` connects to a backend `/live` (default `ws://localhost:8765/live`) and prints message counts, the latest `points` chunk, the latest objects and any change events, so the phone-to-map path can be checked without the dashboard; see `backend/README.md`.

Offline validation (no sockets, camera, car, or weights):

```sh
$HOME/.venvs/godseye/bin/python -m unittest discover -s tools/tests -v
```
