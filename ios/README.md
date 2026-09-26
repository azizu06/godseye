# God's Eye iPhone capture

A native SwiftUI/ARKit sensor app for a physical iPhone running iOS 16 or later. A LiDAR-equipped Pro model is needed for scene depth and reconstruction. Devices without LiDAR can capture RGB and pose, but do not fabricate depth.

## Captured data

One `ARSession` owns the rear camera. Images, calibration, depth, confidence, and pose come from the same `ARFrame`; the app never pairs an image with a later cached pose.

The laptop receives native camera images, lossless color planes, raw and smoothed depth/confidence, feature points, scene meshes/planes, optional people masks/body landmarks, exposure/lighting/calibration, and high-resolution stills. It also receives timestamped raw and fused motion, pressure/altitude, location/heading with accuracy, and device/permission status.

See [the full capture contract](../docs/CAPTURE.md) for the complete inventory, units, rates, availability, binary format and recording limits. The frozen v1 stream continues alongside this separate v2 upload; full data is visible at `http://<laptop IP>:8765/capture`.

Rates are targets, not guaranteed throughput. The app requests both raw and smoothed depth when the combined semantics are supported. "Raw" here means ARKit's unsmoothed scene depth, not access to the LiDAR's underlying laser returns. Confidence is preserved so mapping can reject weak depth measurements. ARKit exposes the camera stream selected by its world-tracking configuration; this app does not claim simultaneous capture from every iPhone lens.

The fastest compatible 4:3 video format is used, choosing the highest resolution at that frame rate. High-resolution stills remain available when that format supports them. The wire contract fixes RGB at 960×720; 4K 16:9 streaming would need explicit crop/calibration changes. Native depth dimensions are read at runtime. Still photos retain their own calibration and timestamp and are not assigned depth from another frame.

## Run on an iPhone

1. Open `ios/GodsEye.xcodeproj` in Xcode. Select the **GodsEye** scheme, your signing team, and a connected iPhone. Set a unique bundle identifier if your signing account requires it.
2. Build and run. Allow camera, local network, motion, and location access when requested.
3. Put the laptop and phone on the same private network. Start the backend using [its setup instructions](../backend/README.md), listening on `0.0.0.0:8765`.
4. Enter `ws://<laptop IP>:8765/phone`. Use the laptop's network IP, not `localhost`. Keep the phone and laptop clocks synchronized; the backend rejects wall-clock skew over 250 ms.
5. Choose streaming, recording, mesh reconstruction, and optional lossless color, then tap **Start capture**. Move the phone gently until tracking becomes normal. Use **High-res photo** for occasional detailed stills.
6. Stop capture before exporting through **Files → On My iPhone → God's Eye → Captures**, or Xcode/Finder file sharing. Delete old captures there when no longer needed.

For capture without a laptop, turn off **Stream to laptop**. Settings apply when starting a new session. Every start creates a new session UUID and increments the map epoch. Backgrounding or an AR interruption stops capture; restart explicitly when ready.

The project is checked in and has no third-party iOS dependencies. Regenerate it after editing `project.yml` with:

```sh
xcodegen generate --spec ios/project.yml
```

## Recording format

Phone archives now use v2 `.capture` packets, the same exact format accepted by the laptop:

```text
manifest.json                         Device/configuration, coordinates, limits
packets/frame/<id>-<uuid>.capture      Images, depth, calibration and feature points
packets/telemetry/<id>-<uuid>.capture   Timestamped sensor batches and device state
packets/geometry/<id>-<uuid>.capture    Complete mesh/plane snapshots
packets/still/<id>-<uuid>.capture       Requested high-resolution ARFrame
summary.json                          Stop reason, frame count, bytes written
```

Files are written atomically. An interrupted process may leave no summary; completed packets remain readable. Phone recordings stop at 2 GB or below 500 MB free; capture/upload continues. Laptop recording defaults to `backend/captures`, stops at 8 GiB per session with a 1 GiB free-space reserve, and reports errors in `/capture`. Neither recorder automatically deletes old sessions. See [CAPTURE.md](../docs/CAPTURE.md) for parsing and environment settings.

