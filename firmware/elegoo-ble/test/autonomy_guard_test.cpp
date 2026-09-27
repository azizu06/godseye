#include "AutonomyGuard.h"
#include <assert.h>
#include <stdio.h>

constexpr auto A = "0123456789ABCDEF0123456789ABCDEF";
constexpr auto B = "1123456789ABCDEF0123456789ABCDEF";

void arm(AutonomyGuard& guard, uint32_t now = 0) {
  guard.connection(true);
  assert(guard.issue(1, now));
  assert(guard.arm(A, 1, now));
  assert(guard.active());
  assert(guard.issue(2, now + 1));
}

int main() {
  {
    AutonomyGuard g;
    assert(!g.issue(1, 0));
    assert(!g.arm(A, 1, 0));
    arm(g);
    assert(!g.canForward(A, 0, 2, 2));
    assert(g.accept(A, 1, 2, 2));
    assert(g.canForward(A, 1, 2, 500));
    assert(!g.canForward(A, 1, 2, 502)); // Valid at receipt, expired before UART.
    assert(g.issue(3, 240));
    assert(g.accept(A, 2, 3, 241));
    assert(!g.canForward(A, 1, 2, 242)); // A newer command superseded the old one.
    assert(g.canForward(A, 2, 3, 242));
    g.forwarded(242, true);
    assert(!g.tick(1241));
    assert(g.tick(1242));
    assert(g.active());
    assert(!g.canForward(A, 2, 3, 443));
    assert(g.issue(4, 444));
    assert(g.accept(A, 3, 4, 445)); // Fresh command may resume after Stop.
    g.stop();
    assert(g.issue(5, 446));
    assert(g.arm(B, 5, 447));
  }
  {
    AutonomyGuard g; arm(g);
    assert(!g.accept(A, 1, 1, 2)); // Arm barrier invalidates earlier permits.
    assert(g.active());
    assert(g.issue(3, 3));
    assert(g.accept(A, 2, 3, 4));
  }
  {
    AutonomyGuard g; arm(g);
    assert(g.accept(A, 1, 2, 2));
    assert(!g.accept(A, 1, 2, 3)); // Duplicate/out-of-order command latches Stop.
    assert(!g.active());
  }
  {
    AutonomyGuard g; arm(g);
    assert(!g.accept(B, 2, 2, 2));
    assert(!g.active());
  }
  {
    AutonomyGuard g; arm(g);
    assert(g.accept(A, 1, 2, 2));
    g.forwarded(3, true);
    assert(g.issue(3, 1002));
    assert(!g.canForward(A, 1, 2, 1003));
    assert(g.accept(A, 2, 3, 1003)); // Old movement stopped; fresh command may resume.
    assert(g.active());
  }
  {
    AutonomyGuard g; arm(g);
    assert(g.accept(A, 1, 2, 2));
    g.forwarded(3, false);
    assert(!g.tick(1000)); // An explicit idle zero does not expire a stopped arm.
    assert(g.active());
    g.connection(false);
    assert(!g.active() && !g.issue(3, 1001));
    g.connection(true);
    assert(g.issue(4, 1002));
    assert(!g.arm(A, 4, 1003)); // Reconnect needs a different drive session.
  }
  {
    AutonomyGuard g; arm(g, UINT32_MAX - 100);
    assert(g.accept(A, 1, 2, UINT32_MAX - 90));
    assert(g.canForward(A, 1, 2, 400));
    assert(!g.canForward(A, 1, 2, 402));
    g.forwarded(UINT32_MAX - 80, true);
    assert(!g.tick(918));
    assert(g.tick(919));
  }
  {
    AutonomyGuard g; g.connection(true);
    assert(!g.issue(0, 0));
    assert(g.issue(1, 0));
    assert(!g.issue(1, 1)); // Never refresh a permit by reissuing its value.
    assert(!g.arm("INVALID", 1, 1));
    assert(g.issue(2, 2));
    assert(!g.arm(A, 2, 503));
  }
  puts("Autonomy permits, session barrier, replay rejection, timed braking and fresh resume checks passed.");
}
