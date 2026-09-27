import Foundation

public struct LiveMeshCache {
    private var identity: CaptureIdentity?
    private var lastSample = -Double.infinity
    private var snapshot: Data?

    public init() {}

    public mutating func current(identity: CaptureIdentity, capture: Double,
                                 sample: () -> Data) -> Data {
        if self.identity != identity {
            self.identity = identity
            snapshot = nil
            lastSample = -.infinity
        }
        if snapshot == nil || capture - lastSample >= 1 {
            snapshot = sample()
            lastSample = capture
        }
        return snapshot ?? Data()
    }
}
