import ARKit
import SensorCore

/// Used only on the encoding queue. Each session has a separate directory and budget.
final class CaptureArchive {
    let directory: URL
    private var bytesWritten = 0
    private let byteLimit = 2_000_000_000
    private var frameCount = 0
    private var lastGeometry = -Double.infinity
    private var closed = false

    init(identity: CaptureIdentity, configuration: [String: Any]) throws {
        directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Captures", isDirectory: true)
            .appendingPathComponent(identity.sessionID, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var manifest = configuration
        manifest["archive_version"] = 1
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

    func frame(_ frame: ARFrame, id: Int, metadata: [String: Any], encoder: SensorEncoder,
               raw: PackedDepth?, smoothed: PackedDepth?, losslessColor: Bool) throws {
        let folder = "frames/\(id)"
        var info = metadata
        try write(encoder.jpeg(frame.capturedImage, quality: 0.95), at: "\(folder)/rgb.jpg")
        for (name, depth) in [("raw", raw), ("smoothed", smoothed)] {
            guard let depth else { continue }
            try write(depth.depth, at: "\(folder)/\(name).depth.f32")
            if let confidence = depth.confidence {
                try write(confidence, at: "\(folder)/\(name).confidence.u8")
            }
            info["\(name)_depth"] = ["width": depth.width, "height": depth.height,
                                     "has_confidence": depth.confidence != nil]
        }
        if losslessColor { info["color_planes"] = try colorPlanes(frame.capturedImage, folder: folder) }
        // Metadata is the completion marker; a partial directory without it is not a frame.
        try write(WireProtocol.json(info), at: "\(folder)/metadata.json")
        frameCount += 1
        if frame.timestamp - lastGeometry >= 1 {
            try geometry(frame.anchors, capture: frame.timestamp, frameID: id)
            lastGeometry = frame.timestamp
        }
    }

    private func colorPlanes(_ buffer: CVPixelBuffer, folder: String) throws -> [String: Any] {
        let format = CVPixelBufferGetPixelFormatType(buffer)
        guard [kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
               kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange].contains(format),
              CVPixelBufferGetPlaneCount(buffer) == 2 else {
            throw SensorError.invalid("Lossless color requires an 8-bit bi-planar YCbCr camera format.")
        }
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
            throw SensorError.invalid("Cannot lock color buffer.")
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        var planes: [[String: Any]] = []
        for index in 0..<2 {
            guard let pointer = CVPixelBufferGetBaseAddressOfPlane(buffer, index) else {
                throw SensorError.invalid("Missing camera color plane.")
            }
            let width = CVPixelBufferGetWidthOfPlane(buffer, index)
            let height = CVPixelBufferGetHeightOfPlane(buffer, index)
            let stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, index)
            let rowBytes = width * (index == 0 ? 1 : 2)
            let data = try WireProtocol.packedRows(
                Data(bytesNoCopy: pointer, count: stride * height, deallocator: .none),
                rowBytes: rowBytes, stride: stride, rows: height)
            let filename = index == 0 ? "color.y.u8" : "color.cbcr.u8"
            try write(data, at: "\(folder)/\(filename)")
            planes.append(["file": filename, "width": width, "height": height, "row_bytes": rowBytes])
        }
        var info: [String: Any] = ["pixel_format": format, "planes": planes]
        if let attachments = CVBufferCopyAttachments(buffer, .shouldPropagate) as? [String: Any] {
            info["attachments"] = attachments.mapValues { String(describing: $0) }
        }
        return info
    }

    private func geometry(_ anchors: [ARAnchor], capture: Double, frameID: Int) throws {
        let folder = "geometry/\(frameID)"
        var index: [[String: Any]] = []
        for anchor in anchors {
            let id = anchor.identifier.uuidString
            var item: [String: Any] = ["id": id, "transform": floats(anchor.transform)]
            if let mesh = anchor as? ARMeshAnchor {
                let geometry = mesh.geometry
                item["type"] = "mesh"
                item["vertex_count"] = geometry.vertices.count
                item["face_count"] = geometry.faces.count
                item["indices_per_face"] = geometry.faces.indexCountPerPrimitive
                item["bytes_per_index"] = geometry.faces.bytesPerIndex
                try write(try packed(geometry.vertices, elementBytes: 12), at: "\(folder)/\(id).vertices.f32")
                try write(try packed(geometry.normals, elementBytes: 12), at: "\(folder)/\(id).normals.f32")
                let faces = geometry.faces
                try write(Data(bytes: faces.buffer.contents(), count: faces.count *
                    faces.indexCountPerPrimitive * faces.bytesPerIndex), at: "\(folder)/\(id).faces.bin")
                if let classification = geometry.classification {
                    try write(try packed(classification, elementBytes: 1), at: "\(folder)/\(id).classes.u8")
                    item["has_classification"] = true
                }
            } else if let plane = anchor as? ARPlaneAnchor {
                item["type"] = "plane"
                item["center"] = vector(plane.center)
                item["extent"] = ["width": plane.planeExtent.width,
                                   "height": plane.planeExtent.height,
                                   "rotation_y_rad": plane.planeExtent.rotationOnYAxis]
                item["alignment"] = plane.alignment.rawValue
                item["classification"] = String(describing: plane.classification)
                let boundary = plane.geometry
                item["boundary"] = boundary.boundaryVertices.map(vector)
            } else { continue }
            index.append(item)
        }
        // Full snapshots, not deltas: anchors absent from a later snapshot are gone.
        try write(WireProtocol.json(["t_capture": capture, "frame_id": frameID, "anchors": index]),
                  at: "\(folder)/anchors.json")
    }

    private func packed(_ source: ARGeometrySource, elementBytes: Int) throws -> Data {
        guard source.count > 0 else { return Data() }
        var data = Data(capacity: source.count * elementBytes)
        guard source.offset + (source.count - 1) * source.stride + elementBytes <= source.buffer.length else {
            throw SensorError.invalid("Mesh buffer length mismatch.")
        }
        for index in 0..<source.count {
            data.append(Data(bytes: source.buffer.contents().advanced(by: source.offset + index * source.stride),
                             count: elementBytes))
        }
        return data
    }

    func still(_ frame: ARFrame, encoder: SensorEncoder, metadata: [String: Any]) throws {
        let path = "stills/\(UUID().uuidString)"
        try write(encoder.jpeg(frame.capturedImage, quality: 1), at: "\(path).jpg")
        try write(WireProtocol.json(metadata), at: "\(path).json")
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