## Responsiveness and compatibility

Live RGB-D bundles target 30 Hz by default (5 and 10 Hz remain selectable), using the same v1 packet layout and calibration. A dedicated high-priority encoder processes one live frame at a time, independently of full-sensor encoding, mesh exports, and disk writes. Full native capture still targets 5 Hz upload and preserves all its original data. The phone displays measured encode/send rates and encoding latency; telemetry also exposes these values. These are achieved rates, unlike the picker target. The network retains at most one pending pose and one pending bundle. Each stream has its own capture-time watermark: an RGB-depth bundle finishing encoding after a newer pose is still sent with its original same-frame pose. Duplicate or out-of-order messages within either stream, and messages older than 200 ms, are dropped. Sends have a two-second timeout, and connection failures retry with bounded backoff. A reconnect sends `hello` again for the current AR session; it does not merge different coordinate systems. Tracking loss is still sent as pose telemetry, while depth bundles require normal tracking.

Full upload uses one request plus one pending packet per kind, exposing replacement/failure counts. A weighted round-robin scheduler reserves three of six opportunities for camera frames and one each for telemetry, geometry, and stills; absent kinds are skipped. Mesh encoding runs separately from camera encoding, so a large mesh export does not block the next RGB-D frame. Raw motion is sampled at a target 100 Hz and batched at 5 Hz. At serious thermal load, live bundles target up to 15 Hz; full frames and recordings fall to 1 Hz, and geometry to 0.5 Hz. Lower requested live rates remain unchanged. Critical temperature stops capture. These policies need sustained testing on the actual mounted phone. Local mesh recording and high-resolution photos can reduce achieved sample rates.

The frozen [v1 packet layout](../docs/INTERFACES.md) is unchanged; the fast profile raises its historical 5–10 Hz target to 30 Hz. Existing receivers accept the same messages, while older mapping servers may still publish fewer frames. Extra data uses HTTP `/capture/ingest` and does not refresh drive health. The backend also publishes v1 live map points and object memory as documented in its README. Neither app nor backend drives hardware.

## Validation

Run from the repository root:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift test --package-path ios
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer python3 ios/Tests/check_backend_contract.py
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project ios/GodsEye.xcodeproj -scheme GodsEye \
  -destination 'generic/platform=iOS' -derivedDataPath /tmp/godseye-ios-build \
  CODE_SIGNING_ALLOWED=NO build
```

The Python check needs the backend dependencies plus NumPy and Pillow. It runs the Swift tests and passes an actual Swift-generated JPEG/depth bundle through both backend decoders, checking calibration, session identity, endianness, and same-frame alignment. It also verifies a real Swift v2 packet preserves depth NaNs, image bytes, timestamps and null landmarks. Swift tests also cover row padding, endpoint validation, nonfinite metadata, queue replacement, stale drops, and timestamp ordering.

The contract check also verifies projected world coordinates and colors against independently calculated points. The dashboard's `npm test` includes a real backend and binary phone-stream test that renders points despite simulated encoding delay; see [dashboard verification](../dashboard/README.md).

A build verifies compilation, not camera behavior. Rebuild and install the current app before testing the independent pose/bundle queues. Physical-device checks still required: RGB/depth registration against a flat surface; all supported archive files; mesh updates/removals; stop/start and background behavior; network loss/reconnect; a sustained capture with measured frame rate, battery, and temperature. Simulator runs cannot validate LiDAR.

Apple references: [scene depth and confidence](https://developer.apple.com/documentation/arkit/ardepthdata), [raw and smoothed depth capture](https://developer.apple.com/documentation/arkit/displaying-a-point-cloud-using-scene-depth), [configuration and high-resolution capture](https://developer.apple.com/documentation/arkit/configuration-objects), [mesh classifications](https://developer.apple.com/documentation/arkit/armeshclassification).
