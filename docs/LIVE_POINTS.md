# Dense live points (optional v2)

The viewport requests WebSocket subprotocol `godseye.points.v2` on `/live`.
An updated backend accepts that subprotocol and sends point chunks as binary
messages. Health, pose, objects, and other control messages stay v1 JSON.
Clients that do not request the subprotocol keep the frozen
[v1 interface](INTERFACES.md): JSON points at up to 4 Hz and 2,500 samples.
The viewport can also consume the JSON stream from older servers and fixtures.
When a server does not accept the dense handshake, the viewport retries once
without a subprotocol. A successful fallback stays in legacy mode across reconnects
until reload; failed connections retain exponential backoff.
The phone app and its v1 frame bundles need no update.

With a dense viewer connected, the backend projects up to **20,000** reliable
samples per frame at up to **30 Hz**. This raises the delivery ceiling from
10,000 to 600,000 points per second. These are observations, not necessarily
new spatial cells. Actual rates depend on phone frame delivery, high-confidence
depth, processing speed, and connection bandwidth. Confidence 2, depth 0.05–5 m,
same-frame calibration, tracking/reset gates, and the 2 million retained-point
limit still apply. Lower-confidence samples are never added to inflate density.

## Binary layout

One WebSocket message:

```
[uint32 LE header_length][UTF-8 JSON header padded with spaces to 4 bytes]
[count × 3 float32 LE XYZ positions][count × 3 uint8 sRGB colors]
```

Example header:

```json
{"version":2,"type":"points","chunk_id":24,"session_id":"uuid","map_epoch":1,"frame_id":171,"t_capture":5.7,"count":20000,"positions":"float32_le","colors":"rgb8_srgb"}
```

Header length includes padding. The receiver limits it to 4,096 bytes, validates
identity, timestamps, finite coordinates, and exact payload length before any
map mutation. Coordinates retain float32 precision in ARKit Y-up world meters;
colors preserve the source JPEG's 8-bit channels. A full chunk uses 300 KB plus
its small header. There are no inferred surfaces or wall rectangles.

## Work and memory limits

The backend retains one newest pending frame and one in-flight computation.
Each viewer retains at most two unsent point chunks, separately from control
messages. Dense chunks are encoded once and shared across dense listeners.
Legacy listeners receive a 2,500-point subset at their original rate.

In the browser, a dedicated worker validates, indexes 1 cm cells, converts colors,
and accumulates points. The main thread sends one job at a time and keeps only
the newest waiting wire chunk, RGB-D capture, and map announcement. A reconnect resets the worker
and rejects responses from the previous connection. Map announcements clear old
geometry; discarded/retired map chunks cannot repopulate it.

The worker transfers changed ranges to the renderer using transferable buffers.
Unchanged observations produce no GPU upload. Scattered changes no longer force
a complete buffer upload. Pending GPU ranges are merged, including while the tab
is hidden. GPU updates run once per rendered frame, outside React reconciliation.
The worker and renderer each hold 48 MB of position/color arrays and an 8 MB
visible-point index. Covered samples leave that index in constant time; only
changed index ranges transfer to the renderer. Original measurements remain for
points-only mode. The worker also uses an 8 MB reverse lookup and 16 MB observation
timestamps; spatial indexes, transfer buffers, and the GPU require additional memory.

## Validation and performance

```sh
python3 -m unittest discover -s backend/tests
cd dashboard
npm run build
npm test
node tools/benchmark-points.mjs
```

The benchmark fills a two-million-point cache with 20,000-point binary chunks,
measures worker processing and renderer copying, then changes 2,500 widely
scattered colors. That update transfers 60 KB of point attributes instead of the
previous 48 MB fallback. It measures CPU/copy costs, not physical-device scan
speed or GPU frame rate. Browser tests cover the actual worker and real backend
transport, map resets, reconnection, unchanged rendering, and camera controls.

The retirement benchmark also covers 90% of a synthetic two-million-point cache.
It keeps all two million original measurements while submitting only 200,000
point vertices in hybrid mode (90% fewer). This measures point draw-list work;
triangle rendering, fragment cost, and physical GPU FPS are separate.
