import ARKit
import CoreImage
import SensorCore

func floats(_ matrix: simd_float4x4) -> [Float] {
    (0..<4).flatMap { column in (0..<4).map { matrix[column][$0] } }
}
func floats(_ matrix: simd_float3x3) -> [Float] {
    (0..<3).flatMap { column in (0..<3).map { matrix[column][$0] } }
}
func vector(_ value: SIMD3<Float>) -> [Float] { [value.x, value.y, value.z] }

func tracking(_ state: ARCamera.TrackingState) -> String {
    switch state {
    case .normal: return "normal"
    case .limited: return "limited"
    case .notAvailable: return "not_available"
    }
}

struct PackedDepth {
    let width: Int
    let height: Int
    let depth: Data
    let confidence: Data?

    init(_ value: ARDepthData) throws {
        width = CVPixelBufferGetWidth(value.depthMap)
        height = CVPixelBufferGetHeight(value.depthMap)
        depth = try SensorEncoder.pack(value.depthMap, bytesPerPixel: 4,
                                       format: kCVPixelFormatType_DepthFloat32)
        if let map = value.confidenceMap {
            guard CVPixelBufferGetWidth(map) == width, CVPixelBufferGetHeight(map) == height else {
                throw SensorError.invalid("Depth and confidence dimensions differ.")
            }
            confidence = try SensorEncoder.pack(map, bytesPerPixel: 1,
                                                format: kCVPixelFormatType_OneComponent8)
        } else { confidence = nil }
    }
}

final class SensorEncoder {
    private let context = CIContext(options: [.cacheIntermediates: false])
    private let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

    static func pack(_ buffer: CVPixelBuffer, bytesPerPixel: Int, format: OSType) throws -> Data {
        guard CVPixelBufferGetPixelFormatType(buffer) == format,
              !CVPixelBufferIsPlanar(buffer) else {
            throw SensorError.invalid("Unexpected depth/confidence pixel format.")
        }
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
            throw SensorError.invalid("Cannot lock sensor buffer.")
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else {
            throw SensorError.invalid("Sensor buffer has no storage.")
        }
        let rows = CVPixelBufferGetHeight(buffer), stride = CVPixelBufferGetBytesPerRow(buffer)
        let source = Data(bytesNoCopy: base, count: rows * stride, deallocator: .none)
        // iOS runs on little-endian hardware. Remove padding, preserving float bits/NaNs.
        return try WireProtocol.packedRows(source,
            rowBytes: CVPixelBufferGetWidth(buffer) * bytesPerPixel, stride: stride, rows: rows)
    }

    func jpeg(_ buffer: CVPixelBuffer, width: Int? = nil, height: Int? = nil,
              quality: Double) throws -> Data {
        var image = CIImage(cvPixelBuffer: buffer)
        if let width, let height {
            image = image.transformed(by: CGAffineTransform(
                scaleX: CGFloat(width) / image.extent.width,
                y: CGFloat(height) / image.extent.height))
        }
        guard let result = context.jpegRepresentation(of: image, colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: quality]) else {
            throw SensorError.invalid("JPEG encoding failed.")
        }
        return result
    }

    func metadata(_ frame: ARFrame, pose: [String: Any]) -> [String: Any] {
        var data = pose
        data["type"] = "capture"
        data["native_image"] = ["width": CVPixelBufferGetWidth(frame.capturedImage),
                                "height": CVPixelBufferGetHeight(frame.capturedImage),
                                "intrinsics": floats(frame.camera.intrinsics),
                                "orientation": "landscape_right"]
        data["tracking_detail"] = String(describing: frame.camera.trackingState)
        data["world_mapping_status"] = frame.worldMappingStatus.rawValue
        data["exposure_duration_s"] = frame.camera.exposureDuration
        data["exposure_offset_ev"] = frame.camera.exposureOffset
        data["euler_angles_rad"] = vector(frame.camera.eulerAngles)
        if let light = frame.lightEstimate {
            data["light"] = ["ambient_intensity_lumens": light.ambientIntensity,
                             "ambient_color_temperature_kelvin": light.ambientColorTemperature]
        }
        if let points = frame.rawFeaturePoints {
            data["feature_points"] = ["positions": points.points.map(vector),
                                      "ids": points.identifiers.map { String($0) }]
        }
        // EXIF can contain Foundation values that JSON does not support; preserve those
        // as descriptions without risking failure of the rest of the frame metadata.
        data["exif"] = frame.exifData.mapValues { value -> Any in
            if JSONSerialization.isValidJSONObject(["v": value]) { return value }
            return String(describing: value)
        }
        return data
    }
}
