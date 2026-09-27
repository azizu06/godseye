#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <string.h>

// All methods run under the caller's mutex. Pure parser/queue logic also runs in
// host tests. Never forwards arbitrary JSON to the stock motor controller.
class BridgeCore {
 public:
  struct Frame { char bytes[96] = {}; uint32_t received = 0; bool present = false; };
  void session(bool connected) {
    online = connected;
    used = 0;
    motion = Frame{};
    query = Frame{};
    stopPending = true;
    moving = false;
  }

  void feed(const uint8_t* data, size_t size, uint32_t now) {
    if (!online) return;
    if (used && uint32_t(now - fragmentAt) > 150) reject();
    fragmentAt = now;
    for (size_t i = 0; i < size; ++i) {
      const char c = char(data[i]);
      if (c == '{') { used = 0; buffer[used++] = c; continue; }
      if (!used) continue;
      if (used >= sizeof(buffer) - 1) { reject(); continue; }
      buffer[used++] = c;
      if (c == '}') {
        buffer[used] = 0;
        parse(now);
        used = 0;
      }
    }
  }

  bool next(uint32_t now, Frame& out) {
    // Incomplete writes cannot leave a previous motion alive indefinitely.
    if (used && uint32_t(now - fragmentAt) > 150) reject();
    if (motion.present && uint32_t(now - motion.received) > 100) {
      motion = Frame{};
      stopPending = true;
    }
    if (moving && uint32_t(now - lastDrive) >= 200) stopPending = true;
    if (stopPending) {
      stopPending = false;
      moving = false;
      out = Frame{};
      strcpy(out.bytes, "{\"H\":\"S\",\"N\":100}");
      return true;
    }
    if (!online) return false;
    if (motion.present) {
      out = motion;
      motion = Frame{};
      lastDrive = now;
      moving = true;
      return true;
    }
    if (query.present) {
      out = query;
      query = Frame{};
      return true;
    }
    return false;
  }

 private:
  bool online = false, stopPending = true, moving = false;
  uint32_t fragmentAt = 0, lastDrive = 0;
  Frame motion, query;
  char buffer[96] = {};
  size_t used = 0;

  void reject() { used = 0; motion = Frame{}; stopPending = true; }

  void parse(uint32_t now) {
    StaticJsonDocument<256> doc;
    if (deserializeJson(doc, buffer) || !doc["N"].is<int>() || !doc["H"].is<const char*>()) {
      reject(); return;
    }
    const char* id = doc["H"];
    const size_t n = strlen(id);
    if (!n || n > 24) { reject(); return; }
    for (size_t i = 0; i < n; ++i) {
      if (!((id[i] >= 'A' && id[i] <= 'Z') || (id[i] >= '0' && id[i] <= '9'))) {
        reject(); return;
      }
    }
    int command = doc["N"];
    StaticJsonDocument<256> canonical;
    canonical["H"] = id;
    canonical["N"] = command;
    Frame* destination = nullptr;
    if (command == 100) {
      motion = Frame{};
      stopPending = true;
      return;
    } else if (command == 2 && doc["D1"].is<int>() && doc["D2"].is<int>() && doc["T"].is<int>()) {
      int direction = doc["D1"], power = doc["D2"], lease = doc["T"];
      if (direction < 1 || direction > 4 || power < 1 || power > 80 || lease != 200) {
        reject(); return;
      }
      canonical["D1"] = direction;
      canonical["D2"] = power;
      canonical["T"] = 200;
      destination = &motion;
    } else if (command == 22 && doc["D1"].is<int>() && doc["D1"].as<int>() == 1) {
      canonical["D1"] = 1;
      destination = &query;
    } else { reject(); return; }
    *destination = Frame{};
    serializeJson(canonical, destination->bytes, sizeof(destination->bytes));
    destination->received = now;
    destination->present = true;
  }
};
