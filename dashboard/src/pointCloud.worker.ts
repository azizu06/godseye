import { PointCloudStore, type CapturedPoints } from "./pointCloud";
import type { CloudRequest, CloudResponse } from "./cloudWorker";

const cloud = new PointCloudStore();
const worker = self as unknown as {
  onmessage: (event: MessageEvent<CloudRequest>) => void;
  postMessage: (message: CloudResponse, transfer: Transferable[]) => void;
};
worker.onmessage = ({ data }) => {
  let result: CloudResponse["result"];
  if (data.kind === "clear") {
    cloud.clear();
    result = "accepted";
  } else if (data.kind === "reconnect") {
    cloud.reconnect();
    result = "ignored";
  } else if (data.kind === "restore") {
    cloud.restoreCoverage();
    result = "accepted";
  } else if (data.kind === "announce") {
    result = cloud.announce(data.value) ? "accepted" : "ignored";
  } else if (data.kind === "capture") {
    result = cloud.ingestCaptured(data.value as CapturedPoints);
  } else {
    result = cloud.ingest(data.value);
  }
  const update = result === "accepted" ? cloud.takeUpdate() : undefined;
  worker.postMessage(
    {
      generation: data.generation,
      id: data.id,
      kind: data.kind,
      result,
      update,
    },
    update
      ? [
          update.positions.buffer,
          update.colors.buffer,
          update.spans.buffer,
          update.visibleSpans.buffer,
          update.visibleIndices.buffer,
        ]
      : [],
  );
};
