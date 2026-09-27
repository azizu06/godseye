import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, Send, Square } from "lucide-react";
import type { ConnectionConfig } from "./transport";

export type VoicePhase =
  "off" | "unavailable" | "idle" | "listening" | "thinking" | "speaking";

export interface VoiceReply {
  question: string | null;
  answer: string | null;
  speech: { mime: string; data: string } | null;
  speechFailed: boolean;
}

const MAX_RECORD_MS = 15000;
const MIN_RECORD_MS = 400;
const MIN_BYTES = 1024;

function text(value: unknown, limit: number): string | null {
  return typeof value === "string" && value.length > 0 && value.length <= limit
    ? value
    : null;
}

/** Accept only the versioned backend reply; anything else is treated as a failure. */
export function parseVoiceReply(data: unknown): VoiceReply | null {
  if (!data || typeof data !== "object") return null;
  const value = data as Record<string, unknown>;
  if (value.version !== 1) return null;
  if (value.status === "no_speech")
    return { question: null, answer: null, speech: null, speechFailed: false };
  const question = text(value.question, 500);
  const answer = text(value.answer, 600);
  if (value.status !== "ok" || !question || !answer) return null;
  const speech = value.speech as Record<string, unknown> | null;
  const ready =
    !!speech &&
    speech.status === "ready" &&
    speech.mime === "audio/wav" &&
    typeof speech.data === "string" &&
    /^[A-Za-z0-9+/]+={0,2}$/.test(speech.data);
  return {
    question,
    answer,
    speech: ready ? { mime: "audio/wav", data: speech!.data as string } : null,
    speechFailed: !ready,
  };
}

function failure(status: number): string {
  if (status === 503) return "Voice Q&A is not configured on this backend.";
  if (status === 429) return "Scout is busy or at its question limit.";
  if (status === 413 || status === 422)
    return "That recording could not be used. Try a shorter question.";
  return "Scout could not answer right now.";
}

/**
 * Click-to-talk questions about Scout's observations. One click opens the
 * microphone, the next click (or the 15 s cap) stops it and sends the clip;
 * every track is released immediately and nothing is stored.
 */
