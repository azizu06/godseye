import { expect, it } from "vitest";
import { relayServer, TEST_ROVER_KEY } from "../tests/fixtures/relayServer";

for (const mode of ["dev", "preview"] as const) {
  it(`${mode} relays existing dashboard actions with bodies, query strings, errors and audio intact`, async () => {
    const relay = await relayServer(mode);
    try {
      for (const [method, path] of [
        ["GET", "/voice"],
        ["POST", "/voice/ask"],
        ["POST", "/voice/confirm"],
        ["POST", "/nav/propose"],
        ["POST", "/nav/confirm"],
        ["POST", "/nav/cancel"],
        ["GET", "/nav/move"],
        ["POST", "/nav/move/speak"],
        ["GET", "/events"],
        ["POST", "/events/215ab289-cfb3-4e19-b26b-eb39c577deac/audio"],
      ]) {
        const body =
          method === "POST" ? JSON.stringify({ proof: path }) : undefined;
        const response = await fetch(`${relay.base}${path}?case=relay`, {
          method,
          body,
          headers: { Origin: relay.base, "Content-Type": "application/json" },
        });
        expect(response.headers.get("x-relay-fixture"), path).toBe("backend");
        expect(response.status, path).toBe(200);
        expect(await response.json()).toMatchObject({ status: "ready" });
        const received = relay.received.at(-1)!;
        expect([
          received.method,
          received.url,
          received.body.toString(),
        ]).toEqual([method, `${path}?case=relay`, body ?? ""]);
        expect(received.headers.authorization).toBe(
          path === "/nav/confirm" ? `Bearer ${TEST_ROVER_KEY}` : undefined,
        );
      }
      const recording = new FormData();
      recording.append(
        "audio",
        new Blob([new Uint8Array([0, 255, 7, 13])], { type: "audio/webm" }),
        "clip.webm",
      );
      await fetch(`${relay.base}/voice/ask`, {
        method: "POST",
        body: recording,
      });
      expect(relay.received.at(-1)!.headers["content-type"]).toMatch(
        /^multipart\/form-data; boundary=/,
      );
      expect(
        relay.received.at(-1)!.body.includes(Buffer.from([0, 255, 7, 13])),
      ).toBe(true);
      const raw = new Uint8Array([0, 255, 18, 37]);
      await fetch(`${relay.base}/voice/ask`, {
        method: "POST",
        body: raw,
        headers: { "Content-Type": "audio/webm" },
      });
      expect(relay.received.at(-1)!.body).toEqual(Buffer.from(raw));
      const failed = await fetch(`${relay.base}/nav/propose?fail=true`, {
        method: "POST",
      });
      expect(failed.status).toBe(409);
      expect(await failed.json()).toEqual({ detail: "Map changed" });
      const audio = await fetch(`${relay.base}/audio/${"a".repeat(64)}.wav`, {
        headers: { Range: "bytes=0-3" },
      });
      expect(audio.status).toBe(206);
      expect(audio.headers.get("content-type")).toBe("audio/wav");
      expect(audio.headers.get("content-range")).toBe("bytes 0-3/4");
      expect(new Uint8Array(await audio.arrayBuffer())).toEqual(
        new Uint8Array([82, 73, 70, 70]),
      );
      expect(relay.received.at(-1)!.headers.range).toBe("bytes=0-3");
    } finally {
      await relay.close();
    }
  }, 20000);

  it(`${mode} retains pairing boundaries and excludes unknown backend paths`, async () => {
    const relay = await relayServer(mode);
    try {
      const status = await (
        await fetch(`${relay.base}/operator/status`)
      ).text();
      expect(JSON.parse(status)).toEqual({ paired: true });
      expect(status).not.toContain(TEST_ROVER_KEY);
      for (const headers of [
        {} as Record<string, string>,
        { Origin: "https://other.example", "Content-Type": "application/json" },
        { Origin: relay.base, "Content-Type": "text/plain" },
      ]) {
        expect(
          (
            await fetch(`${relay.base}/nav/confirm`, {
              method: "POST",
              headers,
            })
          ).status,
        ).toBe(403);
      }
      expect(relay.received).toHaveLength(0);
      await fetch(`${relay.base}/voice/confirm`, {
        method: "POST",
        headers: { Authorization: "Bearer explicit-browser-token" },
      });
      expect(relay.received.at(-1)!.headers.authorization).toBe(
        "Bearer explicit-browser-token",
      );
      await fetch(`${relay.base}/nav/confirm`, {
        method: "POST",
        headers: {
          Origin: relay.base,
          "Content-Type": "application/json",
          Authorization: "Bearer explicit-browser-token",
        },
        body: "{}",
      });
      expect(relay.received.at(-1)!.headers.authorization).toBe(
        `Bearer ${TEST_ROVER_KEY}`,
      );
      const forwarded = relay.received.length;
      for (const path of [
        "/voice/admin",
        "/voice/ask/extra",
        "/nav",
        "/nav/move/extra",
        "/events/id/private",
        "/audio/invalid.wav",
        "/phone",
        "/arbitrary-backend-path",
      ]) {
        const response = await fetch(`${relay.base}${path}`, {
          method: "POST",
        });
        expect(response.headers.get("x-relay-fixture"), path).toBeNull();
        expect(await response.text()).not.toContain(TEST_ROVER_KEY);
      }
      expect(relay.received).toHaveLength(forwarded);
    } finally {
      await relay.close();
    }
  }, 20000);

  it(`${mode} preserves explicit browser authorization without configured server pairing`, async () => {
    const relay = await relayServer(mode, false);
    try {
      const response = await fetch(`${relay.base}/nav/confirm`, {
        method: "POST",
        body: '{"token":"single-use"}',
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer browser-test-token",
        },
      });
      expect(response.status).toBe(200);
      expect(relay.received.at(-1)!.headers.authorization).toBe(
        "Bearer browser-test-token",
      );
      expect(
        await (await fetch(`${relay.base}/operator/status`)).json(),
      ).toEqual({ paired: false });
    } finally {
      await relay.close();
    }
  }, 20000);
}
