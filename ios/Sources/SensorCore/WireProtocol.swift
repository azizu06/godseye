import Foundation

public enum SensorError: Error, LocalizedError {
    case invalid(String)
    public var errorDescription: String? {
        switch self { case .invalid(let reason): return reason }
    }
}

public struct CaptureIdentity: Equatable {
    public let sessionID: String
    public let epoch: Int
    public init(sessionID: String = UUID().uuidString, epoch: Int) {
        self.sessionID = sessionID
        self.epoch = epoch
    }
}

public enum WireProtocol {
    public static func endpoint(_ input: String) throws -> URL {
        guard let url = URL(string: input.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["ws", "wss"].contains(url.scheme?.lowercased() ?? ""),
              let host = url.host, !host.isEmpty, url.path == "/phone",
              url.user == nil, url.password == nil, url.fragment == nil, url.query == nil else {
            throw SensorError.invalid("Enter ws://<laptop IP>:8765/phone or a wss:// endpoint.")
        }
        return url
    }

    public static func hello(_ identity: CaptureIdentity, device: String,
                             sceneDepth: Bool, mesh: Bool) -> [String: Any] {
        ["version": 1, "type": "hello", "device": device,
         "session_id": identity.sessionID, "map_epoch": identity.epoch,
         "supports_scene_depth": sceneDepth, "supports_mesh": mesh]
    }

    public static func pose(_ identity: CaptureIdentity, frameID: Int, capture: Double,
                            wallMS: Int64, transform: [Float], tracking: String) -> [String: Any] {
        ["version": 1, "type": "pose", "session_id": identity.sessionID,
         "map_epoch": identity.epoch, "frame_id": frameID, "t_capture": capture,
         "t_wall_ms": wallMS, "transform": transform, "tracking": tracking]
    }

    public static func json(_ object: [String: Any]) throws -> Data {
        guard JSONSerialization.isValidJSONObject(object) else {
            throw SensorError.invalid("Sensor metadata contains an invalid JSON value.")
        }
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    /// Scale rows of a column-major K; never rotate the transmitted sensor image.
    public static func scaledIntrinsics(_ matrix: [Float], sourceWidth: Int, sourceHeight: Int,
                                        width: Int, height: Int) throws -> [Float] {
        guard matrix.count == 9, matrix.allSatisfy({ $0.isFinite }),
              min(sourceWidth, sourceHeight, width, height) > 0 else {
            throw SensorError.invalid("Invalid image dimensions or camera intrinsics.")
        }
        let sx = Float(width) / Float(sourceWidth), sy = Float(height) / Float(sourceHeight)
        return matrix.enumerated().map { index, value in
            value * (index % 3 == 0 ? sx : index % 3 == 1 ? sy : 1)
        }
    }

    public static func bundle(pose: [String: Any], jpeg: Data, intrinsics: [Float],
                              width: Int = 960, height: Int = 720,
                              depth: Data, confidence: Data, depthWidth: Int,
                              depthHeight: Int) throws -> Data {
        guard (1...8192).contains(width), (1...8192).contains(height),
              (1...4096).contains(depthWidth), (1...4096).contains(depthHeight),
              width * depthHeight == height * depthWidth,
              depth.count == depthWidth * depthHeight * 4,
              confidence.count == depthWidth * depthHeight,
              confidence.allSatisfy({ $0 <= 2 }), !jpeg.isEmpty,
              intrinsics.count == 9, intrinsics.allSatisfy({ $0.isFinite }) else {
            throw SensorError.invalid("RGB, depth, or confidence dimensions do not align.")
        }
        var header = pose
        header["type"] = "frame"
        header["image"] = ["width": width, "height": height, "jpeg_len": jpeg.count,
                           "intrinsics": intrinsics, "orientation": "landscape_right"]
        header["depth"] = ["width": depthWidth, "height": depthHeight,
                           "format": "float32_m", "len": depth.count]
        header["confidence"] = ["width": depthWidth, "height": depthHeight,
                                "format": "uint8_0_2", "len": confidence.count]
        let metadata = try json(header)
        guard metadata.count <= 65_536,
              4 + metadata.count + jpeg.count + depth.count + confidence.count <= 8 * 1024 * 1024 else {
            throw SensorError.invalid("Frame exceeds the v1 transport limit.")
        }
        var size = UInt32(metadata.count).littleEndian
        var result = withUnsafeBytes(of: &size) { Data($0) }
        result.append(metadata)
        result.append(jpeg)
        result.append(depth)
        result.append(confidence)
        return result
    }

    /// Pixel buffers and mesh buffers can contain padding after each row/element.
    public static func packedRows(_ data: Data, rowBytes: Int, stride: Int, rows: Int) throws -> Data {
        guard rowBytes > 0, stride >= rowBytes, rows > 0,
              rows <= data.count / stride else {
            throw SensorError.invalid("Invalid sensor buffer stride.")
        }
        var result = Data(capacity: rowBytes * rows)
        for row in 0..<rows { result.append(data[(row * stride)..<(row * stride + rowBytes)]) }
        return result
    }
}
