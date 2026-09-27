# God's Eye iPhone capture

A native SwiftUI/ARKit sensor app for a physical iPhone running iOS 16 or later. A LiDAR-equipped Pro model is needed for scene depth and reconstruction. Devices without LiDAR can capture RGB and pose, but do not fabricate depth.

The **ELEGOO V4 rover** panel provides a separate manual remote over stock ELEGOO Wi-Fi, Bluetooth with God's Eye firmware on the ESP, or a laptop-to-Uno USB relay. Bluetooth and the tethered USB relay leave the phone's normal Wi-Fi available for mapping. See [ROVER.md](ROVER.md) for firmware requirements, connection, motor-power limits, timed commands and verification.

## Captured data

One `ARSession` owns the rear camera. Images, calibration, depth, confidence, and pose come from the same `ARFrame`; the app never pairs an image with a later cached pose.

The laptop receives native camera images, lossless color planes, raw and smoothed depth/confidence, feature points, scene meshes/planes, optional people masks/body landmarks, exposure/lighting/calibration, and high-resolution stills. It also receives timestamped raw and fused motion, pressure/altitude, location/heading with accuracy, and device/permission status.

See [the full capture contract](../docs/CAPTURE.md) for the complete inventory, units, rates, availability, binary format and recording limits. The live stream uses v1 frames, v2 frames with classified floor anchors, and v3 frames that repeat a compact current ARKit mesh snapshot resampled about once per second. The separate full-sensor v2 upload remains visible at `http://<laptop IP>:8765/capture`. **Upload full sensor data** turns that bulk upload off without stopping the live stream.

Rates are targets, not guaranteed throughput. The app requests both raw and smoothed depth when the combined semantics are supported. "Raw" here means ARKit's unsmoothed scene depth, not access to the LiDAR's underlying laser returns. Confidence is preserved so mapping can reject weak depth measurements. ARKit exposes the camera stream selected by its world-tracking configuration; this app does not claim simultaneous capture from every iPhone lens.

The highest-frame-rate compatible 4:3 video format is used, choosing the highest resolution at that rate. High-resolution still capture is offered only when the chosen format supports it. The wire contract fixes RGB at 960×720; 4K 16:9 streaming would need explicit crop/calibration changes. Native depth dimensions are read at runtime. Still photos retain their own calibration and timestamp and are not assigned depth from another frame.

## Run on an iPhone

1. Open `ios/GodsEye.xcodeproj` in Xcode. Select the **GodsEye** scheme, your signing team, and a connected iPhone. Set a unique bundle identifier if your signing account requires it.
2. Build and run. Allow camera, local network, motion, and location access when requested.
3. Put the laptop and phone on the same private network. Start the backend using [its setup instructions](../backend/README.md), listening on `0.0.0.0:8765`.
4. Enter `ws://<laptop IP>:8765/phone`. Use the laptop's network IP, not `localhost`. Keep the phone and laptop clocks synchronized; the backend rejects wall-clock skew over 250 ms.
5. Choose streaming, full sensor upload, recording, mesh reconstruction, and optional lossless color, then tap **Start capture**. On busy Wi-Fi, turn off **Upload full sensor data**: it shares the uplink with live poses and bundles, which the backend rejects when they arrive more than 250 ms after capture. Move the phone gently until tracking becomes normal. Use **High-res photo** for occasional detailed stills.
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

Live RGB-D targets 30 Hz by default, with a dedicated high-priority encoder separate from full-sensor/archive encoding and mesh export. The UI reports encoded/s, sent/s, encoding time and network drops. Each lane permits one job at a time. The network retains one pending pose and one pending bundle with independent timestamp ordering, so a newer pose does not discard a valid delayed bundle. Sends have a two-second timeout, and connection failures retry with bounded backoff. A reconnect sends `hello` again for the current AR session; it does not merge different coordinate systems. Tracking loss is still sent as pose telemetry, while depth bundles require normal tracking.

Full upload uses one request plus one pending packet per kind, with a weighted frame/telemetry/frame/geometry/frame/still schedule. It prioritizes frames without starving other sensor kinds and exposes replacement/failure counts. Raw motion is sampled at a target 100 Hz and batched at 5 Hz. At serious thermal load, full frame upload and recordings fall to 1 Hz, geometry to 0.5 Hz, and live bundles are capped at 15 Hz (never above the selected rate). Critical temperature stops capture. These policies need sustained testing on the actual mounted phone. Local mesh recording and high-resolution photos can reduce achieved sample rates.

The [wire interface](../docs/INTERFACES.md) retains v1 pose/hello and accepts optional v2 floor and v3 mesh frame evidence. Other full sensor data uses HTTP `/capture/ingest` and does not refresh drive health. The backend also publishes v1 live map points and object memory as documented in its README. Its default drive adapter remains logging-only. The iPhone's separate [ELEGOO manual remote](ROVER.md) can send hardware commands only after a verified Uno reply and explicit user enablement.

## Validation

Laptop rover control automatically reserves network bandwidth: live RGB-D targets
10 Hz while pose updates remain 30 Hz, and bulk v2 uploads pause until control is
disconnected. Native local recording keeps its selected cadence and quality.
This avoids competing bulk uploads delaying the short-lived ESP permits.
Computer-control startup also leaves bulk laptop upload off before the rover connects;
the complete sensor recording remains on the iPhone when recording is enabled. Turn off computer-control-on-launch
if you need bulk laptop upload instead of rover control.
In prototype Explore, a temporary depth, pose or ESP-permit gap pauses movement
without losing the requested mode. The phone accepts permits for up to 500 ms,
matching the ESP gate; the ESP brakes after 1 s without a fresh autonomous
command. A broken connection retires its drive session; foreground setup can
reconnect, and the laptop only resumes after fresh capture, map and rover feedback.
Dashboard Stop, Disable laptop control, and mode changes clear the request.

Run from the repository root:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift test --package-path ios
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer python3 ios/Tests/check_backend_contract.py
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project ios/GodsEye.xcodeproj -scheme GodsEye \
  -destination 'generic/platform=iOS' -derivedDataPath /tmp/godseye-ios-build \
  CODE_SIGNING_ALLOWED=NO build
```

The Python check needs the backend dependencies plus NumPy and Pillow. It runs the Swift tests and passes an actual Swift-generated JPEG/depth bundle through both backend decoders, checking calibration, session identity, endianness, and same-frame alignment. It also verifies a real Swift v2 packet preserves depth NaNs, image bytes, timestamps and null landmarks. Swift tests also cover row padding, endpoint validation, nonfinite metadata, queue replacement, stale drops, timestamp ordering, and turning the full sensor upload off without stopping the live stream.

An unsigned build verifies compilation, not camera behavior. Physical-device checks still required: RGB/depth registration against a flat surface; all supported archive files; mesh updates/removals; stop/start and background behavior; network loss/reconnect; a sustained capture with measured frame rate, battery, and temperature. Simulator runs cannot validate LiDAR.

Apple references: [scene depth and confidence](https://developer.apple.com/documentation/arkit/ardepthdata), [raw and smoothed depth capture](https://developer.apple.com/documentation/arkit/displaying-a-point-cloud-using-scene-depth), [configuration and high-resolution capture](https://developer.apple.com/documentation/arkit/configuration-objects), [mesh classifications](https://developer.apple.com/documentation/arkit/armeshclassification).
