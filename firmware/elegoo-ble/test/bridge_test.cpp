#include "BridgeCore.h"
#include <assert.h>
#include <stdio.h>

void feed(BridgeCore& b, const char* text, uint32_t now) {
  b.feed(reinterpret_cast<const uint8_t*>(text), strlen(text), now);
}
int command(const BridgeCore::Frame& frame) {
  StaticJsonDocument<256> doc;
  assert(!deserializeJson(doc, frame.bytes));
  return doc["N"];
}
void ready(BridgeCore& b, uint32_t now = 0) {
  b.session(true);
  BridgeCore::Frame out;
  assert(b.next(now, out) && command(out) == 100);
}
constexpr auto FORWARD = "{\"H\":\"M\",\"N\":2,\"D1\":3,\"D2\":60,\"T\":200}";
constexpr auto RIGHT = "{\"H\":\"M\",\"N\":2,\"D1\":2,\"D2\":60,\"T\":200}";
constexpr auto QUERY = "{\"H\":\"GE123\",\"N\":22,\"D1\":1}";

int main() {
  BridgeCore::Frame out;
  {
    BridgeCore b; ready(b);
    // Default ATT MTU fragments and coalesced input are both accepted.
    const size_t n = strlen(FORWARD);
    b.feed(reinterpret_cast<const uint8_t*>(FORWARD), 20, 10);
    assert(!b.next(15, out));
    b.feed(reinterpret_cast<const uint8_t*>(FORWARD + 20), n - 20, 20);
    feed(b, QUERY, 21);
    assert(b.next(22, out) && command(out) == 2);
    assert(b.next(65, out) && command(out) == 22);
    assert(strstr(out.bytes, "GE123"));
    assert(!b.next(221, out));
    assert(b.next(222, out) && command(out) == 100);
  }
  {
    BridgeCore b; ready(b);
    feed(b, FORWARD, 1); feed(b, RIGHT, 2);
    assert(b.next(3, out) && strstr(out.bytes, "\"D1\":2"));
    assert(!b.next(4, out)); // Latest input only, no movement backlog.
    feed(b, FORWARD, 5); feed(b, "{\"H\":\"S\",\"N\":100}", 6);
    assert(b.next(7, out) && command(out) == 100);
    assert(!b.next(8, out));
  }
  {
    BridgeCore b; ready(b);
    feed(b, FORWARD, 1);
    assert(b.next(102, out) && command(out) == 100); // Stale before dispatch.
    assert(!b.next(103, out));
    feed(b, FORWARD, 110);
    b.session(false);
    assert(b.next(111, out) && command(out) == 100);
    feed(b, FORWARD, 112); // Disconnected input ignored.
    assert(!b.next(113, out));
    ready(b, 120);
    assert(!b.next(121, out)); // Reconnect never replays motion.
  }
  for (auto invalid : {
      "{\"H\":\"M\",\"N\":3,\"D1\":3,\"D2\":60}",
      "{\"H\":\"M\",\"N\":2,\"D1\":3,\"D2\":255,\"T\":200}",
      "{\"H\":\"M\",\"N\":2,\"D1\":3,\"D2\":60,\"T\":0}",
      "{\"H\":\"M\",\"N\":2,\"D1\":0,\"D2\":60,\"T\":200}",
      "{\"H\":\"bad_id\",\"N\":22,\"D1\":1}",
      "{\"H\":\"M\",\"N\":21,\"D1\":2}",
      "{\"H\":\"M\",\"N\":2,\"D1\":3,\"D2\":1.5,\"T\":200}"}) {
    BridgeCore b; ready(b);
    feed(b, FORWARD, 1); feed(b, invalid, 2);
    assert(b.next(3, out) && command(out) == 100);
    assert(!b.next(4, out));
  }
  {
    BridgeCore b; ready(b);
    feed(b, "{\"H\":", 1);
    assert(b.next(152, out) && command(out) == 100);
    feed(b, "\"M\",\"N\":2,\"D1\":3,\"D2\":60,\"T\":200}", 153);
    assert(!b.next(154, out));
    char huge[200]; memset(huge, 'A', sizeof(huge)); huge[0] = '{';
    b.feed(reinterpret_cast<uint8_t*>(huge), sizeof(huge), 155);
    assert(b.next(156, out) && command(out) == 100);
    feed(b, QUERY, 157);
    assert(b.next(158, out) && command(out) == 22);
  }
  {
    BridgeCore b; ready(b, UINT32_MAX - 50);
    feed(b, FORWARD, UINT32_MAX - 40);
    assert(b.next(UINT32_MAX - 30, out) && command(out) == 2);
    assert(!b.next(168, out));
    assert(b.next(169, out) && command(out) == 100);
  }
  puts("BLE bridge parser, bounded queue, stop, watchdog, reconnect and millis-wrap checks passed.");
}
