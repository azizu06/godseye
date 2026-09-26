import CoreMotion
import CoreLocation
import UIKit

/// Core Motion samples retain their own uptime timestamps; location uses UTC.
/// All measurement storage is confined to the controller's capture queue.
final class PhoneSensors: NSObject, CLLocationManagerDelegate {
    private let queue: DispatchQueue
    private let operations = OperationQueue()
    private let motion = CMMotionManager()
    private let altimeter = CMAltimeter()
    private let location = CLLocationManager() // Constructed on the main thread.
    private var generation = UUID()
    private var active = false
    private var samples: [[String: Any]] = []
    private var latest: [String: Any] = [:]
    private var errors: [String: String] = [:]
    private var dropped = 0
    private var locationRunning = false // Main thread only.
    private var locationToken = UUID() // Main thread only.
    private(set) var latestMotion: [String: Any]?

    init(queue: DispatchQueue) {
        self.queue = queue
        super.init()
        operations.maxConcurrentOperationCount = 1
        operations.underlyingQueue = queue
        location.delegate = self
        location.desiredAccuracy = kCLLocationAccuracyBest
        location.distanceFilter = kCLDistanceFilterNone
    }

    var capabilities: [String: Any] {
        ["accelerometer": motion.isAccelerometerAvailable, "gyroscope": motion.isGyroAvailable,
         "magnetometer": motion.isMagnetometerAvailable, "fused_motion": motion.isDeviceMotionAvailable,
         "relative_altitude": CMAltimeter.isRelativeAltitudeAvailable(),
         "absolute_altitude": CMAltimeter.isAbsoluteAltitudeAvailable(),
         "location": CLLocationManager.locationServicesEnabled(),
         "heading": CLLocationManager.headingAvailable(), "motion_target_hz": 100,
         "motion_authorization": CMAltimeter.authorizationStatus().rawValue]
    }

    func start() {
        stop()
        active = true; samples = []; latest = [:]; errors = [:]; dropped = 0; latestMotion = nil
        let token = generation
        motion.accelerometerUpdateInterval = 0.01
        motion.gyroUpdateInterval = 0.01
        motion.magnetometerUpdateInterval = 0.01
        motion.deviceMotionUpdateInterval = 0.01
        if motion.isAccelerometerAvailable {
            motion.startAccelerometerUpdates(to: operations) { [weak self] data, error in
                guard let self, self.generation == token else { return }
                if let data { self.add("accelerometer", time: data.timestamp, values: ["acceleration_g": [data.acceleration.x, data.acceleration.y, data.acceleration.z]]) }
                if let error { self.errors["accelerometer"] = error.localizedDescription }
            }
        }
        if motion.isGyroAvailable {
            motion.startGyroUpdates(to: operations) { [weak self] data, error in
                guard let self, self.generation == token else { return }
                if let data { self.add("gyroscope", time: data.timestamp, values: ["rotation_rad_s": [data.rotationRate.x, data.rotationRate.y, data.rotationRate.z]]) }
                if let error { self.errors["gyroscope"] = error.localizedDescription }
            }
        }
        if motion.isMagnetometerAvailable {
            motion.startMagnetometerUpdates(to: operations) { [weak self] data, error in
                guard let self, self.generation == token else { return }
                if let data { self.add("magnetometer", time: data.timestamp, values: ["field_microtesla": [data.magneticField.x, data.magneticField.y, data.magneticField.z]]) }
                if let error { self.errors["magnetometer"] = error.localizedDescription }
            }
        }
        if motion.isDeviceMotionAvailable {
            motion.startDeviceMotionUpdates(using: .xArbitraryZVertical, to: operations) { [weak self] data, error in
                guard let self, self.generation == token else { return }
                if let data {
                    let q = data.attitude.quaternion, m = data.magneticField
                    let values: [String: Any] = [
                        "quaternion_xyzw": [q.x, q.y, q.z, q.w],
                        "gravity_g": [data.gravity.x, data.gravity.y, data.gravity.z],
                        "user_acceleration_g": [data.userAcceleration.x, data.userAcceleration.y, data.userAcceleration.z],
                        "rotation_rad_s": [data.rotationRate.x, data.rotationRate.y, data.rotationRate.z],
                        "magnetic_field_microtesla": [m.field.x, m.field.y, m.field.z],
                        "magnetic_accuracy": m.accuracy.rawValue,
                        "reference_frame": "xArbitraryZVertical; not calibrated to ARKit world"]
                    self.add("device_motion", time: data.timestamp, values: values)
                    self.latestMotion = self.latest["device_motion"] as? [String: Any]
                }
                if let error { self.errors["device_motion"] = error.localizedDescription }
            }
        }
        if CMAltimeter.isRelativeAltitudeAvailable() {
            altimeter.startRelativeAltitudeUpdates(to: operations) { [weak self] data, error in
                guard let self, self.generation == token else { return }
                if let data { self.add("barometer", time: data.timestamp, values: ["relative_altitude_m": data.relativeAltitude, "pressure_kpa": data.pressure]) }
                if let error { self.errors["barometer"] = error.localizedDescription }
            }
        }
        if CMAltimeter.isAbsoluteAltitudeAvailable() {
            altimeter.startAbsoluteAltitudeUpdates(to: operations) { [weak self] data, error in
                guard let self, self.generation == token else { return }
                if let data { self.add("absolute_altitude", time: data.timestamp, values: ["altitude_m": data.altitude, "accuracy_m": data.accuracy, "precision_m": data.precision]) }
                if let error { self.errors["absolute_altitude"] = error.localizedDescription }
            }
        }
        DispatchQueue.main.async {
            self.locationRunning = true; self.locationToken = token
            UIDevice.current.isBatteryMonitoringEnabled = true
            self.updateLocationAuthorization()
        }
    }

