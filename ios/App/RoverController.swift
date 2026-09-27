import Foundation
import Combine
import Network
import SensorCore

/// Sole BLE owner for manual control or the explicitly enabled laptop relay.
/// PWM conversion belongs to the laptop's measured actuation profile.
@MainActor
final class RoverController: ObservableObject {
    @Published private(set) var status = "Rover disconnected" {
        didSet { NSLog("Rover: %@", status) }
    }
    @Published private(set) var connected = false
    @Published private(set) var verified = false
    @Published private(set) var enabled = false
    @Published private(set) var roundTripMS: Int?
    @Published var power = 60
    @Published private(set) var bluetoothPeers: [RoverBLEPeer] = []
    @Published private(set) var connecting = false
    @Published private(set) var autonomyEnabled = false
    private(set) var selectedBluetoothPeer: UUID?
    var onOperatorStop: (() -> Void)?
    var onAutonomyReply: ((ElegooReply) -> Void)?
    var onAutonomyLoss: (() -> Void)?
    private var autonomyGate = AutonomyGate()
    private var autonomyPending: AutonomyCommand?
    private var lastPermit = -Double.infinity
    var unoAgeMS: Double { max(0, (now - lastReply) * 1000) }
    var autonomyAvailable: Bool {
        bluetooth != nil && connected && verified && unoAgeMS < 1500 && now - lastPermit < 0.2
    }

    private var connection: NWConnection?
    private var bluetooth: RoverBLELink?
    private var timer: Timer?
    private var generation = 0
    private var decoder = ElegooReplyDecoder()
    private var held: ElegooDirection?
    private var lastMotion = -Double.infinity
    private static let motionInterval = 0.08
    private var busy = false
    private var busySince = 0.0
    private var stopPending = false
    private var heartbeatPending = false
    private var probe: (id: String, sent: Double)?
    private var lastProbe = -Double.infinity
    private var lastReply = -Double.infinity
    private var prefix = ""
    private var sequence = 0
    private var now: Double { ProcessInfo.processInfo.systemUptime }

