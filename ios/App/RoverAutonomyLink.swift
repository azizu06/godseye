import Foundation
import Combine
import SensorCore

/// A small dedicated WebSocket, independent of RGB-D upload backpressure.
/// Every failure requires another local enable. Stop never waits for Wi-Fi.
@MainActor
final class RoverAutonomyLink: ObservableObject {
    @Published private(set) var enabled = false
    @Published private(set) var status = "Laptop control disabled" {
        didSet { if oldValue != status { NSLog("Laptop control: %@", status) } }
    }
    private weak var rover: RoverController?
    private weak var capture: CaptureController?
    private var identity: CaptureIdentity?
    private var socket: URLSessionWebSocketTask?
    private var generation = UUID()
    private var timer: Timer?
    private var sequence = 0
    private var lastServer = 0.0
    private var busySince = 0.0
    private var sending = false
    private var serverReady = false
    private var acknowledgements: [[String: Any]] = []
    private var pendingStatus: [String: Any]?
    private var now: Double { ProcessInfo.processInfo.systemUptime }

    func connect(rover: RoverController, capture: CaptureController, key: String) {
        disconnect()
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (32...128).contains(key.utf8.count), key.utf8.allSatisfy({
            (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95
        }) else { status = "Enter the laptop control pairing key"; return }
        guard let snapshot = capture.controlSnapshot(), capture.running, snapshot.ready,
              now >= snapshot.timestamp, now - snapshot.timestamp < 0.25,
              let endpoint = try? WireProtocol.endpoint(snapshot.endpoint),
              var url = URLComponents(url: endpoint, resolvingAgainstBaseURL: false) else {
            status = "Start live capture with normal tracking and LiDAR depth first"
            return
        }
        guard rover.enableAutonomy() else {
            status = "Connect Bluetooth with Uno feedback and autonomy firmware first"
            return
        }
        self.rover = rover; self.capture = capture; identity = snapshot.identity
        enabled = true; sequence = 0; lastServer = now; serverReady = false
        status = "Connecting laptop control…"
        let token = generation
        rover.onAutonomyReply = { [weak self] reply in self?.feedback(reply) }
        rover.onAutonomyLoss = { [weak self] in self?.disconnect(reason: "Stopped on phone or rover link lost") }
        url.path = "/rover"; url.query = nil; url.fragment = nil
        guard let address = url.url else { disconnect(reason: "Invalid laptop URL"); return }
        var request = URLRequest(url: address)
        request.setValue("Bearer " + key, forHTTPHeaderField: "Authorization")
        let socket = URLSession.shared.webSocketTask(with: request)
        socket.maximumMessageSize = 2048
        self.socket = socket
        socket.resume()
        receive(token: token)
        let timer = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    func disconnect(reason: String = "Laptop control disabled") {
        enabled = false
        generation = UUID()
        timer?.invalidate(); timer = nil
        socket?.cancel(with: .goingAway, reason: nil); socket = nil
        rover?.onAutonomyReply = nil; rover?.onAutonomyLoss = nil
        rover?.stop()
        rover = nil; capture = nil; identity = nil
        acknowledgements.removeAll(); pendingStatus = nil; sending = false; serverReady = false
        status = reason
    }

    private func validCapture() -> Bool {
        guard let capture, capture.running, let snapshot = capture.controlSnapshot(),
              snapshot.identity == identity, snapshot.ready else { return false }
        return now >= snapshot.timestamp && now - snapshot.timestamp < 0.25
    }

    private func tick() {
        guard enabled else { return }
        guard validCapture(), rover?.autonomyAvailable == true else {
            disconnect(reason: "Stopped · capture, depth, tracking or rover feedback lost")
            return
        }
        // Connecting never authorizes movement. Wait for a server message
        // before sending feedback or applying the active transport deadlines.
        if !serverReady {
            if now - lastServer > 3 { disconnect(reason: "Stopped · laptop handshake timed out") }
        } else if sending && now - busySince > 0.2 {
            disconnect(reason: "Stopped · laptop send timed out")
        } else if now - lastServer > 0.5 {
            disconnect(reason: "Stopped · laptop heartbeat timed out")
        }
    }

    private func receive(token: UUID) {
        socket?.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.generation == token, self.enabled else { return }
                do {
                    let message = try result.get()
                    let data: Data
                    switch message {
                    case .string(let text): data = Data(text.utf8)
                    case .data(let bytes): data = bytes
                    @unknown default: throw AutonomyCommand.Error.invalidCommand
                    }
                    // Startup may have taken longer than a permit's lifetime.
                    // Await a new BLE permit instead of flushing old feedback.
                    if !self.serverReady { self.pendingStatus = nil }
                    // Server heartbeat is advisory; no movement or permit in it.
                    if let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                       value["type"] as? String == "heartbeat", value.count == 2,
                       value["version"] as? Int == 1 {
                        self.lastServer = self.now
                        if !self.serverReady { self.status = "Connected · arm from laptop when ready" }
                    } else {
                        let command = try AutonomyCommand.decode(data)
                        guard command.type == .stop || self.validCapture(),
                              self.rover?.acceptAutonomy(command) == true else {
                            throw AutonomyCommand.Error.invalidCommand
                        }
                        self.lastServer = self.now
                        self.status = command.type == .arm ? "Arming rover…" :
                            command.type == .stop ? "Stopped · arm from laptop when ready" : "Laptop controlling rover"
                    }
                    self.serverReady = true
                    self.drain()
                    self.receive(token: token)
                } catch { self.disconnect(reason: "Stopped · laptop disconnected or command rejected") }
            }
        }
    }

    private func feedback(_ reply: ElegooReply) {
        guard enabled, validCapture(), let rover, let identity else { return }
        switch reply {
        case .permit(let permit):
            guard rover.unoAgeMS < 1500 else { return }
            sequence += 1
            pendingStatus = ["version": 1, "type": "status", "seq": sequence,
                "session_id": identity.sessionID, "map_epoch": identity.epoch,
                "permit": permit, "uno_age_ms": rover.unoAgeMS, "enabled": true]
        case .armed(let session):
            pendingStatus = nil
            acknowledgements.append(["version": 1, "type": "ack", "id": "A" + session])
            status = "Armed · awaiting laptop route"
        case .stopAcknowledged(let id):
            pendingStatus = nil
            acknowledgements.append(["version": 1, "type": "ack", "id": "Z" + id])
        case .retired:
            acknowledgements.append(["version": 1, "type": "retired"])
            status = "Stopped · rover session retired"
        default: return
        }
        guard acknowledgements.count <= 8 else { disconnect(reason: "Stopped · control link congested"); return }
        drain()
    }

    private func drain() {
        guard enabled, serverReady, !sending, let socket else { return }
        let message: [String: Any]
        if !acknowledgements.isEmpty { message = acknowledgements.removeFirst() }
        else if let pendingStatus { message = pendingStatus; self.pendingStatus = nil }
        else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: message),
              let text = String(data: data, encoding: .utf8) else { disconnect(); return }
        sending = true; busySince = now
        let token = generation
        socket.send(.string(text)) { [weak self] error in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                self.sending = false
                if error != nil { self.disconnect(reason: "Stopped · laptop send failed") }
                else { self.drain() }
            }
        }
    }
}
