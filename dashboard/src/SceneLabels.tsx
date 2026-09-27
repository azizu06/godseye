import { useEffect, useMemo, type RefObject, type ReactNode } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import { Vector3 } from "three";
import { LIDAR_RANGE_M } from "./sensorProfile";
import { overlayY } from "./floor";
import type { Mission } from "./state";
import type { Vec2, Vec3 } from "./protocol";
import { className, type LiveMarker } from "./detections";
import {
  ageLabel,
  displayedObjects,
  labelPriority,
  objectAgeS,
  objectEvidence,
  objectStale,
  overlappingLabels,
  type ScreenRect,
} from "./objectDisplay";
import { displayedStoredObjects, type PersonTrack } from "./personMemory";
/** An operator-started approach route to draw: start always, path once planned. */
export interface ApproachDrawing {
  start: Vec2;
  points: Vec2[] | null;
  approach: Vec2 | null;
}
export interface SceneLabel {
  id: string;
  position: Vec3;
  content: ReactNode;
  offset?: number;
  /** Overlap priority; higher wins. Omitted labels are never hidden. */
  priority?: number;
}
export function useSceneLabels(
  mission: Mission,
  selected: string | null,
  onSelect: (id: string) => void,
  objectsVisible: boolean,
  live: LiveMarker[] = [],
  route: ApproachDrawing | null = null,
  now = Date.now(),
  showWeak = false,
  floor: number | null = null,
  people: PersonTrack[] = [],
): SceneLabel[] {
  return useMemo(() => {
    const labels: SceneLabel[] = objectsVisible
      ? displayedObjects(
          displayedStoredObjects(mission.objects, mission.people, selected),
          showWeak,
          selected,
        ).map((o) => {
          const possible = objectEvidence(o) === "possible_person";
          const weak = objectEvidence(o) === "weak";
          const stale = objectStale(o, now);
          return {
            id: o.id,
            position: [o.position[0], o.position[1] + 0.82, o.position[2]],
            priority: o.id === selected ? Infinity : labelPriority(o),
            content: (
              <button
                className={`scene-label ${o.id === selected ? "selected" : ""} ${o.state === "moved" ? "moved" : ""} ${stale ? "stale" : ""} ${possible || weak ? "uncertain" : ""}`}
                data-evidence={objectEvidence(o)}
                title={`${o.observations} frame${o.observations === 1 ? "" : "s"} · ${Math.round(o.confidence * 100)}% · ${stale ? `last seen ${ageLabel(objectAgeS(o, now))}` : "recently observed"}`}
                onClick={() => onSelect(o.id)}
              >
                <span className="marker-dot" />
                {className(o.class)}
                {possible ? "?" : ""}
                <span className="marker-confidence">
                  {Math.round(o.confidence * 100)}%
                </span>
                {stale && (
                  <span className="marker-age">
                    {ageLabel(objectAgeS(o, now))}
                  </span>
                )}
              </button>
            ),
          };
        })
      : [];
    for (const person of people) {
      const age = Math.max(0, (now - person.receivedAt) / 1000);
      labels.push({
        id: `__person-${person.id}`,
        priority: 900,
        position: [
          person.position[0],
          person.position[1] + 0.35,
          person.position[2],
        ],
        content: (
          <span
            className="retained-person-label"
            data-testid="retained-person-label"
            title="Latest measured position. Kept until newer depth views see this spot empty; not a live or current position."
          >
            {/* Label ages step every 10 s, so the first minute stays coarse. */}
            PERSON · last detected {age < 60 ? "<1m ago" : ageLabel(age)} · not
            live
          </span>
        ),
      });
    }
    for (const marker of live)
      labels.push({
        id: `__live-${marker.key}`,
        priority: 1000,
        position: [
          marker.position[0],
          marker.position[1] + 0.35,
          marker.position[2],
        ],
        content: (
          <span
            className={`live-detection-label ${marker.class === "person" ? "person" : ""}`}
            data-testid="live-detection-label"
          >
            LIVE · {className(marker.class)}{" "}
            {Math.round(marker.confidence * 100)}%
          </span>
        ),
      });
    if (route) {
      labels.push({
        id: "__route-start",
        position: [route.start[0], overlayY(floor, "rover"), route.start[1]],
        content: (
          <span className="route-label" data-testid="route-start-label">
            START · operator-selected
          </span>
        ),
      });
      if (route.approach)
        labels.push({
          id: "__route-approach",
          position: [
            route.approach[0],
            overlayY(floor, "rover"),
            route.approach[1],
          ],
          content: (
            <span className="route-label" data-testid="route-approach-label">
              APPROACH POINT · suggested
            </span>
          ),
        });
    }
    if (mission.pose) {
      const p = mission.pose;
      labels.push(
        {
          id: "__rover",
          position: [p.position[0], overlayY(floor, "rover"), p.position[2]],
          offset: 25,
          content: <span className="rover-label">ROVER / PHONE</span>,
        },
        {
          id: "__scope",
          position: [
            p.position[0] + Math.sin(p.yaw_rad) * (LIDAR_RANGE_M + 0.16),
            overlayY(floor, "scopeLabel"),
            p.position[2] + Math.cos(p.yaw_rad) * (LIDAR_RANGE_M + 0.16),
          ],
          content: <span className="scope-label">LiDAR · 5 m MAX</span>,
        },
      );
    }
    const event = mission.events
      .filter(
        (e) =>
          (e.object_id === selected || e.new_object_id === selected) &&
          (e.kind === "moved" || e.kind === "possible_move"),
      )
      .at(-1);
    if (event?.old_position)
      labels.push({
        id: "__previous",
        position: event.old_position,
        content: <span className="old-marker">PREVIOUS LOCATION</span>,
      });
    return labels;
  }, [
    mission.objects,
    mission.people,
    people,
    mission.pose,
    mission.events,
    selected,
    onSelect,
    objectsVisible,
    live,
    route,
    now,
    showWeak,
    floor,
  ]);
}
// DOM nodes belong exclusively to the outer React root. Projection only updates
// transforms, avoiding a second React root fighting over labels during unmount.
export function ProjectLabels({
  labels,
  elements,
}: {
  labels: SceneLabel[];
  elements: RefObject<Map<string, HTMLDivElement>>;
}) {
  const point = useMemo(() => new Vector3(), []);
  const invalidate = useThree((state) => state.invalidate);
  // Label text or membership changed: re-project and re-declutter once.
  useEffect(() => invalidate(), [labels, invalidate]);
  useFrame(({ camera, size }) => {
    const rects: ScreenRect[] = [];
    // Read every size before writing any style, to avoid per-label relayout.
    const placed: [HTMLDivElement, SceneLabel, boolean, number, number][] = [];
    for (const label of labels) {
      const el = elements.current.get(label.id);
      if (!el) continue;
      point.set(...label.position).project(camera);
      const visible =
        point.z > -1 &&
        point.z < 1 &&
        Math.abs(point.x) < 1.2 &&
        Math.abs(point.y) < 1.2;
      const x = (point.x * 0.5 + 0.5) * size.width;
      const y = (-point.y * 0.5 + 0.5) * size.height + (label.offset ?? 0);
      if (visible) {
        const width = el.offsetWidth,
          height = el.offsetHeight;
        rects.push({
          id: label.id,
          x: x - width / 2,
          y: y - height / 2,
          width,
          height,
          priority: label.priority ?? Infinity,
        });
      }
      placed.push([el, label, visible, x, y]);
    }
    // Overlapping labels yield to people, live and better-evidenced objects;
    // the hidden label returns when zooming separates them.
    const hidden = overlappingLabels(rects);
    for (const [el, label, visible, x, y] of placed) {
      const overlapped = visible && hidden.has(label.id);
      el.style.visibility = visible && !overlapped ? "visible" : "hidden";
      el.dataset.overlapHidden = overlapped ? "true" : "false";
      el.style.transform = `translate(${x}px,${y}px) translate(-50%,-50%)`;
      el.style.zIndex = label.id.startsWith("__") ? "1" : "2";
    }
  });
  return null;
}
