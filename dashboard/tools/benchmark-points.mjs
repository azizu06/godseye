// Repeatable CPU/transfer benchmark; this does not claim a device GPU frame rate.
import { readFileSync } from "node:fs";
import { transform } from "esbuild";

const source = readFileSync(
  new URL("../src/pointCloud.ts", import.meta.url),
  "utf8",
);
const { code } = await transform(source, { loader: "ts", format: "esm" });
const { PointCloudStore } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64")
);
const packet = (offset, count, revisit = false) => {
  let header = JSON.stringify({
    version: 2,
    type: "points",
    chunk_id: offset + 1,
    session_id: "benchmark",
    map_epoch: 1,
    frame_id: offset + 1,
    t_capture: offset + 1,
    count,
    positions: "float32_le",
    colors: "rgb8_srgb",
  });
  header += " ".repeat((4 - (header.length % 4)) % 4);
  const result = new ArrayBuffer(4 + header.length + count * 15);
  new DataView(result).setUint32(0, header.length, true);
  new Uint8Array(result, 4, header.length).set(
    new TextEncoder().encode(header),
  );
  const positions = new Float32Array(result, 4 + header.length, count * 3);
  const colors = new Uint8Array(result, 4 + header.length + count * 12);
  for (let i = 0; i < count; i++) {
    const index = revisit ? i * 797 : offset + i;
    positions.set(
      [(index % 2000) * 0.02, Math.floor(index / 2000) * 0.02, 0],
      i * 3,
    );
    colors.set(revisit ? [230, 77, 26] : [51, 128, 204], i * 3);
  }
  return result;
};
const worker = new PointCloudStore(),
  renderer = new PointCloudStore();
const processing = [],
  copying = [];
let totalBytes = 0;
for (let offset = 0; offset < 2_000_000; offset += 20_000) {
  const input = packet(offset, 20_000);
  let start = performance.now();
  worker.ingest(input);
  const update = worker.takeUpdate();
  processing.push(performance.now() - start);
  totalBytes +=
    update.positions.byteLength +
    update.colors.byteLength +
    update.spans.byteLength +
    update.visibleIndices.byteLength +
    update.visibleSpans.byteLength;
  start = performance.now();
  renderer.applyUpdate(update);
  renderer.takeUpdateRanges();
  copying.push(performance.now() - start);
}
worker.ingest(packet(2_000_000, 2500, true));
const revisit = worker.takeUpdate();
const p95 = (values) =>
  [...values].sort((a, b) => a - b)[Math.floor(values.length * 0.95)];
console.log(
  JSON.stringify(
    {
      retainedPoints: worker.count,
      workerTotalMs: processing.reduce((sum, ms) => sum + ms, 0),
      workerBatchP95Ms: p95(processing),
      rendererCopyP95Ms: p95(copying),
      initialTransferBytes: totalBytes,
      scatteredRevisitBytes:
        revisit.positions.byteLength + revisit.colors.byteLength,
      oldScatteredRevisitBytes: 48_000_000,
    },
    null,
    2,
  ),
);

// Retire 90% of a full cache without deleting its original measured coordinates.
const retirementTimes = [];
let retirementBytes = 0;
for (let offset = 0; offset < 2_000_000; offset += 50_000) {
  const count = Math.min(50_000, 2_000_000 - offset);
  const captured = {
    sessionId: "benchmark",
    mapEpoch: 1,
    frameId: offset + 1,
    capturedAt: 4_000_000 + offset,
    positions: worker.positions.slice(offset * 3, (offset + count) * 3),
    colors: worker.colors.slice(offset * 3, (offset + count) * 3),
    covered: Uint8Array.from({ length: count }, (_, i) =>
      Number((offset + i) % 10 !== 0),
    ),
  };
  const start = performance.now();
  worker.ingestCaptured(captured);
  const update = worker.takeUpdate();
  renderer.applyUpdate(update);
  retirementTimes.push(performance.now() - start);
  retirementBytes +=
    update.visibleIndices.byteLength + update.visibleSpans.byteLength;
}
if (renderer.count !== 2_000_000 || renderer.visibleCount !== 200_000)
  throw Error("Point retirement lost measurements or left covered dots active");
console.log(
  JSON.stringify(
    {
      retainedPoints: renderer.count,
      drawnPoints: renderer.visibleCount,
      pointVertexReductionPercent: 90,
      retirementBatchP95Ms: p95(retirementTimes),
      retirementIndexBytes: retirementBytes,
    },
    null,
    2,
  ),
);
