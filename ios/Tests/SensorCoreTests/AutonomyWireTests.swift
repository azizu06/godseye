import XCTest
@testable import SensorCore

final class AutonomyWireTests: XCTestCase {
    let session = "0123456789ABCDEF0123456789ABCDEF"
    let permit = "FEDCBA9876543210"
    func decode(_ fields: [String: Any]) throws -> AutonomyCommand {
        try AutonomyCommand.decode(JSONSerialization.data(withJSONObject: fields))
    }
    func drive(_ changes: [String: Any] = [:]) throws -> AutonomyCommand {
        var fields: [String: Any] = ["version": 1, "type": "command", "session": session,
            "permit": permit, "seq": 1, "direction": 3, "power": 40, "lease_ms": 1500]
        fields.merge(changes) { _, new in new }
        return try decode(fields)
    }
    func arm() throws -> AutonomyCommand {
        try decode(["version": 1, "type": "arm", "session": session, "permit": permit])
    }
    func stop() throws -> AutonomyCommand {
        try decode(["version": 1, "type": "stop", "id": "0123ABCD"])
    }

    func testWireKeepsServerPermitAndEncodesOnlyBoundedFirmwareProtocol() throws {
        let packet = try drive().packet
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: packet) as? [String: Any])
        XCTAssertEqual(fields["N"] as? Int, 202)
        XCTAssertEqual(fields["C"] as? String, permit)
        XCTAssertEqual(fields["T"] as? Int, 1500)
        XCTAssertEqual(fields["H"] as? String, session)
        XCTAssertLessThan(packet.count, 192)
        XCTAssertEqual(try drive(["power": 180]).power, 180)
        XCTAssertEqual(try drive(["direction": 5, "power": 180]).direction, 5)
        XCTAssertEqual(try drive(["direction": 6, "power": 180]).direction, 6)
        let idle = try drive(["direction": 0, "power": 0])
        XCTAssertEqual(idle.type, .command)
        XCTAssertEqual(try stop().packet, try ElegooWire.stop(id: "0123ABCD"))
    }

    func testMalformedUnboundedAndArbitraryCommandsRejected() throws {
        for change: [String: Any] in [["power": 181], ["direction": 7], ["direction": 0],
            ["seq": 0], ["seq": -1], ["seq": 4294967296 as UInt64], ["seq": true],
            ["lease_ms": 200], ["permit": "fedcba9876543210"], ["session": "OLD"],
            ["raw": "N=3"], ["version": 2], ["type": "raw"]] {
            XCTAssertThrowsError(try drive(change), "\(change)")
        }
        XCTAssertThrowsError(try decode(["version": 1, "type": "stop", "id": "bad_id"]))
        XCTAssertThrowsError(try AutonomyCommand.decode(Data(repeating: 32, count: 2049)))
    }

    func testArmAcknowledgementAndSubsequentPermitRequired() throws {
        var gate = AutonomyGate()
        gate.sawPermit(permit, now: 10)
        XCTAssertFalse(gate.accept(try drive(), now: 10.01))
        XCTAssertTrue(gate.accept(try arm(), now: 10.02))
        XCTAssertFalse(gate.accept(try drive(), now: 10.03))
        XCTAssertFalse(gate.armed(String(repeating: "A", count: 32)))
        XCTAssertTrue(gate.armed(session))
        XCTAssertFalse(gate.accept(try drive(), now: 10.04))
        gate.sawPermit(permit, now: 10.05)
        XCTAssertTrue(gate.accept(try drive(), now: 10.06))
        XCTAssertFalse(gate.accept(try drive(), now: 10.07))
        XCTAssertTrue(gate.accept(try drive(["seq": 2]), now: 10.08))
        XCTAssertFalse(gate.accept(try drive(["seq": 3, "session": String(repeating: "A", count: 32)]), now: 10.09))
    }

    func testFreshnessCheckedAgainAtDispatchAndDuplicatePermitDoesNotRenew() throws {
        var gate = AutonomyGate()
        gate.sawPermit(permit, now: 20)
        let arm = try arm()
        XCTAssertTrue(gate.accept(arm, now: 20.1))
        gate.sawPermit(permit, now: 20.15)
        XCTAssertFalse(gate.fresh(arm, now: 20.501))
        XCTAssertFalse(gate.fresh(arm, now: 19.9))
    }

    func testStopAndRetirementClearSessionPermitsAndPendingArm() throws {
        var gate = AutonomyGate()
        gate.sawPermit(permit, now: 30)
        XCTAssertTrue(gate.accept(try arm(), now: 30))
        XCTAssertTrue(gate.accept(try stop(), now: 30.01))
        XCTAssertFalse(gate.armed(session))
        XCTAssertFalse(gate.accept(try arm(), now: 30.02))
        gate.sawPermit(permit, now: 30.03)
        XCTAssertTrue(gate.accept(try arm(), now: 30.03))
        XCTAssertTrue(gate.armed(session))
        gate.stop()
        XCTAssertNil(gate.armedSession)
    }

    func testExpiredMovementIsDroppedWithoutRetiringHealthySession() throws {
        var gate = AutonomyGate()
        gate.sawPermit(permit, now: 40)
        XCTAssertTrue(gate.accept(try arm(), now: 40))
        XCTAssertTrue(gate.armed(session))
        gate.sawPermit(permit, now: 40.01)
        XCTAssertTrue(gate.discardExpiredMovement(try drive(), now: 40.52))
        XCTAssertEqual(gate.armedSession, session)
        XCTAssertFalse(gate.discardExpiredMovement(try drive(), now: 40.23), "No replays")
        XCTAssertFalse(gate.discardExpiredMovement(try arm(), now: 40.23), "Never bypass arm freshness")
        let next = "AAAAAAAAAAAAAAAA"
        gate.sawPermit(next, now: 40.24)
        XCTAssertTrue(gate.accept(try drive(["seq": 2, "permit": next]), now: 40.25))
        gate.stop()
        XCTAssertFalse(gate.discardExpiredMovement(try drive(["seq": 3]), now: 40.5))
    }

    func testBLEFeedbackFragmentationKeepsManualProbeAndFirmwareAcksSeparate() {
        var decoder = ElegooReplyDecoder()
        let wire = "boot{P\(permit)}{A\(session)}{Z0123ABCD}{X}{GE12_100}{ok}"
        var replies: [ElegooReply] = []
        for byte in wire.utf8 { replies += decoder.append(Data([byte])) }
        XCTAssertEqual(replies, [.permit(permit), .armed(session), .stopAcknowledged("0123ABCD"),
                                 .retired, .response(id: "GE12", value: "100"), .stopped])
        XCTAssertEqual(decoder.append(Data("{Pbad}{Awrong}{Zbad_id}".utf8)),
                       [.response(id: "Zbad", value: "id")]) // not firmware acknowledgements
    }
}
