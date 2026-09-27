import { expect, it } from "vitest";
import { ReconPlanner, type ReconJob } from "./reconPlanner";
import type { RouteResult } from "./approachRoute";
const unavailable: RouteResult = {
  status: "unavailable",
  reason: "no_observed_free_route",
};
const job = (id: string, key = id): ReconJob => ({
  id,
  key,
  apiUrl: "http://test",
  body: {
    session_id: "room",
    map_epoch: 1,
    object_id: id,
    start: [0, 0],
    purpose: "recon",
  },
});
const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};
it("processes every person with two in flight, despite unchanged snapshots and one unavailable route", async () => {
  const pending: (() => void)[] = [];
  const seen: string[] = [];
  const results: string[] = [];
  const planner = new ReconPlanner(
    (id) => results.push(id),
    async (_url, body) => {
      seen.push(body.object_id);
      return new Promise((resolve) => pending.push(() => resolve(unavailable)));
    },
  );
  const jobs = ["a", "b", "c", "d", "e"].map((id) => job(id));
  planner.reconcile(jobs);
  expect(seen).toEqual(["a", "b"]);
  planner.reconcile(jobs.map((j) => ({ ...j })));
  pending.shift()!();
  await flush();
  expect(seen).toEqual(["a", "b", "c"]);
  while (pending.length) {
    pending.shift()!();
    await flush();
  }
  expect(seen).toEqual(["a", "b", "c", "d", "e"]);
  expect(results).toHaveLength(5);
  planner.dispose();
});
it("a changed or removed person's late reply cannot publish, and later people keep their queue position", async () => {
  const pending: (() => void)[] = [];
  const seen: string[] = [];
  const results: string[] = [];
  const planner = new ReconPlanner(
    (id, record) => results.push(id + record.key),
    async (_url, body) => {
      seen.push(body.object_id);
      return new Promise((resolve) => pending.push(() => resolve(unavailable)));
    },
  );
  planner.reconcile([job("a"), job("b"), job("c")]);
  planner.reconcile([job("a", "new"), job("c")]);
  pending.shift()!();
  await flush();
  pending.shift()!();
  await flush();
  expect(results).toEqual([]);
  expect(seen.slice(2)).toEqual(["c", "a"]);
  while (pending.length) {
    pending.shift()!();
    await flush();
  }
  expect(results).toEqual(["cc", "anew"]);
  planner.dispose();
});
it("remove and re-add of the same key cannot publish an aborted reply or lose the retry", async () => {
  const pending: (() => void)[] = [];
  const results: string[] = [];
  const planner = new ReconPlanner(
    (id) => results.push(id),
    async () =>
      new Promise((resolve) => pending.push(() => resolve(unavailable))),
  );
  planner.reconcile([job("a")]);
  planner.reconcile([]);
  planner.reconcile([job("a")]);
  pending.shift()!();
  await flush();
  expect(results).toEqual([]);
  expect(pending).toHaveLength(1);
  pending.shift()!();
  await flush();
  expect(results).toEqual(["a"]);
  planner.dispose();
});
it("repeated evidence changes preserve FIFO progress and disposal rejects late results", async () => {
  const pending: (() => void)[] = [];
  const seen: string[] = [];
  const results: string[] = [];
  const planner = new ReconPlanner(
    (id) => results.push(id),
    async (_url, body) => {
      seen.push(body.object_id);
      return new Promise((resolve) => pending.push(() => resolve(unavailable)));
    },
  );
  const ids = ["a", "b", "c", "d", "e"];
  planner.reconcile(ids.map((id) => job(id)));
  for (let epoch = 1; epoch <= 3; epoch++) {
    planner.reconcile(ids.map((id) => job(id, String(epoch))));
    pending.shift()!();
    await flush();
  }
  expect(seen.slice(0, 5)).toEqual(ids);
  planner.dispose();
  while (pending.length) {
    pending.shift()!();
    await flush();
  }
  expect(results).toEqual([]);
});
