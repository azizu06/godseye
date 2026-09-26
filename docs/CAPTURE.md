# Full phone capture (v2)

This inspection/recording channel supplements the frozen [v1 interface](INTERFACES.md).
`/phone`, `/live`, mapping, detection, and drive health still use v1. Additional
sensor packets never refresh pose freshness or arm hardware.

## Transport and envelope

First connect `/phone` with the session's v1 hello. Send full packets using
`POST /capture/ingest`, `Content-Type: application/octet-stream`. The phone
uploads one at a time. Concurrent requests wait and are processed sequentially,
without a 429 rejection. Success is HTTP 200; the response includes
`recording_enabled`, `recorded_bytes`, and a nullable `recording_error`. Accepted
live data can still have a recording error: inspect that field rather than
assuming disk success.

Each `.capture` packet has four little-endian bytes giving the UTF-8 JSON header
length, followed by the header, then contiguous binary sections:

```json
{
  "version": 2, "type": "capture", "kind": "frame",
  "session_id": "uuid", "map_epoch": 1, "frame_id": 12,
  "t_capture": 12345.67, "t_wall_ms": 1790000000000,
  "metadata": {},
  "sections": [{"name": "raw_depth", "format": "f32le",
                "offset": 0, "length": 196608, "shape": [192, 256]}]
}
```

- Kinds: `frame`, `telemetry`, `geometry`, `still`. Each kind has an independent,
  strictly increasing capture timestamp; kinds may arrive interleaved.
- `t_capture` is device uptime in seconds. Frame/still poses use that same
  ARFrame's capture time. `t_wall_ms` is UTC at packet encoding, for upload-age
  checks. Sensor samples retain their own acquisition timestamps and clock names;
  nearest motion/location readings are **not** claimed to be simultaneous.
- Offsets are relative to the binary body. No gaps, overlaps, duplicate names or
  trailing bytes. Shapes are row-major `[height,width]`, `[height,width,channels]`,
  `[count,components]`, or `[count]`. Zero-length feature/mesh arrays are allowed.
- Formats: `jpeg`, `u8`, `u16le`, `u32le`, `u64le`, `f32le`. Matrices inside JSON
  are column-major, following ARKit. Packed buffers omit row/element padding.
- Maximum packet 32 MiB, header 2 MiB, 8192 sections. JSON nonfinite numbers are
  invalid; unavailable body landmarks become `null`. Binary depth NaN, infinity,
  and nonpositive readings remain unchanged; consumers must reject invalid depth.
- HTTP 400: malformed packet; 409: inactive session/epoch, duplicate/old packet,
  or encoding wall time more than 15 seconds from the laptop; 413: size limit.
  No upload alters v1 freshness rules.

## Data collected

| Kind | Contents | Target delivery |
| --- | --- | --- |
| Frame | Native JPEG (quality .95), original lossless Y/CbCr color planes by default; raw and smoothed scene depth with separate confidence; sparse feature positions/uint64 IDs; optional people mask, estimated person depth and tracked 2D skeleton | 5 Hz upload; local archive 2/5/10 Hz |
| Frame metadata | Native resolution and camera intrinsics, camera-to-world transform, tracking detail, mapping status, exposure, EXIF, ambient light/color temperature, Euler angles, latest fused-motion sample with its own time | With each frame |
| Telemetry | Separate raw accelerometer, gyroscope, magnetometer and fused-motion samples; gravity, user acceleration, quaternion, rotation rate and magnetic accuracy | Motion requested at 100 Hz; batches uploaded at 5 Hz |
| Telemetry | ARKit poses at up to 30 Hz; relative altitude/pressure, absolute altitude, GPS coordinates/altitude/speed/course and uncertainties; compass headings and uncertainty; battery, thermal, low power, authorization state | Each sensor's delivered rate; batches at 5 Hz |
| Telemetry | Latest sample per sensor, capability flags, errors, dropped samples/uploads/archive batches, skipped image captures, active camera configuration | With each batch |
| Geometry | Complete snapshot of mesh vertices/normals/triangle indices/classifications and plane centers/extents/boundaries; anchor-to-world transforms | Up to 1 Hz |
| Still | High-resolution ARFrame with its own timestamp, calibration, pose and any buffers actually supplied for that frame | Button in app; supported video formats only |

Availability is checked at runtime. People segmentation/depth and body detection
are requested only when their combination with the depth semantics is supported.
An omitted buffer does not mean the scene is empty. Location requires when-in-use
permission. Negative iOS accuracy/speed/heading values and battery `-1` mean
unknown, not a valid measurement. Sensor error strings are recorded.

