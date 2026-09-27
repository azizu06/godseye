import { expect, it } from "vitest";
import {
  rememberControlPairing,
  restoreControlPairing,
} from "./controlPairing";
import { defaultConfig } from "./transport";
it("restores pairing after reload only for the same backend and clears on disable", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
  const paired = { ...defaultConfig, commands: true, roverKey: "test-key" };
  rememberControlPairing(paired, storage);
  expect(restoreControlPairing(defaultConfig, storage)).toEqual(paired);
  for (const changed of [
    { apiUrl: "http://other:8765" },
    { wsUrl: "ws://other:8765/live" },
  ]) {
    const different = { ...defaultConfig, ...changed };
    expect(restoreControlPairing(different, storage)).toEqual(different);
  }
  rememberControlPairing(defaultConfig, storage);
  expect(restoreControlPairing(defaultConfig, storage)).toEqual(defaultConfig);
});
it("keeps controls off when storage is corrupt or unavailable", () => {
  expect(restoreControlPairing(defaultConfig, { getItem: () => "{" })).toEqual(
    defaultConfig,
  );
  expect(
    restoreControlPairing(defaultConfig, {
      getItem: () => {
        throw Error();
      },
    }),
  ).toEqual(defaultConfig);
});
