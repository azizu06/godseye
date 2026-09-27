#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// Clock-local autonomous command gate. Caller serializes access and must check
// canForward immediately before UART dispatch, then call forwarded. No motor I/O.
class AutonomyGuard {
 public:
  static constexpr uint32_t PERMIT_MS = 500, WATCHDOG_MS = 1000;

  void connection(bool connected) {
    stop();
    online = connected;
  }

  // Use a fresh unpredictable 64-bit token every 50 ms. Zero is never a permit.
  bool issue(uint64_t token, uint32_t now) {
    if (!online || !token) return false;
    for (const auto& p : permits) if (p.token == token) return false;
    permits[cursor].token = token;
    permits[cursor].issued = now;
    cursor = (cursor + 1) % PERMIT_COUNT;
    return true;
  }

  bool arm(const char* id, uint64_t token, uint32_t now) {
    if (!validID(id) || !validPermit(token, now) || strcmp(id, driveSession) == 0) {
      stop();
      return false;
    }
    // An arm is a stopped barrier, not a movement. Clearing permits ensures
    // commands must use evidence produced AFTER this new session opened.
    stop();
    strcpy(driveSession, id);
    armed = true;
    lastSequence = 0;
    brakeSequence = 0;
    return true;
  }

  bool accept(const char* id, uint32_t sequence, uint64_t token, uint32_t now) {
    tick(now); // A late arrival cannot extend the previous motor lease.
    if (!armed || !validID(id) || strcmp(id, driveSession) != 0 || !sequence ||
        sequence <= lastSequence) {
      stop();
      return false;
    }
    if (!validPermit(token, now)) {
      // A delayed Wi-Fi/BLE packet cannot move the car, but may be followed
      // by a fresh one. Consume its sequence without retiring the arm.
      lastSequence = sequence;
      return false;
    }
    lastSequence = sequence;
    return true;
  }

  bool canForward(const char* id, uint32_t sequence, uint64_t token, uint32_t now) const {
    return armed && validID(id) && strcmp(id, driveSession) == 0 && sequence &&
      sequence == lastSequence && sequence > brakeSequence && validPermit(token, now) &&
      (!moving || uint32_t(now - lastDrive) < WATCHDOG_MS);
  }

  void forwarded(uint32_t now, bool nonzero) {
    if (!armed) return;
    moving = nonzero;
    lastDrive = now;
  }

  void brake() { moving = false; brakeSequence = lastSequence; }

  // True requests an immediate Stop; the Uno's 1.5 s fallback timer remains
  // independent. A later fresh command may resume the explicitly armed run.
  bool tick(uint32_t now) {
    if (moving && uint32_t(now - lastDrive) >= WATCHDOG_MS) {
      brake();
      return true;
    }
    return false;
  }

  void stop() {
    armed = moving = false;
    for (auto& p : permits) p = {};
    cursor = 0;
    // Retain the last session so reconnect cannot reopen that stopped session.
  }

  bool active() const { return armed; }
  const char* session() const { return driveSession; }
  uint32_t sequence() const { return lastSequence; }

 private:
  static constexpr size_t PERMIT_COUNT = 6;
  struct Permit { uint64_t token = 0; uint32_t issued = 0; };
  Permit permits[PERMIT_COUNT];
  size_t cursor = 0;
  bool online = false, armed = false, moving = false;
  char driveSession[33] = {};
  uint32_t lastSequence = 0, brakeSequence = 0, lastDrive = 0;

  bool validPermit(uint64_t token, uint32_t now) const {
    if (!online || !token) return false;
    for (const auto& p : permits)
      if (p.token == token && uint32_t(now - p.issued) <= PERMIT_MS) return true;
    return false;
  }

  static bool validID(const char* id) {
    if (!id || strlen(id) != 32) return false;
    for (size_t i = 0; i < 32; ++i)
      if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'A' && id[i] <= 'F'))) return false;
    return true;
  }
};
