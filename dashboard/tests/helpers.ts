import type { Page, WebSocketRoute } from "@playwright/test";

export async function workspace(page: Page) {
  if (await page.getByRole("navigation", { name: "Workspace panels" }).count())
    return;
  const back = page.getByRole("button", {
    name: "Workspace menu",
    exact: true,
  });
  if (await back.count()) await back.click();
  else {
    // The launcher is intentionally visible only while keyboard-focused.
    const launcher = page.getByRole("button", {
      name: "Open workspace",
      exact: true,
    });
    await launcher.focus();
    await launcher.press("Enter");
  }
}

export async function closeWorkspace(page: Page) {
  const close = page.getByRole("button", { name: "Close panel", exact: true });
  if (await close.count()) await close.click();
}

export async function workspaceAction(page: Page, name: string) {
  await workspace(page);
  await page.getByRole("button", { name, exact: true }).click();
}

export async function panel(
  page: Page,
  name:
    | "Spatial memory"
    | "Object intelligence"
    | "Recent activity"
    | "Rover controls"
    | "Scene settings",
) {
  if (
    await page
      .getByRole("dialog", { name: `${name} panel`, exact: true })
      .count()
  )
    return;
  await workspace(page);
  await page
    .getByRole("navigation", { name: "Workspace panels" })
    .getByRole("button", { name, exact: true })
    .click();
}

// Synthetic motion lives only in the test harness. The shipped application
// receives these observations and commands over the public external protocols.
export async function connectObservedFeed(page: Page) {
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9877/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9877");
  await page.getByLabel("Enable REST commands").check();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
}

export async function observedFeed(page: Page) {
  const { Simulator } = await import("./fixtures/simulator");
  const { capture } = await import("./captureFixture");
  const fixture = new Simulator();
  fixture.health.stop_reason = null;
  let epoch = 1;
  let connected = true;
  const sockets = new Set<WebSocketRoute>();
  const scope = () => ({ session_id: "surface-room", map_epoch: epoch });
  const publish = () => {
    if (!connected) return;
    for (const message of fixture.snapshot(true))
      for (const socket of sockets)
        socket.send(JSON.stringify({ ...message, ...scope() }));
  };
  await page.routeWebSocket("ws://localhost:8765/live", (socket) =>
    socket.close(),
  );
  await page.route("http://localhost:8765/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.routeWebSocket("ws://localhost:9877/live", (socket) => {
    if (!connected) {
      socket.close();
      return;
    }
    sockets.add(socket);
    publish();
    socket.onClose(() => sockets.delete(socket));
  });
  await page.route("http://localhost:9877/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/capture/status")
      return route.fulfill({
        json: connected
          ? {
              tracking: "normal",
              frame: { capture_id: `fixture-${epoch}`, age_ms: 10 },
            }
          : { tracking: "unavailable", frame: null },
      });
    if (path === "/capture/frame.bin")
      return route.fulfill({
        body: capture(epoch),
        contentType: "application/octet-stream",
      });
    if (path === "/events")
      return route.fulfill({ json: { version: 1, ...scope(), events: [] } });
    if (path === "/baselines")
      return route.fulfill({ json: { version: 1, ...scope(), baselines: [] } });
    try {
      const result = fixture.command(
        path,
        route.request().postDataJSON() ?? {},
      );
      if (path === "/session") epoch++;
      publish();
      return route.fulfill({ json: { ...result, ...scope() } });
    } catch (error) {
      return route.fulfill({ status: 409, json: { detail: String(error) } });
    }
  });
  const timer = setInterval(() => {
    if (connected) {
      fixture.tick(0.1);
      publish();
    }
  }, 100);
  page.on("close", () => clearInterval(timer));
  await page.goto("/");
  await workspaceAction(page, "Connection settings");
  await connectObservedFeed(page);
  await closeWorkspace(page);
  await page.getByRole("button", { name: "3D", exact: true }).click();
  return {
    fixture,
    publish,
    disconnect() {
      connected = false;
      for (const socket of sockets) socket.close();
      sockets.clear();
    },
  };
}
