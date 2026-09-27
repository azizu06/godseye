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
    var controlPriority = false
    func setControlPriority(_ enabled: Bool) { controlPriority = enabled }
    var identity = CaptureIdentity(sessionID: "relay-capture", epoch: 1)
    func controlSnapshot() -> ControlSnapshot? {
        ControlSnapshot(identity: identity, endpoint: CommandLine.arguments[1],
                        timestamp: ProcessInfo.processInfo.systemUptime, ready: ready)
    }
}
@main struct Check {
    @MainActor static func until(_ predicate: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 7
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
                precondition(capture.controlPriority, "Control must reserve upload bandwidth")
                if CommandLine.arguments.count > 3 && CommandLine.arguments[3] == "handshake-loss" {
                    try await until { !relay.enabled }
                    precondition(relay.status == "Stopped · laptop handshake timed out", relay.status)
                    precondition(!rover.autonomyEnabled)
                    precondition(!ble.packets.contains { [201, 202].contains($0["N"] as? Int ?? 0) })
                    rover.disconnect()
                    print("Silent laptop startup expired without arm or movement.")
                    exit(0)
                }
                try await until { ble.packets.contains { $0["N"] as? Int == 202 } }
                let heartbeatLoss = CommandLine.arguments.count > 3 && CommandLine.arguments[3] == "heartbeat-loss"
                if heartbeatLoss {
                    // A silent laptop eventually retires the transport.
                    try await until { !relay.enabled && !rover.autonomyEnabled }
                    precondition(relay.status == "Stopped · laptop heartbeat timed out", relay.status)
                } else {
                    // A capture gap pauses motion but keeps the requested laptop mode.
                    capture.ready = false
                    try await until { relay.status == "Paused · waiting for fresh capture and rover feedback" }
                    let paused = ble.packets.filter { $0["N"] as? Int == 202 }.count
                    try await Task.sleep(nanoseconds: 200_000_000)
                    precondition(relay.enabled && rover.autonomyEnabled && capture.controlPriority)
                    precondition(ble.packets.filter { $0["N"] as? Int == 202 }.count == paused)
                    capture.ready = true
                    try await until { ble.packets.filter { $0["N"] as? Int == 202 }.count > paused }
                    relay.disconnect()
                    try await until { !relay.enabled && !rover.autonomyEnabled }
                }
                precondition(!capture.controlPriority, "Disconnect must restore capture uploads")
                let count = ble.packets.filter { $0["N"] as? Int == 202 }.count
                try await Task.sleep(nanoseconds: 150_000_000)
                precondition(ble.packets.filter { $0["N"] as? Int == 202 }.count == count)
                precondition(ble.packets.contains { $0["N"] as? Int == 100 })
                try await Task.sleep(nanoseconds: 100_000_000)
                precondition(!relay.enabled && !rover.enabled, "Disconnected control must not resume")
                rover.disconnect()
                print("Swift relay with fake BLE: authentication, arm/Stop, capture pause and loss handling passed.")
                exit(0)
            } catch { print(error); exit(1) }
        }
        RunLoop.main.run()
    }
}
'''


async def check(handshake_delay=0., heartbeat_loss=False, handshake_loss=False, idle_delay=0.):
    completed = asyncio.Event()
    failures = []
    async def phone(ws):
        try:
            assert ws.request.headers['Authorization'] == 'Bearer ' + KEY
            assert ws.request.path == '/rover'
            if handshake_loss:
                async for raw in ws:
                    raise AssertionError('Phone sent feedback before the server was ready')
                return
            await asyncio.sleep(handshake_delay)
            stop_id = 'TESTAUTONOMYSTOP'
            await ws.send(json.dumps(dict(version=1, type='stop', id=stop_id)))
            idle_until = asyncio.get_running_loop().time() + idle_delay
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
                    if phase == 'stopping' and stop_ack and asyncio.get_running_loop().time() >= idle_until:
                        await ws.send(json.dumps(dict(version=1, type='arm', session=SESSION,
                                                     permit=message['permit'])))
                        phase = 'arming'
                    elif phase == 'arming' and arm_ack:
                        commands += 1
                        await ws.send(json.dumps(dict(version=1, type='command', session=SESSION,
                            permit=message['permit'], seq=commands, direction=3, power=40, lease_ms=1500)))
                        if heartbeat_loss:
                            phase = 'silent'
                    elif phase != 'silent' and asyncio.get_running_loop().time() >= idle_until:
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
            args=(f'ws://127.0.0.1:{port}/phone', KEY,
                  'handshake-loss' if handshake_loss else 'heartbeat-loss' if heartbeat_loss else 'capture-loss'))
        await asyncio.wait_for(completed.wait(), 2)
        if failures:
            raise failures[0]
    print(f'Relay verified: startup delay={handshake_delay}s, heartbeat loss={heartbeat_loss}, handshake loss={handshake_loss}')


if __name__ == '__main__':
    asyncio.run(check())
    asyncio.run(check(handshake_delay=.8, heartbeat_loss=True))
    asyncio.run(check(idle_delay=1.2))
    asyncio.run(check(handshake_loss=True))
