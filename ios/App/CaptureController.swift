import ARKit
import AVFoundation
import Combine
import CoreMotion
import SensorCore
import UIKit

struct CaptureOptions {
    var endpoint = ""
    var stream = true
    var record = true
    var mesh = true
    var losslessColor = false
    var frameHz = 10.0
    var archiveHz = 2.0
}

final class CaptureController: NSObject, ObservableObject, ARSessionDelegate {
    let session = ARSession()
    @Published private(set) var running = false
    @Published private(set) var status = "Ready to capture"
    @Published private(set) var network = "Offline"
    @Published private(set) var trackingStatus = "Not started"
    @Published private(set) var capabilities = "Checking capabilities…"
    @Published private(set) var sensorStats = ""
    @Published private(set) var thermalStatus = "Nominal"
    @Published private(set) var archiveStatus = "Recording off"
    @Published private(set) var canTakeStill = false

    private let captureQueue = DispatchQueue(label: "com.godseye.capture", qos: .userInitiated)
    private let encodingQueue = DispatchQueue(label: "com.godseye.encoding", qos: .utility)
    private let encoder = SensorEncoder()
    private let motion = CMMotionManager()
    private lazy var stream = PhoneStream(queue: captureQueue)
    private var identity: CaptureIdentity?
    private var epoch = 0
    private var frameID = 0
    private var options = CaptureOptions()
    private var archive: CaptureArchive?
    private var encoding = false
    private var stillPending = false
    private var stillSupported = false
    private var lastPose = -Double.infinity
    private var lastBundle = -Double.infinity
    private var lastArchive = -Double.infinity
    private var lastUI = -Double.infinity
    private var droppedCaptures = 0
    private var requestToken: UUID?

    override init() {
        super.init()
        session.delegate = self
        session.delegateQueue = captureQueue
        capabilities = "LiDAR: \(ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) ? "available" : "unavailable") · Classified mesh: \(ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) ? "available" : "unavailable")"
        stream.onStatus = { [weak self] text in
            DispatchQueue.main.async { self?.network = text }
        }
    }

    /// UI entry points run on the main thread; session work runs on captureQueue.
    func start(_ requested: CaptureOptions) {
        guard !running else { return }
        guard ARWorldTrackingConfiguration.isSupported else {
            status = "ARKit world tracking requires a supported physical iPhone."
            return
        }
        if requested.stream {
            do { _ = try WireProtocol.endpoint(requested.endpoint) }
            catch { status = error.localizedDescription; return }
        }
        let token = UUID()
        requestToken = token; running = true; status = "Starting camera…"
        AVCaptureDevice.requestAccess(for: .video) { [weak self] allowed in
            DispatchQueue.main.async {
                guard let self, self.requestToken == token else { return }
                guard allowed else {
                    self.running = false
                    self.status = "Camera access is required. Enable it in Settings for God's Eye."
                    return
                }
                UIApplication.shared.isIdleTimerDisabled = true
                self.captureQueue.async { self.begin(requested) }
            }
        }
    }

    func stop(reason: String = "Stopped by operator") {
        requestToken = nil; running = false; canTakeStill = false; status = reason
        UIApplication.shared.isIdleTimerDisabled = false
        captureQueue.async { self.end(reason: reason) }
    }

