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
    @AppStorage("computerControlOnLaunch") private var computerMode = true
    @State private var stream = true
    @AppStorage("uploadFullSensors") private var fullSensorUpload = true
    @AppStorage("recordFullSensors") private var record = true
    @AppStorage("reconstructMesh") private var mesh = true
    @AppStorage("losslessColor") private var lossless = true
    @AppStorage("liveFrameRate") private var rate = 30.0
    @AppStorage("archiveFrameRate") private var archiveRate = 2.0
    @StateObject private var autonomy = RoverAutonomyLink()
    @StateObject private var remote = PhoneRemoteLink()
    @State private var provisionedRemote = false
    @State private var autonomyKey = ""
    @State private var pairingNotice = ""

    private var captureOptions: CaptureOptions {
        CaptureOptions(endpoint: endpoint, stream: stream, fullSensorUpload: fullSensorUpload,
                       record: record, mesh: mesh, losslessColor: lossless, frameHz: rate, archiveHz: archiveRate)
    }

    private func connectDashboard() {
        remote.connect(capture: capture, rover: rover, autonomy: autonomy,
                       options: captureOptions, key: autonomyKey, automaticSetup: computerMode)
        if remote.enabled {
            let key = autonomyKey.trimmingCharacters(in: .whitespacesAndNewlines)
            Task {
                pairingNotice = await ComputerPairing.shared.save(key)
                    ? "Laptop pairing remembered on this iPhone"
                    : "Connected, but pairing could not be saved"
            }
        }
    }

    private func startComputerMode() {
        guard provisionedRemote, computerMode, !remote.enabled, !autonomyKey.isEmpty else { return }
        connectDashboard()
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
                            Toggle("Computer control on launch", isOn: $computerMode)
                            Text("Starts capture, connects the saved rover, and enables laptop control when tracking is ready. Arm and drive from the dashboard.")
                                .font(.caption).foregroundStyle(.secondary)
                            Text(autonomy.status).font(.subheadline)
                            SecureField("Laptop control pairing key", text: $autonomyKey)
                                .textInputAutocapitalization(.never).autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder).disabled(autonomy.enabled || remote.enabled)
                            Text(remote.status).font(.subheadline)
                            Button(remote.enabled ? "Disconnect dashboard remote" : "Connect dashboard remote") {
                                if remote.enabled { remote.disconnect() }
                                else { connectDashboard() }
                            }.buttonStyle(.bordered)
                            Text("Keep this app open; the screen stays awake while paired. Stop stays stopped until you enable control again from the dashboard or reopen the app.")
                                .font(.caption).foregroundStyle(.secondary)
                            if !pairingNotice.isEmpty { Text(pairingNotice).font(.caption) }
                            Button("Forget laptop pairing", role: .destructive) {
                                computerMode = false
                                remote.disconnect(); autonomy.disconnect(); rover.stop()
                                autonomyKey = ""
                                Task {
                                    pairingNotice = await ComputerPairing.shared.forget()
                                        ? "Laptop pairing forgotten" : "Could not remove saved pairing"
                                }
                            }.buttonStyle(.bordered)
                            Button(autonomy.enabled ? "Disable laptop control" : "Enable laptop control") {
                                if autonomy.enabled { remote.cancelAutomaticSetup(); autonomy.disconnect() }
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
                            if capture.running { remote.cancelAutomaticSetup(); autonomy.disconnect(); rover.stop(); capture.stop() }
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
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
                startComputerMode()
            }
            .onChange(of: computerMode) { enabled in
                if enabled { startComputerMode() }
                else { remote.cancelAutomaticSetup(); autonomy.disconnect(); rover.stop() }
            }
            .task {
                guard !provisionedRemote else { return }
                autonomyKey = await ComputerPairing.shared.load() ?? ""
                #if DEBUG
                // One-time local provisioning survives ordinary Xcode launches.
                let environment = ProcessInfo.processInfo.environment
                if let value = environment["GODSEYE_ROVER_PAIRING_KEY"] { autonomyKey = value }
                if let value = environment["GODSEYE_LAPTOP_ENDPOINT"] { endpoint = value }
                if let value = environment["GODSEYE_ROVER_IDENTIFIER"], let id = UUID(uuidString: value) {
                    UserDefaults.standard.set(id.uuidString, forKey: "preferredRoverIdentifier")
                }
                #endif
                provisionedRemote = true
                if UIApplication.shared.applicationState == .active { startComputerMode() }
            }
        }
    }
}
