import Foundation
import XCTest
@testable import SensorCore

final class SensorCoreTests: XCTestCase {
    private let identity = CaptureIdentity(sessionID: "swift-contract-fixture", epoch: 3)
    private let transform: [Float] = [0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 2, 1, -3, 1]

    func testIntrinsicsUseIndependentRowScaling() throws {
        let matrix: [Float] = [1200, 0, 0, 0, 900, 0, 960, 720, 1]
        let scaled = try WireProtocol.scaledIntrinsics(matrix, sourceWidth: 1920,
            sourceHeight: 1440, width: 960, height: 360)
        XCTAssertEqual(scaled, [600, 0, 0, 0, 225, 0, 480, 180, 1])
        XCTAssertThrowsError(try WireProtocol.scaledIntrinsics([.nan], sourceWidth: 1,
            sourceHeight: 1, width: 1, height: 1))
    }

    func testStridedDepthCopiesOnlyPixels() throws {
        let bytes = Data([0, 0, 128, 63, 99, 99, 0, 0, 0, 64, 88, 88])
        XCTAssertEqual(try WireProtocol.packedRows(bytes, rowBytes: 4, stride: 6, rows: 2),
                       Data([0, 0, 128, 63, 0, 0, 0, 64]))
        XCTAssertThrowsError(try WireProtocol.packedRows(bytes, rowBytes: 4, stride: 3, rows: 2))
        XCTAssertThrowsError(try WireProtocol.packedRows(bytes, rowBytes: 4, stride: 6, rows: 3))
    }

    func testEndpointsRequirePhoneRouteAndWebSocketScheme() throws {
        XCTAssertEqual(try WireProtocol.endpoint(" ws://192.168.1.10:8765/phone ").host, "192.168.1.10")
        XCTAssertNoThrow(try WireProtocol.endpoint("wss://example.test/phone"))
        for invalid in ["http://example.test/phone", "ws://example.test/live", "ws://example.test/phone?secret=a", "ws://user:pass@example.test/phone", ""] {
            XCTAssertThrowsError(try WireProtocol.endpoint(invalid))
        }
    }

    func testBackpressureKeepsOnlyLatestAndCaptureOrder() {
        var queue = PendingMessages()
        func message(_ time: Double, frame: Bool = false) -> SensorMessage {
            SensorMessage(capture: time, data: Data(), isFrame: frame)
        }
        queue.offer(message(1.00))
        queue.offer(message(1.03))
        queue.offer(message(1.02, frame: true))
        XCTAssertEqual(queue.next(now: 1.04)?.capture, 1.02)
        XCTAssertEqual(queue.next(now: 1.04)?.capture, 1.03)
        // Older bundles cannot replace a newer bundle, even though poses are independent.
        queue.offer(message(1.01, frame: true))
        XCTAssertNil(queue.next(now: 1.04))
        queue.offer(message(1.04))
        XCTAssertNil(queue.next(now: 1.30))
        XCTAssertEqual(queue.dropped, 3)
    }

    func testEncodedBundleSurvivesNewerPosesWithoutReorderingEitherStream() {
        var queue = PendingMessages()
        queue.offer(SensorMessage(capture: 10, data: Data(), isFrame: false))
        XCTAssertEqual(queue.next(now: 10)?.capture, 10)
        queue.offer(SensorMessage(capture: 10.05, data: Data(), isFrame: false))
        XCTAssertEqual(queue.next(now: 10.05)?.capture, 10.05)
        // RGB/depth encoded 80 ms after the same ARFrame's pose went out.
        queue.offer(SensorMessage(capture: 10, data: Data([42]), isFrame: true))
        XCTAssertEqual(queue.next(now: 10.08)?.data, Data([42]))
        queue.offer(SensorMessage(capture: 10, data: Data(), isFrame: true))
        queue.offer(SensorMessage(capture: 10.04, data: Data(), isFrame: false))
        XCTAssertNil(queue.next(now: 10.09))
        queue.offer(SensorMessage(capture: 10.01, data: Data(), isFrame: true))
        XCTAssertNil(queue.next(now: 10.3)) // Encoded too late: age bound still applies.
        XCTAssertEqual(queue.dropped, 3)
    }

    func testEqualCapturePosePrecedesBundleAndResetClearsWatermark() {
        var queue = PendingMessages()
        queue.offer(SensorMessage(capture: 10, data: Data(), isFrame: true))
        queue.offer(SensorMessage(capture: 10, data: Data(), isFrame: false))
        XCTAssertEqual(queue.next(now: 10)?.isFrame, false)
        XCTAssertEqual(queue.next(now: 10)?.isFrame, true)
        queue = PendingMessages()
        queue.offer(SensorMessage(capture: 1, data: Data(), isFrame: false))
        XCTAssertEqual(queue.next(now: 1)?.capture, 1)
    }

    func testNonfiniteMetadataFailsExplicitly() {
        XCTAssertThrowsError(try WireProtocol.json(["depth": Double.nan]))
    }

