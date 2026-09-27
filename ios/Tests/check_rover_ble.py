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
                second.onFailure?("Bluetooth lost")
                precondition(!rover.connected && !rover.enabled)
                print("BLE controller selection, fragmented feedback, explicit enable, paced writes, Stop, lost feedback and stale callbacks passed.")
                exit(0)
            } catch { print(error); exit(1) }
        }
        RunLoop.main.run()
    }
}
'''

with tempfile.TemporaryDirectory(prefix='godseye-ble-controller-') as folder:
    folder = Path(folder)
    source = folder / 'Check.swift'
    source.write_text(SOURCE)
    env = dict(os.environ, DEVELOPER_DIR=os.environ.get('DEVELOPER_DIR', '/Applications/Xcode.app/Contents/Developer'))
    subprocess.run(['xcrun', 'swiftc', '-emit-library', '-emit-module', '-module-name', 'SensorCore',
                    str(ROOT / 'ios/Sources/SensorCore/ElegooWire.swift'),
                    '-emit-module-path', str(folder / 'SensorCore.swiftmodule'),
                    '-o', str(folder / 'libSensorCore.dylib')], check=True, env=env)
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-I', str(folder), '-L', str(folder),
                    '-lSensorCore', '-Xlinker', '-rpath', '-Xlinker', str(folder),
                    str(ROOT / 'ios/App/RoverController.swift'), str(source),
                    '-o', str(folder / 'check')], check=True, env=env)
    subprocess.run([str(folder / 'check')], check=True, timeout=15)
