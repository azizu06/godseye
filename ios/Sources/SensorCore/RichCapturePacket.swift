import Foundation

public struct CaptureSection {
    public let name: String
    public let format: String
    public let data: Data
    public let shape: [Int]
    public init(_ name: String, format: String, data: Data, shape: [Int] = []) {
        self.name = name; self.format = format; self.data = data; self.shape = shape
    }
}

/// Independent v2 capture envelope. The frozen /phone v1 messages remain unchanged.
public enum RichCapturePacket {
    public static let maximumBytes = 32 * 1024 * 1024
    public static func encode(identity: CaptureIdentity, kind: String, frameID: Int,
                              capture: Double, metadata: [String: Any],
                              sections: [CaptureSection] = []) throws -> Data {
        guard ["frame", "telemetry", "geometry", "still"].contains(kind),
              frameID >= 0, identity.epoch > 0, !identity.sessionID.isEmpty,
              identity.sessionID.count <= 128, capture.isFinite, capture >= 0, sections.count <= 8192 else {
            throw SensorError.invalid("Invalid full-capture identity or timestamp.")
        }
        var offset = 0
        var names = Set<String>()
        var descriptors: [[String: Any]] = []
        for section in sections {
            guard !section.name.isEmpty, section.name.count <= 100,
                  section.name.range(of: "^[a-zA-Z0-9_.-]+$", options: .regularExpression) != nil,
                  names.insert(section.name).inserted, (1...3).contains(section.shape.count),
                  section.shape.allSatisfy({ $0 >= 0 && $0 <= maximumBytes }) else {
                throw SensorError.invalid("Invalid or duplicate capture section.")
            }
            var elements = 1
            for axis in section.shape {
                let product = elements.multipliedReportingOverflow(by: axis)
                guard !product.overflow else { throw SensorError.invalid("Capture dimensions overflow.") }
                elements = product.partialValue
            }
            if section.format == "jpeg" {
                guard section.shape.count == 2, (1...64_000_000).contains(elements), !section.data.isEmpty else {
                    throw SensorError.invalid("Invalid capture JPEG dimensions.")
                }
            } else {
                guard let bytes = ["u8": 1, "u16le": 2, "u32le": 4, "u64le": 8, "f32le": 4][section.format],
                      elements <= maximumBytes / bytes, elements * bytes == section.data.count else {
                    throw SensorError.invalid("Capture shape/format does not match its data.")
                }
            }
            guard section.data.count <= maximumBytes - offset else {
                throw SensorError.invalid("Full sensor packet exceeds the 32 MiB limit.")
            }
            descriptors.append(["name": section.name, "format": section.format,
                                "offset": offset, "length": section.data.count, "shape": section.shape])
            offset += section.data.count
        }
        let header = try WireProtocol.json([
            "version": 2, "type": "capture", "kind": kind,
            "session_id": identity.sessionID, "map_epoch": identity.epoch,
            "frame_id": frameID, "t_capture": capture,
            "t_wall_ms": Int64(Date().timeIntervalSince1970 * 1000),
            "metadata": cleanJSON(metadata), "sections": descriptors])
        guard header.count <= 2 * 1024 * 1024, offset + header.count + 4 <= maximumBytes else {
            throw SensorError.invalid("Full sensor packet exceeds the 32 MiB limit.")
        }
        var length = UInt32(header.count).littleEndian
        var packet = withUnsafeBytes(of: &length) { Data($0) }
        packet.append(header)
        for section in sections { packet.append(section.data) }
        return packet
    }

    private static func cleanJSON(_ value: Any) -> Any {
        if let values = value as? [String: Any] { return values.mapValues(cleanJSON) }
        if let values = value as? [Any] { return values.map(cleanJSON) }
        if let number = value as? NSNumber, !number.doubleValue.isFinite { return NSNull() }
        return value
    }
}