    func stop() {
        generation = UUID(); active = false
        motion.stopAccelerometerUpdates(); motion.stopGyroUpdates()
        motion.stopMagnetometerUpdates(); motion.stopDeviceMotionUpdates()
        altimeter.stopRelativeAltitudeUpdates(); altimeter.stopAbsoluteAltitudeUpdates()
        DispatchQueue.main.async {
            self.locationRunning = false
            self.location.stopUpdatingLocation(); self.location.stopUpdatingHeading()
            UIDevice.current.isBatteryMonitoringEnabled = false
        }
    }

    private func add(_ sensor: String, time: Double, values: [String: Any], clock: String = "uptime_seconds") {
        guard active else { return }
        let sample: [String: Any] = ["sensor": sensor, "timestamp": time, "clock": clock, "values": values]
        latest[sensor] = sample
        if samples.count == 2000 { samples.removeFirst(); dropped += 1 }
        samples.append(sample)
    }

    func drain() -> [String: Any] {
        let result: [String: Any] = ["samples": samples, "latest": latest, "errors": errors,
                                    "dropped_samples": dropped, "capabilities": capabilities]
        samples.removeAll(keepingCapacity: true)
        // Battery APIs belong to the main thread; return the last completed reading.
        let token = generation
        DispatchQueue.main.async {
            let device = UIDevice.current
            let values: [String: Any] = ["battery_level": device.batteryLevel,
                "battery_state": device.batteryState.rawValue,
                "thermal_state": ProcessInfo.processInfo.thermalState.rawValue,
                "low_power_mode": ProcessInfo.processInfo.isLowPowerModeEnabled,
                "location_authorization": self.location.authorizationStatus.rawValue,
                "location_accuracy_authorization": self.location.accuracyAuthorization.rawValue]
            self.queue.async {
                guard self.generation == token else { return }
                self.add("device", time: ProcessInfo.processInfo.systemUptime, values: values)
            }
        }
        return result
    }

    func recordPose(_ pose: [String: Any], timestamp: Double) {
        add("arkit_pose", time: timestamp, values: pose)
    }

    private func updateLocationAuthorization() {
        guard locationRunning else { return }
        if location.authorizationStatus == .notDetermined { location.requestWhenInUseAuthorization() }
        else if [.authorizedAlways, .authorizedWhenInUse].contains(location.authorizationStatus) {
            location.startUpdatingLocation()
            if CLLocationManager.headingAvailable() { location.startUpdatingHeading() }
        } else {
            let token = locationToken
            queue.async { if self.generation == token { self.errors["location"] = "Location permission is unavailable or denied" } }
        }
    }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) { updateLocationAuthorization() }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let token = locationToken
        for fix in locations {
            let values: [String: Any] = ["latitude_deg": fix.coordinate.latitude, "longitude_deg": fix.coordinate.longitude,
                "altitude_m": fix.altitude, "ellipsoidal_altitude_m": fix.ellipsoidalAltitude,
                "horizontal_accuracy_m": fix.horizontalAccuracy, "vertical_accuracy_m": fix.verticalAccuracy,
                "speed_m_s": fix.speed, "speed_accuracy_m_s": fix.speedAccuracy,
                "course_deg": fix.course, "course_accuracy_deg": fix.courseAccuracy,
                "simulated": fix.sourceInformation.map { $0.isSimulatedBySoftware as Any } ?? NSNull(),
                "external_accessory": fix.sourceInformation.map { $0.isProducedByAccessory as Any } ?? NSNull()]
            queue.async {
                guard self.generation == token else { return }
                self.errors.removeValue(forKey: "location")
                self.add("location", time: fix.timestamp.timeIntervalSince1970, values: values, clock: "unix_seconds")
            }
        }
    }
    func locationManager(_ manager: CLLocationManager, didUpdateHeading heading: CLHeading) {
        let token = locationToken
        let values: [String: Any] = ["magnetic_heading_deg": heading.magneticHeading, "true_heading_deg": heading.trueHeading,
                                    "accuracy_deg": heading.headingAccuracy, "field_microtesla": [heading.x, heading.y, heading.z],
                                    "heading_orientation": "portrait device top; not calibrated to rover forward"]
        queue.async {
            guard self.generation == token else { return }
            self.add("heading", time: heading.timestamp.timeIntervalSince1970, values: values, clock: "unix_seconds")
        }
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        let token = locationToken
        queue.async { if self.generation == token { self.errors["location"] = error.localizedDescription } }
    }
}
