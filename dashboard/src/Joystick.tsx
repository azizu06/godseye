import { useEffect, useRef, useState } from "react";
import type { MissionController } from "./useMission";
import { joystickVector } from "./joystick";

/** One captured pointer owns the pad; release never ends a different input owner. */
export function Joystick({
  controller,
  blocked = false,
}: {
  controller: MissionController;
  blocked?: boolean;
}) {
  const enabled = controller.canJoystick && !blocked;
  const [thumb, setThumb] = useState({ x: 0, y: 0 });
  const pointer = useRef<number | null>(null);
  const driving = useRef(false);
  const owner = useRef<number | null>(null);
  const keys = useRef(new Set<string>());
  const callbacks = useRef(controller);
  callbacks.current = controller;
  const release = () => {
    pointer.current = null;
    keys.current.clear();
    driving.current = false;
    callbacks.current.releaseDrive(owner.current);
    owner.current = null;
    setThumb({ x: 0, y: 0 });
  };
  useEffect(() => {
    if (!enabled) release();
  }, [enabled]);
  useEffect(() => {
    const stop = () => release();
    window.addEventListener("blur", stop);
    return () => {
      window.removeEventListener("blur", stop);
      callbacks.current.releaseDrive(owner.current);
      owner.current = null;
    };
  }, []);
  const move = (x: number, y: number) => {
    if (!enabled) return;
    const vector = joystickVector(x, y, controller.manualCapabilities);
    setThumb({ x: vector.x, y: vector.y });
    if (!driving.current) {
      if (!vector.v && !vector.w) return;
      driving.current = true;
      owner.current = callbacks.current.beginDrive(vector.v, vector.w);
    } else callbacks.current.drive(owner.current, vector.v, vector.w);
  };
  const keyMove = () =>
    move(
      Number(keys.current.has("ArrowRight")) -
        Number(keys.current.has("ArrowLeft")),
      Number(keys.current.has("ArrowDown")) -
        Number(keys.current.has("ArrowUp")),
    );
  const hint = !enabled
    ? controller.mission.health?.armed
      ? "Manual control unavailable"
      : "Arm to use joystick"
    : controller.manualCapabilities?.arcs
      ? "Drag to drive · release to stop"
      : "Drag to drive or turn · release to stop";
  return (
    <div className="joystick-control">
      <button
        type="button"
        className={`rover-joystick ${enabled ? "ready" : ""}`}
        disabled={!enabled}
        aria-label="Rover joystick"
        aria-describedby="joystick-help"
        aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight"
        title={hint}
        onPointerDown={(event) => {
          if (!enabled || event.button !== 0 || pointer.current !== null)
            return;
          event.preventDefault();
          event.stopPropagation();
          pointer.current = event.pointerId;
          event.currentTarget.focus({ preventScroll: true });
          event.currentTarget.setPointerCapture(event.pointerId);
          const box = event.currentTarget.getBoundingClientRect();
          move(
            (event.clientX - box.x - box.width / 2) / (box.width * 0.38),
            (event.clientY - box.y - box.height / 2) / (box.height * 0.38),
          );
        }}
        onPointerMove={(event) => {
          if (pointer.current !== event.pointerId) return;
          event.preventDefault();
          const box = event.currentTarget.getBoundingClientRect();
          move(
            (event.clientX - box.x - box.width / 2) / (box.width * 0.38),
            (event.clientY - box.y - box.height / 2) / (box.height * 0.38),
          );
        }}
        onPointerUp={(event) => {
          if (pointer.current === event.pointerId) release();
        }}
        onPointerCancel={(event) => {
          if (pointer.current === event.pointerId) release();
        }}
        onLostPointerCapture={(event) => {
          if (pointer.current === event.pointerId) release();
        }}
        onBlur={release}
        onContextMenu={(event) => event.preventDefault()}
        onKeyDown={(event) => {
          if (!event.key.startsWith("Arrow")) return;
          event.preventDefault();
          event.stopPropagation();
          if (pointer.current !== null) return;
          keys.current.add(event.key);
          keyMove();
        }}
        onKeyUp={(event) => {
          if (!event.key.startsWith("Arrow")) return;
          event.preventDefault();
          event.stopPropagation();
          keys.current.delete(event.key);
          if (!keys.current.size) release();
          else keyMove();
        }}
      >
        <span className="joystick-axis horizontal" />
        <span className="joystick-axis vertical" />
        <span className="joystick-mark forward">⌃</span>
        <span className="joystick-mark left">‹</span>
        <span className="joystick-mark right">›</span>
        {controller.manualCapabilities?.reverse && (
          <span className="joystick-mark reverse">⌄</span>
        )}
        <span
          className="joystick-thumb"
          style={{
            transform: `translate(${thumb.x * 40}px,${thumb.y * 40}px)`,
          }}
        >
          <span />
        </span>
      </button>
      <span id="joystick-help" className="joystick-hint">
        {hint}
      </span>
    </div>
  );
}
