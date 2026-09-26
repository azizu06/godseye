import { expect, it } from "vitest";
import { playbackUrl } from "./SpokenEvent";
import { mapKey } from "./protocol";
const scope = { session_id: "s", map_epoch: 1 };
const key = mapKey(scope)!;
const data = {
  version: 1,
  ...scope,
  event_id: "event-1",
  status: "ready",
  duration_s: 2,
  audio_url: `/audio/${"a".repeat(64)}.wav`,
};
it("accepts only bounded same-map same-event backend audio paths", () => {
  expect(playbackUrl(data, "http://localhost:8765", key, "event-1")).toBe(
    `http://localhost:8765/audio/${"a".repeat(64)}.wav`,
  );
  for (const patch of [
    { map_epoch: 2 },
    { event_id: "other" },
    { duration_s: 13 },
    { duration_s: NaN },
    { status: "error" },
    { audio_url: "https://attacker.test/a.wav" },
    { audio_url: "/audio/../../secret" },
  ]) {
    expect(
      playbackUrl(
        { ...data, ...patch },
        "http://localhost:8765",
        key,
        "event-1",
      ),
    ).toBeNull();
  }
});
