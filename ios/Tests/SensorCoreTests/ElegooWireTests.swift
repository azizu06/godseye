import XCTest
@testable import SensorCore

final class ElegooWireTests: XCTestCase {
    func testMotionAlwaysUsesBoundedTimedCommand() throws {
        for direction in ElegooDirection.allCases {
            let packet = try ElegooWire.drive(id: "GE01", direction: direction, power: 60)
            let json = try XCTUnwrap(JSONSerialization.jsonObject(with: packet) as? [String: Any])
            XCTAssertEqual(json["N"] as? Int, 2)
            XCTAssertEqual(json["D1"] as? Int, direction.rawValue)
            XCTAssertEqual(json["T"] as? Int, 200)
            XCTAssertLessThan(packet.count, 128)
        }
        for power in [-1, 0, 81, 255] {
            XCTAssertThrowsError(try ElegooWire.drive(id: "GE01", direction: .forward, power: power))
        }
        XCTAssertThrowsError(try ElegooWire.stop(id: "bad_id}"))
    }

    func testProbeAndStopCannotSelectMotion() throws {
        let probe = try JSONSerialization.jsonObject(with: ElegooWire.feedbackQuery(id: "GE02")) as! [String: Any]
        let stop = try JSONSerialization.jsonObject(with: ElegooWire.stop(id: "GE03")) as! [String: Any]
        XCTAssertEqual(probe["N"] as? Int, 22)
        XCTAssertEqual(probe["D1"] as? Int, 1)
        XCTAssertEqual(stop["N"] as? Int, 100)
        XCTAssertNil(stop["D2"])
    }

    func testDecoderHandlesSplitCoalescedAndNoisySerialData() {
        var decoder = ElegooReplyDecoder()
        XCTAssertEqual(decoder.append(Data("MPU6050_chip_id: 52\r\n{GE".utf8)), [])
        XCTAssertEqual(decoder.append(Data("01_42}{Heartbeat}{ok}{GE02_ok}".utf8)),
                       [.response(id: "GE01", value: "42"), .heartbeat, .stopped,
                        .response(id: "GE02", value: "ok")])
    }

    func testDecoderRecoversAfterOversizeOrAbandonedMessage() {
        var decoder = ElegooReplyDecoder()
        XCTAssertEqual(decoder.append(Data(("{" + String(repeating: "x", count: 10000) + "}").utf8)), [])
        XCTAssertEqual(decoder.append(Data("{broken{GE03_0}".utf8)), [.response(id: "GE03", value: "0")])
        XCTAssertEqual(decoder.append(Data("{_bad}{missing}{GE04_}".utf8)), [])
    }
}