export function VoiceAsk({ config }: { config: ConnectionConfig }) {
  const enabled = config.source === "external" && config.commands;
  const api = config.apiUrl.replace(/\/$/, "");
  const [phase, setPhase] = useState<VoicePhase>("off");
  const [message, setMessage] = useState("");
  const [exchange, setExchange] = useState<{ q: string; a: string } | null>(
    null,
  );
  const recording = useRef(false);
  const started = useRef(0);
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const discard = useRef(false);
  const request = useRef<AbortController | null>(null);
  const player = useRef<HTMLAudioElement | null>(null);
  const playbackUrl = useRef<string | null>(null);
  const limit = useRef<ReturnType<typeof setTimeout> | null>(null);

  const releaseMic = useCallback(() => {
    if (limit.current) clearTimeout(limit.current);
    limit.current = null;
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  }, []);
  const stopPlayback = useCallback(() => {
    player.current?.pause();
    player.current = null;
    if (playbackUrl.current) URL.revokeObjectURL(playbackUrl.current);
    playbackUrl.current = null;
  }, []);
  const reset = useCallback(() => {
    recording.current = false;
    discard.current = true;
    if (recorder.current?.state === "recording") recorder.current.stop();
    recorder.current = null;
    releaseMic();
    request.current?.abort();
    request.current = null;
    stopPlayback();
  }, [releaseMic, stopPlayback]);

  useEffect(() => {
    reset();
    setExchange(null);
    setMessage("");
    if (!enabled) {
      setPhase("off");
      return;
    }
    setPhase("unavailable");
    const abort = new AbortController();
    fetch(`${api}/voice`, { signal: abort.signal })
      .then((response) => (response.ok ? response.json() : null))
      .then((data: unknown) => {
        const ready =
          !!data &&
          (data as Record<string, unknown>).version === 1 &&
          (data as Record<string, unknown>).status === "ready";
        setPhase(ready ? "idle" : "unavailable");
      })
      .catch(() => {
        if (!abort.signal.aborted) setPhase("unavailable");
      });
    return () => {
      abort.abort();
      reset();
    };
  }, [enabled, api, reset]);

  async function send(clip: Blob) {
    const abort = new AbortController();
    request.current = abort;
    setPhase("thinking");
    setMessage("Thinking…");
    try {
      const response = await fetch(`${api}/voice/ask`, {
        method: "POST",
        headers: { "Content-Type": clip.type || "audio/webm" },
        body: clip,
        signal: abort.signal,
      });
      if (!response.ok) {
        setPhase("idle");
        setMessage(failure(response.status));
        return;
      }
      const reply = parseVoiceReply(await response.json());
      if (abort.signal.aborted) return;
      if (!reply) throw new Error("invalid reply");
      if (!reply.answer || !reply.question) {
        setPhase("idle");
        setMessage("No question heard. Click the microphone and speak.");
        return;
      }
      setExchange({ q: reply.question, a: reply.answer });
      if (!reply.speech) {
        setPhase("idle");
        setMessage("Spoken reply unavailable; the answer is shown.");
        return;
      }
      const bytes = Uint8Array.from(atob(reply.speech.data), (c) =>
        c.charCodeAt(0),
      );
      const url = URL.createObjectURL(
        new Blob([bytes], { type: reply.speech.mime }),
      );
      playbackUrl.current = url;
      const audio = new Audio(url);
      player.current = audio;
      const done = () => {
        if (player.current !== audio) return;
        stopPlayback();
        setPhase("idle");
        setMessage("");
      };
      audio.onended = done;
      audio.onerror = () => {
        if (player.current !== audio) return;
        stopPlayback();
        setPhase("idle");
        setMessage("Playback unavailable; the answer is shown.");
      };
      setPhase("speaking");
      setMessage("Speaking…");
      await audio.play().catch(() => audio.onerror?.(new Event("error")));
    } catch {
      if (abort.signal.aborted) return;
      setPhase("idle");
      setMessage("Scout could not answer right now.");
    } finally {
      if (request.current === abort) request.current = null;
    }
  }

  async function begin() {
    if (phase !== "idle" || recording.current) return;
    recording.current = true;
    discard.current = false;
    setPhase("listening");
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      recording.current = false;
      setPhase("idle");
      setMessage("Microphone permission is needed to ask by voice.");
      return;
    }
    stream.current = media;
    if (!recording.current) {
      releaseMic();
      setPhase("idle");
      setMessage("Cancelled.");
      return;
    }
    const chunks: Blob[] = [];
    const rec = new MediaRecorder(media);
    recorder.current = rec;
    rec.ondataavailable = (event) => {
      if (event.data.size) chunks.push(event.data);
    };
    rec.onstop = () => {
      releaseMic();
      if (recorder.current === rec) recorder.current = null;
      if (discard.current) return;
      const clip = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      if (
        performance.now() - started.current < MIN_RECORD_MS ||
        clip.size < MIN_BYTES
      ) {
        setPhase("idle");
        setMessage("Too short. Click, speak, then click again to send.");
        return;
      }
      void send(clip);
    };
    started.current = performance.now();
    rec.start();
    limit.current = setTimeout(end, MAX_RECORD_MS);
  }

  function end() {
    if (!recording.current) return;
    recording.current = false;
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  function cancel() {
    const speaking = phase === "speaking";
    reset();
    setPhase("idle");
    setMessage(speaking ? "Stopped." : "Cancelled.");
  }

  const busy =
    phase === "listening" || phase === "thinking" || phase === "speaking";
  const status =
    phase === "off"
      ? "Voice questions need an external backend with API actions enabled."
      : phase === "unavailable"
        ? "Voice Q&A unavailable on this backend."
        : phase === "listening"
          ? "Listening… click again to send."
          : message || "Click to ask Scout about what it has seen.";
  return (
    <section className={`voice-ask ${phase}`} aria-label="Ask Scout by voice">
      <div className="voice-row">
        <button
          className="voice-talk"
          aria-label={phase === "listening" ? "Stop and send" : "Ask Scout"}
          aria-pressed={phase === "listening"}
          disabled={phase !== "idle" && phase !== "listening"}
          onClick={() => (phase === "listening" ? end() : void begin())}
        >
          {phase === "listening" ? <Send size={16} /> : <Mic size={17} />}
        </button>
        <p className="voice-status" aria-live="polite">
          {status}
        </p>
        {busy && (
          <button
            className="voice-stop"
            aria-label={
              phase === "speaking" ? "Stop speaking" : "Cancel question"
            }
            onClick={cancel}
          >
            <Square size={12} />
          </button>
        )}
      </div>
      {exchange && (
        <dl className="voice-exchange" aria-label="Last voice answer">
          <dt>You</dt>
          <dd>{exchange.q}</dd>
          <dt>Scout</dt>
          <dd>{exchange.a}</dd>
        </dl>
      )}
    </section>
  );
}
