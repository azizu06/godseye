import Foundation

/// One foreground setup attempt. There is deliberately no arm/drive action.
/// Stop consumes the attempt; recovery cannot silently re-enable control.
public struct ComputerStartup {
    public enum Action: Equatable {
        case startCapture, scanRover, selectRover(UUID), enableLaptop
    }
    public private(set) var active = false
    private var requestedCapture = false
    private var requestedScan = false
    private var selected = false
    public init() {}
    public mutating func begin() { self = Self(); active = true }
    public mutating func stop() { active = false }

    public mutating func next(captureRunning: Bool, captureReady: Bool,
                              roverConnected: Bool, roverConnecting: Bool, roverReady: Bool,
                              peers: [UUID], preferred: UUID?) -> Action? {
        guard active else { return nil }
        if !captureRunning && !requestedCapture {
            requestedCapture = true
            return .startCapture
        }
        if !roverConnected {
            if !roverConnecting && !requestedScan {
                requestedScan = true
                return .scanRover
            }
            if !selected, let preferred, peers.contains(preferred) {
                selected = true
                return .selectRover(preferred)
            }
            return nil
        }
        if captureRunning && captureReady && roverReady {
            active = false
            return .enableLaptop
        }
        return nil
    }
}
