import { useMemo, type RefObject, type ReactNode } from "react";
import { useFrame } from "@react-three/fiber";
import { Vector3 } from "three";
import { LIDAR_RANGE_M } from "./sensorProfile";
import type { Mission } from "./state";
import type { Vec2, Vec3 } from "./protocol";
import { className, type LiveMarker } from "./detections";
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
}
export function useSceneLabels(
  mission: Mission,
  selected: string | null,
  onSelect: (id: string) => void,
  objectsVisible: boolean,
  live: LiveMarker[] = [],
  route: ApproachDrawing | null = null,
): SceneLabel[] {
  return useMemo(() => {
    const labels: SceneLabel[] = objectsVisible
      ? mission.objects.map((o) => ({
          id: o.id,
          position: [o.position[0], o.position[1] + 0.82, o.position[2]],
          content: (
            <button
              className={`scene-label ${o.id === selected ? "selected" : ""} ${o.state === "moved" ? "moved" : ""}`}
              onClick={() => onSelect(o.id)}
            >
              <span className="marker-dot" />
              {o.class === "potted plant"
                ? "Plant"
                : o.class.charAt(0).toUpperCase() + o.class.slice(1)}
              <span className="marker-confidence">
                {Math.round(o.confidence * 100)}%
              </span>
            </button>
          ),
        }))
      : [];
    for (const marker of live)
      labels.push({
        id: `__live-${marker.key}`,
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
        position: [route.start[0], 0.1, route.start[1]],
        content: (
          <span className="route-label" data-testid="route-start-label">
            START · operator-selected
          </span>
        ),
      });
      if (route.approach)
        labels.push({
          id: "__route-approach",
          position: [route.approach[0], 0.1, route.approach[1]],
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
          position: [p.position[0], 0.1, p.position[2]],
          offset: 25,
          content: <span className="rover-label">ROVER / PHONE</span>,
        },
        {
          id: "__scope",
          position: [
            p.position[0] + Math.sin(p.yaw_rad) * (LIDAR_RANGE_M + 0.16),
            0.085,
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
    mission.pose,
    mission.events,
    selected,
    onSelect,
    objectsVisible,
    live,
    route,
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
  useFrame(({ camera, size }) => {
    for (const label of labels) {
      const el = elements.current.get(label.id);
      if (!el) continue;
      point.set(...label.position).project(camera);
      const visible =
        point.z > -1 &&
        point.z < 1 &&
        Math.abs(point.x) < 1.2 &&
        Math.abs(point.y) < 1.2;
      el.style.visibility = visible ? "visible" : "hidden";
      el.style.transform = `translate(${(point.x * 0.5 + 0.5) * size.width}px,${(-point.y * 0.5 + 0.5) * size.height + (label.offset ?? 0)}px) translate(-50%,-50%)`;
      el.style.zIndex = label.id.startsWith("__") ? "1" : "2";
    }
  });
  return null;
}
