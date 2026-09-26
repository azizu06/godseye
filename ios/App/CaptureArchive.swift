import ARKit
import SensorCore

/// Used only on the encoding queue. Each session has a separate directory and budget.
final class CaptureArchive {
    let directory: URL
    private var bytesWritten = 0
    private let byteLimit = 2_000_000_000
    private var frameCount = 0
    private var closed = false

    init(identity: CaptureIdentity, configuration: [String: Any]) throws {
        directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Captures", isDirectory: true)
            .appendingPathComponent(identity.sessionID, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var manifest = configuration
        manifest["archive_version"] = 2
        manifest["session_id"] = identity.sessionID
        manifest["map_epoch"] = identity.epoch
        manifest["coordinates"] = "ARKit world, meters, +Y up, column-major matrices"
        manifest["depth_format"] = "little-endian float32 meters, optical-axis depth; invalid values preserved"
        manifest["confidence_format"] = "uint8: 0 low, 1 medium, 2 high"
        manifest["geometry_coordinates"] = "anchor-local vertices/normals; transform maps anchor to world"
        manifest["geometry_timing"] = "accumulated ARKit estimate at snapshot time, not an instantaneous depth scan"
        manifest["byte_limit"] = byteLimit
        try write(WireProtocol.json(manifest), at: "manifest.json")
    }

    private func write(_ data: Data, at path: String) throws {
        guard !closed else { throw SensorError.invalid("Recording is closed.") }
        guard bytesWritten + data.count <= byteLimit else {
            throw SensorError.invalid("Recording reached its 2 GB limit. Start a new session after exporting or deleting captures.")
        }
        let space = try directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        if let available = space.volumeAvailableCapacityForImportantUsage,
           available < Int64(data.count) + 500_000_000 {
            throw SensorError.invalid("Recording stopped: less than 500 MB of free storage remains.")
        }
        let target = directory.appendingPathComponent(path)
        try FileManager.default.createDirectory(at: target.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try data.write(to: target, options: .atomic)
        bytesWritten += data.count
    }

    func packet(_ data: Data, kind: String, frameID: Int) throws {
        try write(data, at: "packets/\(kind)/\(frameID)-\(UUID().uuidString).capture")
        if kind == "frame" { frameCount += 1 }
    }

    func finish(reason: String) {
        guard !closed else { return }
        // Reserve-free summary: even a full archive should retain its stop reason.
        let summary: [String: Any] = ["frames": frameCount, "bytes_written": bytesWritten,
                                      "reason": reason, "finished_at": ISO8601DateFormatter().string(from: Date())]
        if let data = try? WireProtocol.json(summary) {
            try? data.write(to: directory.appendingPathComponent("summary.json"), options: .atomic)
        }
        closed = true
    }
}
