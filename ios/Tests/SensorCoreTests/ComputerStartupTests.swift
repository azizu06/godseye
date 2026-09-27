import XCTest
@testable import SensorCore

final class ComputerStartupTests: XCTestCase {
    private let saved = UUID()

    private func next(_ state: inout ComputerStartup, running: Bool = true, ready: Bool = true,
                      connected: Bool = false, connecting: Bool = true, roverReady: Bool = false,
                      peers: [UUID] = [], preferred: UUID? = nil) -> ComputerStartup.Action? {
        state.next(captureRunning: running, captureReady: ready,
                   roverConnected: connected, roverConnecting: connecting, roverReady: roverReady,
                   peers: peers, preferred: preferred)
    }

    func testStartsCaptureScansOnlySavedRoverAndEnablesOnceAfterReadiness() {
        var state = ComputerStartup()
        state.begin()
        XCTAssertEqual(next(&state, running: false, connecting: false), .startCapture)
        XCTAssertEqual(next(&state, ready: false, connecting: false), .scanRover)
        XCTAssertNil(next(&state, peers: [UUID()], preferred: saved))
        XCTAssertEqual(next(&state, peers: [saved], preferred: saved), .selectRover(saved))
        XCTAssertNil(next(&state, peers: [saved], preferred: saved))
        XCTAssertNil(next(&state, ready: false, connected: true, roverReady: true))
        XCTAssertNil(next(&state, connected: true, roverReady: false))
        XCTAssertEqual(next(&state, connected: true, roverReady: true), .enableLaptop)
        XCTAssertFalse(state.active)
        XCTAssertNil(next(&state, connected: true, roverReady: true))
    }

    func testNoSavedRoverRequiresDashboardSelectionEvenWithOneNearby() {
        var state = ComputerStartup()
        state.begin()
        XCTAssertNil(next(&state, peers: [saved]))
        // An operator selection can complete startup without another phone tap.
        XCTAssertEqual(next(&state, connected: true, roverReady: true), .enableLaptop)
    }

    func testStopBeforeReadinessCancelsAllAutomaticActions() {
        var state = ComputerStartup()
        state.begin()
        state.stop()
        XCTAssertNil(next(&state, running: false, connecting: false))
        XCTAssertNil(next(&state, peers: [saved], preferred: saved))
        XCTAssertNil(next(&state, connected: true, roverReady: true))
    }

    func testLossAfterEnableDoesNotRetryOrRestartCapture() {
        var state = ComputerStartup()
        state.begin()
        XCTAssertEqual(next(&state, connected: true, roverReady: true), .enableLaptop)
        XCTAssertNil(next(&state, running: false, connecting: false))
        XCTAssertNil(next(&state, connected: true, roverReady: true))
        state.begin() // Only a new foreground setup or explicit operator action.
        XCTAssertEqual(next(&state, running: false, connecting: false), .startCapture)
    }

    func testFailedCaptureOrScanIsNotRetriedInALoop() {
        var state = ComputerStartup()
        state.begin()
        XCTAssertEqual(next(&state, running: false, connecting: false), .startCapture)
        XCTAssertEqual(next(&state, running: false, connecting: false), .scanRover)
        XCTAssertNil(next(&state, running: false, connecting: false))
        XCTAssertNil(next(&state, running: false, connected: true, roverReady: true))
    }
}
