import { describe, expect, it } from "vitest";
import { parseMessage, type Occupancy } from "./protocol";
import { emptyMission, reconnectMission, reduceMessage } from "./state";
import { OVERLAY_OFFSET, displayFloorY, overlayY } from "./floor";

const scope = (session: string) => ({ session_id: session, map_epoch: 1 });
const occupancy = (
  extra: Record<string, unknown> = {},
  session = "hall",
): Record<string, unknown> => ({
  version: 1,
  type: "occupancy",
  ...scope(session),
  origin: [-1, -1],
  cell_m: 0.05,
  width: 2,
  height: 2,
  cells: btoa("\u0001\u0001\u0002\u0000"),
  ...extra,
});
const grid = (floor_y: unknown) =>
  ({ ...occupancy(), floor_y }) as unknown as Occupancy;

describe("confirmed display floor", () => {
  it("uses a finite occupancy floor, including zero, negative and positive values", () => {
    expect(displayFloorY(grid(0))).toBe(0);
    expect(displayFloorY(grid(-0.35))).toBe(-0.35);
    expect(displayFloorY(grid(0.42))).toBe(0.42);
  });

  it("reports unknown instead of guessing for missing, null, non-finite or out-of-map floors", () => {
    expect(displayFloorY(null)).toBeNull();
    expect(displayFloorY({ ...grid(0), floor_y: undefined })).toBeNull();
    for (const bad of [null, NaN, Infinity, -Infinity, "-0.35", 4, -4.01])
      expect(displayFloorY(grid(bad))).toBeNull();
  });

  it("places every overlay at the same floor plus its existing small visual offset", () => {
    for (const floor of [0, -0.35, 0.42]) {
      expect(overlayY(floor, "grid")).toBeCloseTo(floor - 0.025);
      expect(overlayY(floor, "rover")).toBeCloseTo(floor + 0.1);
      // A click on the picking plane and the ring drawn for it share one floor.
      expect(overlayY(floor, "approach") - overlayY(floor, "pick")).toBeCloseTo(
        OVERLAY_OFFSET.approach - OVERLAY_OFFSET.pick,
      );
    }
    // Unknown keeps the previous AR-origin placement rather than inventing a floor.
    expect(overlayY(null, "rover")).toBe(OVERLAY_OFFSET.rover);
  });

  it("parses the additive floor without rejecting grids whose floor is unknown", () => {
    const withFloor = parseMessage(occupancy({ floor_y: -0.35 }));
    expect(withFloor?.type === "occupancy" && withFloor.floor_y).toBe(-0.35);
    const withoutFloor = parseMessage(occupancy());
    expect(withoutFloor?.type).toBe("occupancy");
    expect(
      displayFloorY(withoutFloor?.type === "occupancy" ? withoutFloor : null),
    ).toBeNull();
    const garbage = parseMessage(occupancy({ floor_y: "low" }));
    expect(garbage?.type).toBe("occupancy");
    expect(
      displayFloorY(garbage?.type === "occupancy" ? garbage : null),
    ).toBeNull();
  });

  it("keeps a same-map floor across reconnect but never leaks it into a new map", () => {
    let state = reduceMessage(
      emptyMission(),
      parseMessage(occupancy({ floor_y: -0.35 }))!,
    );
    expect(displayFloorY(state.occupancy)).toBe(-0.35);
    state = reconnectMission(state);
    expect(displayFloorY(state.occupancy)).toBe(-0.35);
    // New session/epoch: the old map's floor must not carry over.
    state = reduceMessage(
      state,
      parseMessage({
        version: 1,
        type: "objects",
        ...scope("next-session"),
        objects: [],
      })!,
    );
    expect(state.occupancy).toBeNull();
    expect(displayFloorY(state.occupancy)).toBeNull();
    state = reduceMessage(
      state,
      parseMessage(occupancy({ floor_y: 0.2 }, "next-session"))!,
    );
    expect(displayFloorY(state.occupancy)).toBe(0.2);
  });
});
