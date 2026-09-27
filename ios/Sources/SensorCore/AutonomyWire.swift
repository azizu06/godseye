import Foundation

/// Separate from the frozen sensor wire and from the stock manual remote.
/// Never accepts raw UART or substitutes a new permit for the laptop's permit.
public struct AutonomyCommand: Decodable {
    public enum Kind: String, Decodable { case stop, arm, command }
    public let version: Int
    public let type: Kind
    public let id: String?
    public let session: String?
    public let permit: String?
    public let seq: UInt32?
    public let direction: Int?
    public let power: Int?
    public let lease_ms: Int?

    public enum Error: Swift.Error { case invalidCommand }

    public static func decode(_ data: Data) throws -> Self {
        guard data.count <= 2048,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Error.invalidCommand
        }
        let value = try JSONDecoder().decode(Self.self, from: data)
        guard value.version == 1 else { throw Error.invalidCommand }
        let keys: Set<String>
        switch value.type {
        case .stop:
            keys = ["version", "type", "id"]
            guard let id = value.id, !id.isEmpty, id.utf8.count <= 24,
                  id.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) }) else {
                throw Error.invalidCommand
            }
        case .arm:
            keys = ["version", "type", "session", "permit"]
            guard hex(value.session, count: 32), hex(value.permit, count: 16) else { throw Error.invalidCommand }
        case .command:
            keys = ["version", "type", "session", "permit", "seq", "direction", "power", "lease_ms"]
            guard hex(value.session, count: 32), hex(value.permit, count: 16),
                  let seq = value.seq, seq > 0, value.lease_ms == 200,
                  let direction = value.direction, let power = value.power,
                  (direction == 0 && power == 0) || ((1...4).contains(direction) && (1...80).contains(power)) else {
                throw Error.invalidCommand
            }
        }
        guard Set(object.keys) == keys else { throw Error.invalidCommand }
        return value
    }

    public var packet: Data {
        get throws {
            var fields: [String: Any]
            switch type {
            case .stop: return try ElegooWire.stop(id: id!)
            case .arm: fields = ["N": 201, "H": session!, "C": permit!]
            case .command:
                fields = ["N": 202, "H": session!, "C": permit!, "S": seq!,
                          "D1": direction!, "D2": power!, "T": 200]
            }
            return try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
        }
    }

    static func hex(_ text: String?, count: Int) -> Bool {
        guard let text, text.utf8.count == count else { return false }
        return text.utf8.allSatisfy { (48...57).contains($0) || (65...70).contains($0) }
    }
}

/// Phone-side ownership and queue guard; firmware independently checks its own
/// clock at UART dispatch. A local stop always clears pending movement.
public struct AutonomyGate {
    public private(set) var armedSession: String?
    public private(set) var pendingArm: String?
    public private(set) var sequence: UInt32 = 0
    private var permits: [String: Double] = [:]
    public init() {}

    public mutating func sawPermit(_ permit: String, now: Double) {
        permits = permits.filter { now - $0.value < 0.2 }
        // Repeated notifications cannot renew the original local receipt time.
        if permits[permit] == nil && AutonomyCommand.hex(permit, count: 16) { permits[permit] = now }
    }

    public mutating func stop() {
        armedSession = nil; pendingArm = nil; sequence = 0; permits.removeAll()
    }

    public mutating func armed(_ session: String) -> Bool {
        guard pendingArm == session else { return false }
        pendingArm = nil; armedSession = session; permits.removeAll()
        return true
    }

    public func fresh(_ command: AutonomyCommand, now: Double) -> Bool {
        guard let permit = command.permit, let received = permits[permit] else { return false }
        return now >= received && now - received < 0.2
    }

    public mutating func accept(_ command: AutonomyCommand, now: Double) -> Bool {
        if command.type == .stop { stop(); return true }
        guard fresh(command, now: now) else { return false }
        if command.type == .arm {
            guard armedSession == nil, pendingArm == nil else { return false }
            pendingArm = command.session; sequence = 0
        } else {
            guard command.session == armedSession, let seq = command.seq, seq > sequence else { return false }
            sequence = seq
        }
        return true
    }
}
