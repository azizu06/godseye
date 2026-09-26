import Foundation
import SensorCore

/// All mutable state is confined to the capture queue. Completion handlers hop back.
final class PhoneStream {
    private let queue: DispatchQueue
    private let session = URLSession(configuration: .ephemeral)
    private var socket: URLSessionWebSocketTask?
    private var pending = PendingMessages()
    private var busy = false
    private var ready = false
    private var generation = UUID()
    private var retry: DispatchWorkItem?
    private var retryDelay = 1.0
    private var endpoint: URL?
    private var hello: Data?
    var onStatus: ((String) -> Void)?
    private(set) var sentFrames = 0
    var dropped: Int { pending.dropped }

    init(queue: DispatchQueue) { self.queue = queue }

    func connect(url: URL, hello: Data) {
        disconnect()
        endpoint = url; self.hello = hello
        retryDelay = 1
        open()
    }

    func disconnect() {
        generation = UUID()
        retry?.cancel(); retry = nil
        socket?.cancel(with: .goingAway, reason: nil); socket = nil
        endpoint = nil; hello = nil; ready = false; busy = false
        pending = PendingMessages(); sentFrames = 0
    }

    private func open() {
        guard let endpoint, let hello else { return }
        generation = UUID()
        let token = generation
        ready = false; busy = true; pending = PendingMessages()
        let task = session.webSocketTask(with: endpoint)
        socket = task; task.resume()
        onStatus?("Connecting…")
        send(.string(String(decoding: hello, as: UTF8.self)), token: token) { [weak self] in
            guard let self else { return }
            self.ready = true; self.busy = false
            self.onStatus?("Streaming")
            self.receive(token: token)
            self.drain()
        }
    }

    func offer(_ message: SensorMessage) {
        guard ready else { return }
        pending.offer(message)
        drain()
    }

    private func drain() {
        guard ready, !busy,
              let message = pending.next(now: ProcessInfo.processInfo.systemUptime) else { return }
        busy = true
        let payload: URLSessionWebSocketTask.Message = message.isFrame
            ? .data(message.data) : .string(String(decoding: message.data, as: UTF8.self))
        send(payload, token: generation) { [weak self] in
            guard let self else { return }
            if message.isFrame { self.sentFrames += 1 }
            self.busy = false
            self.drain()
        }
    }

    private func send(_ message: URLSessionWebSocketTask.Message, token: UUID,
                      completion: @escaping () -> Void) {
        // Fail a stalled connection instead of letting queued data get old indefinitely.
        let sendID = UUID()
        activeSend = sendID
        queue.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self, self.generation == token, self.activeSend == sendID else { return }
            self.failed("Send timed out", token: token)
        }
        socket?.send(message) { [weak self] error in
            guard let self else { return }
            self.queue.async {
                guard self.generation == token else { return }
                self.activeSend = nil
                if let error { self.failed(error.localizedDescription, token: token) }
                else { completion() }
            }
        }
    }
    private var activeSend: UUID?

    private func receive(token: UUID) {
        socket?.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard self.generation == token else { return }
                switch result {
                case .success: self.receive(token: token)
                case .failure(let error): self.failed(error.localizedDescription, token: token)
                }
            }
        }
    }

    private func failed(_ reason: String, token: UUID) {
        guard generation == token, endpoint != nil else { return }
        generation = UUID(); ready = false; busy = false; activeSend = nil
        socket?.cancel(with: .goingAway, reason: nil); socket = nil
        pending = PendingMessages()
        onStatus?("Reconnecting: \(reason)")
        let nextToken = generation
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.generation == nextToken else { return }
            self.open()
        }
        retry = work
        queue.asyncAfter(deadline: .now() + retryDelay, execute: work)
        retryDelay = min(10, retryDelay * 2)
    }
}
