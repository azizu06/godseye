import Foundation
import SensorCore

/// Capture-queue confined. One request in flight and one latest packet per kind.
final class RichUploader {
    private let queue: DispatchQueue
    private let session = URLSession(configuration: .ephemeral)
    private var endpoint: URL?
    private var generation = UUID()
    private var request: URLSessionUploadTask?
    private var pending: [String: (data: Data, created: Double)] = [:]
    private var busy = false
    private var retry: DispatchWorkItem?
    private(set) var dropped = 0
    private(set) var uploaded = 0
    var onStatus: ((String) -> Void)?
    init(queue: DispatchQueue) { self.queue = queue }

    func start(phoneURL: URL) {
        stop()
        var components = URLComponents(url: phoneURL, resolvingAgainstBaseURL: false)!
        components.scheme = phoneURL.scheme == "wss" ? "https" : "http"
        components.path = "/capture/ingest"
        endpoint = components.url
        uploaded = 0; dropped = 0
        onStatus?("Full sensor upload ready")
    }
    func stop() {
        generation = UUID(); endpoint = nil
        retry?.cancel(); retry = nil
        request?.cancel(); request = nil
        pending.removeAll(); busy = false
    }
    func offer(_ data: Data, kind: String) {
        guard endpoint != nil else { return }
        if pending[kind] != nil { dropped += 1 }
        pending[kind] = (data, ProcessInfo.processInfo.systemUptime)
        drain()
    }
    private func drain() {
        guard !busy, let endpoint else { return }
        let now = ProcessInfo.processInfo.systemUptime
        for (kind, packet) in pending where now - packet.created > 5 {
            pending.removeValue(forKey: kind); dropped += 1
        }
        guard let item = pending.min(by: { $0.value.created < $1.value.created }) else { return }
        pending.removeValue(forKey: item.key)
        busy = true
        let token = generation
        var urlRequest = URLRequest(url: endpoint, timeoutInterval: 10)
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request = session.uploadTask(with: urlRequest, from: item.value.data) { [weak self] data, response, error in
            guard let self else { return }
            self.queue.async { [weak self] in
                guard let self, self.generation == token else { return }
                self.request = nil
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if code == 200 {
                    self.uploaded += 1; self.busy = false
                    let result = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                    let record = result?["recording_error"] as? String
                    let recording = result?["recording_enabled"] as? Bool ?? false
                    self.onStatus?(record.map { "Full capture live · \($0)" } ??
                        (recording ? "Full capture live · laptop recording" : "Full capture live · laptop recording off"))
                    self.drain()
                } else {
                    self.dropped += 1
                    self.onStatus?("Full capture retrying · \(error?.localizedDescription ?? "HTTP \(code)")")
                    // Leave newer samples queued; never retry an old sensor sample.
                    let work = DispatchWorkItem { [weak self] in
                        guard let self, self.generation == token else { return }
                        self.busy = false; self.drain()
                    }
                    self.retry = work
                    self.queue.asyncAfter(deadline: .now() + 1, execute: work)
                }
            }
        }
        request?.resume()
    }
}
