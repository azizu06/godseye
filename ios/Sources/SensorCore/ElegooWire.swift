import Foundation

/// ELEGOO V4 stock UART/TCP protocol, separate from God's Eye's v1 sensor wire.
public enum ElegooDirection: Int, CaseIterable {
    case left = 1, right = 2, forward = 3, backward = 4
}

public enum ElegooReply: Equatable {
    case heartbeat
    case stopped
    case response(id: String, value: String)
}

public enum ElegooWire {
    public static let maximumPower = 80
    public static let leaseMilliseconds = 200

    public enum Error: Swift.Error { case invalidID, invalidPower }

    private static func encode(_ fields: [String: Any], id: String) throws -> Data {
        guard !id.isEmpty, id.utf8.count <= 24,
              id.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) }) else {
            throw Error.invalidID
        }
        var fields = fields
        fields["H"] = id
        return try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    }

    public static func feedbackQuery(id: String) throws -> Data {
        try encode(["N": 22, "D1": 1], id: id)
    }

    public static func stop(id: String) throws -> Data {
        try encode(["N": 100], id: id)
    }

    /// N=2 is timed in the manufacturer's implementation. N=3 is indefinite.
    /// Power is PWM duty, not a calibrated speed in meters per second.
    public static func drive(id: String, direction: ElegooDirection, power: Int) throws -> Data {
        guard (1...maximumPower).contains(power) else { throw Error.invalidPower }
        return try encode(["N": 2, "D1": direction.rawValue, "D2": power,
                           "T": leaseMilliseconds], id: id)
    }
}

/// TCP and UART can split or concatenate messages. Ignore boot text and bound
/// storage even if a peer never sends a closing brace.
public struct ElegooReplyDecoder {
    private var bytes: [UInt8] = []
    private var collecting = false
    public init() {}

    public mutating func append(_ data: Data) -> [ElegooReply] {
        var replies: [ElegooReply] = []
        for byte in data {
            if byte == 123 {
                bytes.removeAll(keepingCapacity: true)
                collecting = true
                continue
            }
            guard collecting else { continue }
            if byte == 125 {
                collecting = false
                if let text = String(bytes: bytes, encoding: .utf8) {
                    if text == "Heartbeat" { replies.append(.heartbeat) }
                    else if text == "ok" { replies.append(.stopped) }
                    else if let split = text.firstIndex(of: "_") {
                        let id = String(text[..<split])
                        let value = String(text[text.index(after: split)...])
                        if !id.isEmpty && !value.isEmpty {
                            replies.append(.response(id: id, value: value))
                        }
                    }
                }
                bytes.removeAll(keepingCapacity: true)
            } else if bytes.count < 126 {
                bytes.append(byte)
            } else {
                collecting = false
                bytes.removeAll(keepingCapacity: true)
            }
        }
        return replies
    }
}
