# God's Eye Bluetooth rover bridge

This firmware replaces the ELEGOO ESP camera/access-point firmware with a BLE-to-Uno manual-control bridge. The iPhone stays on its normal Wi-Fi for RGB-D uploads and uses Bluetooth for the car. The laptop does not need Bluetooth or the ELEGOO network. Phone and laptop still need a reachable network path for capture.

**Deployment status (2026-09-26):** the connected ESP32-S3 has been backed up, flashed and flash-hash verified. It advertises **GodsEye-Rover-D022**. After powering the battery and switching to **Cam**, ten real sensor queries passed over Mac Bluetooth → ESP → Uno and back in 105.2–123.4 ms, including fragmented acknowledged writes. The installed iPhone 17 Pro app then connected over Bluetooth, received its own matching Uno reply and enabled manual controls; both its console and the user confirmed readiness. Simultaneous RGB-D/full-sensor uploads over the normal LAN and real 3D dashboard rendering also passed; sampled RGB-D rates were 17–31 Hz with intermittent tracking/network freshness drops. The operator confirmed the held-forward/release test worked well. See [the phone validation record](../../ios/ROVER.md#bluetooth-keep-normal-wi-fi-for-mapping). Speed and stopping distance are still unmeasured. Keep the original full-flash backup outside the repository.

## Hardware target

- The attached kit was electronically identified as **ESP32-S3 revision 0.2, 8 MB flash, 8 MB embedded PSRAM**. Use target `esp32-s3` (the default). Older ESP32-WROVER modules use target `esp32`. ESP32-S2 has no Bluetooth and cannot run this design.
- UART2 at 9600 baud, 8N1: **S3 RX GPIO3, TX GPIO40**; **WROVER RX GPIO33, TX GPIO4**. These differ: never flash a guessed pin mapping. The S3 pins come from ELEGOO's `ESP32_CameraServer_AP_2023_V1.3.ino` in the [program archive](https://drive.google.com/file/d/19IENruwaLPVMKnKpy1bk7TjwF5JThqie/view) linked by its official camera FAQ. The [older manufacturer's firmware](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0/blob/main/ESP32-WROVER-Camera.zip) confirms the WROVER pins.
- Stock Uno timed `N=2` firmware. The Uno firmware is not changed.
- No ESP camera capture or Wi-Fi AP is started. After flashing, the stock ELEGOO camera page, Wi-Fi SSID and stock Wi-Fi app path are unavailable until the original firmware is restored. The phone supplies our images/depth.

