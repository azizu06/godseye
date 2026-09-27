"""Actual iPhone relay + RoverController over loopback WebSocket and fake BLE.

No phone, radio, motor, cloud, saved key or real calibration is used.
"""
import asyncio
import json

from websockets.asyncio.server import serve
from check_rover_ble import ROOT, SOURCE, run_check

KEY = 'ONLY_A_TEST_KEY_NOT_A_REAL_ROVER_KEY'
SESSION = '0123456789ABCDEF0123456789ABCDEF'
SWIFT = SOURCE.split('@main struct Check')[0] + r'''
final class CaptureController {
    struct ControlSnapshot {
        let identity: CaptureIdentity
        let endpoint: String
        let timestamp: Double
        let ready: Bool
    }
    var running = true
    var ready = true
    var identity = CaptureIdentity(sessionID: "relay-capture", epoch: 1)
    func controlSnapshot() -> ControlSnapshot? {
        ControlSnapshot(identity: identity, endpoint: CommandLine.arguments[1],
                        timestamp: ProcessInfo.processInfo.systemUptime, ready: ready)
    }
}
@main struct Check {
    @MainActor static func until(_ predicate: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        while !predicate() {
            precondition(ProcessInfo.processInfo.systemUptime < deadline, "Timed out")
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }
    static func main() {
        Task { @MainActor in
            do {
                let rover = RoverController()
                let capture = CaptureController()
                let relay = RoverAutonomyLink()
                rover.connectBluetooth()
                let ble = RoverBLELink.latest!
                rover.selectBluetoothPeer(ble.id)
                ble.startPermits()
                try await until { rover.autonomyAvailable }
                relay.connect(rover: rover, capture: capture, key: CommandLine.arguments[2])
                precondition(relay.enabled)
                try await until { ble.packets.contains { $0["N"] as? Int == 202 } }
                let count = ble.packets.filter { $0["N"] as? Int == 202 }.count
                // Main-thread capture loss stops locally even while the server
                // keeps sending valid heartbeats and movement.
                capture.ready = false
                try await until { !relay.enabled && !rover.autonomyEnabled }
                try await Task.sleep(nanoseconds: 150_000_000)
                precondition(ble.packets.filter { $0["N"] as? Int == 202 }.count == count)
                precondition(ble.packets.contains { $0["N"] as? Int == 100 })
                capture.ready = true
                try await Task.sleep(nanoseconds: 100_000_000)
                precondition(!relay.enabled && !rover.enabled, "Capture recovery must not resume control")
                rover.disconnect()
                print("Real iPhone WebSocket relay: authenticated status, Stop/arm acknowledgement, BLE motion and local tracking-loss stop passed.")
                exit(0)
            } catch { print(error); exit(1) }
        }
        RunLoop.main.run()
    }
}
'''


async def check():
    completed = asyncio.Event()
    failures = []
    async def phone(ws):
        try:
            assert ws.request.headers['Authorization'] == 'Bearer ' + KEY
            assert ws.request.path == '/rover'
            stop_id = 'TESTAUTONOMYSTOP'
            await ws.send(json.dumps(dict(version=1, type='stop', id=stop_id)))
            phase, stop_ack, arm_ack = 'stopping', False, False
            previous, commands = 0, 0
            async for raw in ws:
                message = json.loads(raw)
                if message['type'] == 'ack':
                    stop_ack |= message['id'] == 'Z' + stop_id
                    arm_ack |= message['id'] == 'A' + SESSION
                elif message['type'] == 'status':
                    assert message['session_id'] == 'relay-capture' and message['map_epoch'] == 1
                    assert message['seq'] > previous and message['enabled'] is True
                    assert 0 <= message['uno_age_ms'] < 1500
                    previous = message['seq']
                    if phase == 'stopping' and stop_ack:
                        await ws.send(json.dumps(dict(version=1, type='arm', session=SESSION,
                                                     permit=message['permit'])))
                        phase = 'arming'
                    elif phase == 'arming' and arm_ack:
                        commands += 1
                        await ws.send(json.dumps(dict(version=1, type='command', session=SESSION,
                            permit=message['permit'], seq=commands, direction=3, power=40, lease_ms=200)))
                    else:
                        await ws.send(json.dumps(dict(version=1, type='heartbeat')))
            assert stop_ack and arm_ack and commands > 0
        except Exception as error:
            failures.append(error)
        finally:
            completed.set()

    async with serve(phone, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.to_thread(run_check, SWIFT,
            extra_sources=(str(ROOT / 'ios/App/RoverAutonomyLink.swift'),),
            args=(f'ws://127.0.0.1:{port}/phone', KEY))
        await asyncio.wait_for(completed.wait(), 2)
        if failures:
            raise failures[0]


if __name__ == '__main__':
    asyncio.run(check())
