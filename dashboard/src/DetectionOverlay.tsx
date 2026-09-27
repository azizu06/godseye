import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { ReceivedDetections } from "./state";
import {
  boxPercent,
  className,
  detectionAgeMs,
  detectionFrameKey,
  detectionImageUrl,
  detectionsLive,
} from "./detections";

interface ShownImage {
  key: string;
  url: string;
  detections: ReceivedDetections;
}

const sameMap = (a: ReceivedDetections, b: ReceivedDetections) =>
  a.frame.session_id === b.frame.session_id &&
  a.frame.map_epoch === b.frame.map_epoch;

/**
 * The newest received detector output over the exact phone frame it describes.
 * Boxes are drawn only on the JPEG the backend confirms belongs to their frame;
 * a superseded or unavailable image is never substituted.
 */
export function DetectionOverlay({
  detections,
  apiUrl,
  now,
}: {
  detections: ReceivedDetections | null;
  apiUrl: string;
  now: number;
}) {
  const [shown, setShown] = useState<ShownImage | null>(null);
  const [open, setOpen] = useState(true);
  const objectUrl = useRef<string | null>(null);
  const key = detections ? detectionFrameKey(detections) : null;
  useEffect(() => {
    if (!detections || !key) return;
    const abort = new AbortController();
    void fetch(detectionImageUrl(apiUrl, detections), {
      signal: abort.signal,
      cache: "no-store",
    })
      .then((response) =>
        response.ok &&
        response.headers.get("Content-Type")?.startsWith("image/jpeg")
          ? response.blob()
          : null,
      )
      .then((blob) => {
        if (!blob || abort.signal.aborted) return;
        if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
        const url = (objectUrl.current = URL.createObjectURL(blob));
        setShown({ key, url, detections });
      })
      .catch(() => {});
    return () => abort.abort();
  }, [apiUrl, key]); // only a new frame identity needs a new image
  useEffect(
    () => () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    },
    [],
  );
  if (!detections) return null;
  // An image from a previous map is never shown beside this map's detections.
  const image = shown && sameMap(shown.detections, detections) ? shown : null;
  const live = detectionsLive(detections, now);
  const age = detectionAgeMs(detections, now) / 1000;
  const { frame } = detections;
  const captured = new Date(frame.t_wall_ms).toLocaleTimeString();
  return (
    <section
      className={`detection-panel ${live ? "live" : "stale"}`}
      aria-label="Received detections"
      data-testid="detection-panel"
    >
      <header>
        <span className="eyebrow">Received detections</span>
        <span className="detection-status" data-testid="detection-status">
          {live ? "LIVE" : "STALE"} · last seen {age.toFixed(1)} s ago
        </span>
        <button
          className="icon-button"
          aria-label={open ? "Collapse detections" : "Expand detections"}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </header>
      <p className="detection-source">
        Backend detector · phone frame #{frame.frame_id} · captured {captured}
      </p>
      {open && (
        <>
          <div
            className="detection-image"
            style={{
              aspectRatio: `${frame.image.width} / ${frame.image.height}`,
            }}
          >
            {image ? (
              <>
                <img
                  src={image.url}
                  alt={`Phone frame #${image.detections.frame.frame_id} analysed by the backend detector`}
                />
                {image.detections.frame.detections.map((box, i) => {
                  const p = boxPercent(box.box, image.detections.frame.image);
                  return (
                    <div
                      key={i}
                      className={`detection-box ${box.class === "person" ? "person" : ""}`}
                      data-testid="detection-box"
                      style={{
                        left: `${p.left}%`,
                        top: `${p.top}%`,
                        width: `${p.width}%`,
                        height: `${p.height}%`,
                      }}
                    >
                      <span>
                        {className(box.class)}{" "}
                        {Math.round(box.confidence * 100)}%
                      </span>
                    </div>
                  );
                })}
              </>
            ) : (
              <p className="detection-image-empty">
                Frame image unavailable · boxes listed below
              </p>
            )}
          </div>
          {frame.detections.length ? (
            <ul className="detection-list">
              {frame.detections.map((box, i) => (
                <li key={i}>
                  <span>
                    {className(box.class)} {Math.round(box.confidence * 100)}%
                  </span>
                  <span className="detection-placement">
                    {box.position
                      ? `3D placed · ${box.depth_m?.toFixed(2)} m depth`
                      : "2D only · no reliable depth"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="detection-source">
              No {frame.classes.join(", ")} in this frame.
            </p>
          )}
        </>
      )}
    </section>
  );
}
