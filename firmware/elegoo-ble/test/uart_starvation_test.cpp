// Reproduction and fix verification for the armed-idle "Uno response lost"
// report (data/godseye-voice-qa/explore-03/uno-link-diagnosis.md). No
// hardware; a fake clock drives BridgeCore::next() exactly the way
// main.cpp's loop() paces real UART bytes at 9600 baud, so this exercises
// the real scheduling interaction, not just BridgeCore in isolation.
#include "BridgeCore.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

namespace {

int command(const BridgeCore::Frame& frame) {
  StaticJsonDocument<256> doc;
  assert(!deserializeJson(doc, frame.bytes));
  return doc["N"];
}

// Mirrors main.cpp's loop(): one frame per UART-free window, same pacing formula.
uint32_t simulate(BridgeCore& bridge, uint32_t startAt, uint32_t durationMs,
                   uint32_t driveEveryMs, bool& queryServiced) {
  uint32_t uartFreeAt = startAt;
  uint32_t lastDrive = startAt;
  uint32_t sequence = 1;
  queryServiced = false;
  for (uint32_t now = startAt; now < startAt + durationMs; ++now) {
    // Fresh, valid, on-time armed-idle zero-velocity frame every driveEveryMs,
    // standing in for the backend continuously holding the arm while blocked.
    if (driveEveryMs && (now - startAt) % driveEveryMs == 0) {
      char permitHex[17];
      snprintf(permitHex, sizeof(permitHex), "%016X", 0x1000 + sequence);
      bridge.issuePermit(0x1000ULL + sequence, now);
      char idle[192];
      snprintf(idle, sizeof(idle),
        "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"%s\",\"S\":%u,\"D1\":0,\"D2\":0,\"T\":1500}",
        permitHex, sequence);
      bridge.feed(reinterpret_cast<const uint8_t*>(idle), strlen(idle), now);
      ++sequence;
    }
    if (now - startAt == 5) {
      // The phone's own ~1 Hz feedback watchdog, fed once, shortly after arm.
      const char* query = "{\"H\":\"GE00000001\",\"N\":22,\"D1\":1}";
      bridge.feed(reinterpret_cast<const uint8_t*>(query), strlen(query), now);
    }
    if (int32_t(now - uartFreeAt) >= 0) {
      BridgeCore::Frame frame;
      if (bridge.next(now, frame)) {
        size_t size = strlen(frame.bytes);
        uartFreeAt = now + (size * 10000 + 9599) / 9600 + 2;
        StaticJsonDocument<256> doc;
        if (!deserializeJson(doc, frame.bytes) && doc["N"].is<int>() && doc["N"].as<int>() == 22) {
          queryServiced = true;
          return now - startAt;
        }
      }
    }
  }
  (void)lastDrive;
  return durationMs; // Never serviced within the window.
}

void arm(BridgeCore& b, uint32_t now) {
  BridgeCore::Frame out;
  b.session(true);
  assert(b.next(now, out));
  assert(b.issuePermit(1, now));
  const char* armMsg = "{\"N\":201,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000001\"}";
  b.feed(reinterpret_cast<const uint8_t*>(armMsg), strlen(armMsg), now + 1);
  assert(b.next(now + 2, out));
  assert(b.issuePermit(2, now + 3));
}

}  // namespace

int main() {
  // Fix verification: continuous armed-idle zero-velocity frames every
  // 20 ms (matching the phone's own 50 Hz tick, well inside the ESP's own
  // 20 Hz/50 ms permit cadence) must no longer starve the feedback query
  // past its bounded fairness window, comfortably inside the phone's 2.5 s
  // watchdog (data/godseye-voice-qa/explore-03/uno-link-diagnosis.md).
  {
    BridgeCore b;
    arm(b, 0);
    bool serviced = false;
    uint32_t at = simulate(b, 10, 2600, 20, serviced);
    printf("continuous 20ms drive: query serviced=%d at=%ums\n", serviced, at);
    assert(serviced && at < 400); // QUERY_FAIRNESS_MS=250 plus one UART slot.
  }
  // Counterfactual: same arm, same single query, but no competing motion
  // traffic at all -- the query must be serviced promptly.
  {
    BridgeCore b;
    arm(b, 0);
    bool serviced = false;
    uint32_t at = simulate(b, 10, 2600, 0, serviced);
    printf("no drive traffic: query serviced=%d at=%ums\n", serviced, at);
    assert(serviced && at < 200);
  }
  // Counterfactual: sparse drive traffic (every 600 ms, well under 2.5 s
  // apart) leaves UART room between frames -- the query must still get through.
  {
    BridgeCore b;
    arm(b, 0);
    bool serviced = false;
    uint32_t at = simulate(b, 10, 2600, 600, serviced);
    printf("sparse 600ms drive: query serviced=%d at=%ums\n", serviced, at);
    assert(serviced && at < 2500);
  }
  // Safety regression: an explicit Stop must still preempt everything
  // immediately, even while a query has been starving -- fairness never
  // outranks Stop.
  {
    BridgeCore b;
    arm(b, 0);
    BridgeCore::Frame out;
    const char* query = "{\"H\":\"GE00000001\",\"N\":22,\"D1\":1}";
    b.feed(reinterpret_cast<const uint8_t*>(query), strlen(query), 10);
    const char* idle = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\","
                        "\"C\":\"0000000000001002\",\"S\":1,\"D1\":0,\"D2\":0,\"T\":1500}";
    b.issuePermit(0x1002, 10);
    b.feed(reinterpret_cast<const uint8_t*>(idle), strlen(idle), 10);
    // Query has been waiting 260ms (past QUERY_FAIRNESS_MS) when Stop arrives.
    const char* stop = "{\"H\":\"S\",\"N\":100}";
    b.feed(reinterpret_cast<const uint8_t*>(stop), strlen(stop), 270);
    assert(b.next(270, out) && command(out) == 100);
    printf("stop preempts starving query: command=%d\n", command(out));
  }
  // Safety regression: a late (>100ms) autonomous motion frame is still
  // braked/rejected exactly as before, regardless of query fairness state.
  {
    BridgeCore b;
    arm(b, 0);
    BridgeCore::Frame out;
    const char* query = "{\"H\":\"GE00000001\",\"N\":22,\"D1\":1}";
    b.feed(reinterpret_cast<const uint8_t*>(query), strlen(query), 10);
    b.issuePermit(0x1003, 10);
    const char* drive = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\","
                         "\"C\":\"0000000000001003\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":1500}";
    b.feed(reinterpret_cast<const uint8_t*>(drive), strlen(drive), 10);
    // Motion frame goes stale (>100ms) before being serviced.
    assert(b.next(150, out) && command(out) == 100); // Braked, not forwarded.
    printf("stale motion braked regardless of query state: command=%d\n", command(out));
  }
  puts("UART starvation fix verified: continuous armed-idle motion traffic no "
       "longer starves the feedback query past its bounded fairness window; "
       "Stop and staleness braking are unaffected.");
}