The hardware capability is documented in the [ESP32-WROVER datasheet](https://www.espressif.com/sites/default/files/documentation/esp32-wrover_datasheet_en.pdf); the GATT implementation uses Espressif's [BLE API](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/ble.html).

## Build and test

Install PlatformIO in a Python virtual environment, then from the repository root:

```sh
pio run -d firmware/elegoo-ble -e esp32-s3
python3 firmware/elegoo-ble/test/run.py
python3 ios/Tests/check_rover_ble.py
python3 ios/Tests/check_rover_connection.py
```

The platform/framework and ArduinoJson are pinned in `platformio.ini`. The S3 output is `.pio/build/esp32-s3/firmware.bin`; use the matching bootloader and partition table when uploading. Use `-e esp32` for a verified older WROVER board. The host test compiles the actual bridge parser/queue with address and undefined-behavior sanitizers. It checks fragmented/oversized input, restricted commands, latest-only queues, Stop priority, input expiry, disconnect/reconnect clearing, watchdog timing and millisecond wraparound. The iPhone controller tests use a simulated BLE link; they do not validate radio latency or CoreBluetooth on hardware.

## Deployment

1. Use the smaller ESP camera board's programming connection. [ELEGOO's instructions](https://www.elegoo.com/blogs/learn/elegoo-smart-robot-car-v4-0-with-camera-upload-code-to-the-camera-module) locate a USB-C port on the back of the camera module; it may need to be removed from the car to reach it. The Uno's USB connection does not automatically bridge the ESP bootloader. Probe the chip electronically after connecting the camera module, rather than asking the user to identify the silicon. For variants without that port, use the appropriate programming adapter; do not guess wiring or apply 5 V to ESP signal pins.
2. Before replacement, identify the chip and flash size and save the entire original flash using esptool on that **verified ESP port**. With esptool 5, `esptool --port <ESP_PORT> flash-id` identifies flash, and `esptool --port <ESP_PORT> read-flash 0 ALL stock-elegoo-backup.bin` saves it. Keep that local backup outside the repository.
3. With motor power disabled, use `pio run -d firmware/elegoo-ble -e esp32-s3 --target upload --upload-port <ESP_PORT>` for the S3. The existing S3 camera firmware may require opening its USB CDC port at 1200 baud and dropping DTR first; it then reappears under a different port as **USB JTAG/serial debug unit**. Re-detect that port before using esptool with `--before no-reset`. No firmware is written by this reset sequence. Do not upload an ESP target to the Uno.
4. Install the updated iPhone app. Keep the phone on the same fast network as the laptop; keep its existing reachable laptop capture URL.
5. Reattach the camera/ESP board to the Uno if removed, return **Upload/Cam to Cam**, and power the car. Select **Bluetooth** in **ELEGOO V4 rover**, tap **Connect rover**, grant Bluetooth access, then choose the advertised **GodsEye-Rover-XXXX** device. Selection is explicit; the app does not connect to the first nearby rover automatically.
6. Confirm **Uno responding** before enabling manual controls. Bluetooth discovery alone does not verify the UART/Uno connection. Verify any physical motion separately with the car supported and its wheels clear.

Use the phone's Bluetooth setting only to turn Bluetooth on; discovery and connection happen inside God's Eye. Keep the app in the foreground. App inactivity/disconnect disables controls, and reconnect requires explicit re-enabling.

## Wire and limits

Custom service `9E9E0001-3A17-4D2E-9A61-5C7D581F1800` has RX `...0002...` (write with response) and TX `...0003...` (notifications). The app respects the negotiated write length and waits for each write response before continuing. The bridge emits at most 20 bytes per notification so default ATT MTU works. Brace frames are reassembled on both sides.

Only these commands pass the bridge, reconstructed from validated fields:

- `N=2`: direction 1–4, PWM 1–80, lease exactly 200 ms.
- `N=22,D1=1`: cached line-sensor query, preserving its request ID.
- `N=100`: Stop.

No indefinite movement, arbitrary UART passthrough, ultrasonic blocking query, firmware-write command, or autonomous backend control is exposed. A GATT write response acknowledges BLE delivery, not motor execution. Only a matching Uno reply verifies the downstream connection.

There is one pending movement and one pending query. New movement replaces pending movement; Stop clears it and takes priority. Movement waiting more than 100 ms is discarded with Stop. Incomplete frames expire after 150 ms. UART writes are paced at their actual 9600-baud serialization cost. The bridge sends Stop after 200 ms without forwarding a new movement and on BLE disconnect; each forwarded movement also retains the Uno's 200 ms lease. These are software timers, not a measured hardware stopping guarantee. Already-transmitted bytes and radio delays are not absolute end-to-end command expiry.

The demo service does not implement authenticated pairing or bonding; anyone in Bluetooth range with a compatible client could connect while advertising. It supports one active connection and does not advertise again until it disconnects. Use only in a supervised demo environment; add authenticated pairing before wider deployment.

## Physical acceptance still required

To verify the downstream UART without moving anything, install `tools/requirements-rover.txt` and run:

```sh
python3 -m tools.probe_rover_ble --name GodsEye-Rover-D022 --samples 10
```

This connects over actual Bluetooth and sends only Stop and cached sensor queries. Close/disconnect the iPhone rover connection first so the Mac can temporarily use the bridge's single connection. The probe disconnects when finished.

Check discovery, actual Uno feedback, all four directions, release/Stop, phone disconnect and background behavior, and simultaneous 30 Hz RGB-D capture over normal Wi-Fi. Record actual motor continuity, feedback round-trip times, capture sent/s and network drops. Bluetooth removes the ESP AP from the imaging path; it does not promise a particular capture rate or eliminate iPhone thermal/radio contention.
