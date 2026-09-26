/// Weighted round robin: camera frames get half the upload opportunities, while
/// telemetry, geometry, and requested stills cannot starve under continuous capture.
public struct UploadSchedule {
    private let slots = ["frame", "telemetry", "frame", "geometry", "frame", "still"]
    private var cursor = 0
    public init() {}

    public mutating func next(available: Set<String>) -> String? {
        for _ in slots.indices {
            let kind = slots[cursor]
            cursor = (cursor + 1) % slots.count
            if available.contains(kind) { return kind }
        }
        return nil
    }
}
