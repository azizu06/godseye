import { describe, expect, it } from "vitest";
import { parseVoiceReply } from "./VoiceAsk";

const ok = {
  version: 1,
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
    });
  });
  it("rejects unversioned, incomplete or oversized replies", () => {
    for (const bad of [
      null,
      "text",
      { ...ok, version: 2 },
      { ...ok, answer: "" },
      { ...ok, answer: "x".repeat(601) },
      { ...ok, question: undefined },
      { ...ok, status: "error" },
    ])
      expect(parseVoiceReply(bad)).toBeNull();
  });
});
