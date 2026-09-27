import { validManualCapabilities, type ManualCapabilities } from "./joystick";
import { useEffect, useState } from "react";

export interface AutonomyReadiness {
  version: 1;
  adapter: "logging" | "iphone";
  profile?: "prototype" | "measured";
  warnings?: string[];
  prototype_cruise_pwm?: number | null;
  ready: boolean;
  manual_control?: ManualCapabilities | null;
  auto_requested?: boolean;
  blockers: string[];
}

export function useAutonomy(apiUrl: string) {
  const [value, setValue] = useState<AutonomyReadiness | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();
    setValue(null);
    const poll = async () => {
      try {
        const response = await fetch(`${apiUrl.replace(/\/$/, "")}/autonomy`, {
          signal: AbortSignal.any([abort.signal, AbortSignal.timeout(1500)]),
          cache: "no-store",
        });
        const data = await response.json();
        if (
          !response.ok ||
          data.version !== 1 ||
          !["logging", "iphone"].includes(data.adapter) ||
          typeof data.ready !== "boolean" ||
          (data.manual_control !== undefined &&
            !validManualCapabilities(data.manual_control)) ||
          !Array.isArray(data.blockers) ||
          !data.blockers.every((item: unknown) => typeof item === "string")
        )
          throw Error("Invalid readiness");
        if (!cancelled) setValue(data);
      } catch {
        if (!cancelled) setValue(null);
      }
      if (!cancelled) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      abort.abort();
    };
  }, [apiUrl]);
  return value;
}
