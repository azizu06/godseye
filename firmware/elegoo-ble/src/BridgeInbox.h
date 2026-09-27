#pragma once
#include <stdint.h>
#include <string.h>
#include "BridgeCore.h"

// BLE's BTC_TASK has a small stack. Its callback only copies bounded bytes;
// JSON parsing/serialization runs on loop(), including on malformed input.
// Call under the same mutex as BridgeCore. Overflow/old bytes become Stop.
class BridgeInbox {
 public:
  void clear() { head = count = 0; failed = false; }
  void push(const uint8_t* data, size_t size, uint32_t now) {
    if (failed) return;
    if (size > sizeof(slots[0].bytes) || count == 4) {
      failed = true;
      head = count = 0;
      return;
    }
    Slot& slot = slots[(head + count) % 4];
    memcpy(slot.bytes, data, size);
    slot.size = size;
    slot.received = now;
    ++count;
  }
  void drain(BridgeCore& bridge, uint32_t now) {
    if (failed) { clear(); bridge.inputFailure(); return; }
    while (count) {
      Slot& slot = slots[head];
      if (uint32_t(now - slot.received) > 100) {
        clear(); bridge.inputFailure(); return;
      }
      bridge.feed(slot.bytes, slot.size, now);
      head = (head + 1) % 4;
      --count;
    }
  }
 private:
  struct Slot { uint8_t bytes[256]; size_t size = 0; uint32_t received = 0; } slots[4];
  size_t head = 0, count = 0;
  bool failed = false;
};
