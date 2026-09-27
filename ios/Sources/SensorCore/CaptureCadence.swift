/// Spend the warm-device budget on temporal coverage before archival detail.
/// Critical temperature is handled by the controller's capture shutdown.
public struct CaptureCadence {
    public let liveHz: Double
    public let fullHz: Double
    public let archiveHz: Double
    public let geometryHz: Double

    public init(liveHz: Double, archiveHz: Double, seriousThermal: Bool, controlPriority: Bool = false) {
        self.liveHz = controlPriority ? min(liveHz, 10) : (seriousThermal ? min(liveHz, 15) : liveHz)
        self.fullHz = seriousThermal ? 1 : 5
        self.archiveHz = seriousThermal ? min(archiveHz, 1) : archiveHz
        self.geometryHz = seriousThermal ? 0.5 : 1
    }
}
