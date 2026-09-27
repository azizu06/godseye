import CoreBluetooth
import Foundation

struct RoverBLEPeer: Identifiable {
    let id: UUID
    let name: String
}

/// One selected BLE peripheral, one acknowledged write at a time. No auto-reconnect.
@MainActor
final class RoverBLELink: NSObject, @preconcurrency CBCentralManagerDelegate, @preconcurrency CBPeripheralDelegate {
    static let service = CBUUID(string: "9E9E0001-3A17-4D2E-9A61-5C7D581F1800")
    static let input = CBUUID(string: "9E9E0002-3A17-4D2E-9A61-5C7D581F1800")
    static let output = CBUUID(string: "9E9E0003-3A17-4D2E-9A61-5C7D581F1800")

    var onReady: (() -> Void)?
    var onData: ((Data) -> Void)?
    var onFailure: ((String) -> Void)?
    var onPeers: (([RoverBLEPeer]) -> Void)?
    var onStatus: ((String) -> Void)?
    private var central: CBCentralManager?
    private var found: [UUID: CBPeripheral] = [:]
    private var peripheral: CBPeripheral?
    private var input: CBCharacteristic?
    private var output: CBCharacteristic?
    private var pending = Data()
    private var completion: ((Error?) -> Void)?
    private var closed = false
    private var ready = false

    func start() {
        central = CBCentralManager(delegate: self, queue: .main)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 20_000_000_000)
            guard let self, !self.closed, self.peripheral == nil else { return }
            self.central?.stopScan()
            if self.found.isEmpty {
                self.onFailure?("No God's Eye rover found. Check rover power and Bluetooth firmware.")
            } else {
                self.onStatus?("Select a discovered Bluetooth rover to connect")
            }
        }
    }

    func select(_ id: UUID) {
        guard !closed, peripheral == nil, let selected = found[id],
              central?.state == .poweredOn else { return }
        central?.stopScan()
        peripheral = selected
        selected.delegate = self
        onPeers?([])
        onStatus?("Connecting to \(selected.name ?? "God's Eye rover")…")
        central?.connect(selected)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard let self, !self.closed, !self.ready else { return }
            self.onFailure?("Bluetooth connection timed out")
        }
    }

    func send(_ data: Data, completion: @escaping (Error?) -> Void) {
        guard !closed, ready, self.completion == nil, data.count <= 256 else {
            completion(NSError(domain: "RoverBLE", code: 1,
                               userInfo: [NSLocalizedDescriptionKey: "Bluetooth link not ready or busy"]))
            return
        }
        pending = data
        self.completion = completion
        writeNext()
    }

    private func writeNext() {
        guard let peripheral, let input, !closed else { return }
        if pending.isEmpty {
            let done = completion
            completion = nil
            done?(nil)
            return
        }
        let size = min(pending.count, peripheral.maximumWriteValueLength(for: .withResponse))
        guard size > 0 else { onFailure?("Bluetooth negotiated an invalid write size"); return }
        let chunk = Data(pending.prefix(size))
        pending.removeFirst(size)
        peripheral.writeValue(chunk, for: input, type: .withResponse)
    }

    func close() {
        guard !closed else { return }
        closed = true
        ready = false
        central?.stopScan()
        peripheral?.delegate = nil
        if let peripheral { central?.cancelPeripheralConnection(peripheral) }
        central?.delegate = nil
        completion = nil
        pending.removeAll()
        found.removeAll()
        onReady = nil; onData = nil; onFailure = nil; onPeers = nil; onStatus = nil
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard !closed else { return }
        switch central.state {
        case .poweredOn:
            onStatus?("Searching for God's Eye Bluetooth rover…")
            central.scanForPeripherals(withServices: [Self.service])
        case .unknown, .resetting: break
        case .unauthorized: onFailure?("Allow Bluetooth access for God's Eye in Settings")
        case .poweredOff: onFailure?("Turn on Bluetooth to connect to the rover")
        default: onFailure?("Bluetooth is unavailable on this device")
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard !closed, self.peripheral == nil else { return }
        found[peripheral.identifier] = peripheral
        onPeers?(found.values.map {
            RoverBLEPeer(id: $0.identifier, name: $0.name ?? "God's Eye rover")
        }.sorted { $0.id.uuidString < $1.id.uuidString })
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard !closed, peripheral === self.peripheral else { return }
        peripheral.discoverServices([Self.service])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral,
                        error: Error?) {
        guard !closed, peripheral === self.peripheral else { return }
        onFailure?("Bluetooth connection failed: \(error?.localizedDescription ?? "unavailable")")
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
        guard !closed, peripheral === self.peripheral else { return }
        onFailure?("Bluetooth rover disconnected · controls disabled")
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard !closed, peripheral === self.peripheral else { return }
        guard error == nil, let service = peripheral.services?.first(where: { $0.uuid == Self.service }) else {
            onFailure?("God's Eye BLE service is missing"); return
        }
        peripheral.discoverCharacteristics([Self.input, Self.output], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService,
                    error: Error?) {
        guard !closed, peripheral === self.peripheral else { return }
        input = service.characteristics?.first { $0.uuid == Self.input }
        output = service.characteristics?.first { $0.uuid == Self.output }
        guard error == nil, let input, input.properties.contains(.write),
              let output, output.properties.contains(.notify) else {
            onFailure?("Rover Bluetooth characteristics are incompatible"); return
        }
        peripheral.setNotifyValue(true, for: output)
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                    error: Error?) {
        guard !closed, peripheral === self.peripheral, characteristic === output else { return }
        guard error == nil, characteristic.isNotifying else {
            onFailure?("Bluetooth rover feedback could not be enabled"); return
        }
        if !ready { ready = true; onReady?() }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic,
                    error: Error?) {
        guard !closed, peripheral === self.peripheral, characteristic === output else { return }
        guard error == nil, let data = characteristic.value else {
            onFailure?("Bluetooth rover feedback failed"); return
        }
        onData?(data)
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic,
                    error: Error?) {
        guard !closed, peripheral === self.peripheral, characteristic === input, completion != nil else { return }
        if let error {
            let done = completion
            completion = nil
            pending.removeAll()
            done?(error)
        } else { writeNext() }
    }
}
