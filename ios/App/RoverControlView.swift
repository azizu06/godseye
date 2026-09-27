import SwiftUI
import SensorCore

struct RoverControlView: View {
    @ObservedObject var rover: RoverController
    @AppStorage("roverHost") private var host = "192.168.4.1"
    @AppStorage("roverPort") private var port = "100"
    @AppStorage("roverTransport") private var transport = "wifi"
    @AppStorage("roverRelayHost") private var relayHost = ""
    @AppStorage("roverRelayPort") private var relayPort = "8768"
    @State private var relayKey = ""

    var body: some View {
        GroupBox("ELEGOO V4 rover") {
            VStack(alignment: .leading, spacing: 12) {
                Text(rover.status).font(.subheadline)
                Picker("Connection", selection: $transport) {
                    Text("Bluetooth").tag("ble")
                    Text("ELEGOO Wi-Fi").tag("wifi")
                    Text("Laptop USB").tag("usb")
                }.pickerStyle(.segmented)
                    .disabled(rover.connected || rover.connecting)
                Text(transport == "ble"
                     ? "Keep your fast Wi-Fi for mapping. Bluetooth requires God's Eye firmware on the ESP board."
                     : transport == "usb"
                     ? "Keep normal Wi-Fi. The laptop relays controls over USB to the Uno; keep the cable attached and switch Upload/Cam to Upload."
                     : "Join the car's ELEGOO Wi-Fi network, then connect here.")
                    .font(.caption).foregroundStyle(.secondary)
                if transport == "wifi" {
                HStack {
                    TextField("ESP address", text: $host)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityLabel("ESP address")
                    TextField("Port", text: $port).keyboardType(.numberPad)
                        .frame(width: 65).accessibilityLabel("ESP TCP port")
                }.textFieldStyle(.roundedBorder).disabled(rover.connected || rover.connecting)
                }
                if transport == "usb" {
                    HStack {
                        TextField("Laptop IP", text: $relayHost)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        TextField("Port", text: $relayPort).keyboardType(.numberPad).frame(width: 65)
                    }.textFieldStyle(.roundedBorder).disabled(rover.connected || rover.connecting)
                    SecureField("Relay pairing code", text: $relayKey)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                        .textFieldStyle(.roundedBorder).disabled(rover.connected || rover.connecting)
                }
                HStack {
                    Button(rover.connected ? "Disconnect" : rover.connecting ? "Cancel" : "Connect rover") {
                        if rover.connected || rover.connecting { rover.disconnect() }
                        else if transport == "ble" { rover.connectBluetooth() }
                        else if transport == "usb" { rover.connect(host: relayHost, port: relayPort, relayKey: relayKey) }
                        else { rover.connect(host: host, port: port) }
                    }.buttonStyle(.bordered)
                    if let delay = rover.roundTripMS {
                        Text("Reply: \(delay) ms").font(.caption.monospaced())
                    }
                }
                ForEach(rover.bluetoothPeers) { peer in
                    Button("Connect \(peer.name)") { rover.selectBluetoothPeer(peer.id) }
                        .buttonStyle(.bordered)
                }
                HStack {
                    Button(rover.enabled ? "Disable controls" : "Enable manual controls") {
                        if rover.enabled { rover.stop() } else { rover.enable() }
                    }.buttonStyle(.bordered).disabled(!rover.verified)
                    Button("STOP") { rover.stop() }
                        .buttonStyle(.borderedProminent).tint(.red).disabled(!rover.connected)
                }
                Text("Motor power: \(Int(Double(rover.power) / 255 * 100))%")
                    .font(.caption.monospaced())
                Slider(value: Binding(get: { Double(rover.power) }, set: { rover.power = Int($0) }),
                       in: 30...80, step: 1)
                    .accessibilityLabel("Motor power")
                VStack(spacing: 8) {
                    direction(.forward, symbol: "arrow.up", label: "Forward")
                    HStack(spacing: 8) {
                        direction(.left, symbol: "arrow.left", label: "Left")
                        direction(.backward, symbol: "arrow.down", label: "Reverse")
                        direction(.right, symbol: "arrow.right", label: "Right")
                    }
                }.frame(maxWidth: .infinity).opacity(rover.enabled ? 1 : 0.4)
                Text("Hold to drive; release to stop. Motor power is not measured speed. Direct manual control is separate from laptop navigation.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(.vertical, 4)
        }
        .onDisappear { rover.stop() }
    }

    private func direction(_ direction: ElegooDirection, symbol: String, label: String) -> some View {
        GeometryReader { geometry in
            Label(label, systemImage: symbol)
                .font(.caption.weight(.semibold))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if CGRect(origin: .zero, size: geometry.size).contains(value.location) {
                            rover.hold(direction)
                        } else { rover.release(direction) }
                    }
                    .onEnded { _ in rover.release(direction) })
                .accessibilityLabel(label)
                .accessibilityHint("Hold to move while manual controls are enabled")
        }.frame(width: 86, height: 52)
    }
}
