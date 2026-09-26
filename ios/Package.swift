// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "GodsEyeSensorCore",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "SensorCore", targets: ["SensorCore"])],
    targets: [
        .target(name: "SensorCore"),
        .testTarget(name: "SensorCoreTests", dependencies: ["SensorCore"], resources: [.copy("Fixtures/rgb.jpg")])
    ]
)
