// CPU/transfer costs of updating one observed region in a large retained map.
import { readFileSync } from "node:fs";
import { transform } from "esbuild";
const { code } = await transform(
  readFileSync(new URL("../src/surfaceTiles.ts", import.meta.url), "utf8"),
  { loader: "ts", format: "esm" },
);
const { SurfaceTiles } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64")
);
function patch(id, color = 0.5) {
  const positions = [],
    indices = [];
  for (let y = 0; y < 16; y++)
    for (let x = 0; x < 16; x++) {
      positions.push(
        (id % 40) * 2 + x * 0.05 + 0.01,
        0.1,
        Math.floor(id / 40) * 2 + y * 0.05 + 0.01,
      );
      if (x < 15 && y < 15) {
        const a = y * 16 + x;
        indices.push(a, a + 16, a + 1, a + 1, a + 16, a + 17);
      }
    }
  return {
    id: String(id),
    positions: new Float32Array(positions),
    indices: new Uint32Array(indices),
    colors: new Float32Array(positions.length).fill(color),
  };
}
const bytes = (tile) =>
  tile.positions.byteLength + tile.colors.byteLength + tile.indices.byteLength;
const map = new SurfaceTiles();
let retainedBytes = 0;
for (let i = 0; i < 1000; i++)
  retainedBytes += map
    .add(patch(i))
    .reduce((sum, tile) => sum + bytes(tile), 0);
const samples = [];
let touched = [],
  changedBytes = 0;
for (let i = 0; i < 100; i++) {
  const input = patch(0, 0.6 + i * 0.001),
    start = performance.now();
  touched = map.add(input);
  samples.push(performance.now() - start);
  changedBytes = touched.reduce((sum, tile) => sum + bytes(tile), 0);
}
console.log(
  JSON.stringify(
    {
      retainedTriangles: map.triangles,
      touchedTiles: touched.length,
      updateP95Ms: samples.sort((a, b) => a - b)[95],
      changedBytes,
      fullMapBytes: retainedBytes,
    },
    null,
    2,
  ),
);
