import SwiftUI
import ARKit

@main
struct GodsEyeApp: App {
    @StateObject private var capture = CaptureController()
    @StateObject private var rover = RoverController()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            CaptureView(capture: capture, rover: rover)
                .onChange(of: scenePhase) { phase in
                    if phase != .active { rover.disconnect(reason: "Rover disconnected while app inactive") }
                    if phase == .background { capture.stop(reason: "Capture stopped in background") }
                }
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: ARSession
    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session = session
        view.automaticallyUpdatesLighting = false
        view.preferredFramesPerSecond = 30
        return view
    }
    func updateUIView(_ view: ARSCNView, context: Context) {}
}

struct CaptureView: View {
    @ObservedObject var capture: CaptureController
    @ObservedObject var rover: RoverController
    @AppStorage("laptopEndpoint") private var endpoint = ""
    @State private var stream = true
    @State private var fullSensorUpload = true
    @State private var record = true
    @State private var mesh = true
    @State private var lossless = true
    @State private var rate = 30.0
    @State private var archiveRate = 2.0
    @StateObject private var autonomy = RoverAutonomyLink()
    @StateObject private var remote = PhoneRemoteLink()
    @State private var provisionedRemote = false
    // Local development provisioning from devicectl; stays in process memory.
    #if DEBUG
    @State private var autonomyKey = ProcessInfo.processInfo.environment["GODSEYE_ROVER_PAIRING_KEY"] ?? ""
    #else
    @State private var autonomyKey = ""
    #endif

    private var captureOptions: CaptureOptions {
        CaptureOptions(endpoint: endpoint, stream: stream, fullSensorUpload: fullSensorUpload,
                       record: record, mesh: mesh, losslessColor: lossless, frameHz: rate, archiveHz: archiveRate)
    }

    private func connectDashboard() {
        remote.connect(capture: capture, rover: rover, autonomy: autonomy,
                       options: captureOptions, key: autonomyKey)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    CameraPreview(session: capture.session)
                        .frame(height: 250)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .overlay(alignment: .bottomLeading) {
                            Text(capture.trackingStatus)
                                .font(.caption.monospaced()).padding(8)
                                .background(.ultraThinMaterial, in: Capsule()).padding(12)
                        }
                        .accessibilityLabel("Live rear camera preview")
                    VStack(alignment: .leading, spacing: 6) {
                        Text(capture.status).font(.headline)
                        Text(capture.capabilities)
                        Text(capture.network)
                        Text(capture.fullCaptureStatus)
                        Text(capture.archiveStatus)
                        Text("Temperature: \(capture.thermalStatus)")
                        if !capture.sensorStats.isEmpty { Text(capture.sensorStats).font(.caption.monospaced()) }
                    }.font(.subheadline)

                    RoverControlView(rover: rover)
                    GroupBox("Autonomous driving") {
                        VStack(alignment: .leading, spacing: 10) {
                            Text(autonomy.status).font(.subheadline)
                            SecureField("Laptop control pairing key", text: $autonomyKey)
                                .textInputAutocapitalization(.never).autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder).disabled(autonomy.enabled || remote.enabled)
                            Text(remote.status).font(.subheadline)
                            Button(remote.enabled ? "Disconnect dashboard remote" : "Connect dashboard remote") {
                                if remote.enabled { remote.disconnect() }
                                else { connectDashboard() }
                            }.buttonStyle(.bordered)
                            Text("Connect before mounting, then use the dashboard to start capture, connect Bluetooth, and enable laptop control. Keep this app open; the screen stays awake while paired.")
                                .font(.caption).foregroundStyle(.secondary)
                            Button(autonomy.enabled ? "Disable laptop control" : "Enable laptop control") {
                                if autonomy.enabled { autonomy.disconnect() }
                                else { autonomy.connect(rover: rover, capture: capture, key: autonomyKey) }
                            }.buttonStyle(.bordered)
                                .disabled(!autonomy.enabled && (!capture.running || !rover.verified))
                            Text("Uses this capture's laptop address and Bluetooth rover. Enabling allows a paired laptop to arm; motion still requires measured calibration and a ready map. Stop on the phone always takes control.")
                                .font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }

                    GroupBox("Capture settings") {
                        VStack(alignment: .leading, spacing: 12) {
                            Toggle("Stream to laptop", isOn: $stream)
                            if stream {
                                Text("Streams pose and RGB-depth bundles for the live map and drive health.")
                                    .font(.caption).foregroundStyle(.secondary)
                                Toggle("Upload full sensor data", isOn: $fullSensorUpload)
                                Text(fullSensorUpload
                                     ? "Also uploads native RGB, lossless sensor data, scene geometry, and motion/location telemetry. The laptop records received full-capture packets. Turn off on busy Wi-Fi to keep the live stream fresh."
                                     : "Only the live stream is sent. Full sensor data stays on the phone if recording is on.")
                                    .font(.caption).foregroundStyle(.secondary)
                                TextField("ws://<laptop IP>:8765/phone", text: $endpoint)
                                    .textContentType(.URL).keyboardType(.URL)
                                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                                    .textFieldStyle(.roundedBorder)
                                Picker("Sensor bundles", selection: $rate) {
                                    Text("5 / sec").tag(5.0)
                                    Text("10 / sec").tag(10.0)
                                    Text("30 / sec").tag(30.0)
                                }.pickerStyle(.segmented)
                            }
                            Toggle("Record full sensor data", isOn: $record)
                            Toggle("Reconstruct classified mesh", isOn: $mesh)
                            Toggle("Keep lossless camera planes", isOn: $lossless)
                            if record {
                                Picker("Recorded frames per second", selection: $archiveRate) {
                                    Text("2 / sec").tag(2.0)
                                    Text("5 / sec").tag(5.0)
                                    Text("10 / sec").tag(10.0)
                                    Text("30 / sec").tag(30.0)
                                }.pickerStyle(.segmented)
                                Text("Records native RGB, raw and smoothed depth, confidence, scene geometry, and metadata. Lossless color uses substantially more storage.")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }.disabled(capture.running)

                    HStack {
                        Button(capture.running ? "Stop capture" : "Start capture") {
                            if capture.running { capture.stop() }
                            else {
                                capture.start(captureOptions)
                            }
                        }.buttonStyle(.borderedProminent)
                        Button("High-res photo") { capture.takeStill() }
                            .buttonStyle(.bordered).disabled(!capture.canTakeStill || !capture.running)
                    }
                    Text("After stopping, open Files → On My iPhone → God's Eye → Captures to export or delete recordings. Each session is capped at 2 GB.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text("Mount the rear cameras and LiDAR facing forward. The ELEGOO remote provides manual control; autonomous driving still requires a calibrated hardware adapter.")
                        .font(.footnote).foregroundStyle(.secondary)
                }.padding()
            }
            .navigationTitle("God's Eye")
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.willResignActiveNotification)) { _ in
                remote.disconnect(reason: "Dashboard remote disconnected while app inactive")
            }
            .onAppear {
                #if DEBUG
                // Explicit local development launch provisioning, never shipped
                // credentials or automatic motion. No reconnect after a stop.
                if !provisionedRemote {
                    provisionedRemote = true
                    if let value = ProcessInfo.processInfo.environment["GODSEYE_LAPTOP_ENDPOINT"] { endpoint = value }
                    if ProcessInfo.processInfo.environment["GODSEYE_DASHBOARD_REMOTE"] == "1" { connectDashboard() }
                }
                #endif
            }
        }
    }
}
