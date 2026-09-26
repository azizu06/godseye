/// What a capture sends to the laptop.
///
/// The v1 `/phone` stream (poses and RGB-D bundles) feeds the live map and drive
/// health, and the backend rejects anything older than 250 ms. The full-sensor v2
/// upload (native frames, geometry, telemetry, stills) is heavy best-effort data
/// on the same Wi-Fi uplink, so it is a separate choice: turning it off never
/// stops the live stream.
public struct LaptopUploads: Equatable {
    /// v1 WebSocket stream of poses and RGB-D bundles.
    public let live: Bool
    /// v2 `POST /capture/ingest`. Its endpoint comes from the stream URL, so it needs `live`.
    public let fullSensor: Bool

    public init(streamToLaptop: Bool, fullSensorUpload: Bool) {
        live = streamToLaptop
        fullSensor = streamToLaptop && fullSensorUpload
    }
}
