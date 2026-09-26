import ARKit
import SensorCore

enum FullFrameEncoder {
    static func frame(_ frame: ARFrame, pose: [String: Any], encoder: SensorEncoder,
                      raw: PackedDepth?, losslessColor: Bool) throws -> (metadata: [String: Any], sections: [CaptureSection]) {
        var info = encoder.metadata(frame, pose: pose)
        var sections = [CaptureSection("rgb", format: "jpeg",
            data: try encoder.jpeg(frame.capturedImage, quality: 0.95),
            shape: [CVPixelBufferGetHeight(frame.capturedImage), CVPixelBufferGetWidth(frame.capturedImage)])]
        for (name, map) in [("raw", raw), ("smoothed", try frame.smoothedSceneDepth.map(PackedDepth.init))] {
            guard let map else { continue }
            sections.append(CaptureSection("\(name)_depth", format: "f32le", data: map.depth, shape: [map.height, map.width]))
            if let confidence = map.confidence {
                sections.append(CaptureSection("\(name)_confidence", format: "u8", data: confidence, shape: [map.height, map.width]))
            }
        }
        if let mask = frame.segmentationBuffer {
            sections.append(CaptureSection("person_mask", format: "u8",
                data: try SensorEncoder.pack(mask, bytesPerPixel: 1, format: kCVPixelFormatType_OneComponent8),
                shape: [CVPixelBufferGetHeight(mask), CVPixelBufferGetWidth(mask)]))
        }
        if let depth = frame.estimatedDepthData {
            let format = CVPixelBufferGetPixelFormatType(depth)
            if [kCVPixelFormatType_DepthFloat32, kCVPixelFormatType_OneComponent32Float].contains(format) {
                sections.append(CaptureSection("person_depth", format: "f32le",
                    data: try SensorEncoder.pack(depth, bytesPerPixel: 4, format: format),
                    shape: [CVPixelBufferGetHeight(depth), CVPixelBufferGetWidth(depth)]))
            } else {
                info["person_depth_unavailable"] = "Unsupported pixel format: \(format)"
            }
        }
        if let points = frame.rawFeaturePoints {
            var positions = Data(), ids = Data()
            for point in points.points {
                for value in [point.x, point.y, point.z] {
                    var bits = value.bitPattern.littleEndian
                    positions.append(withUnsafeBytes(of: &bits) { Data($0) })
                }
            }
            for identifier in points.identifiers {
                var value = identifier.littleEndian
                ids.append(withUnsafeBytes(of: &value) { Data($0) })
            }
            info.removeValue(forKey: "feature_points")
            info["feature_point_count"] = points.points.count
            sections.append(CaptureSection("feature_positions", format: "f32le", data: positions, shape: [points.points.count, 3]))
            sections.append(CaptureSection("feature_ids", format: "u64le", data: ids, shape: [points.points.count]))
        }
        if let body = frame.detectedBody {
            info["body_2d"] = ["joint_names": body.skeleton.definition.jointNames,
                "landmarks": body.skeleton.jointLandmarks.map { [$0.x, $0.y] },
                "tracked": (0..<body.skeleton.jointLandmarks.count).map { body.skeleton.isJointTracked($0) }]
        }
        if losslessColor { info["color"] = try color(frame.capturedImage, sections: &sections) }
        info["available_sections"] = sections.map(\.name)
        return (info, sections)
    }

