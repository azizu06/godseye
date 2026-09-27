#include "BridgeCore.h"
#include <assert.h>
#include <stdio.h>

constexpr auto ARM = "{\"N\":201,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000001\"}";
constexpr auto DRIVE = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":200}";
constexpr auto IDLE = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":2,\"D1\":0,\"D2\":0,\"T\":200}";
constexpr auto RESUME = "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000003\",\"S\":2,\"D1\":3,\"D2\":40,\"T\":200}";
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
    // The longer autonomy message also works at ATT MTU 23.
    const size_t length = strlen(DRIVE);
    for (size_t i = 0; i < length; i += 20)
      b.feed(reinterpret_cast<const uint8_t*>(DRIVE + i), length - i < 20 ? length - i : 20, 5 + i / 20);
    assert(b.next(20, out) && command(out) == 2);
    assert(out.autonomous && out.nonzero && out.sequence == 1);
    assert(strcmp(out.bytes, "{\"H\":\"M\",\"N\":2,\"D1\":3,\"D2\":40,\"T\":200}") == 0);
    assert(!b.next(219, out));
    assert(b.next(220, out) && command(out) == 100);
    assert(!out.notification[0]); // Stop on the existing 200 ms deadline.
    assert(b.issuePermit(3, 221));
    feed(b, RESUME, 222);
    assert(b.next(223, out) && command(out) == 2); // Fresh command resumes that arm.
  }
  {
    BridgeCore b; arm(b);
    feed(b, DRIVE, 220); // Permit still valid at input.
    assert(b.next(255, out) && command(out) == 100); // But expired before UART.
    assert(!out.notification[0]);
    assert(b.issuePermit(3, 256));
    feed(b, RESUME, 257);
    assert(b.next(258, out) && command(out) == 2);
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
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":-1,\"D1\":3,\"D2\":40,\"T\":200}",
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":81,\"T\":200}",
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000002\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":1000}",
      "{\"N\":202,\"H\":\"0123456789ABCDEF0123456789ABCDEF\",\"C\":\"0000000000000000\",\"S\":1,\"D1\":3,\"D2\":40,\"T\":200}"}) {
    BridgeCore b; arm(b);
    feed(b, input, 5);
    assert(b.next(6, out) && command(out) == 100);
    assert(strcmp(out.notification, "{X}") == 0);
  }
  puts("Autonomous wire-to-UART framing, arm barrier, timed braking, resume and fail-closed checks passed.");
}
