import Foundation

public struct SensorMessage {
    public let capture: Double
    public let data: Data
    public let isFrame: Bool
    public init(capture: Double, data: Data, isFrame: Bool) {
        self.capture = capture; self.data = data; self.isFrame = isFrame
    }
}

/// One pending pose and one pending bundle, plus the caller's single in-flight send.
/// Pose and image encoding have different latency. Order each stream independently
/// so a fresh encoded bundle can follow a newer pose without losing its own pose.
public struct PendingMessages {
    private var pose: SensorMessage?
    private var frame: SensorMessage?
    private var lastPose = -Double.infinity
    private var lastFrame = -Double.infinity
    public private(set) var dropped = 0
    public init() {}

    public mutating func offer(_ message: SensorMessage) {
        let watermark = message.isFrame ? lastFrame : lastPose
        guard message.capture.isFinite, message.capture > watermark else { dropped += 1; return }
        let previous = message.isFrame ? frame : pose
        if let previous, previous.capture > message.capture { dropped += 1; return }
        if previous != nil { dropped += 1 }
        if message.isFrame { frame = message } else { pose = message }
    }

    public mutating func next(now: Double, maximumAge: Double = 0.20) -> SensorMessage? {
        if let value = pose, now - value.capture > maximumAge { pose = nil; dropped += 1 }
        if let value = frame, now - value.capture > maximumAge { frame = nil; dropped += 1 }
        let value: SensorMessage?
        if let p = pose, let f = frame {
            // Preserve capture order. Equal-time pose precedes its bundle.
            if p.capture <= f.capture { value = p; pose = nil }
            else { value = f; frame = nil }
        } else if let p = pose { value = p; pose = nil }
        else { value = frame; frame = nil }
        if let value {
            if value.isFrame { lastFrame = value.capture }
            else { lastPose = value.capture }
        }
        return value
    }
}
