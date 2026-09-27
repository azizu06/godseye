import { expect, test } from "@playwright/test";
import { JPEG } from "./captureFixture";

// Recorded-shape detector output for the 80x60 fixture frame; the harness is the
// backend here, the shipped app only receives and renders it.
const scope = { session_id: "room", map_epoch: 1 };
const detections = (frame_id: number, boxes: unknown[]) => ({
  version: 1,
  type: "detections",
  ...scope,
  frame_id,
  t_capture: frame_id,
  t_wall_ms: Date.now(),
  image: { width: 80, height: 60 },
  source: "backend_detector",
  classes: ["person", "backpack", "chair", "bottle"],
  detections: boxes,
});
const person = {
  class: "person",
  confidence: 0.87,
  box: [40, 10, 56, 50],
  position: [-1, 0.2, 2.6],
  depth_m: 2,
  object_id: "person-1",
};
const backpack = {
  class: "backpack",
  confidence: 0.71,
  box: [4, 24, 16, 36],
  position: null,
  depth_m: null,
  object_id: null,
};

test("received detections draw on their own frame, place only measured boxes, and retire when stale or reset", async ({
  page,
}) => {
  let send: (value: unknown) => void = () => {};
  const requested: string[] = [];
  await page.routeWebSocket("ws://localhost:9879/live", (ws) => {
    send = (value) => ws.send(JSON.stringify(value));
    send({
      version: 1,
      type: "objects",
      ...scope,
      objects: [
        {
          id: "person-1",
          class: "person",
          position: [-1, 0.2, 2.6],
          confidence: 0.87,
          first_seen: 1,
          last_seen: 1,
          observations: 1,
          state: "present",
        },
      ],
    });
    send(detections(7, [person, backpack]));
  });
  await page.route("http://localhost:9879/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/capture/detections.jpg")
      return route.fulfill({ status: 404 });
    requested.push(url.search);
    // Only frame 7's image exists; any other frame is refused, never substituted.
    return url.searchParams.get("frame_id") === "7"
      ? route.fulfill({ body: JPEG, contentType: "image/jpeg" })
      : route.fulfill({ status: 409 });
  });
  await page.goto("/?live=ws%3A%2F%2Flocalhost%3A9879%2Flive");

  const panel = page.getByTestId("detection-panel");
  await expect(panel).toContainText("Received detections");
  await expect(panel).toContainText("Backend detector · phone frame #7");
  await expect(page.getByTestId("detection-status")).toContainText("LIVE");
  const image = panel.getByRole("img", {
    name: "Phone frame #7 analysed by the backend detector",
  });
  await expect(image).toBeVisible();
  const boxes = panel.getByTestId("detection-box");
  await expect(boxes).toHaveCount(2);
  await expect(boxes.first()).toHaveText("Person 87%");
  await expect(boxes.first()).toHaveAttribute("style", /left: 50%/);
  await expect(panel).toContainText("3D placed · 2.00 m depth");
  await expect(panel).toContainText("2D only · no reliable depth");
  // Only the depth-measured person appears in the spatial view.
  const live = page.getByTestId("live-detection-label");
  await expect(live).toHaveCount(1);
  await expect(live).toHaveText("LIVE · Person 87%");
  expect(requested).toEqual(["?session_id=room&map_epoch=1&frame_id=7"]);

  // A newer frame without depth and without an available image: the list
  // updates, frame 7's image keeps only frame 7's boxes, and nothing is placed.
  send(detections(8, [{ ...person, position: null, depth_m: null }]));
  await expect(panel).toContainText("phone frame #8");
  await expect(image).toBeVisible();
  await expect(boxes).toHaveCount(2);
  await expect(live).toHaveCount(0);

  send(detections(9, [person]));
  await expect(live).toHaveCount(1);
  await expect(page.getByTestId("detection-status")).toContainText("STALE", {
    timeout: 8000,
  });
  await expect(live).toHaveCount(0);

  // A new map epoch retires the previous map's detections entirely.
  send({
    version: 1,
    type: "objects",
    session_id: "room",
    map_epoch: 2,
    objects: [],
  });
  await expect(panel).toHaveCount(0);
  await expect(live).toHaveCount(0);
});
