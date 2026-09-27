#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <atomic>
#include "BridgeCore.h"

// ELEGOO's 2023 S3 source and 2021 WROVER source use different UART pins.
#if defined(CONFIG_IDF_TARGET_ESP32S3)
constexpr int UNO_RX = 3, UNO_TX = 40;
#elif defined(CONFIG_IDF_TARGET_ESP32)
constexpr int UNO_RX = 33, UNO_TX = 4;
#else
#error "Unsupported ELEGOO board; verify the UART mapping before adding a target"
#endif
constexpr char SERVICE[] = "9E9E0001-3A17-4D2E-9A61-5C7D581F1800";
constexpr char RX_UUID[] = "9E9E0002-3A17-4D2E-9A61-5C7D581F1800";
constexpr char TX_UUID[] = "9E9E0003-3A17-4D2E-9A61-5C7D581F1800";
BridgeCore bridge;
SemaphoreHandle_t guard;
BLECharacteristic* feedback;
std::atomic<bool> online{false}, advertiseAgain{false};
std::atomic<uint32_t> generation{0};

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    xSemaphoreTake(guard, portMAX_DELAY);
    bridge.session(true);
    generation++;
    online = true;
    xSemaphoreGive(guard);
    Serial.println("BLE connected; motion queue cleared");
  }
  void onDisconnect(BLEServer*) override {
    xSemaphoreTake(guard, portMAX_DELAY);
    online = false;
    generation++;
    bridge.session(false);
    xSemaphoreGive(guard);
    advertiseAgain = true;
    Serial.println("BLE disconnected; Stop queued");
  }
};

class InputCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    auto data = characteristic->getValue();
    xSemaphoreTake(guard, portMAX_DELAY);
    bridge.feed(reinterpret_cast<const uint8_t*>(data.data()), data.size(), millis());
    xSemaphoreGive(guard);
  }
};

void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, UNO_RX, UNO_TX);
  // No camera or Wi-Fi stack: the phone keeps its normal Wi-Fi connection.
  Serial2.print("{\"H\":\"S\",\"N\":100}");
  guard = xSemaphoreCreateMutex();
  uint64_t mac = ESP.getEfuseMac();
  char name[28];
  snprintf(name, sizeof(name), "GodsEye-Rover-%04X", unsigned((mac >> 32) & 0xffff));
  BLEDevice::init(name);
  BLEDevice::setMTU(185);
  BLEServer* server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService* service = server->createService(SERVICE);
  feedback = service->createCharacteristic(TX_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  feedback->addDescriptor(new BLE2902());
  auto input = service->createCharacteristic(RX_UUID, BLECharacteristic::PROPERTY_WRITE);
  input->setCallbacks(new InputCallbacks());
  service->start();
  auto advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE);
  advertising->setScanResponse(true);
  advertising->setMinPreferred(0x06);
  advertising->setMaxPreferred(0x0c);
  BLEDevice::startAdvertising();
  Serial.printf("%s ready; UART RX=%d TX=%d at 9600 baud\n", name, UNO_RX, UNO_TX);
}

void loop() {
  static uint32_t uartFreeAt = 0, seenGeneration = 0;
  static char reply[128];
  static size_t used = 0;
  if (advertiseAgain.exchange(false)) BLEDevice::startAdvertising();
  uint32_t currentGeneration = generation.load();
  if (seenGeneration != currentGeneration) { used = 0; seenGeneration = currentGeneration; }
  uint32_t now = millis();
  // Pace actual serial bytes, not just successful BLE writes. At most one
  // movement and one feedback request can be pending; never build a backlog.
  if (int32_t(now - uartFreeAt) >= 0) {
    BridgeCore::Frame frame;
    xSemaphoreTake(guard, portMAX_DELAY);
    if (bridge.next(now, frame)) {
      size_t size = strlen(frame.bytes);
      Serial2.write(reinterpret_cast<const uint8_t*>(frame.bytes), size);
      uartFreeAt = now + (size * 10000 + 9599) / 9600 + 2;
    }
    xSemaphoreGive(guard);
  }
  // All response assembly and notifications run here, not in BLE callbacks.
  while (Serial2.available()) {
    char c = Serial2.read();
    if (c == '{') { used = 0; reply[used++] = c; continue; }
    if (!used) continue;
    if (used >= sizeof(reply)) { used = 0; continue; }
    reply[used++] = c;
    if (c == '}') {
      if (online) {
        // Works even at the default 23-byte ATT MTU. The phone reassembles frames.
        for (size_t offset = 0; offset < used; offset += 20) {
          feedback->setValue(reinterpret_cast<uint8_t*>(reply + offset), min(size_t(20), used - offset));
          feedback->notify();
        }
      }
      used = 0;
    }
  }
  delay(1);
}