    private static func color(_ buffer: CVPixelBuffer, sections: inout [CaptureSection]) throws -> [String: Any] {
        let format = CVPixelBufferGetPixelFormatType(buffer)
        guard [kCVPixelFormatType_420YpCbCr8BiPlanarFullRange, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange].contains(format),
              CVPixelBufferGetPlaneCount(buffer) == 2 else {
            return ["available": false, "pixel_format": format, "reason": "Unsupported color plane format"]
        }
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
            throw SensorError.invalid("Cannot lock full-color image.")
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        for index in 0..<2 {
            guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, index) else { continue }
            let height = CVPixelBufferGetHeightOfPlane(buffer, index), width = CVPixelBufferGetWidthOfPlane(buffer, index)
            let stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, index)
            let channels = index == 0 ? 1 : 2
            let data = try WireProtocol.packedRows(Data(bytesNoCopy: base, count: height * stride, deallocator: .none),
                rowBytes: width * channels, stride: stride, rows: height)
            sections.append(CaptureSection(index == 0 ? "color_y" : "color_cbcr", format: "u8", data: data,
                                           shape: [height, width, channels]))
        }
        var info: [String: Any] = ["available": true, "pixel_format": format,
                                   "range": format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ? "full" : "video"]
        if let attachments = CVBufferCopyAttachments(buffer, .shouldPropagate) as? [String: Any] {
            info["attachments"] = attachments.mapValues { String(describing: $0) }
        }
        return info
    }

    static func geometry(_ anchors: [ARAnchor]) throws -> (metadata: [String: Any], sections: [CaptureSection]) {
        var items: [[String: Any]] = [], sections: [CaptureSection] = []
        var vertexCount = 0, faceCount = 0, meshCount = 0, planeCount = 0
        for anchor in anchors {
            let id = anchor.identifier.uuidString
            var item: [String: Any] = ["id": id, "transform": floats(anchor.transform)]
            if let mesh = anchor as? ARMeshAnchor {
                let geometry = mesh.geometry, faces = mesh.geometry.faces
                item["type"] = "mesh"
                item["vertices"] = geometry.vertices.count; item["faces"] = faces.count
                meshCount += 1; vertexCount += geometry.vertices.count; faceCount += faces.count
                sections.append(CaptureSection("\(id).vertices", format: "f32le",
                    data: try packed(geometry.vertices, bytes: 12), shape: [geometry.vertices.count, 3]))
                sections.append(CaptureSection("\(id).normals", format: "f32le",
                    data: try packed(geometry.normals, bytes: 12), shape: [geometry.normals.count, 3]))
                guard [2, 4].contains(faces.bytesPerIndex) else { throw SensorError.invalid("Unsupported mesh index width") }
                sections.append(CaptureSection("\(id).faces", format: faces.bytesPerIndex == 4 ? "u32le" : "u16le",
                    data: Data(bytes: faces.buffer.contents(), count: faces.count * faces.indexCountPerPrimitive * faces.bytesPerIndex),
                    shape: [faces.count, faces.indexCountPerPrimitive]))
                if let classes = geometry.classification {
                    sections.append(CaptureSection("\(id).classes", format: "u8", data: try packed(classes, bytes: 1), shape: [classes.count]))
                }
            } else if let plane = anchor as? ARPlaneAnchor {
                planeCount += 1
                item["type"] = "plane"; item["center"] = vector(plane.center)
                item["alignment"] = plane.alignment.rawValue
                item["classification"] = String(describing: plane.classification)
                item["extent"] = ["width": plane.planeExtent.width, "height": plane.planeExtent.height,
                                   "rotation_y_rad": plane.planeExtent.rotationOnYAxis]
                item["boundary"] = plane.geometry.boundaryVertices.map(vector)
            } else { continue }
            items.append(item)
        }
        return (["anchors": items, "mesh_count": meshCount, "plane_count": planeCount,
                 "vertex_count": vertexCount, "face_count": faceCount,
                 "snapshot": "complete; absent anchors removed; geometry is an accumulated estimate",
                 "coordinates": "anchor-local; transform maps into ARKit world"], sections)
    }
    private static func packed(_ source: ARGeometrySource, bytes: Int) throws -> Data {
        guard source.count > 0 else { return Data() }
        guard source.offset + (source.count - 1) * source.stride + bytes <= source.buffer.length else {
            throw SensorError.invalid("Mesh buffer is too short")
        }
        var data = Data(capacity: source.count * bytes)
        for i in 0..<source.count {
            data.append(Data(bytes: source.buffer.contents().advanced(by: source.offset + i * source.stride), count: bytes))
        }
        return data
    }
}
