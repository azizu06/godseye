// CPU/transfer costs of updating one observed region in a large retained map.
import { readFileSync } from "node:fs";
import { transform } from "esbuild";
import { execFileSync } from "node:child_process";
async function load(source) {
  const { code } = await transform(source, { loader: "ts", format: "esm" });
  return (
    await import(
      "data:text/javascript;base64," + Buffer.from(code).toString("base64")
    )
  ).SurfaceTiles;
}
const implementations = new Map();
const baseline = process.argv.indexOf("--baseline");
if (baseline !== -1) {
  const ref = process.argv[baseline + 1];
  if (!ref) throw Error("--baseline needs a git revision");
  implementations.set(
    ref,
    await load(
      execFileSync("git", ["show", `${ref}:dashboard/src/surfaceTiles.ts`], {
        encoding: "utf8",
      }),
    ),
  );
}
implementations.set(
  "working-tree",
  await load(
    readFileSync(new URL("../src/surfaceTiles.ts", import.meta.url), "utf8"),
  ),
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
function densePatch() {
  const positions = [],
    indices = [];
  for (let y = 0; y < 192; y++)
    for (let x = 0; x < 256; x++) {
      positions.push(
        (x - 128) * 0.01,
        (y - 96) * 0.01,
        -2 + 0.015 * Math.sin(x / 7),
      );
      if (x < 255 && y < 191) {
        const a = y * 256 + x;
        indices.push(a, a + 256, a + 1, a + 1, a + 256, a + 257);
      }
    }
  return {
    id: "dense",
    positions: new Float32Array(positions),
    indices: new Uint32Array(indices),
    colors: new Float32Array(positions.length).fill(0.5),
  };
}
const percentile = (samples, fraction) =>
  [...samples].sort((a, b) => a - b)[Math.floor(samples.length * fraction)];
for (const [label, SurfaceTiles] of implementations) {
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
  const dense = new SurfaceTiles(),
    input = densePatch(),
    denseTimes = [];
  dense.add(input);
  for (let i = 0; i < 40; i++) {
    input.colors.fill(0.4 + i * 0.001);
    const start = performance.now();
    dense.add(input);
    if (i >= 10) denseTimes.push(performance.now() - start);
  }
  console.log(
    JSON.stringify(
      {
        label,
        retainedTriangles: map.triangles,
        touchedTiles: touched.length,
        updateP95Ms: percentile(samples, 0.95),
        changedBytes,
        fullMapBytes: retainedBytes,
        denseTriangles: dense.triangles,
        denseUpdateP50Ms: percentile(denseTimes, 0.5),
        denseUpdateP95Ms: percentile(denseTimes, 0.95),
      },
      null,
      2,
    ),
  );
}
