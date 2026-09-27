"""macOS loopback check of the actual iPhone Network/Combine controller. No hardware."""
from pathlib import Path
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import sys

ROOT = Path(__file__).resolve().parents[2]
HARNESS = r'''
import Foundation
import SensorCore

@main struct Check {
    @MainActor static func until(_ condition: () -> Bool, timeout: Double = 6) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        while !condition() {
            if ProcessInfo.processInfo.systemUptime > deadline { throw NSError(domain: "Timeout", code: 1) }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
    }
    static func main() {
        Task { @MainActor in
            do {
                let rover = RoverController()
                let key = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : nil
                rover.connect(host: "127.0.0.1", port: CommandLine.arguments[1], relayKey: key)
                try await until { rover.verified }
                precondition(!rover.enabled && rover.roundTripMS != nil)
                rover.enable()
                rover.hold(.forward)
                // Stay healthy beyond the ESP's several-second heartbeat window.
                try await Task.sleep(nanoseconds: 5_000_000_000)
                precondition(rover.connected && rover.verified && rover.enabled)
                rover.release()
                try await Task.sleep(nanoseconds: 250_000_000)
                rover.stop()
                precondition(!rover.enabled)
                rover.enable()
                rover.hold(.right)
                // The fake Uno now stops replying while TCP remains connected.
                try await until { !rover.connected }
                precondition(!rover.enabled && !rover.verified)
                rover.connect(host: "127.0.0.1", port: CommandLine.arguments[1], relayKey: key)
                try await until { rover.verified }
                precondition(!rover.enabled)
                rover.disconnect()
                try await Task.sleep(nanoseconds: 300_000_000)
                print("Rover connection, held commands, explicit Stop, lost-feedback stop and disarmed reconnect passed.")
                exit(0)
            } catch { print("Rover test failed: \(error)"); exit(1) }
        }
        RunLoop.main.run()
    }
}
'''


def main():
    received = []
    heartbeat_replies = []
    forward_arrivals = []
    errors = []
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(2)
    server.settimeout(15)
    port = server.getsockname()[1]
    key = '0123456789ABCDEF' if '--relay-auth' in sys.argv else None

    def peer():
        try:
            for connection in range(2):
                client, _ = server.accept()
                with client:
                    if key:
                        client.settimeout(3)
                        auth = b''
                        while not auth.endswith(b'\n'):
                            part = client.recv(1)
                            assert part, 'Missing authentication'
                            auth += part
                            assert len(auth) < 128
                        assert json.loads(auth) == {'token': key}
                    client.settimeout(0.1)
                    last_heartbeat = 0.0
                    last_echo = time.monotonic()
                    pending = b''
                    feedback = True
                    while True:
                        now = time.monotonic()
                        if now - last_heartbeat >= 1:
                            client.sendall(b'{Heartbeat}')
                            last_heartbeat = now
                        assert now - last_echo < 3, 'ESP heartbeat echo stopped'
                        try:
                            chunk = client.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        pending += chunk
                        while b'}' in pending:
                            end = pending.index(b'}') + 1
                            packet, pending = pending[:end], pending[end:]
                            if packet == b'{Heartbeat}':
                                last_echo = time.monotonic()
                                heartbeat_replies.append(connection)
                                continue
                            command = json.loads(packet)
                            # ESP forwards commands over 9600-baud 8N1 UART.
                            # Include that serialization cost before emulating the Uno.
                            time.sleep(len(packet) * 10 / 9600)
                            received.append((connection, command))
                            if command['N'] == 2 and command['D1'] == 3:
                                forward_arrivals.append(time.monotonic())
                            if command['N'] == 2 and command['D1'] == 2:
                                feedback = False
                            if command['N'] == 22 and feedback:
                                # Fragmented replies and a wrong request ID must be tolerated.
                                client.sendall(b'{WRONG_88}{' + command['H'].encode() + b'_')
                                client.sendall(b'42}')
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as error:
            errors.append(error)

    with tempfile.TemporaryDirectory(prefix='godseye-rover-test-') as folder:
        folder = Path(folder)
        source = folder / 'Check.swift'
        source.write_text(HARNESS)
        env = dict(os.environ, DEVELOPER_DIR=os.environ.get(
            'DEVELOPER_DIR', '/Applications/Xcode.app/Contents/Developer'))
        subprocess.run(['xcrun', 'swiftc', '-emit-library', '-emit-module', '-module-name', 'SensorCore',
                        str(ROOT / 'ios/Sources/SensorCore/ElegooWire.swift'),
                        str(ROOT / 'ios/Sources/SensorCore/AutonomyWire.swift'),
                        '-emit-module-path', str(folder / 'SensorCore.swiftmodule'),
                        '-o', str(folder / 'libSensorCore.dylib')], check=True, env=env)
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-I', str(folder), '-L', str(folder),
                        '-lSensorCore', '-Xlinker', '-rpath', '-Xlinker', str(folder),
                        str(ROOT / 'ios/App/RoverBLELink.swift'),
                        str(ROOT / 'ios/App/RoverController.swift'), str(source),
                        '-o', str(folder / 'check')], check=True, env=env)
        worker = threading.Thread(target=peer, daemon=True)
        worker.start()
        try:
            subprocess.run([str(folder / 'check'), str(port)] + ([key] if key else []), check=True, timeout=15)
            worker.join(timeout=2)
            assert not worker.is_alive(), 'Peer did not close'
            assert not errors, errors
            assert heartbeat_replies.count(0) >= 5, 'Sustained heartbeat was not exercised'
            gaps = [later - earlier for earlier, later in zip(forward_arrivals, forward_arrivals[1:])]
            assert len(gaps) >= 40, 'Held refresh rate was too low'
            assert max(gaps) < 0.2, f'Movement lease expired between updates: {max(gaps):.3f}s'
            motion = [(connection, message) for connection, message in received if message['N'] == 2]
            assert motion and all(connection == 0 for connection, _ in motion)
            assert all(message['T'] == 200 and 0 < message['D2'] <= 80 for _, message in motion)
            assert all(message['N'] in (2, 22, 100) for _, message in received)
            first = [message for connection, message in received if connection == 0]
            forward = [i for i, message in enumerate(first) if message['N'] == 2 and message['D1'] == 3]
            right = [i for i, message in enumerate(first) if message['N'] == 2 and message['D1'] == 2]
            assert forward and right
            assert any(message['N'] == 100 for message in first[forward[-1] + 1:right[0]])
            print(f'Validated {len(motion)} timed packets; no indefinite commands or replay after reconnect.')
            print(f'At simulated 9600 baud, maximum forward refresh gap: {max(gaps) * 1000:.0f} ms (lease: 200 ms).')
        finally:
            server.close()


if __name__ == '__main__':
    main()