    func testFullCapturePreservesBinaryAndMarksMissingLandmarks() throws {
        let jpeg = try Data(contentsOf: Bundle.module.url(forResource: "rgb", withExtension: "jpg")!)
        let depth = Data([0, 0, 128, 63, 0, 0, 192, 127]) // 1 m and a preserved NaN.
        let packet = try RichCapturePacket.encode(identity: identity, kind: "frame", frameID: 18,
            capture: 12.7, metadata: ["landmarks": [[Double.nan, 0.25]], "motion": ["timestamp": 12.69]],
            sections: [CaptureSection("rgb", format: "jpeg", data: jpeg, shape: [3, 4]),
                       CaptureSection("raw_depth", format: "f32le", data: depth, shape: [1, 2])])
        let size = packet.prefix(4).enumerated().reduce(0) { $0 | Int($1.element) << ($1.offset * 8) }
        let header = try XCTUnwrap(JSONSerialization.jsonObject(with: packet[4..<(4 + size)]) as? [String: Any])
        XCTAssertEqual(header["version"] as? Int, 2)
        XCTAssertEqual(header["kind"] as? String, "frame")
        let metadata = try XCTUnwrap(header["metadata"] as? [String: Any])
        let landmarks = try XCTUnwrap(metadata["landmarks"] as? [[Any]])
        XCTAssertTrue(landmarks[0][0] is NSNull)
        XCTAssertEqual(packet.suffix(depth.count), depth)
        if let path = ProcessInfo.processInfo.environment["GODSEYE_WIRE_FIXTURE"] {
            try packet.write(to: URL(fileURLWithPath: path + ".capture"))
        }
    }

    func testFullCaptureRejectsIncorrectShapesFormatsAndDuplicateSections() {
        let invalid = [
            CaptureSection("raw_depth", format: "f32le", data: Data([0]), shape: [1, 1]),
            CaptureSection("../escape", format: "u8", data: Data([0]), shape: [1]),
            CaptureSection("rgb", format: "jpeg", data: Data([0]), shape: [1]),
            CaptureSection("color", format: "unknown", data: Data([0]), shape: [1]),
            CaptureSection("color", format: "u8", data: Data(), shape: [Int.max, 2])]
        for item in invalid {
            XCTAssertThrowsError(try RichCapturePacket.encode(identity: identity, kind: "frame", frameID: 1,
                capture: 1, metadata: [:], sections: [item]))
        }
        let item = CaptureSection("mask", format: "u8", data: Data([1]), shape: [1, 1])
        XCTAssertThrowsError(try RichCapturePacket.encode(identity: identity, kind: "frame", frameID: 1,
            capture: 1, metadata: [:], sections: [item, item]))
    }

    func testBundleRoundTripAndPythonFixture() throws {
        // Real 4x3 JPEG: backend validation decodes these bytes independently.
        let jpegURL = Bundle.module.url(forResource: "rgb", withExtension: "jpg")!
        let jpeg = try Data(contentsOf: jpegURL)
        let pose = WireProtocol.pose(identity, frameID: 17, capture: 12.5,
            wallMS: 1_790_400_000_123, transform: transform, tracking: "normal")
        var depth = Data()
        for value in 1...12 {
            var bits = Float(value).bitPattern.littleEndian
            depth.append(withUnsafeBytes(of: &bits) { Data($0) })
        }
        let confidence = Data([0, 1, 2, 2, 2, 2, 1, 0, 2, 2, 2, 2])
        let bundle = try WireProtocol.bundle(pose: pose, jpeg: jpeg,
            intrinsics: [3, 0, 0, 0, 3, 0, 2, 1.5, 1], width: 4, height: 3,
            depth: depth, confidence: confidence, depthWidth: 4, depthHeight: 3)
        let headerSize = bundle.prefix(4).enumerated().reduce(0) { $0 | Int($1.element) << ($1.offset * 8) }
        let header = try XCTUnwrap(JSONSerialization.jsonObject(with: bundle[4..<(4 + headerSize)]) as? [String: Any])
        XCTAssertEqual(header["type"] as? String, "frame")
        XCTAssertEqual(header["session_id"] as? String, identity.sessionID)
        XCTAssertEqual(header["t_capture"] as? Double, 12.5)
        XCTAssertEqual((header["transform"] as? [Float]), transform)
        XCTAssertEqual(bundle.suffix(60), depth + confidence)
        XCTAssertThrowsError(try WireProtocol.bundle(pose: pose, jpeg: jpeg,
            intrinsics: [3, 0, 0, 0, 3, 0, 2, 1.5, 1], width: 4, height: 3,
            depth: depth, confidence: Data(repeating: 3, count: 12), depthWidth: 4, depthHeight: 3))
        if let path = ProcessInfo.processInfo.environment["GODSEYE_WIRE_FIXTURE"] {
            try bundle.write(to: URL(fileURLWithPath: path))
            try WireProtocol.json(pose).write(to: URL(fileURLWithPath: path + ".pose.json"))
        }
    }
}
