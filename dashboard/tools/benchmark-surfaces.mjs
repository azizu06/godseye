// CPU/transfer costs of updating one observed region in a large retained map.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, basename } from "node:path";
import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
async function load(source, exported = "SurfaceTiles") {
  const result = await build({
    stdin: {
      contents: source,
      loader: "ts",
      resolveDir: fileURLToPath(new URL("../src", import.meta.url)),
    },
    bundle: true,
    write: false,
    format: "esm",
    platform: "node",
  });
  const code = result.outputFiles[0].text;
  return (
    await import(
      "data:text/javascript;base64," + Buffer.from(code).toString("base64")
    )
  )[exported];
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
  tile.positions.byteLength +
  tile.colors.byteLength +
  tile.indices.byteLength +
  (tile.spans?.byteLength ?? 0);
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
      .reduce(
        (sum, tile) => sum + bytes(tile) - (tile.spans?.byteLength ?? 0),
        0,
      );
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
  input.colors.fill(0.9);
  const smallObservationBytes = dense
    .add({ ...input, indices: input.indices.slice(0, 3) })
    .reduce((sum, tile) => sum + bytes(tile), 0);
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
        smallObservationBytes,
      },
      null,
      2,
    ),
  );
}

// Optional local-only replay: never connects to a backend or publishes imagery.
const captureArg = process.argv.indexOf("--captures");
if (captureArg !== -1) {
  const folder = process.argv[captureArg + 1];
  if (!folder) throw Error("--captures needs a capture directory");
  const decode = await load(
    readFileSync(new URL("../src/captureSurface.ts", import.meta.url), "utf8"),
    "decodeCaptureSurface",
  );
  const TileBuffer = await load(
    readFileSync(
      new URL("../src/surfaceTileBuffer.ts", import.meta.url),
      "utf8",
    ),
    "SurfaceTileBuffer",
  );
  const files = readdirSync(folder, { recursive: true })
    .filter((p) => basename(p).startsWith("frame-") && p.endsWith(".capture"))
    .map((p) => ({
      path: join(folder, p),
      time: statSync(join(folder, p)).mtimeMs,
    }))
    .sort((a, b) => b.time - a.time)
    .slice(0, 60)
    .reverse();
  const frames = [],
    skipped = {};
  for (const file of files) {
    try {
      const bytes = readFileSync(file.path);
      const frame = decode(
        bytes.buffer.slice(
          bytes.byteOffset,
          bytes.byteOffset + bytes.byteLength,
        ),
      );
      // Isolate geometry costs; actual image decoding/color baking is separate.
      frames.push({
        ...frame,
        colors: new Float32Array(frame.positions.length).fill(0.5),
      });
    } catch (error) {
      skipped[error.message] = (skipped[error.message] ?? 0) + 1;
    }
  }
  let expected;
  for (const [label, SurfaceTiles] of implementations) {
    let map,
      identity,
      mirrors = new Map(),
      transferBytes = 0,
      maxUpdateBytes = 0;
    const hashes = [],
      times = [],
      mirrorTimes = [];
    for (const frame of frames) {
      const next = JSON.stringify([frame.sessionId, frame.mapEpoch]);
      if (identity !== next) {
        map = new SurfaceTiles();
        identity = next;
        mirrors = new Map();
      }
      const start = performance.now();
      const updates = map.add(frame);
      times.push(performance.now() - start);
      const size = updates.reduce((sum, u) => sum + bytes(u), 0);
      transferBytes += size;
      maxUpdateBytes = Math.max(maxUpdateBytes, size);
      const hash = createHash("sha256"),
        mirrorStart = performance.now();
      for (const update of updates)
        if (update.spans) {
          const buffer = mirrors.get(update.id) ?? new TileBuffer(update.id);
          buffer.apply(update);
          mirrors.set(update.id, buffer);
        }
      mirrorTimes.push(performance.now() - mirrorStart);
      for (const update of updates.sort((a, b) => a.id.localeCompare(b.id))) {
        hash.update(update.id);
        const buffer = mirrors.get(update.id);
        const arrays = buffer
          ? [
              buffer.positions.subarray(0, buffer.vertexCount * 3),
              buffer.colors.subarray(0, buffer.vertexCount * 3),
              buffer.indices.subarray(0, buffer.indexCount),
            ]
          : [update.positions, update.colors, update.indices];
        for (const array of arrays)
          hash.update(
            new Uint8Array(array.buffer, array.byteOffset, array.byteLength),
          );
      }
      hashes.push(hash.digest("hex"));
    }
    if (expected)
      assert.deepEqual(
        hashes,
        expected,
        "Retained geometry differs from baseline",
      );
    else expected = hashes;
    console.log(
      JSON.stringify(
        {
          label,
          recordedFrames: frames.length,
          skipped,
          transferBytes,
          maxUpdateBytes,
          integrationP95Ms: percentile(times, 0.95),
          mirrorP95Ms: percentile(mirrorTimes, 0.95),
          geometryMatchesBaseline: implementations.size > 1,
        },
        null,
        2,
      ),
    );
  }
}
