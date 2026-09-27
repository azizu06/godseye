#include <assert.h>
#include <iostream>
#include "BridgeInbox.h"

int main() {
  BridgeCore bridge;
  BridgeInbox inbox;
  BridgeCore::Frame out;
  bridge.session(true);
  assert(bridge.next(0, out));
  const char* motion = "{\"N\":2,\"H\":\"M\",\"D1\":3,\"D2\":40,\"T\":200}";
  size_t size = strlen(motion);
  inbox.push(reinterpret_cast<const uint8_t*>(motion), 20, 1);
  inbox.push(reinterpret_cast<const uint8_t*>(motion + 20), size - 20, 2);
  assert(!bridge.next(3, out)); // callback does not parse or dispatch
  inbox.drain(bridge, 3);
  assert(bridge.next(3, out) && strstr(out.bytes, "\"N\":2"));
  for (int i = 0; i < 5; ++i) inbox.push(reinterpret_cast<const uint8_t*>(motion), size, 4);
  inbox.drain(bridge, 4);
  assert(bridge.next(4, out) && strstr(out.bytes, "100"));
  assert(!bridge.next(4, out));
  inbox.push(reinterpret_cast<const uint8_t*>(motion), size, 5);
  inbox.drain(bridge, 106);
  assert(bridge.next(106, out) && strstr(out.bytes, "100"));
  inbox.push(reinterpret_cast<const uint8_t*>(motion), size, 107);
  inbox.clear(); // disconnect erases old bytes
  inbox.drain(bridge, 108);
  assert(!bridge.next(108, out));
  inbox.push(reinterpret_cast<const uint8_t*>(motion), 300, 109); // oversize rejected before copy
  inbox.drain(bridge, 109);
  assert(bridge.next(109, out) && strstr(out.bytes, "100"));
  std::cout << "BLE callback inbox bounds, deferred parsing, expiry and disconnect clearing passed.\n";
}
