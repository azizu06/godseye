# iPhone 17 Pro sensor profile

The dashboard uses a **5.0 m maximum LiDAR depth range** in world meters.
[Apple's ARKit scene-depth sample](https://developer.apple.com/documentation/arkit/displaying-a-point-cloud-using-scene-depth) documents that the LiDAR Scanner supports a maximum value of 5.0 meters.
[Apple's iPhone 17 Pro specifications](https://support.apple.com/en-us/125090) confirm the LiDAR Scanner, but do not publish a separate model-specific range measurement.
This profile uses Apple's documented ARKit limit, not a measured guarantee that every surface returns depth at five meters.

The cyan ground sector terminates on a five-meter radius, with one- and three-meter guide arcs in 3D.
Its angular width is illustrative and explicitly labeled uncalibrated.
The 120-degree figure in the phone specifications belongs to the Ultra Wide lens; it must not be substituted for the AR session camera's field of view.
[ARCamera intrinsics](https://developer.apple.com/documentation/arkit/arcamera/intrinsics), image resolution, and mounting calibration are needed to render an accurate optical frustum.
The current frozen `/live` schema provides pose but not those calibration fields.
The sector is a sensor range reference, not evidence of visibility, free space, or obstacle clearance.
