#include "BridgeCore.h"
#include <assert.h>
#include <stdio.h>

constexpr auto ARM = "{\"N\":201,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000001\"}";
constexpr auto DRIVE = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":1500}";
constexpr auto FAST_DRIVE = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":180,\"T\":1500}";
constexpr auto LEFT_ARC = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":5,\"D2\":180,\"T\":1500}";
constexpr auto RIGHT_ARC = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":6,\"D2\":180,\"T\":1500}";
constexpr auto IDLE = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":2,\"D1\":0,\"D2\":0,\"T\":1500}";
constexpr auto RESUME = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000003\",\"S\":2,\"D1\":3,\"D2\":40,\"T\":1500}";
constexpr auto STOP = "{\"N\":100,\"H\":\"S\"}";
constexpr auto MANUAL = "{\"N\":2,\"H\":\"M\",\"D1\":3,\"D2\":40,\"T\":200}";

void feed(BridgeCore& b, const char* bytes, uint32_t now) {
  b.feed(reinterpret_cast<const uint8_t*>(bytes), strlen(bytes), now);
}
int command(const BridgeCore::Frame& frame) {
  StaticJsonDocument<256> doc;
  assert(!deserializeJson(doc, frame.bytes));
  return doc["N"];
}
void arm(BridgeCore& b) {
  BridgeCore::Frame out;
  b.session(true);
  assert(b.next(0, out) && command(out) == 100);
  assert(b.issuePermit(1, 1));
  feed(b, ARM, 2);
  assert(b.next(3, out) && command(out) == 100);
  assert(strcmp(out.notification, "{A0123456789ABCDEF0123456789ABCDEF}") == 0);
  assert(b.issuePermit(2, 4));
}

int main() {
  BridgeCore::Frame out;
  {
    BridgeCore b; arm(b);
    feed(b, FAST_DRIVE, 5);
    assert(b.next(6, out) && command(out) == 2);
    assert(strstr(out.bytes, "\"D2\":180") != nullptr);
  }
  for (const auto arc : {LEFT_ARC, RIGHT_ARC}) {
    BridgeCore b; arm(b);
    feed(b, arc, 5);
    assert(b.next(6, out) && command(out) == 4);
    StaticJsonDocument<256> motor;
    assert(!deserializeJson(motor, out.bytes));
    const bool left = strcmp(arc, LEFT_ARC) == 0;
    assert(motor["D1"].as<int>() == (left ? 180 : 90));
    assert(motor["D2"].as<int>() == (left ? 90 : 180));
    assert(b.next(1006, out) && command(out) == 100); // ESP brakes untimed Uno arc.
  }
  {
    BridgeCore b; arm(b);
    // The longer autonomy message also works at ATT MTU 23.
    const size_t length = strlen(DRIVE);
    for (size_t i = 0; i < length; i += 20)
      b.feed(reinterpret_cast<const uint8_t*>(DRIVE + i), length - i < 20 ? length - i : 20, 5 + i / 20);
    assert(b.next(20, out) && command(out) == 2);
    assert(out.autonomous && out.nonzero && out.sequence == 1);
    assert(strcmp(out.bytes, "{\"H\":\"M\",\"N\":2,\"D1\":3,\"D2\":40,\"T\":1500}") == 0);
    assert(!b.next(1019, out));
    assert(b.next(1020, out) && command(out) == 100);
    assert(!out.notification[0]); // ESP brakes after its 1 s command gap.
    assert(b.issuePermit(3, 1021));
    feed(b, RESUME, 1022);
    assert(b.next(1023, out) && command(out) == 2); // Fresh command resumes that arm.
  }
  {
    BridgeCore b; arm(b);
    feed(b, DRIVE, 470); // Permit still valid at input.
    assert(b.next(505, out) && command(out) == 100); // But expired before UART.
    assert(!out.notification[0]);
    assert(b.issuePermit(3, 506));
    feed(b, RESUME, 507);
    assert(b.next(508, out) && command(out) == 2);
  }
  {
    BridgeCore b; arm(b);
    feed(b, DRIVE, 5);
    assert(b.next(6, out) && command(out) == 2);
    feed(b, IDLE, 70);
    assert(b.next(71, out) && command(out) == 100);
    assert(out.autonomous && !out.nonzero && !out.notification[0]);
    assert(!b.next(1000, out)); // Intentional idle keeps the explicit arm.
    feed(b, STOP, 1001);
    assert(b.next(1002, out) && command(out) == 100);
    assert(strcmp(out.notification, "{ZS}") == 0);
    feed(b, MANUAL, 1003); // Stop is required before manual takeover.
    assert(b.next(1004, out) && command(out) == 2);
  }
  {
    BridgeCore b; arm(b);
    feed(b, MANUAL, 5);
    assert(b.next(6, out) && command(out) == 100);
    assert(strcmp(out.notification, "{X}") == 0);
  }
  for (const char* input : {
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":-1,\"D1\":3,\"D2\":40,\"T\":1500}",
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":181,\"T\":1500}",
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":200}",
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000000\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":1500}"}) {
    BridgeCore b; arm(b);
    feed(b, input, 5);
    assert(b.next(6, out) && command(out) == 100);
    assert(strcmp(out.notification, "{X}") == 0);
  }
  puts("Autonomous wire-to-UART framing, arm barrier, timed braking, resume and fail-closed checks passed.");
}