    func connect(host: String, port: String, relayKey: String? = nil) {
        disconnect()
        let host = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty, !host.contains("://"), !host.contains("/"),
              let number = UInt16(port), number > 0,
              let port = NWEndpoint.Port(rawValue: number) else {
            status = "Enter the rover or laptop IP address and TCP port."
            return
        }
        var authentication: Data?
        if let relayKey {
            let key = relayKey.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            guard (16...64).contains(key.utf8.count),
                  key.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) }),
                  var data = try? JSONSerialization.data(withJSONObject: ["token": key]) else {
                status = "Enter the laptop relay's pairing code."
                return
            }
            data.append(10)
            authentication = data
        }
        prepareSession()
        status = "Connecting to rover…"
        let token = generation
        let tcp = NWProtocolTCP.Options()
        // Commands must not wait for Nagle/delayed-ACK coalescing against a 200 ms lease.
        tcp.noDelay = true
        let socket = NWConnection(host: NWEndpoint.Host(host), port: port,
                                  using: NWParameters(tls: nil, tcp: tcp))
        connection = socket
        socket.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                switch state {
                case .ready:
                    self.receive(socket, generation: token)
                    if let authentication {
                        socket.send(content: authentication, completion: .contentProcessed { [weak self] error in
                            Task { @MainActor in
                                guard let self, self.generation == token else { return }
                                if let error { self.disconnect(reason: "Relay authentication send failed: \(error.localizedDescription)") }
                                else { self.linkReady(label: "Laptop USB relay") }
                            }
                        })
                    } else { self.linkReady() }
                case .failed(let error):
                    self.disconnect(reason: "Rover connection failed: \(error.localizedDescription)")
                case .waiting(let error):
                    self.disconnect(reason: "Rover unavailable: \(error.localizedDescription)")
                default: break
                }
            }
        }
        socket.start(queue: .global(qos: .userInitiated))
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            guard let self, self.generation == token, !self.verified else { return }
            self.disconnect(reason: relayKey == nil
                ? "No Uno reply. Join the car's ELEGOO Wi-Fi and check its power switch."
                : "No Uno reply. Check the laptop relay, pairing code and Upload/Cam switch.")
        }
    }

    func connectBluetooth() {
        disconnect()
        prepareSession()
        status = "Starting Bluetooth…"
        let token = generation
        let link = RoverBLELink()
        bluetooth = link
        link.onStatus = { [weak self] status in
            guard let self, self.generation == token else { return }
            self.status = status
        }
        link.onPeers = { [weak self] peers in
            guard let self, self.generation == token else { return }
            self.bluetoothPeers = peers
        }
        link.onReady = { [weak self] in
            guard let self, self.generation == token else { return }
            self.linkReady()
        }
        link.onData = { [weak self] data in
            guard let self, self.generation == token else { return }
            self.accept(data)
        }
        link.onFailure = { [weak self] reason in
            guard let self, self.generation == token else { return }
            self.disconnect(reason: reason)
        }
        link.start()
    }

    func selectBluetoothPeer(_ id: UUID) {
        guard bluetoothPeers.contains(where: { $0.id == id }) else { return }
        selectedBluetoothPeer = id
        bluetooth?.select(id)
    }

    private func prepareSession() {
        connecting = true
        prefix = "GE" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8).uppercased()
        sequence = 0
        decoder = ElegooReplyDecoder()
        busy = false
        stopPending = false
        heartbeatPending = false
        probe = nil
        lastProbe = -.infinity
        lastReply = -.infinity
        lastMotion = -.infinity
        roundTripMS = nil
        autonomyGate.stop()
        autonomyPending = nil
        lastPermit = -.infinity
    }

    private func linkReady(label: String? = nil) {
        connecting = false
        connected = true
        status = "\(label ?? (bluetooth == nil ? "Wi-Fi" : "Bluetooth")) connected · checking Uno response…"
        requestStop()
        let timer = Timer(timeInterval: 0.02, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func enable() {
        guard !autonomyEnabled else { return }
        guard verified, now - lastReply < 2.5 else { return }
        onOperatorStop?() // An explicit manual takeover cancels automatic setup.
        held = nil
        enabled = true
        status = "Manual controls enabled · hold a direction to move"
    }

    func hold(_ direction: ElegooDirection) {
        guard !autonomyEnabled, enabled, verified, !stopPending else { return }
        guard held != direction else { return }
        held = direction
        lastMotion = -.infinity
        if !busy { tick() }
    }

    func release(_ direction: ElegooDirection? = nil) {
        if let direction, direction != held { return }
        held = nil
        requestStop()
    }

    func stop() {
        endAutonomy()
        enabled = false
        release()
        if verified { status = "Stopped · enable controls to drive again" }
    }

    func operatorStop() {
        onOperatorStop?()
        stop()
    }

    func disconnect(reason: String = "Rover disconnected") {
        endAutonomy()
        generation += 1
        timer?.invalidate()
        timer = nil
        held = nil
        enabled = false
        verified = false
        connected = false
        connecting = false
        bluetoothPeers = []
        selectedBluetoothPeer = nil
        roundTripMS = nil
        let oldBluetooth = bluetooth
        bluetooth = nil
        if let oldBluetooth {
            if let stop = try? ElegooWire.stop(id: "S"), !busy {
                oldBluetooth.send(stop) { _ in oldBluetooth.close() }
            } else { oldBluetooth.close() }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 200_000_000)
                oldBluetooth.close()
            }
        }
        let old = connection
        connection = nil
        // Best-effort explicit stop, then close even if the write stalls.
        // Each motion command also has its own 200 ms timer on the Uno.
        if let old {
            let stop = try? ElegooWire.stop(id: "GESTOP")
            old.send(content: stop, completion: .contentProcessed { _ in old.cancel() })
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.2) { old.cancel() }
        }
        status = reason
    }

    private func id() -> String {
        sequence += 1
        return prefix + String(sequence, radix: 16).uppercased()
    }

    private func requestStop() {
        guard connected else { return }
        stopPending = true
        if !busy { tick() }
    }

    func enableAutonomy() -> Bool {
        guard autonomyAvailable else { return false }
        stop()
        autonomyEnabled = true
        status = "Laptop control enabled · awaiting explicit arm"
        return true
    }

    private func endAutonomy() {
        let wasEnabled = autonomyEnabled
        autonomyEnabled = false
        autonomyPending = nil
        autonomyGate.stop()
        if wasEnabled { onAutonomyLoss?() }
    }

    func acceptAutonomy(_ command: AutonomyCommand) -> Bool {
        if autonomyEnabled, connected, autonomyAvailable,
           autonomyGate.discardExpiredMovement(command, now: now) {
            autonomyPending = nil
            return true // Dropped, never sent to BLE and never renews a motor lease.
        }
        guard autonomyEnabled, connected,
              (command.type == .stop || autonomyAvailable),
              autonomyGate.accept(command, now: now) else { return false }
        // Stop supersedes every pending command, including an arm handshake.
        // Movement can replace only movement within the acknowledged session.
        autonomyPending = command
        if command.type == .stop { stopPending = false }
        if !busy { tick() }
        return true
    }

    private func tick() {
        guard connected else { return }
        if autonomyEnabled && (!verified || unoAgeMS >= 1500 || now - lastPermit >= 0.25) {
            stop()
            status = "Laptop control stopped · rover feedback expired"
        }
        if busy {
            if now - busySince > 0.5 { disconnect(reason: "Rover send stalled · controls disabled") }
            return
        }
        if let probe, now - probe.sent > 2.5 {
            disconnect(reason: "Uno response lost · controls disabled")
            return
        }
        do {
            var packet = Data()
            if stopPending {
                packet.append(try ElegooWire.stop(id: "S"))
                stopPending = false
            } else if autonomyEnabled, let command = autonomyPending {
                autonomyPending = nil
                guard command.type == .stop || autonomyGate.fresh(command, now: now) else {
                    // A superseded movement may age while a BLE write finishes.
                    // Drop it; the firmware owns the unchanged 200 ms timeout.
                    if command.type != .command { stop() }
                    return
                }
                packet.append(try command.packet)
            } else if enabled, let held, now - lastMotion >= Self.motionInterval {
                // Stock N2 has no immediate acknowledgment. Reserve unique IDs for
                // probes; shorter movement frames leave room on the 9600-baud UART.
                packet.append(try ElegooWire.drive(id: "M", direction: held, power: power))
                lastMotion = now
            }
            if heartbeatPending {
                packet.append(Data("{Heartbeat}".utf8))
                heartbeatPending = false
            }
            if probe == nil && now - lastProbe >= 1 {
                let queryID = id()
                packet.append(try ElegooWire.feedbackQuery(id: queryID))
                probe = (queryID, now)
                lastProbe = now
            }
            if !packet.isEmpty { send(packet) }
        } catch {
            disconnect(reason: "Invalid rover command · controls disabled")
        }
    }

    private func send(_ packet: Data) {
        guard connection != nil || bluetooth != nil else { return }
        busy = true
        busySince = now
        let token = generation
        if let bluetooth {
            bluetooth.send(packet) { [weak self] error in
                guard let self, self.generation == token else { return }
                self.didSend(error)
            }
            return
        }
        guard let connection else { return }
        connection.send(content: packet, completion: .contentProcessed { [weak self] error in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                self.didSend(error)
            }
        })
    }

    private func didSend(_ error: Error?) {
        busy = false
        if let error { disconnect(reason: "Rover send failed: \(error.localizedDescription)") }
        else if stopPending || autonomyPending != nil { tick() }
    }

    private func accept(_ data: Data) {
        for reply in decoder.append(data) {
            switch reply {
            case .heartbeat: heartbeatPending = true
            case .response(let id, let value):
                if let pending = probe, id == pending.id,
                   let sensor = Int(value), (0...1023).contains(sensor) {
                    roundTripMS = Int((now - pending.sent) * 1000)
                    probe = nil
                    lastReply = now
                    if !verified {
                        verified = true
                        status = "Uno responding · manual controls ready"
                    }
                }
            case .stopped: break
            case .permit(let permit):
                lastPermit = now
                autonomyGate.sawPermit(permit, now: now)
                if autonomyEnabled { onAutonomyReply?(reply) }
            case .armed(let session):
                if autonomyEnabled && autonomyGate.armed(session) { onAutonomyReply?(reply) }
            case .stopAcknowledged:
                if autonomyEnabled { onAutonomyReply?(reply) }
            case .retired:
                autonomyGate.stop()
                autonomyPending = nil
                if autonomyEnabled { onAutonomyReply?(reply) }
            }
        }
    }

    private func receive(_ socket: NWConnection, generation token: Int) {
        socket.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] data, _, complete, error in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                if let data { self.accept(data) }
                if complete || error != nil {
                    self.disconnect(reason: "Rover disconnected · controls disabled")
                } else {
                    self.receive(socket, generation: token)
                }
            }
        }
    }
}
