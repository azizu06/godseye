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
    var losslessColor = true
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
    @Published private(set) var fullCaptureStatus = "Full sensor capture ready"
    private var sensors: PhoneSensors!
    private lazy var rich = RichUploader(queue: captureQueue)
    private var telemetryTimer: DispatchSourceTimer?
    private var telemetryRecordingBusy = false
    private var telemetryRecordingDropped = 0
    private var lastRichFrame = -Double.infinity
    private var lastGeometry = -Double.infinity
    private lazy var stream = PhoneStream(queue: captureQueue)
    private var identity: CaptureIdentity?
    private var epoch = 0
    private var frameID = 0
    private var options = CaptureOptions()
    private var archive: CaptureArchive?
    private var encoding = false
    private var stillPending = false
    private var stillSupported = false
    private var captureConfiguration: [String: Any] = [:]
    private var lastPose = -Double.infinity
    private var lastBundle = -Double.infinity
    private var lastArchive = -Double.infinity
    private var lastUI = -Double.infinity
    private var droppedCaptures = 0
    private var requestToken: UUID?

    override init() {
        super.init()
        sensors = PhoneSensors(queue: captureQueue)
        rich.onStatus = { [weak self] text in
            DispatchQueue.main.async { self?.fullCaptureStatus = text }
        }
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
        lastRichFrame = -.infinity; lastGeometry = -.infinity; telemetryRecordingDropped = 0
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
        for semantic: ARConfiguration.FrameSemantics in [.personSegmentationWithDepth, .bodyDetection] {
            let combined = config.frameSemantics.union(semantic)
            if ARWorldTrackingConfiguration.supportsFrameSemantics(combined) { config.frameSemantics = combined }
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
        captureConfiguration = ["device": device, "os": UIDevice.current.systemVersion,
            "scene_depth": hasDepth, "frame_semantics": config.frameSemantics.rawValue,
            "scene_reconstruction": config.sceneReconstruction.rawValue,
            "native_video_width": config.videoFormat.imageResolution.width,
            "native_video_height": config.videoFormat.imageResolution.height,
            "native_video_fps": config.videoFormat.framesPerSecond,
            "full_frame_target_hz": 5, "telemetry_target_hz": 5, "geometry_target_hz": 1,
            "lossless_color": requested.losslessColor, "high_resolution_stills": stillSupported,
            "clock_sync": "ARKit and Core Motion: uptime seconds; location: unix seconds; envelope t_wall_ms: encoding time"]
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
                    "sensors": sensors.capabilities,
                    "frame_semantics": config.frameSemantics.rawValue,
                    "high_resolution_stills": stillSupported])
            } catch { publish { $0.archiveStatus = error.localizedDescription } }
        }
        if let archive { publish { $0.archiveStatus = "Recording · \(archive.directory.lastPathComponent.prefix(8))" } }
        else if !requested.record { publish { $0.archiveStatus = "Recording off" } }
        if requested.stream, let url = try? WireProtocol.endpoint(requested.endpoint),
           let hello = try? WireProtocol.json(WireProtocol.hello(current, device: device,
                sceneDepth: hasDepth, mesh: ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh))) {
            stream.connect(url: url, hello: hello)
            rich.start(phoneURL: url)
        }
        sensors.start()
        let timer = DispatchSource.makeTimerSource(queue: captureQueue)
        timer.schedule(deadline: .now() + 0.2, repeating: 0.2)
        timer.setEventHandler { [weak self] in self?.sendTelemetry() }
        telemetryTimer = timer; timer.resume()
        session.delegate = self
        session.delegateQueue = captureQueue
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        let availableStill = stillSupported && (archive != nil || requested.stream)
        publish {
            $0.canTakeStill = availableStill
            $0.status = hasDepth ? "Capturing camera + LiDAR" : "Capturing RGB + pose; this device has no scene depth"
        }
    }

    private func end(reason: String) {
        identity = nil
        telemetryTimer?.cancel(); telemetryTimer = nil
        session.pause(); sensors.stop(); stream.disconnect(); rich.stop()
        if let previous = archive {
            encodingQueue.async { previous.finish(reason: reason) }
        }
        archive = nil
        publish { $0.network = "Offline"; $0.fullCaptureStatus = "Full sensor upload stopped" }
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
            sensors.recordPose(pose, timestamp: frame.timestamp)
        }
        let frameHz = thermal == .serious ? 5 : options.frameHz
        let normal = tracking(frame.camera.trackingState) == "normal"
        let wantBundle = options.stream && normal && frame.sceneDepth != nil && frame.timestamp - lastBundle >= 1 / frameHz - 0.001
        let archiveInterval = thermal == .serious ? 1 : 1 / options.archiveHz
        let wantArchive = archive != nil && frame.timestamp - lastArchive >= archiveInterval - 0.001
        let wantRich = options.stream && frame.timestamp - lastRichFrame >= (thermal == .serious ? 0.5 : 0.2)
        if wantBundle || wantArchive || wantRich {
            if encoding { droppedCaptures += 1 }
            else {
                encoding = true
                if wantBundle { lastBundle = frame.timestamp }
                if wantArchive { lastArchive = frame.timestamp }
                if wantRich { lastRichFrame = frame.timestamp }
                let wantGeometry = (wantArchive || wantRich) && frame.timestamp - lastGeometry >= 1
                if wantGeometry { lastGeometry = frame.timestamp }
                let recording = wantArchive ? archive : nil
                let geometryRecording = wantGeometry ? archive : nil
                let lossless = options.losslessColor
                let motionData = sensors.latestMotion
                encodingQueue.async { [self] in
                    encode(frame, id: id, identity: identity, pose: pose, send: wantBundle,
                           recording: recording, lossless: lossless, motion: motionData,
                           sendFull: wantRich, geometry: wantGeometry, geometryRecording: geometryRecording)
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
                        lossless: Bool, motion: [String: Any]?, sendFull: Bool, geometry: Bool,
                        geometryRecording: CaptureArchive?) {
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
            if recording != nil || sendFull {
                do {
                    var encoded = try FullFrameEncoder.frame(frame, pose: pose, encoder: encoder,
                                                            raw: raw, losslessColor: lossless)
                    encoded.metadata["motion"] = motion
                    let packet = try RichCapturePacket.encode(identity: identity, kind: "frame", frameID: id,
                        capture: frame.timestamp, metadata: encoded.metadata, sections: encoded.sections)
                    if sendFull { upload(packet, kind: "frame", identity: identity) }
                    save(packet, kind: "frame", frameID: id, recording: recording, identity: identity)
                    if geometry {
                        let mesh = try FullFrameEncoder.geometry(frame.anchors)
                        let packet = try RichCapturePacket.encode(identity: identity, kind: "geometry", frameID: id,
                            capture: frame.timestamp, metadata: mesh.metadata, sections: mesh.sections)
                        upload(packet, kind: "geometry", identity: identity)
                        save(packet, kind: "geometry", frameID: id, recording: geometryRecording, identity: identity)
                    }
                } catch {
                    captureQueue.async {
                        guard self.identity == identity else { return }
                        self.publish { $0.fullCaptureStatus = error.localizedDescription }
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

    private func upload(_ packet: Data, kind: String, identity: CaptureIdentity) {
        captureQueue.async {
            guard self.identity == identity else { return }
            self.rich.offer(packet, kind: kind)
        }
    }

    /// Encoding queue only. A full disk stops recording, while capture/upload continues.
    private func save(_ packet: Data, kind: String, frameID: Int,
                      recording: CaptureArchive?, identity: CaptureIdentity) {
        guard let recording else { return }
        do { try recording.packet(packet, kind: kind, frameID: frameID) }
        catch {
            recording.finish(reason: error.localizedDescription)
            captureQueue.async {
                guard self.identity == identity, self.archive === recording else { return }
                self.archive = nil
                self.publish { $0.archiveStatus = error.localizedDescription }
            }
        }
    }

    private func sendTelemetry() {
        guard let identity else { return }
        var metadata = sensors.drain()
        metadata["upload_dropped_packets"] = rich.dropped
        metadata["capture_skipped_frames"] = droppedCaptures
        metadata["archive_dropped_telemetry"] = telemetryRecordingDropped
        metadata["frame_semantics"] = session.configuration?.frameSemantics.rawValue
        metadata["configuration"] = captureConfiguration
        do {
            let packet = try RichCapturePacket.encode(identity: identity, kind: "telemetry", frameID: frameID,
                capture: ProcessInfo.processInfo.systemUptime, metadata: metadata)
            rich.offer(packet, kind: "telemetry")
            if let recording = archive {
                if telemetryRecordingBusy { telemetryRecordingDropped += 1 }
                else {
                    telemetryRecordingBusy = true
                    let id = frameID
                    encodingQueue.async {
                        self.save(packet, kind: "telemetry", frameID: id, recording: recording, identity: identity)
                        self.captureQueue.async { self.telemetryRecordingBusy = false }
                    }
                }
            }
        } catch { publish { $0.fullCaptureStatus = error.localizedDescription } }
    }

    func takeStill() {
        captureQueue.async {
            guard let identity = self.identity, self.stillSupported else { return }
            let recording = self.archive
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
                            let value = try FullFrameEncoder.frame(frame, pose: pose, encoder: self.encoder,
                                raw: try frame.sceneDepth.map(PackedDepth.init), losslessColor: true)
                            let packet = try RichCapturePacket.encode(identity: identity, kind: "still",
                                frameID: pose["frame_id"] as! Int, capture: frame.timestamp,
                                metadata: value.metadata, sections: value.sections)
                            self.upload(packet, kind: "still", identity: identity)
                            self.save(packet, kind: "still", frameID: pose["frame_id"] as! Int, recording: recording, identity: identity)
                            self.publish { $0.status = "High-resolution photo captured; upload queued when streaming" }
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
