# ELEGOO V4 manual remote

The iPhone can control the ELEGOO Smart Robot Car V4 through its ESP board using stock Wi-Fi firmware or the new God's Eye Bluetooth bridge firmware, or through a laptop USB relay to the Uno. The Uno remains the motor controller. The default backend still logs only and reports the car down; the separate opt-in [autonomous relay](../docs/AUTONOMY.md) uses the phone's Bluetooth connection and requires measured calibration.

## Mounted-phone dashboard controls

**Computer control on launch** defaults on. Pair once; the phone remembers the key in Keychain, the laptop URL, capture settings and verified selected rover. Opening or foregrounding the app starts capture, reconnects that rover and enables laptop control when tracking/Uno feedback are ready. It never arms or resumes a drive. Keep the app foregrounded. All further setup, Stop and enable actions are available in the dashboard's **Rover controls → Mounted phone**. Stop cancels pending automatic setup; a later tracking recovery cannot override it. If no rover is saved, select one from the dashboard once. **Forget laptop pairing** clears the saved key. See [setup and validation](../docs/AUTONOMY.md#run-and-use-the-mounted-phone-controls).

## Laptop USB: use the currently connected Uno

This alternative needs no ESP reflash. The phone uses normal Wi-Fi for both capture and manual commands to the laptop; the laptop sends only the small control messages over USB. **The car stays physically tethered to the laptop.** It is a development fallback, not untethered rover control.

1. Leave the Uno connected to the laptop by USB and set the car's **Upload/Cam** switch to **Upload** so the USB serial adapter can reach it. Keep motor tests supervised with wheels clear.
2. Install the optional serial dependency and verify a non-motion reply:

   ```sh
   python3 -m pip install -r tools/requirements-rover.txt
   python3 -m tools.rover_usb_relay --device /dev/cu.usbserial-1110 --check
   ```

3. Once that succeeds, start the manual relay:

   ```sh
   python3 -m tools.rover_usb_relay --device /dev/cu.usbserial-1110 --listen 0.0.0.0 --port 8768
   ```

4. In the iPhone rover panel, select **Laptop USB**. Enter the laptop's reachable LAN address, port **8768**, and the pairing code printed by the relay. Tap Connect rover, wait for the matching Uno response, then explicitly enable controls. Capture keeps its existing laptop URL and normal Wi-Fi path.

The relay refuses to start if its initial cached-sensor query gets no valid Uno reply. Startup/connection changes send Stop. It accepts one authenticated controller at a time, limits movement to the same direction/PWM/200 ms rules, replaces queued movement, discards movement waiting over 100 ms, and paces the 9600-baud UART. A 200 ms relay watchdog and the Uno's own timed command protect against missing refreshes. Disconnect, malformed input, partial-frame timeout and shutdown request Stop. Serial or radio timing still needs physical verification.

The pairing code is generated into `~/.config/godseye/rover-pairing-key` with restrictive creation permissions. The phone keeps the entered code only in memory. This is an authenticated **plaintext local TCP** demo link; do not port-forward it onto the Internet. It does not provide TLS, calibrated velocity or the backend navigation protocol. Campus client isolation may prevent phone-to-laptop connections even when both use eduroam.

Validation: `python3 -m unittest tools.tests.test_rover_usb_relay -v` and `python3 ios/Tests/check_rover_connection.py --relay-auth`. These checks use fake serial data and loopback TCP; they do not drive a real car.

The connected kit has also answered real `N=22` queries through the authenticated laptop LAN socket and USB relay after switching **Upload/Cam to Upload**. Five non-motion request/reply round trips measured 59–130 ms on the bench. This verifies the serial path, not iPhone-to-laptop reachability, motor behavior, or simultaneous capture throughput; those remain physical acceptance checks.

## Bluetooth: keep normal Wi-Fi for mapping

The **Bluetooth** option talks directly to the ESP while the phone streams RGB-D over normal Wi-Fi. It requires [the BLE bridge firmware](../firmware/elegoo-ble/README.md) on the smaller ESP board; changing the iPhone app alone cannot activate it. The connected S3 kit now has that firmware installed and advertises **GodsEye-Rover-D022**. Return the Upload/Cam switch to **Cam**, select Bluetooth, tap Connect rover, then choose the advertised rover. The same matching-Uno-reply check, explicit enable, timed movement, release-to-stop and no-auto-reconnect rules apply. Firmware replacement disables the stock ESP camera/Wi-Fi AP; the original flash was backed up and verified first.

On 2026-09-26, ten real Mac Bluetooth → ESP → Uno sensor queries succeeded in 105.2–123.4 ms after battery power was enabled and the switch moved to **Cam**. The signed app containing all three transports was installed and launched on the attached iPhone 17 Pro. Its live console then confirmed Bluetooth connection, a matching Uno reply and explicit manual enable; the user also confirmed the ready status. The verification probe sent no movement commands.

