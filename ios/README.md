# God's Eye iPhone capture

A native SwiftUI/ARKit sensor app for a physical iPhone running iOS 16 or later. A LiDAR-equipped Pro model is needed for scene depth and reconstruction. Devices without LiDAR can capture RGB and pose, but do not fabricate depth.

## Captured data

One `ARSession` owns the rear camera. Images, calibration, depth, confidence, and pose come from the same `ARFrame`; the app never pairs an image with a later cached pose.

| Data | Live v1 stream | Local recording |
| --- | --- | --- |
| Camera pose, tracking, session/epoch, frame ID, timestamps | Up to 30 Hz | With each recorded frame |
| RGB and camera intrinsics | 960×720 JPEG, quality 0.6, target 5 or 10 Hz | Native video resolution JPEG, quality 0.95, target 2, 5, or 10 Hz (default 2) |
| Unsmoothed scene depth and its confidence | Native depth resolution, lossless float32 / uint8 | Same lossless buffers |
| Temporally smoothed depth and its confidence | No | Separate lossless buffers, when supported |
| Mesh vertices, normals, faces, face classifications | No | Full anchor snapshots, up to 1 Hz |
| Horizontal/vertical planes and boundaries | No | Included in anchor snapshots |
| Feature point positions and IDs | No | Per recorded frame |
| Exposure, EXIF, light estimate, tracking detail, mapping status | No | Per recorded frame |
| Fused motion: attitude, gravity, acceleration, rotation rate | No | Latest available sample, with its own timestamp, per recorded frame |
| Original Y and CbCr color planes | No | Optional lossless recording at the archive rate |
| High-resolution stills | No | On request, when the active video format supports them |

Rates are targets, not guaranteed throughput. The app requests both raw and smoothed depth when the combined semantics are supported. "Raw" here means ARKit's unsmoothed scene depth, not access to the LiDAR's underlying laser returns. Confidence is preserved so mapping can reject weak depth measurements. ARKit exposes the camera stream selected by its world-tracking configuration; this app does not claim simultaneous capture from every iPhone lens.

The highest-resolution compatible 4:3 video format is used, preferring a format that also supports high-resolution stills. The wire contract fixes RGB at 960×720; 4K 16:9 streaming would need explicit crop/calibration changes. Native depth dimensions are read at runtime. Still photos retain their own calibration and timestamp and are not assigned depth from another frame.

## Run on an iPhone

1. Open `ios/GodsEye.xcodeproj` in Xcode. Select the **GodsEye** scheme, your signing team, and a connected iPhone. Set a unique bundle identifier if your signing account requires it.
2. Build and run. Allow camera, local network, and motion access when requested.
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

Each session directory contains:

```text
manifest.json                  Capabilities, capture configuration, coordinates
frames/<frame_id>/
  rgb.jpg                      Native sensor orientation, no UI rotation
  raw.depth.f32                Little-endian float32 optical-axis meters
  raw.confidence.u8            0 low / 1 medium / 2 high
  smoothed.depth.f32           Separate temporally filtered depth, if available
  smoothed.confidence.u8       If supplied by ARKit
  color.y.u8, color.cbcr.u8    Optional packed original color planes
  metadata.json                Completion marker, dimensions, K, pose, timestamps
geometry/<frame_id>/
  anchors.json                 Complete snapshot and anchor-to-world transforms
  <anchor_id>.vertices.f32     Packed float3, anchor-local coordinates
  <anchor_id>.normals.f32       Packed float3, anchor-local coordinates
  <anchor_id>.faces.bin        Indices; width/count specified in anchors.json
  <anchor_id>.classes.u8       ARMeshClassification values, when available
stills/<uuid>.jpg, <uuid>.json  High-resolution image and its own frame metadata
summary.json                   Stop reason, completed frame count, bytes written
```

Strip row/element padding before writing. Invalid depth values remain intact; readers must filter nonfinite/nonpositive values and use confidence. Feature point IDs are strings to avoid JavaScript integer precision loss. Meshes are accumulated estimates and need their anchor transform; they are not instantaneous scans. Each geometry snapshot replaces the prior anchor set, including removal of absent anchors. Motion timestamps use device uptime but the motion reference frame is not calibrated to ARKit world coordinates.

A frame without `metadata.json`, or geometry without `anchors.json`, is incomplete and should be ignored. An interrupted process may leave no `summary.json`. Completed files remain readable. Recordings stop at 2 GB per session or below 500 MB of free space; streaming continues. The size budget includes intermediate writes and is conservative.

## Responsiveness and compatibility

Only one frame encoding job is active at a time. The network retains at most one pending pose and one pending bundle; old/late frames are dropped to preserve capture order. Sends have a two-second timeout, and connection failures retry with bounded backoff. A reconnect sends `hello` again for the current AR session; it does not merge different coordinate systems. Tracking loss is still sent as pose telemetry, while depth bundles require normal tracking.

At serious thermal load, bundles fall to 5 Hz and recordings to 1 Hz. Critical temperature stops capture. These policies need sustained testing on the actual mounted phone. Local mesh recording and high-resolution photos can reduce achieved sample rates.

The frozen [v1 interface](../docs/INTERFACES.md) is unchanged. Extra data stays in local archives because v1 does not accept mesh or extended metadata messages. The current backend validates and discards image/depth payloads; it does not yet reconstruct or store a scene. Neither app nor backend drives hardware.

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

The Python check needs the backend dependencies plus NumPy and Pillow. It runs the Swift tests and passes an actual Swift-generated JPEG/depth bundle through both backend decoders, checking calibration, session identity, endianness, and same-frame alignment. Swift tests also cover row padding, endpoint validation, nonfinite metadata, queue replacement, stale drops, and timestamp ordering.

An unsigned build verifies compilation, not camera behavior. Physical-device checks still required: RGB/depth registration against a flat surface; all supported archive files; mesh updates/removals; stop/start and background behavior; network loss/reconnect; a sustained capture with measured frame rate, battery, and temperature. Simulator runs cannot validate LiDAR.

Apple references: [scene depth and confidence](https://developer.apple.com/documentation/arkit/ardepthdata), [raw and smoothed depth capture](https://developer.apple.com/documentation/arkit/displaying-a-point-cloud-using-scene-depth), [configuration and high-resolution capture](https://developer.apple.com/documentation/arkit/configuration-objects), [mesh classifications](https://developer.apple.com/documentation/arkit/armeshclassification).
