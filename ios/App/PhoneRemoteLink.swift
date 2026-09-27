import Foundation
import Combine
import UIKit
import SensorCore

/// Setup controls for a phone that is hard to reach once mounted. This channel
/// never accepts motor power, velocity, goals or arming commands.
@MainActor
final class PhoneRemoteLink: ObservableObject {
    @Published private(set) var enabled = false
    @Published private(set) var status = "Dashboard remote disabled" {
        didSet { if oldValue != status { NSLog("Dashboard remote: %@", status) } }
    }
    private weak var capture: CaptureController?
    private weak var rover: RoverController?
    private weak var autonomy: RoverAutonomyLink?
    private var options = CaptureOptions()
    private var key = ""
    private var socket: URLSessionWebSocketTask?
    private var generation = UUID()
    private var timer: Timer?
    private var lastServer = 0.0
    private var sending = false
    private var sendingAt = 0.0
    private var sequence = 0
    private var replies: [[String: Any]] = []
    private var pendingStatus: [String: Any]?
    private var startup = ComputerStartup()
    private var maintainControl = false
    private var serverReady = false
    private var now: Double { ProcessInfo.processInfo.systemUptime }

    func connect(capture: CaptureController, rover: RoverController, autonomy: RoverAutonomyLink,
                 options: CaptureOptions, key: String, automaticSetup: Bool = false) {
        disconnect()
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (32...128).contains(key.utf8.count), key.utf8.allSatisfy({
            (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95
        }), let endpoint = try? WireProtocol.endpoint(options.endpoint),
              var url = URLComponents(url: endpoint, resolvingAgainstBaseURL: false) else {
            status = "Enter a valid laptop URL and pairing key first"
            return
        }
        url.path = "/device"; url.query = nil; url.fragment = nil
        guard let address = url.url else { return }
        self.capture = capture; self.rover = rover; self.autonomy = autonomy
        self.options = options; self.key = key
        maintainControl = automaticSetup
        if automaticSetup { startup.begin() }
        rover.onOperatorStop = { [weak self] in self?.cancelAutomaticSetup() }
        var request = URLRequest(url: address)
        request.setValue("Bearer " + key, forHTTPHeaderField: "Authorization")
        let socket = URLSession.shared.webSocketTask(with: request)
        socket.maximumMessageSize = 2048
        self.socket = socket
        enabled = true; sequence = 0; lastServer = now
        serverReady = false
        status = "Connecting dashboard remote…"
        UIApplication.shared.isIdleTimerDisabled = true
        socket.resume()
        receive(generation)
        let timer = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        tick()
    }

    func disconnect(reason: String = "Dashboard remote disabled") {
        cancelAutomaticSetup()
        enabled = false; generation = UUID()
        timer?.invalidate(); timer = nil
        socket?.cancel(with: .goingAway, reason: nil); socket = nil
        autonomy?.disconnect()
        rover?.stop()
        rover?.onOperatorStop = nil
        UIApplication.shared.isIdleTimerDisabled = capture?.running == true
        capture = nil; rover = nil; autonomy = nil; key = ""
        sending = false; replies.removeAll(); pendingStatus = nil
        serverReady = false
        status = reason
    }

    private func failed(_ reason: String) {
        guard enabled, let capture, let rover, let autonomy else { return }
        let options = self.options, key = self.key
        let resumeSetup = maintainControl
        disconnect(reason: reason)
        // A broken socket stops the current motor session. The foreground
        // computer-control preference restores the setup link; the backend
        // separately decides whether Explore is still requested.
        enabled = true
        let token = generation
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard let self, self.enabled, self.generation == token else { return }
            self.connect(capture: capture, rover: rover, autonomy: autonomy, options: options,
                         key: key, automaticSetup: resumeSetup)
        }
    }

    func cancelAutomaticSetup() { maintainControl = false; startup.stop() }

    private func advanceStartup(capture: CaptureController, rover: RoverController, autonomy: RoverAutonomyLink) {
        if rover.verified, let id = rover.selectedBluetoothPeer,
           UserDefaults.standard.string(forKey: "preferredRoverIdentifier") != id.uuidString {
            UserDefaults.standard.set(id.uuidString, forKey: "preferredRoverIdentifier")
        }
        guard serverReady else { return }
        if maintainControl && !startup.active && !autonomy.enabled { startup.begin() }
        let snapshot = capture.controlSnapshot()
        let fresh = snapshot.map { $0.ready && now >= $0.timestamp && now - $0.timestamp < 0.25 } ?? false
        let preferred = UserDefaults.standard.string(forKey: "preferredRoverIdentifier").flatMap(UUID.init(uuidString:))
        switch startup.next(captureRunning: capture.running, captureReady: fresh,
                            roverConnected: rover.connected, roverConnecting: rover.connecting,
                            roverReady: rover.autonomyAvailable, peers: rover.bluetoothPeers.map(\.id),
                            preferred: preferred) {
        case .startCapture:
            var requested = options; requested.stream = true
            capture.start(requested)
        case .scanRover: rover.connectBluetooth()
        case .selectRover(let id): rover.selectBluetoothPeer(id)
        case .enableLaptop:
            if !autonomy.enabled { autonomy.connect(rover: rover, capture: capture, key: key) }
        case nil: break
        }
    }