    private func begin(_ requested: CaptureOptions) {
        end(reason: "New capture session")
        options = requested
        epoch += 1
        let current = CaptureIdentity(epoch: epoch)
        identity = current; frameID = 0; droppedCaptures = 0
        lastPose = -.infinity; lastBundle = -.infinity; lastArchive = -.infinity; lastUI = -.infinity
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        config.isAutoFocusEnabled = true
        config.isLightEstimationEnabled = true
        config.planeDetection = [.horizontal, .vertical]
        let hasDepth = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        if hasDepth { config.frameSemantics.insert(.sceneDepth) }
        if ARWorldTrackingConfiguration.supportsFrameSemantics([.sceneDepth, .smoothedSceneDepth]) {
            config.frameSemantics.formUnion([.sceneDepth, .smoothedSceneDepth])
        }
        if requested.mesh {
            if ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) {
                config.sceneReconstruction = .meshWithClassification
            } else if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
                config.sceneReconstruction = .mesh
            }
        }
        // v1 expects native 4:3 image/depth alignment. 4K 16:9 cropping would require
        // a separate calibrated protocol, not a silent stretch of either sensor.
        func fourByThree(_ format: ARConfiguration.VideoFormat) -> Bool {
            abs(format.imageResolution.width / format.imageResolution.height - 4.0 / 3.0) < 0.001
        }
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats.filter(fourByThree)
        if let highRes = ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing,
           fourByThree(highRes) {
            config.videoFormat = highRes
        } else if let best = formats.max(by: {
            let a = $0.imageResolution.width * $0.imageResolution.height
            let b = $1.imageResolution.width * $1.imageResolution.height
            return a == b ? $0.framesPerSecond < $1.framesPerSecond : a < b
        }) { config.videoFormat = best }
        stillSupported = config.videoFormat.isRecommendedForHighResolutionFrameCapturing
        // Keep the sensor color planes 8-bit and compatible with JPEG/raw YCbCr export.
        config.videoHDRAllowed = false
        let device = UIDevice.current.model
        if requested.record {
            do {
                archive = try CaptureArchive(identity: current, configuration: [
                    "device": device, "os": UIDevice.current.systemVersion,
                    "scene_depth": hasDepth,
                    "smoothed_depth": config.frameSemantics.contains(.smoothedSceneDepth),
                    "scene_reconstruction": config.sceneReconstruction.rawValue,
                    "native_video_width": config.videoFormat.imageResolution.width,
                    "native_video_height": config.videoFormat.imageResolution.height,
                    "native_video_fps": config.videoFormat.framesPerSecond,
                    "stream_frame_hz": requested.frameHz, "archive_target_hz": requested.archiveHz,
                    "lossless_color": requested.losslessColor,
                    "high_resolution_stills": stillSupported])
            } catch { publish { $0.archiveStatus = error.localizedDescription } }
        }
        if let archive { publish { $0.archiveStatus = "Recording · \(archive.directory.lastPathComponent.prefix(8))" } }
        else if !requested.record { publish { $0.archiveStatus = "Recording off" } }
        if requested.stream, let url = try? WireProtocol.endpoint(requested.endpoint),
           let hello = try? WireProtocol.json(WireProtocol.hello(current, device: device,
                sceneDepth: hasDepth, mesh: ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh))) {
            stream.connect(url: url, hello: hello)
        }
        if motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 1.0 / 100
            // We retain the latest fused motion sample with its own timestamp per archive frame.
            motion.startDeviceMotionUpdates(using: .xArbitraryZVertical)
        }
        session.delegate = self
        session.delegateQueue = captureQueue
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        let availableStill = stillSupported && archive != nil
        publish {
            $0.canTakeStill = availableStill
            $0.status = hasDepth ? "Capturing camera + LiDAR" : "Capturing RGB + pose; this device has no scene depth"
        }
    }

    private func end(reason: String) {
        identity = nil
        session.pause(); motion.stopDeviceMotionUpdates(); stream.disconnect()
        if let previous = archive {
            encodingQueue.async { previous.finish(reason: reason) }
        }
        archive = nil
        publish { $0.network = "Offline" }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard let identity else { return }
        frameID += 1
        let id = frameID
        let pose = WireProtocol.pose(identity, frameID: id, capture: frame.timestamp,
            wallMS: Int64(Date().timeIntervalSince1970 * 1000),
            transform: floats(frame.camera.transform), tracking: tracking(frame.camera.trackingState))
        let thermal = ProcessInfo.processInfo.thermalState
        if thermal == .critical {
            publish { $0.stop(reason: "Capture paused: device temperature is critical. Let it cool, then start again.") }
            end(reason: "Critical thermal state")
            return
        }
        if frame.timestamp - lastPose >= 1.0 / 30 - 0.001 {
            if let data = try? WireProtocol.json(pose) {
                stream.offer(SensorMessage(capture: frame.timestamp, data: data, isFrame: false))
            }
            lastPose = frame.timestamp
        }
        let frameHz = thermal == .serious ? 5 : options.frameHz
        let normal = tracking(frame.camera.trackingState) == "normal"
        let wantBundle = options.stream && normal && frame.sceneDepth != nil && frame.timestamp - lastBundle >= 1 / frameHz - 0.001
        let archiveInterval = thermal == .serious ? 1 : 1 / options.archiveHz
        let wantArchive = archive != nil && frame.timestamp - lastArchive >= archiveInterval - 0.001
        if wantBundle || wantArchive {
            if encoding { droppedCaptures += 1 }
            else {
                encoding = true
                if wantBundle { lastBundle = frame.timestamp }
                if wantArchive { lastArchive = frame.timestamp }
                let recording = wantArchive ? archive : nil
                let lossless = options.losslessColor
                let motionData = motionMetadata()
                encodingQueue.async { [self] in
                    encode(frame, id: id, identity: identity, pose: pose, send: wantBundle,
                           recording: recording, lossless: lossless, motion: motionData)
                }
            }
        }
        if frame.timestamp - lastUI >= 0.5 {
            lastUI = frame.timestamp
            let depth = frame.sceneDepth?.depthMap
            let depthText = depth.map { "\(CVPixelBufferGetWidth($0))×\(CVPixelBufferGetHeight($0))" } ?? "waiting/unavailable"
            let native = "\(CVPixelBufferGetWidth(frame.capturedImage))×\(CVPixelBufferGetHeight(frame.capturedImage))"
            let stats = "RGB \(native) · depth \(depthText)\nSent bundles \(stream.sentFrames) · skipped captures \(droppedCaptures) · network drops \(stream.dropped)"
            publish {
                $0.sensorStats = stats
                $0.trackingStatus = String(describing: frame.camera.trackingState)
                $0.thermalStatus = thermal == .serious ? "Serious · reduced capture rate" : thermal == .fair ? "Fair" : "Nominal"
            }
        }
    }

    private func encode(_ frame: ARFrame, id: Int, identity: CaptureIdentity,
                        pose: [String: Any], send: Bool, recording: CaptureArchive?,
                        lossless: Bool, motion: [String: Any]?) {
        defer { captureQueue.async { self.encoding = false } }
        do {
            let raw = try frame.sceneDepth.map(PackedDepth.init)
            if send, let raw, let confidence = raw.confidence {
                do {
                    let image = frame.capturedImage
                    let width = CVPixelBufferGetWidth(image), height = CVPixelBufferGetHeight(image)
                    guard width * 3 == height * 4 else {
                        throw SensorError.invalid("Native camera is not 4:3; cannot stream uncalibrated cropped imagery.")
                    }
                    let jpeg = try encoder.jpeg(image, width: 960, height: 720, quality: 0.6)
                    let intrinsics = try WireProtocol.scaledIntrinsics(floats(frame.camera.intrinsics),
                        sourceWidth: width, sourceHeight: height, width: 960, height: 720)
                    let bundle = try WireProtocol.bundle(pose: pose, jpeg: jpeg, intrinsics: intrinsics,
                        depth: raw.depth, confidence: confidence, depthWidth: raw.width, depthHeight: raw.height)
                    captureQueue.async {
                        guard self.identity == identity else { return }
                        self.stream.offer(SensorMessage(capture: frame.timestamp, data: bundle, isFrame: true))
                    }
                } catch {
                    captureQueue.async {
                        guard self.identity == identity else { return }
                        self.publish { $0.status = "Stream frame skipped: \(error.localizedDescription)" }
                    }
                }
            }
            if let recording {
                do {
                    var metadata = encoder.metadata(frame, pose: pose)
                    metadata["motion"] = motion
                    try recording.frame(frame, id: id, metadata: metadata, encoder: encoder, raw: raw,
                        smoothed: try frame.smoothedSceneDepth.map(PackedDepth.init), losslessColor: lossless)
                } catch {
                    recording.finish(reason: error.localizedDescription)
                    captureQueue.async {
                        guard self.identity == identity else { return }
                        self.archive = nil
                        self.publish { $0.archiveStatus = error.localizedDescription; $0.canTakeStill = false }
                    }
                }
            }
        } catch {
            captureQueue.async {
                guard self.identity == identity else { return }
                self.publish { $0.status = "Capture error: \(error.localizedDescription)" }
            }
        }
    }

    private func motionMetadata() -> [String: Any]? {
        guard let data = motion.deviceMotion else { return nil }
        let q = data.attitude.quaternion
        return ["t_motion": data.timestamp, "attitude_quaternion_xyzw": [q.x, q.y, q.z, q.w],
                "gravity_g": [data.gravity.x, data.gravity.y, data.gravity.z],
                "user_acceleration_g": [data.userAcceleration.x, data.userAcceleration.y, data.userAcceleration.z],
                "rotation_rate_rad_s": [data.rotationRate.x, data.rotationRate.y, data.rotationRate.z],
                "reference_frame": "xArbitraryZVertical; not calibrated into ARKit world"]
    }

    func takeStill() {
        captureQueue.async {
            guard let identity = self.identity, let recording = self.archive,
                  self.stillSupported else { return }
            guard !self.stillPending, !self.encoding else {
                self.publish { $0.status = "Capture is busy. Try the high-resolution photo again in a moment." }
                return
            }
            guard ProcessInfo.processInfo.thermalState != .serious else {
                self.publish { $0.status = "High-resolution photos are paused until the phone cools down." }
                return
            }
            self.stillPending = true; self.encoding = true
            self.publish { $0.status = "Taking high-resolution photo…" }
            self.session.captureHighResolutionFrame { frame, error in
                self.captureQueue.async {
                    self.stillPending = false
                    guard self.identity == identity, let frame else {
                        self.encoding = false
                        if let error { self.publish { $0.status = error.localizedDescription } }
                        return
                    }
                    let pose = WireProtocol.pose(identity, frameID: self.frameID, capture: frame.timestamp,
                        wallMS: Int64(Date().timeIntervalSince1970 * 1000),
                        transform: floats(frame.camera.transform), tracking: tracking(frame.camera.trackingState))
                    self.encodingQueue.async {
                        do {
                            // Stills carry their own calibration; no live depth is substituted.
                            try recording.still(frame, encoder: self.encoder,
                                metadata: self.encoder.metadata(frame, pose: pose))
                            self.publish { $0.status = "High-resolution photo saved" }
                        } catch { self.publish { $0.status = error.localizedDescription } }
                        self.captureQueue.async { self.encoding = false }
                    }
                }
            }
        }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        end(reason: "AR session interrupted")
        publish { $0.stop(reason: "Camera interrupted. Start a new capture when ready.") }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        end(reason: error.localizedDescription)
        publish { $0.stop(reason: error.localizedDescription) }
    }

    private func publish(_ update: @escaping (CaptureController) -> Void) {
        DispatchQueue.main.async { [weak self] in if let self { update(self) } }
    }
}
