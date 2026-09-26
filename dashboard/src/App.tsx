import Scene from "./Scene";
import PointCloudLayer from "./PointCloudLayer";
import { usePointCloud } from "./usePointCloud";
import { useSurfaceView } from "./useSurfaceView";
import SurfaceLayer from "./SurfaceLayer";
import { useState } from "react";

export default function App() {
  const {
    cloud,
    live,
    label,
    rejected,
    source,
    map,
    canCapture,
    now,
    ingestCaptured,
  } = usePointCloud();
  const surfaces = useSurfaceView(source, map, canCapture, ingestCaptured);
  const [pointsOnly, setPointsOnly] = useState(false);
  const showSurfaces = !pointsOnly && surfaces.triangles > 0;
  const surfaceUpdating =
    canCapture && surfaces.updatedAt > 0 && now - surfaces.updatedAt < 2500;
  const surfaceLive =
    surfaceUpdating &&
    surfaces.frameAgeMs + Math.max(0, now - surfaces.updatedAt) < 2500;
  const viewLive = surfaceLive || live;
  const viewLabel =
    (showSurfaces || surfaceUpdating) && canCapture
      ? surfaceUpdating
        ? surfaceLive
          ? "Live RGB + depth"
          : "Rendering delayed RGB + depth"
        : live
          ? label
          : surfaces.issue || "Waiting for capture updates"
      : canCapture && !live && surfaces.issue
        ? surfaces.issue
        : label;
  return (
    <main
      className="viewport"
      aria-label="God's Eye 3D viewport"
      onKeyDown={(event) => {
        if (
          event.key.toLowerCase() === "p" &&
          !event.ctrlKey &&
          !event.metaKey &&
          !event.altKey
        ) {
          setPointsOnly((value) => !value);
          event.preventDefault();
        }
      }}
    >
      <Scene frameCloud={() => cloud.bounds() ?? surfaces.bounds}>
        <PointCloudLayer cloud={cloud} surfaceOcclusion={showSurfaces} />
        {showSurfaces && (
          <SurfaceLayer tiles={surfaces.tiles} recent={surfaces.recent} />
        )}
      </Scene>
      <div className="view-label" aria-hidden="true">
        User Perspective
      </div>
      <div
        className="cloud-status"
        role="status"
        aria-label="Point cloud status"
        aria-live="off"
        data-live={viewLive}
        data-surfaces={showSurfaces}
        data-capture-frame={surfaces.frameId}
        data-drawn-points={showSurfaces ? cloud.visibleCount : cloud.count}
        data-source={source ?? undefined}
      >
        {viewLabel} · {cloud.count.toLocaleString()} points
        {showSurfaces
          ? ` · ${surfaces.triangles.toLocaleString()} surface triangles · ${cloud.visibleCount.toLocaleString()} dots drawn`
          : ""}
        {surfaces.capacity ? " · surface memory full" : ""}
        {cloud.evicted > 0 ? " · buffer full; older points replaced" : ""}
        {rejected > 0 ? ` · ${rejected} invalid packets skipped` : ""}
        {source ? ` · Backend ${new URL(source).host}` : ""}
      </div>
      <p className="navigation-hint" id="navigation-help">
        <span>
          <b>Right drag</b> Orbit
        </span>
        <span>
          <b>Left drag</b> Pan
        </span>
        <span>
          <b>Scroll</b> Zoom
        </span>
        <span>
          <b>F</b> Frame scan
        </span>
        <span>
          <b>P</b> Points / hybrid
        </span>
        <span>
          <b>Home</b> Reset view
        </span>
      </p>
      <p className="sr-only" id="keyboard-help">
        Focus the viewport to navigate with the keyboard. Arrow keys pan, Shift
        plus arrow keys rotate, plus and minus zoom, F frames the scan, and Home
        resets the view. P switches between points only and points with
        confirmed textured surfaces. On a touchscreen, drag with one finger to
        orbit, or use two fingers to pan and pinch to zoom.
      </p>
    </main>
  );
}
