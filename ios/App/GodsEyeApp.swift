import SwiftUI
import ARKit

@main
struct GodsEyeApp: App {
    @StateObject private var capture = CaptureController()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            CaptureView(capture: capture)
                .onChange(of: scenePhase) { phase in
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
    @AppStorage("laptopEndpoint") private var endpoint = ""
    @State private var stream = true
    @State private var record = true
    @State private var mesh = true
    @State private var lossless = true
    @State private var rate = 10.0
    @State private var archiveRate = 2.0

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

                    GroupBox("Capture settings") {
                        VStack(alignment: .leading, spacing: 12) {
                            Toggle("Stream to laptop", isOn: $stream)
                            if stream {
                                Text("Includes native RGB, lossless sensor data, scene geometry, and motion/location telemetry. The laptop records received full-capture packets.")
                                    .font(.caption).foregroundStyle(.secondary)
                                TextField("ws://<laptop IP>:8765/phone", text: $endpoint)
                                    .textContentType(.URL).keyboardType(.URL)
                                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                                    .textFieldStyle(.roundedBorder)
                                Picker("Sensor bundles", selection: $rate) {
                                    Text("5 / sec").tag(5.0)
                                    Text("10 / sec").tag(10.0)
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
                                capture.start(CaptureOptions(endpoint: endpoint, stream: stream,
                                    record: record, mesh: mesh, losslessColor: lossless,
                                    frameHz: rate, archiveHz: archiveRate))
                            }
                        }.buttonStyle(.borderedProminent)
                        Button("High-res photo") { capture.takeStill() }
                            .buttonStyle(.bordered).disabled(!capture.canTakeStill || !capture.running)
                    }
                    Text("After stopping, open Files → On My iPhone → God's Eye → Captures to export or delete recordings. Each session is capped at 2 GB.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text("Mount the rear cameras and LiDAR facing forward. This app captures sensor data; rover control is not implemented.")
                        .font(.footnote).foregroundStyle(.secondary)
                }.padding()
            }
            .navigationTitle("God's Eye")
        }
    }
}
