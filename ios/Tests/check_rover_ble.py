"""Actual RoverController against a fake BLE link; no radio or motors are used."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = r'''
import Foundation
import SensorCore

struct RoverBLEPeer: Identifiable { let id: UUID; let name: String }
@MainActor final class RoverBLELink {
    static var latest: RoverBLELink!
    let id = UUID()
    var onReady: (() -> Void)?
    var onData: ((Data) -> Void)?
    var onFailure: ((String) -> Void)?
    var onPeers: (([RoverBLEPeer]) -> Void)?
    var onStatus: ((String) -> Void)?
    var packets: [[String: Any]] = []
    var reply = true
    var closed = false
    var pendingWrites = 0
    var permitNumber: UInt64 = 0
    var permits = false
    var permit: String { String(format: "%016llX", permitNumber) }
    func startPermits() {
        permits = true
        Task { @MainActor in
            while self.permits && !self.closed {
                self.permitNumber += 1
                self.onData?(Data("{P\(self.permit)}".utf8))
                try? await Task.sleep(nanoseconds: 50_000_000)
            }
        }
    }
    func start() { Self.latest = self; onPeers?([RoverBLEPeer(id: id, name: "Test rover")]) }
    func select(_ id: UUID) { precondition(id == self.id); onPeers?([]); onReady?() }
    func close() { closed = true }
    func send(_ data: Data, completion: @escaping (Error?) -> Void) {
        precondition(pendingWrites == 0, "Unbounded parallel BLE writes")
        pendingWrites += 1
        for raw in String(decoding: data, as: UTF8.self).split(separator: "}") {
            let text = String(raw) + "}"
            guard let message = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else { continue }
            packets.append(message)
            if message["N"] as? Int == 201 {
                onData?(Data("{A\(message["H"] as! String)}".utf8))
            }
            if message["N"] as? Int == 100 {
                onData?(Data("{Z\(message["H"] as! String)}".utf8))
            }
            if message["N"] as? Int == 22 && reply {
                let id = message["H"] as! String
                let response = Array("{WRONG_42}{\(id)_512}".utf8)
                for offset in stride(from: 0, to: response.count, by: 7) {
                    onData?(Data(response[offset..<min(response.count, offset + 7)]))
                }
            }
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 30_000_000)
            self.pendingWrites -= 1
            completion(nil)
        }
    }
}

@main struct Check {
    @MainActor static func until(_ predicate: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        while !predicate() {
            precondition(ProcessInfo.processInfo.systemUptime < deadline, "Timed out")
            try await Task.sleep(nanoseconds: 20_000_000)
        }
    }
    static func main() {
        Task { @MainActor in
            do {
                let rover = RoverController()
                rover.connectBluetooth()
                let first = RoverBLELink.latest!
                precondition(rover.connecting && !rover.connected && !rover.enabled)
                precondition(rover.bluetoothPeers.count == 1)
                rover.selectBluetoothPeer(first.id)
                try await until { rover.verified }
                precondition(!rover.enabled && rover.roundTripMS != nil)
                rover.hold(.forward)
                try await Task.sleep(nanoseconds: 100_000_000)
                precondition(!first.packets.contains { $0["N"] as? Int == 2 })
                rover.enable(); rover.hold(.forward)
                try await Task.sleep(nanoseconds: 500_000_000)
                rover.release()
                try await Task.sleep(nanoseconds: 100_000_000)
                precondition(first.packets.last?["N"] as? Int == 100)
                let motions = first.packets.filter { $0["N"] as? Int == 2 }
                precondition(motions.count >= 4)
                precondition(motions.allSatisfy { ($0["T"] as? Int) == 200 && ($0["D2"] as? Int ?? 999) <= 80 })
                first.reply = false
                rover.hold(.right)
                try await until { !rover.connected }
                try await until { first.closed }
                precondition(!rover.enabled && !rover.verified && first.closed)
                // Delayed delegate callbacks from the previous link cannot affect the new one.
                rover.connectBluetooth()
                first.onReady?(); first.onData?(Data("{WRONG_42}".utf8)); first.onFailure?("old failure")
                precondition(!rover.connected && rover.connecting)
                let second = RoverBLELink.latest!
                rover.selectBluetoothPeer(second.id)
                try await until { rover.verified }
                precondition(!rover.enabled)
                precondition(!second.packets.contains { $0["N"] as? Int == 2 })
                // Same real controller, laptop ownership. A fake firmware sends
                // permits/acks; no CoreBluetooth, radio, or motor is touched.
                second.startPermits()
                try await until { rover.autonomyAvailable }
                precondition(rover.enableAutonomy())
                var armed = false
                rover.onAutonomyReply = { if case .armed = $0 { armed = true } }
                try await Task.sleep(nanoseconds: 100_000_000)
                @MainActor func command(_ type: String, seq: Int = 1) throws -> AutonomyCommand {
                    var fields: [String: Any] = ["version": 1, "type": type,
                        "session": "0123456789ABCDEF0123456789ABCDEF", "permit": second.permit]
                    if type == "command" {
                        fields.merge(["seq": seq, "direction": 3, "power": 40, "lease_ms": 1500]) { _, new in new }
                    }
                    return try AutonomyCommand.decode(JSONSerialization.data(withJSONObject: fields))
                }
                let premature = try command("command")
                precondition(!rover.acceptAutonomy(premature))
                let arm = try command("arm")
                precondition(rover.acceptAutonomy(arm))
                try await until { armed }
                try await Task.sleep(nanoseconds: 60_000_000)
                for seq in 1...100 {
                    let movement = try command("command", seq: seq)
                    precondition(rover.acceptAutonomy(movement))
                }
                try await Task.sleep(nanoseconds: 90_000_000)
                let autonomous = second.packets.filter { $0["N"] as? Int == 202 }
                precondition(autonomous.count <= 2 && autonomous.last?["S"] as? Int == 100,
                             "Movement must use one latest-only slot")
                rover.enable(); rover.hold(.left)
                precondition(!rover.enabled, "Manual control must not silently take over autonomy")
                let stop = try AutonomyCommand.decode(Data(#"{"version":1,"type":"stop","id":"TESTSTOP"}"#.utf8))
                precondition(rover.acceptAutonomy(stop))
                let late = try command("command", seq: 101)
                precondition(!rover.acceptAutonomy(late))
                try await Task.sleep(nanoseconds: 80_000_000)
                precondition(second.packets.contains { $0["N"] as? Int == 100 && $0["H"] as? String == "TESTSTOP" })
                second.permits = false
                try await until { !rover.autonomyEnabled }
                precondition(!rover.enabled)
                second.onFailure?("Bluetooth lost")
                precondition(!rover.connected && !rover.enabled)
                print("BLE controller manual regression, autonomous ownership, arm/permit gates, latest-only movement, priority Stop and lost-feedback checks passed.")
                exit(0)
            } catch { print(error); exit(1) }
        }
        RunLoop.main.run()
    }
}
'''

def run_check(check_source=SOURCE, *, extra_sources=(), args=()):
    with tempfile.TemporaryDirectory(prefix='godseye-ble-controller-') as folder:
        folder = Path(folder)
        source = folder / 'Check.swift'
        source.write_text(check_source)
        env = dict(os.environ, DEVELOPER_DIR=os.environ.get('DEVELOPER_DIR', '/Applications/Xcode.app/Contents/Developer'))
        subprocess.run(['xcrun', 'swiftc', '-emit-library', '-emit-module', '-module-name', 'SensorCore',
                        *map(str, (ROOT / 'ios/Sources/SensorCore').glob('*.swift')),
                        '-emit-module-path', str(folder / 'SensorCore.swiftmodule'),
                        '-o', str(folder / 'libSensorCore.dylib')], check=True, env=env)
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-I', str(folder), '-L', str(folder),
                        '-lSensorCore', '-Xlinker', '-rpath', '-Xlinker', str(folder),
                        str(ROOT / 'ios/App/RoverController.swift'), *extra_sources, str(source),
                        '-o', str(folder / 'check')], check=True, env=env)
        return subprocess.run([str(folder / 'check'), *args], check=True, timeout=20)


if __name__ == '__main__':
    run_check()