    private func tick() {
        guard enabled, let capture, let rover, let autonomy else { return }
        // Starting ARKit can briefly occupy the UI thread. This setup channel
        // tolerates that; the independent motion relay keeps its tight leases.
        guard now - lastServer < 2, !sending || now - sendingAt < 1.5 else {
            failed("Dashboard remote reconnecting · rover stopped")
            return
        }
        UIApplication.shared.isIdleTimerDisabled = true
        advanceStartup(capture: capture, rover: rover, autonomy: autonomy)
        sequence += 1
        pendingStatus = ["version": 1, "type": "status", "seq": sequence,
            "capture_running": capture.running, "capture_status": String(capture.status.prefix(512)),
            "tracking": String(capture.trackingStatus.prefix(256)), "network": String(capture.network.prefix(512)),
            "rover_connected": rover.connected, "rover_verified": rover.verified,
            "rover_status": String(rover.status.prefix(512)), "control_enabled": autonomy.enabled,
            "control_status": String(autonomy.status.prefix(512)),
            "peers": rover.bluetoothPeers.prefix(12).map { ["id": $0.id.uuidString, "name": String($0.name.prefix(120))] }]
        drain()
    }

    private func receive(_ token: UUID) {
        socket?.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.enabled, self.generation == token else { return }
                do {
                    let message = try result.get()
                    let data: Data
                    switch message {
                    case .data(let value): data = value
                    case .string(let value): data = Data(value.utf8)
                    @unknown default: throw AutonomyCommand.Error.invalidCommand
                    }
                    guard let fields = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                          fields["version"] as? Int == 1 else { throw AutonomyCommand.Error.invalidCommand }
                    if fields["type"] as? String == "heartbeat", fields.count == 2 {
                        self.lastServer = self.now
                        self.serverReady = true
                        self.status = "Dashboard remote connected · keep app open"
                    } else {
                        guard fields["type"] as? String == "action", let id = fields["id"] as? String,
                              id.count == 32, let action = fields["action"] as? String,
                              Set(fields.keys).isSubset(of: ["version", "type", "id", "action", "peer_id"]) else {
                            throw AutonomyCommand.Error.invalidCommand
                        }
                        self.lastServer = self.now
                        let outcome = self.perform(action, peerID: fields["peer_id"] as? String)
                        self.replies.append(["version": 1, "type": "ack", "id": id,
                                             "ok": outcome.0, "message": outcome.1])
                        guard self.replies.count <= 8 else { throw AutonomyCommand.Error.invalidCommand }
                        self.tick()
                    }
                    self.receive(token)
                } catch { self.failed("Dashboard remote reconnecting · rover stopped") }
            }
        }
    }

    private func perform(_ action: String, peerID: String?) -> (Bool, String) {
        guard let capture, let rover, let autonomy else { return (false, "Phone is not ready") }
        switch action {
        case "capture_start":
            guard !capture.running else { return (true, "Capture is already running") }
            var requested = options
            requested.stream = true
            capture.start(requested)
            return (capture.running, capture.status)
        case "capture_stop":
            cancelAutomaticSetup()
            autonomy.disconnect(); rover.stop(); capture.stop()
            return (true, "Capture stopped; rover disarmed")
        case "rover_scan":
            cancelAutomaticSetup()
            guard !rover.connected else { return (false, "Disconnect the current rover before scanning") }
            rover.connectBluetooth()
            return (true, "Scanning for Bluetooth rovers")
        case "rover_select":
            guard let peerID, let id = UUID(uuidString: peerID), rover.bluetoothPeers.contains(where: { $0.id == id }) else {
                return (false, "Select a rover from the current scan")
            }
            rover.selectBluetoothPeer(id)
            return (true, "Connecting to selected rover")
        case "rover_disconnect":
            cancelAutomaticSetup()
            autonomy.disconnect(); rover.disconnect()
            return (true, "Rover disconnected")
        case "control_enable":
            maintainControl = true
            guard !autonomy.enabled else { return (true, "Laptop control is already enabled") }
            autonomy.connect(rover: rover, capture: capture, key: key)
            return (autonomy.enabled, autonomy.status)
        case "control_disable":
            cancelAutomaticSetup()
            autonomy.disconnect(); rover.stop()
            return (true, "Laptop control disabled; rover stopped")
        case "stop":
            cancelAutomaticSetup()
            autonomy.disconnect(); rover.stop()
            return (true, "Rover stopped")
        default: return (false, "Unknown phone setup action")
        }
    }

    private func drain() {
        guard enabled, !sending, let socket else { return }
        let message: [String: Any]
        if !replies.isEmpty { message = replies.removeFirst() }
        else if let pendingStatus { message = pendingStatus; self.pendingStatus = nil }
        else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: message),
              let text = String(data: data, encoding: .utf8) else { disconnect(); return }
        sending = true; sendingAt = now
        let token = generation
        socket.send(.string(text)) { [weak self] error in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                self.sending = false
                if error == nil { self.drain() }
                else { self.failed("Dashboard remote reconnecting · rover stopped") }
            }
        }
    }
}