The phone subsequently streamed RGB-D and full sensor packets to the Mac over its normal LAN while the Bluetooth controller remained connected. Normal-tracking samples measured 17–31 accepted RGB-D frames/s; the 3D dashboard rendered over 100,000 live points and 300,000 triangles. Initial tracking loss and intermittent network freshness drops were also observed, so this is evidence of simultaneous operation, not sustained 30 Hz throughput. The operator then reported the requested held-forward/release test working well during capture. This is operator-confirmed manual operation, not measured velocity, braking distance or autonomous calibration.

## Connect using stock ELEGOO Wi-Fi

1. Power the car using its battery switch. Join its ELEGOO Wi-Fi network on the iPhone. The observed kit advertises `ELEGOO-D02227D9B4DC`.
2. Open God's Eye and find **ELEGOO V4 rover**. The stock defaults are ESP address `192.168.4.1`, TCP port `100`. This is a plain TCP endpoint, not a WebSocket URL.
3. Tap **Connect rover** and allow Local Network access. The app sends Stop and a non-motion cached line-sensor query. It enables the manual-control option only after a valid 10-bit sensor reply carrying the matching request ID arrives from the Uno. An ESP connection or heartbeat alone is insufficient.
4. For a supervised motor test, support the car with wheels clear first. Tap **Enable manual controls**, then hold a direction. Release or drag outside the button to stop. **STOP** disables controls until explicitly enabled again.

To map and control simultaneously, the laptop must be reachable from the same network. With stock ESP access-point firmware, join the laptop to that ELEGOO network too, then enter the laptop's new IP in the phone's capture settings. The previous campus/LAN address will not automatically remain reachable. This shared ESP network needs measured throughput testing with the high-rate RGB-D stream.

USB connected to the main Uno is a programming/serial connection to the Uno, not the ESP. It does not expose the ESP bootloader. The phone uses Wi-Fi for this remote, and the car must be powered independently when USB is removed.

## Protocol and behavior

The implementation follows the manufacturer's [stock ESP firmware archive](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0/blob/main/ESP32-WROVER-Camera.zip) and [Uno firmware archive](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0/blob/main/SmartRobotCarV4.0_V0_20210104.zip). The ESP listens on TCP 100, forwards brace-delimited packets over UART, and requires replies to `{Heartbeat}`.

- `N=22, D1=1` reads the cached center line-sensor value to verify the Uno link without blocking on ultrasonic ranging. Replies are `{requestID_value}`.
- `N=2, D1=direction, D2=PWM, T=200` requests a timed movement. Directions are left=1, right=2, forward=3, backward=4. The actual manufacturer's implementations use **N=2 for timed control**, even though comments/examples in the newer repository conflict. This app never sends the indefinite `N=3` command.
- `N=100` requests standby/Stop.

Held input targets one movement refresh every 80 ms (up to 12.5/second), with one application write in flight and no catch-up bursts. A 20 ms scheduler runs in common run-loop modes so holding controls or scrolling does not pause heartbeat and command updates. TCP_NODELAY disables small-packet coalescing. Movement and Stop use short IDs; only feedback probes need unique IDs. A movement frame is 38 bytes (about 40 ms at 9600 baud, 8N1); nominal motion traffic uses 475 of the UART's 960 bytes/second, leaving room for probes and Stop. Manual motor power is capped at 80/255, initially 60/255; the separate authenticated autonomous path permits up to 180/255 with updated ESP firmware. PWM duty is **not a calibrated linear or angular speed**. Manual commands keep a 200 ms Uno timer; autonomous commands use 1.5 s with a separate 1 s ESP command-loss brake. Actual stop time, direction, wheel response and firmware behavior still require physical verification.

The app polls Uno feedback once per second and disconnects after a query has gone unanswered for 2.5 seconds. A stalled application write is abandoned after 0.5 seconds. Stop has priority over subsequent motion. Disconnect, app inactivity and source changes discard held input and disable controls. Reconnecting never resumes an earlier movement or auto-enables controls. TCP can still delay bytes already sent; this stock protocol has no authenticated sessions, sequence rejection, or absolute command expiry.

Use this with the matching stock ELEGOO V4 firmware on a trusted local network. It does not implement the stronger calibrated, acknowledged drive envelope described in the backend or authorize the autonomous navigation adapter. No firmware flashing or real motor test is part of the software validation.

## Validation

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift test --package-path ios
python3 ios/Tests/check_rover_connection.py
python3 ios/Tests/check_rover_ble.py
```

The macOS loopback check compiles the actual phone connection controller against a simulated TCP rover with 9600-baud command serialization delay. It checks sustained held-command gaps against the 200 ms lease and exercises repeated heartbeats, fragmented replies, mismatched IDs, explicit enabling, held/released commands, Stop, lost Uno feedback despite a connected socket, and disarmed reconnect. All emitted motor packets are timed and bounded; no hardware is involved. This does not model every real Wi-Fi delay or Uno processing stall.
