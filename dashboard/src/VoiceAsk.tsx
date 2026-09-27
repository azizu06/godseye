import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Mic, Send, Square } from "lucide-react";
import type { ConnectionConfig } from "./transport";
import { parseActions, type VoiceAction } from "./dashboardActions";
import { mapKey } from "./protocol";

export type VoicePhase =
  "off" | "unavailable" | "idle" | "listening" | "thinking" | "speaking";

export interface VoiceReply {
  question: string | null;
  answer: string | null;
  speech: { mime: string; data: string } | null;
  speechFailed: boolean;
  /** Dashboard view actions; the model's answer is then never shown or spoken as their result. */
  actions: VoiceAction[];
  /** The map the reply was grounded on. */
  scope: string | null;
  /** One-use token to have the applied result spoken in Scout's voice. */
  confirm: string | null;
}

const MAX_RECORD_MS = 15000;
const MIN_RECORD_MS = 400;
const MIN_BYTES = 1024;
// A reply arriving later than this applies nothing, so a slow answer never surprises the operator.
const ACTION_DEADLINE_MS = 30000;

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
  const scope =
    mapKey({
      session_id: value.session_id as string | null | undefined,
      map_epoch: value.map_epoch as number | null | undefined,
    }) ?? null;
  if (value.status === "no_speech")
    return {
      question: null,
      answer: null,
      speech: null,
      speechFailed: false,
      actions: [],
      scope,
      confirm: null,
    };
  const question = text(value.question, 500);
  const answer = text(value.answer, 600);
  const actions = parseActions(value.actions);
  if (
    value.status !== "ok" ||
    !question ||
    !actions ||
    (!answer && !actions.length)
  )
    return null;
  const speech = parseSpeech(value.speech);
  return {
    question,
    answer,
    speech,
    speechFailed: !speech && !actions.length,
    actions,
    scope,
    confirm: actions.length ? text(value.confirm, 64) : null,
  };
}

/** A ready spoken WAV reply, or null. */
export function parseSpeech(
  value: unknown,
): { mime: string; data: string } | null {
  const speech = value as Record<string, unknown> | null;
  const ready =
    !!speech &&
    speech.status === "ready" &&
    speech.mime === "audio/wav" &&
    typeof speech.data === "string" &&
    /^[A-Za-z0-9+/]+={0,2}$/.test(speech.data);
  return ready ? { mime: "audio/wav", data: speech!.data as string } : null;
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
export function VoiceAsk({
  config,
  onActions,
  children,
}: {
  config: ConnectionConfig;
  /** Applies validated view actions and returns what actually happened. */
  onActions?: (actions: VoiceAction[], scope: string | null) => Promise<string>;
  children?: ReactNode;
}) {
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
    const sent = performance.now();
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
      if (!reply.question) {
        setPhase("idle");
        setMessage("No question heard. Click the microphone and speak.");
        return;
      }
      if (reply.actions.length) {
        const result =
          performance.now() - sent > ACTION_DEADLINE_MS
            ? "Scout answered too late, so nothing changed. Ask again."
            : !onActions
              ? "Dashboard actions are unavailable here. Nothing changed."
              : await onActions(reply.actions, reply.scope).catch(
                  () => "The dashboard action failed.",
                );
        setExchange({ q: reply.question, a: result });
        if (abort.signal.aborted) return;
        // Only the applied result is spoken, after it happened, in Scout's own voice.
        const spoken =
          reply.confirm &&
          (await fetch(`${api}/voice/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: reply.confirm, text: result }),
            signal: abort.signal,
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => parseSpeech(data?.speech))
            .catch(() => null));
        if (abort.signal.aborted) return;
        await play(
          spoken || null,
          "Spoken result unavailable; the result is shown.",
        );
        return;
      }
      if (!reply.answer) throw new Error("invalid reply");
      setExchange({ q: reply.question, a: reply.answer });
      await play(
        reply.speech,
        "Spoken reply unavailable; the answer is shown.",
      );
    } catch {
      if (abort.signal.aborted) return;
      setPhase("idle");
      setMessage("Scout could not answer right now.");
    } finally {
      if (request.current === abort) request.current = null;
    }
  }

  async function play(
    speech: { mime: string; data: string } | null,
    unavailable: string,
  ) {
    if (!speech) {
      setPhase("idle");
      setMessage(unavailable);
      return;
    }
    const bytes = Uint8Array.from(atob(speech.data), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: speech.mime }));
    playbackUrl.current = url;
    const audio = new Audio(url);
    player.current = audio;
    audio.onended = () => {
      if (player.current !== audio) return;
      stopPlayback();
      setPhase("idle");
      setMessage("");
    };
    audio.onerror = () => {
      if (player.current !== audio) return;
      stopPlayback();
      setPhase("idle");
      setMessage("Playback unavailable; the text is shown.");
    };
    setPhase("speaking");
    setMessage("Speaking…");
    await audio.play().catch(() => audio.onerror?.(new Event("error")));
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
      {children}
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
