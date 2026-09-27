// Synthetic worker/main-thread CPU and payload comparison for confirmed face
// deletion, not physical-device, GPU or FPS measurement.
// Usage: node tools/benchmark-surface-deletion.mjs [baseline-ref]
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";
const dashboard = resolve(fileURLToPath(new URL("..", import.meta.url)));
const root = resolve(dashboard, "..");
const ref = process.argv[2] ?? "8a5cf25";
const directory = mkdtempSync(join(tmpdir(), "godseye-deletion-bench-"));
const source = (name) =>
  execFileSync("git", ["show", `${ref}:dashboard/src/${name}.ts`], {
    cwd: root,
    encoding: "utf8",
  }).replace(/"\.\/(\w+)"/g, (_, module) =>
    JSON.stringify(join(dashboard, `src/${module}.ts`)),
  );
try {
  for (const name of ["persistentSurfaceMap", "surfaceBuffer"])
    writeFileSync(join(directory, `${name}.ts`), source(name));
  const current = (name) => JSON.stringify(join(dashboard, `src/${name}.ts`));
  const baseline = (name) => JSON.stringify(join(directory, `${name}.ts`));
  writeFileSync(
    join(directory, "entry.ts"),
    `
import { PersistentSurfaceMap as BeforeMap } from ${baseline("persistentSurfaceMap")};
import { SurfaceBuffer as BeforeBuffer } from ${baseline("surfaceBuffer")};
import { PersistentSurfaceMap as AfterMap } from ${current("persistentSurfaceMap")};
import { SurfaceBuffer as AfterBuffer } from ${current("surfaceBuffer")};
import { DepthContradiction } from ${current("depthRetirement")};
const W = 256, H = 192, K = [40, 0, 0, 0, 40, 0, 40, 30, 1];
let clock = 1;
// Two newer calibrated views: background at 3 m, optionally nearer depth over [x0, x1].
const proof = (x0 = Infinity, x1 = -Infinity) => new DepthContradiction([0, 1].map(() => {
  const depth = new Float32Array(W * H).fill(3);
  for (let v = 0; v < H; v++) for (let u = 0; u < W; u++) {
    const x = ((u + 0.5) * 80 / W - 40) / 40; // ray X at 1 m depth
    const y = -((v + 0.5) * 60 / H - 30) / 40;
    if (x >= x0 - 0.08 && x <= x1 + 0.08 && Math.abs(y) < 0.3) depth[v * W + u] = 1;
  }
  return { sessionId: "bench", mapEpoch: 1, capturedAt: clock++, width: W, height: H, depth,
    confidence: new Uint8Array(W * H).fill(2),
    projection: { transform: [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1], intrinsics: K, imageWidth: 80, imageHeight: 60 } };
}));
const wall = (frame) => {
  const positions = new Float32Array(1200 * 9), colors = new Float32Array(1200 * 9).fill(.5), indices = new Uint32Array(1200 * 3);
  for (let i = 0; i < 1200; i++) {
    const x = (i % 40) * .03 - .6 + frame * .0002, y = Math.floor(i / 40) * .03 - .45;
    positions.set([x, y, -3, x + .01, y, -3, x, y + .01, -3], i * 9);
    indices.set([i * 3, i * 3 + 1, i * 3 + 2], i * 3);
  }
  return { id: "wall" + frame, positions, colors, indices };
};
// A 0.3 m wide, 0.4 m tall moving foreground block at 1 m, 48 faces.
const person = (x0) => {
  const positions = [], indices = [];
  for (let i = 0; i < 48; i++) {
    const x = x0 + (i % 8) * .0375, y = Math.floor(i / 8) * .066 - .2;
    positions.push(x, y, -1, x + .03, y, -1, x, y + .05, -1);
    indices.push(i * 3, i * 3 + 1, i * 3 + 2);
  }
  return { id: "person" + x0, positions: new Float32Array(positions),
    colors: new Float32Array(positions.length).fill(.9), indices: new Uint32Array(indices) };
};
const bytes = (d) => d.positions.byteLength + d.colors.byteLength + d.indices.byteLength;
const faces = (patch) => {
  const out = [];
  if (!patch) return out;
  for (let i = 0; i < patch.indices.length; i += 3)
    out.push([0, 1, 2].map((j) => Array.from(patch.positions.subarray(patch.indices[i + j] * 3, patch.indices[i + j] * 3 + 3)).join()).join("|"));
  return out.sort();
};
const median = (v) => [...v].sort((a, b) => a - b)[Math.floor(v.length / 2)];
function build(Map, Buffer, frames, foregroundFirst = false) {
  const map = new Map(), buffer = new Buffer();
  if (foregroundFirst) { map.add(person(0)); buffer.apply(map.takeDelta()); }
  for (let f = 0; f < frames; f++) { map.add(wall(f)); buffer.apply(map.takeDelta()); }
  return { map, buffer };
}
const results = { single: [], moving: [] };
for (const frames of [10, 100]) for (const first of [false, true]) for (const [label, Map, Buffer] of [["before " + ${JSON.stringify(ref)}, BeforeMap, BeforeBuffer], ["after", AfterMap, AfterBuffer]]) {
  const runs = [];
  for (let run = 0; run < 5; run++) {
    const { map, buffer } = build(Map, Buffer, frames, first);
    if (!first) map.add(person(0));
    const published = buffer.apply(map.takeDelta());
    const evidence = proof();
    let t = performance.now(); const removed = map.retire(evidence); const retireMs = performance.now() - t;
    t = performance.now(); const delta = map.takeDelta(); const deltaMs = performance.now() - t;
    t = performance.now(); const patch = buffer.apply(delta); const applyMs = performance.now() - t;
    if (JSON.stringify(faces(patch)) !== JSON.stringify(faces(map.snapshot()))) throw Error("draw geometry mismatch");
    runs.push({ faces_before: frames * 1200 + 48, removed, retireMs, deltaMs, applyMs, bytes: bytes(delta), reset: delta.reset,
      vertex_storage_reallocated: patch.update.positions !== published.update.positions });
  }
  const r = runs[0];
  results.single.push({ label, deleted: first ? "oldest faces (worst case)" : "newest faces", faces_before: r.faces_before, removed: r.removed, delta_reset: r.reset, delta_bytes: r.bytes,
    vertex_storage_reallocated: r.vertex_storage_reallocated,
    median_retire_ms: +median(runs.map((x) => x.retireMs)).toFixed(2),
    median_take_delta_ms: +median(runs.map((x) => x.deltaMs)).toFixed(2),
    median_main_thread_apply_ms: +median(runs.map((x) => x.applyMs)).toFixed(2) });
}
const finals = [];
for (const [label, Map, Buffer] of [["before " + ${JSON.stringify(ref)}, BeforeMap, BeforeBuffer], ["after", AfterMap, AfterBuffer]]) {
  const { map, buffer } = build(Map, Buffer, 10);
  let total = 0, resets = 0, removedTotal = 0; const times = [];
  for (let step = 0; step < 30; step++) {
    const x0 = -.5 + step * .03; // Walks 3 cm per capture.
    map.add(person(x0));
    const t = performance.now();
    removedTotal += map.retire(proof(x0, x0 + .3));
    const delta = map.takeDelta(); buffer.apply(delta);
    times.push(performance.now() - t);
    total += bytes(delta); resets += delta.reset ? 1 : 0;
  }
  const drawn = faces(buffer.apply(map.takeDelta()) ?? map.snapshot());
  finals.push(drawn);
  results.moving.push({ label, captures: 30, faces_removed: removedTotal, full_replacements: resets, total_delta_bytes: total,
    median_retire_publish_apply_ms: +median(times).toFixed(2), final_faces: drawn.length });
}
if (JSON.stringify(finals[0]) !== JSON.stringify(finals[1])) throw Error("moving sequence final geometry differs");
console.log(JSON.stringify({ fixture: "disconnected 3 m wall batches (1200 faces each) plus 1 m foreground; two calibrated 256x192 views",
  note: "Node CPU and transferred typed-array bytes only; GPU upload, rendering and physical capture are excluded",
  identical_final_geometry: true, ...results }, null, 2));
`,
  );
  const bundled = join(directory, "run.mjs");
  buildSync({
    entryPoints: [join(directory, "entry.ts")],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: bundled,
  });
  process.stdout.write(
    execFileSync(process.execPath, [bundled], { encoding: "utf8" }),
  );
} finally {
  rmSync(directory, { recursive: true, force: true });
}
