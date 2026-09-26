import Scene from "./Scene";
import PointCloudLayer from "./PointCloudLayer";
import { usePointCloud } from "./usePointCloud";

export default function App() {
  const { cloud, live, label, rejected } = usePointCloud();
  return (
    <main className="viewport" aria-label="God's Eye 3D viewport">
      <Scene frameCloud={() => cloud.bounds()}>
        <PointCloudLayer cloud={cloud} />
      </Scene>
      <div className="view-label" aria-hidden="true">
        User Perspective
      </div>
      <div
        className="cloud-status"
        role="status"
        aria-label="Point cloud status"
        aria-live="off"
        data-live={live}
      >
        {label} · {cloud.count.toLocaleString()} points
        {cloud.evicted > 0 ? " · buffer full; oldest points replaced" : ""}
        {rejected > 0 ? ` · ${rejected} invalid packets skipped` : ""}
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
          <b>Home</b> Reset view
        </span>
      </p>
      <p className="sr-only" id="keyboard-help">
        Focus the viewport to navigate with the keyboard. Arrow keys pan, Shift
        plus arrow keys rotate, plus and minus zoom, F frames the scan, and Home
        resets the view. On a touchscreen, drag with one finger to orbit, or use
        two fingers to pan and pinch to zoom.
      </p>
    </main>
  );
}