Coordinates: ARKit world meters, +Y up, camera forward -Z. Depth is optical-axis
meters; scale native intrinsics to the depth dimensions. Images retain native
landscape-right pixel orientation. Color metadata includes pixel format,
full/video range and available attachments for YCbCr conversion. Feature positions
are world-space; mesh vertices/normals are anchor-local and require their
transforms. A geometry snapshot replaces the previous anchor set; missing anchors
were removed. Meshes and smoothed depth are accumulated estimates.

Core Motion vectors use device axes. Its fused attitude reference is
`xArbitraryZVertical`, **not calibrated to ARKit world or the rover chassis**.
GPS/heading sample times use UTC seconds. Frame and telemetry packet times use
uptime. Rover mount calibration and wheel/steering measurements remain separate.

ARKit supplies its selected camera stream, not every lens simultaneously. “Raw
depth” means unsmoothed ARKit scene depth, not laser returns from the LiDAR.
High-resolution images do not increase native depth resolution. This capture
does not access unrelated personal information or microphone audio.

## Storage, backpressure and inspection

The iPhone uploader keeps one request in flight and one latest packet per kind.
Replaced/failed/over-5-second-old queued packets count as dropped. Failed packets
are not replayed. Slow links therefore miss samples; the independent phone archive
can retain data that did not reach the laptop. No claim of complete 100 Hz delivery
is made. Live and full-sensor encoders run independently with at most one job each;
busy frame opportunities count as skipped. Serious thermal state reduces full
frames and local recordings to 1 Hz, geometry to 0.5 Hz, and live v1 bundles to
at most 15 Hz; critical thermal state stops capture.

The laptop stores exact received packets by default in
`backend/captures/<session-hash>/` with a `recording.json` index. Set
`GODSEYE_CAPTURE_DIR` to another folder, or an empty string to disable recording.
Recordings persist across disconnect/reset; live buffers clear. The per-session
limit is 8 GiB with a 1 GiB free-space reserve. On reaching a limit, disk recording
stops and the error appears in the page/app; live inspection continues. Sessions
are not automatically deleted. Export/delete old recordings as needed.

The phone's `Captures/<session-uuid>/` contains `manifest.json`, atomic
`packets/<kind>/<frame-id>-<uuid>.capture` files and a best-effort `summary.json`.
These use the same v2 envelope. Phone archives stop at 2 GB or below 500 MB free.
Older v1 archives are unchanged and are not migrated.

Open `/capture` for sensor previews and metadata. Downloads:

- `/capture/rich/{frame|telemetry|geometry|still}.bin`: exact latest v2 packet.
- `/capture/sensor/{rgb|raw_depth|raw_confidence|smoothed_depth|smoothed_confidence|person_mask|person_depth}`:
  native JPEG or rendered PNG; 204 when unavailable. ETags avoid redundant transfer.
- `/capture/status`: v1 preview/health plus `rich` packet descriptions, receipt ages,
  counts, latest telemetry and recording state. Batch samples and anchor arrays are
  omitted here; download the full packet to inspect them.
- `/capture/frame.jpg` and `/capture/frame.bin`: original v1 image/bundle.
- `/capture/surface.bin`: live rendering subset. Selects a fresh tracked native
  v2 RGB/raw-depth/raw-confidence frame when it is at most 200 ms behind v1;
  otherwise returns the eligible original v1 bundle. V2 uses the existing envelope
  with only those three sections and same-frame calibration/pose metadata. Section
  bytes are unchanged. Returns 204 if unavailable/stale, or 304 for a matching
  `If-None-Match`. `ETag` and `X-Capture-Age-Ms` are exposed to cross-origin viewers.
  Pose receipt must be at most 250 ms old; frame receipt and capture time relative
  to the current pose must be at most one second old. Frames preceding tracking
  loss or belonging to a retired session cannot be served. Full download and
  archive routes retain the complete original packets.

Depth PNGs use a fixed 0–5 m display scale; invalid readings are black. Confidence
is gray/amber/green for 0/1/2. Rendering changes only previews, never raw packets.
Raw depth/confidence previews also work with the older v1 phone app. These are
development endpoints on the same trusted network as the backend, with no added
authentication. Capture files include imagery and location; they are gitignored.

Validation: `python3 -m unittest discover -s backend/tests` and the Swift/Python
contract check in [ios/README.md](../ios/README.md).

The live v1 RGB-D path targets 30 Hz in the fast profile and uses a separate
high-priority encoder; full native v2 captures keep the rates and data above.
Mesh exports have their own serial queue. Telemetry includes `live_encoded_hz`,
`live_sent_hz`, `live_encode_ms`, and `live_network_drops` for measured throughput.
