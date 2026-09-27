import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { buildResponderRover, ROVER_FOOTPRINT } from "./roverModel";

const rover = buildResponderRover();
const box = (key: Parameters<typeof rover.get>[0]) =>
  rover.get(key)!.boundingBox!;

describe("responder rover model", () => {
  it("stays inside the footprint outline on X and Z", () => {
    const all = new THREE.Box3();
    for (const g of rover.values()) all.union(g.boundingBox!);
    expect(all.min.x).toBeGreaterThanOrEqual(-ROVER_FOOTPRINT.width / 2);
    expect(all.max.x).toBeLessThanOrEqual(ROVER_FOOTPRINT.width / 2);
    expect(all.min.z).toBeGreaterThanOrEqual(-ROVER_FOOTPRINT.length / 2);
    expect(all.max.z).toBeLessThanOrEqual(ROVER_FOOTPRINT.length / 2);
    // Tyres sit on the outline floor; nothing rises past the old phone plate.
    expect(all.min.y).toBeCloseTo(-ROVER_FOOTPRINT.height / 2, 6);
    expect(all.max.y).toBeLessThan(0.16);
  });

  it("has finite, non-empty geometry", () => {
    for (const g of rover.values()) {
      const position = g.getAttribute("position").array;
      expect(position.length).toBeGreaterThan(0);
      expect(position.every(Number.isFinite)).toBe(true);
      expect(g.getAttribute("normal").array.every(Number.isFinite)).toBe(true);
    }
  });

  it("faces +Z: sensor eyes forward, battery pack behind", () => {
    expect(box("servo").min.z).toBeGreaterThan(0.1);
    expect(box("silver").max.z).toBeGreaterThan(0.16);
    expect(box("beaconRed").max.z).toBeLessThan(0);
    expect(box("tire").min.z).toBeCloseTo(-box("tire").max.z, 6);
    expect(box("tire").min.x).toBeCloseTo(-box("tire").max.x, 6);
  });

  it("merges parts into one draw per material", () => {
    expect(rover.size).toBeLessThanOrEqual(13);
    for (const g of rover.values()) expect(g.groups).toHaveLength(0);
  });
});
