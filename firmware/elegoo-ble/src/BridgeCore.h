#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <string.h>
#include "AutonomyGuard.h"

// All methods run under the caller's mutex. Pure parser/queue logic also runs in
// host tests. Never forwards arbitrary JSON to the stock motor controller.
class BridgeCore {
 public:
  struct Frame {
    char bytes[96] = {}, notification[40] = {}, session[33] = {};
    uint32_t received = 0, sequence = 0;
    uint64_t permit = 0;
    bool present = false, autonomous = false, nonzero = false;
  };
  void session(bool connected) {
    autonomy.connection(connected);
    armAck = ended = false;
    stopAck[0] = 0;
    online = connected;
    used = 0;
    motion = Frame{};
    query = Frame{};
    stopPending = true;
    moving = false;
  }

  bool issuePermit(uint64_t token, uint32_t now) { return autonomy.issue(token, now); }
  void inputFailure() { reject(); }

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
    if (autonomy.tick(now)) { ended = true; reject(); }
    // Incomplete writes cannot leave a previous motion alive indefinitely.
    if (used && uint32_t(now - fragmentAt) > 150) reject();
    if (motion.present && uint32_t(now - motion.received) > 100) {
      reject();
    }
    if (motion.present && motion.autonomous &&
        !autonomy.canForward(motion.session, motion.sequence, motion.permit, now)) reject();
    if (moving && uint32_t(now - lastDrive) >= 200) reject();
    if (stopPending) {
      stopPending = false;
      moving = false;
      out = Frame{};
      strcpy(out.bytes, "{\"H\":\"S\",\"N\":100}");
      if (armAck && autonomy.active()) {
        strcpy(out.notification, "{A");
        strcat(out.notification, autonomy.session());
        strcat(out.notification, "}");
      } else if (stopAck[0]) strcpy(out.notification, stopAck);
      else if (ended) strcpy(out.notification, "{X}");
      armAck = ended = false;
      stopAck[0] = 0;
      return true;
    }
    if (!online) return false;
    if (motion.present) {
      out = motion;
      motion = Frame{};
      lastDrive = now;
      moving = !out.autonomous;
      if (out.autonomous) autonomy.forwarded(now, out.nonzero);
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
  AutonomyGuard autonomy;
  bool armAck = false, ended = false;
  char stopAck[40] = {};
  Frame motion, query;
  char buffer[192] = {};
  size_t used = 0;

  void reject() {
    ended = ended || autonomy.active() || armAck || motion.autonomous;
    autonomy.stop();
    armAck = false;
    used = 0; motion = Frame{}; stopPending = true;
  }

  static bool permitValue(JsonVariantConst value, uint64_t& result) {
    if (!value.is<const char*>()) return false;
    const char* text = value.as<const char*>();
    if (value.as<JsonString>().size() != 16) return false;
    result = 0;
    for (size_t i = 0; i < 16; ++i) {
      unsigned digit;
      if (text[i] >= '0' && text[i] <= '9') digit = text[i] - '0';
      else if (text[i] >= 'A' && text[i] <= 'F') digit = text[i] - 'A' + 10;
      else return false;
      result = (result << 4) | digit;
    }
    return result != 0;
  }

  void parseAutonomy(JsonDocument& doc, uint32_t now) {
    uint64_t permit;
    if (!doc["H"].is<const char*>() || doc["H"].as<JsonString>().size() != 32 ||
        !permitValue(doc["C"], permit)) { reject(); return; }
    const char* id = doc["H"];
    int command = doc["N"];
    if (command == 201) {
      if (!autonomy.arm(id, permit, now)) { ended = true; reject(); return; }
      motion = Frame{};
      moving = false;
      armAck = stopPending = true;
      stopAck[0] = 0;
      ended = false;
      return;
    }
    if (!doc["S"].is<uint32_t>() || !doc["D1"].is<int>() || !doc["D2"].is<int>() ||
        !doc["T"].is<int>() || doc["T"].as<int>() != 200) { reject(); return; }
    int direction = doc["D1"], power = doc["D2"];
    const bool nonzero = direction != 0 || power != 0;
    if (nonzero && (direction < 1 || direction > 4 || power < 1 || power > 80)) {
      reject(); return;
    }
    const uint32_t sequence = doc["S"];
    if (!autonomy.accept(id, sequence, permit, now)) { ended = true; reject(); return; }
    motion = Frame{};
    motion.present = motion.autonomous = true;
    motion.nonzero = nonzero;
    motion.received = now;
    motion.sequence = sequence;
    motion.permit = permit;
    strcpy(motion.session, id);
    StaticJsonDocument<256> canonical;
    canonical["H"] = nonzero ? "M" : "S";
    canonical["N"] = nonzero ? 2 : 100;
    if (nonzero) { canonical["D1"] = direction; canonical["D2"] = power; canonical["T"] = 200; }
    serializeJson(canonical, motion.bytes, sizeof(motion.bytes));
  }

  void parse(uint32_t now) {
    StaticJsonDocument<768> doc;
    if (deserializeJson(doc, buffer) || !doc["N"].is<int>() || !doc["H"].is<const char*>()) {
      reject(); return;
    }
    if (doc["N"].as<int>() == 201 || doc["N"].as<int>() == 202) {
      parseAutonomy(doc, now);
      return;
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
      reject();
      strcpy(stopAck, "{Z");
      strcat(stopAck, id);
      strcat(stopAck, "}");
      return;
    } else if (command == 2 && doc["D1"].is<int>() && doc["D2"].is<int>() && doc["T"].is<int>()) {
      if (autonomy.active()) { reject(); return; } // Explicit Stop before manual takeover.
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
