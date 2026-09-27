import { describe, expect, it } from "vitest";
import { parseVoiceReply } from "./VoiceAsk";

const ok = {
  version: 1,
  session_id: "room",
  map_epoch: 1,
  status: "ok",
  question: "Where is the backpack?",
  answer: "The backpack was last seen 12 seconds ago.",
  speech: { status: "ready", mime: "audio/wav", data: "UklGRg==" },
};

describe("parseVoiceReply", () => {
  it("accepts a versioned answer with spoken audio", () => {
    expect(parseVoiceReply(ok)).toEqual({
      question: ok.question,
      answer: ok.answer,
      speech: { mime: "audio/wav", data: "UklGRg==" },
      speechFailed: false,
      actions: [],
      scope: '["room",1]',
      confirm: null,
    });
  });
  it("keeps the text answer when speech failed or is malformed", () => {
    for (const speech of [
      { status: "error" },
      null,
      { status: "ready", mime: "audio/mpeg", data: "UklGRg==" },
      { status: "ready", mime: "audio/wav", data: "not base64!" },
    ])
      expect(parseVoiceReply({ ...ok, speech })).toMatchObject({
        answer: ok.answer,
        speech: null,
        speechFailed: true,
      });
  });
  it("reports no speech without an answer", () => {
    expect(parseVoiceReply({ version: 1, status: "no_speech" })).toEqual({
      question: null,
      answer: null,
      speech: null,
      speechFailed: false,
      actions: [],
      scope: null,
      confirm: null,
    });
  });
  it("accepts typed actions without speech and rejects any invalid action whole", () => {
    const actions = [
      { id: "a1", name: "filter_classes", args: { classes: ["chair"] } },
    ];
    expect(
      parseVoiceReply({ ...ok, answer: null, speech: null, actions }),
    ).toMatchObject({ actions, speech: null, speechFailed: false });
    for (const bad of [
      [{ id: "a1", name: "propose_navigation", args: {} }],
      [{ id: "a1", name: "set_view", args: { mode: "3d", url: "x" } }],
      "filter_classes",
    ])
      expect(parseVoiceReply({ ...ok, actions: bad })).toBeNull();
  });
  it("rejects unversioned, incomplete or oversized replies", () => {
    for (const bad of [
      null,
      "text",
      { ...ok, version: 2 },
      { ...ok, answer: "" },
      { ...ok, answer: null, actions: [] },
      { ...ok, answer: "x".repeat(601) },
      { ...ok, question: undefined },
      { ...ok, status: "error" },
    ])
      expect(parseVoiceReply(bad)).toBeNull();
  });
});
