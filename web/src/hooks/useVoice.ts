import { useConversation } from "@elevenlabs/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type RelayApi } from "../api/client";
import type { AgentState } from "../components/BarVisualizer";

export interface Caption {
  role: "user" | "agent";
  text: string;
}

export interface VoiceControls {
  active: boolean;
  agentState: AgentState;
  caption: Caption | null;
  /** Why voice is unavailable (no mic, permission denied, busy, mock). Null when fine. */
  problem: string | null;
  secondsLeft: number | null;
  maxSeconds: number | null;
  start: () => Promise<void>;
  stop: () => void;
  getFrequencyData: () => Uint8Array | undefined;
}

function micErrorMessage(err: unknown): string {
  const name = err instanceof DOMException ? err.name : "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Microphone permission was denied. You can type instead, or allow the microphone in your browser settings and try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No microphone was found. You can type instead.";
  return "The microphone could not be opened. You can type instead.";
}

/**
 * One ElevenLabs voice conversation for a relay demo session, started with a
 * server-issued token (the agent is private). STT, turn-taking and barge-in
 * are handled by ElevenLabs; the Delegator sees the turns because the session
 * id travels as a dynamic variable and in the custom-LLM extra body.
 */
export function useVoice(api: RelayApi, sessionId: string | null): VoiceControls {
  const [caption, setCaption] = useState<Caption | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [deadline, setDeadline] = useState<number | null>(null);
  const [maxSeconds, setMaxSeconds] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [starting, setStarting] = useState(false);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  const conversation = useConversation({
    onMessage: (m) => setCaption({ role: m.role, text: m.message }),
    onError: (message) => setProblem(`Voice error: ${message}`),
    onDisconnect: () => {
      setDeadline(null);
      const sid = sessionRef.current;
      if (sid) api.endVoice(sid).catch(() => undefined);
    },
  });
  const { status, isSpeaking, endSession, startSession, getInputByteFrequencyData, getOutputByteFrequencyData } = conversation;
  const active = status === "connected" || status === "connecting";

  // Tell the viewer up front when the browser already blocks the mic.
  useEffect(() => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setProblem("This browser gives the page no microphone access (it needs HTTPS). You can type instead.");
      return;
    }
    navigator.permissions
      ?.query({ name: "microphone" as PermissionName })
      .then((p) => {
        if (p.state === "denied") setProblem(micErrorMessage(new DOMException("", "NotAllowedError")));
      })
      .catch(() => undefined);
  }, []);

  const stop = useCallback(() => {
    endSession();
    setDeadline(null);
  }, [endSession]);

  // Countdown and the client-side cap (the server enforces its own cap too).
  useEffect(() => {
    if (deadline === null) return;
    const id = setInterval(() => {
      setNow(Date.now());
      if (Date.now() >= deadline) stop();
    }, 500);
    return () => clearInterval(id);
  }, [deadline, stop]);

  const start = useCallback(async () => {
    if (!sessionId || active || starting) return;
    setProblem(null);
    setCaption(null);
    if (api.mode === "mock") {
      setProblem("Voice needs the live relay server. In this mock demo, type below or run a guided scenario.");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setProblem("This browser gives the page no microphone access (it needs HTTPS). You can type instead.");
      return;
    }
    setStarting(true);
    try {
      try {
        const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
        probe.getTracks().forEach((t) => t.stop());
      } catch (err) {
        setProblem(micErrorMessage(err));
        return;
      }
      let creds;
      try {
        creds = await api.getVoiceCredentials(sessionId);
      } catch (err) {
        if (err instanceof ApiError && err.code === "voice_busy")
          setProblem("Someone else is using the voice demo right now. You can type instead, or try again in a few minutes.");
        else setProblem(err instanceof ApiError && err.message ? err.message : "Voice could not be started.");
        return;
      }
      const common = { dynamicVariables: { session_id: sessionId }, customLlmExtraBody: { session_id: sessionId } };
      if (creds.transport === "webrtc") {
        startSession({ ...common, conversationToken: creds.conversation_token, connectionType: "webrtc" });
      } else {
        startSession({ ...common, signedUrl: creds.signed_url, connectionType: "websocket" });
      }
      setMaxSeconds(creds.max_duration_s);
      setDeadline(Date.now() + creds.max_duration_s * 1000);
      setNow(Date.now());
    } finally {
      setStarting(false);
    }
  }, [api, sessionId, active, starting, startSession]);

  const getFrequencyData = useCallback(
    () => (status !== "connected" ? undefined : isSpeaking ? getOutputByteFrequencyData() : getInputByteFrequencyData()),
    [status, isSpeaking, getOutputByteFrequencyData, getInputByteFrequencyData],
  );

  let agentState: AgentState = "idle";
  if (starting || status === "connecting") agentState = "connecting";
  else if (status === "connected") agentState = isSpeaking ? "speaking" : caption?.role === "user" ? "thinking" : "listening";

  return {
    active: active || starting,
    agentState,
    caption,
    problem,
    secondsLeft: deadline === null ? null : Math.max(0, Math.ceil((deadline - now) / 1000)),
    maxSeconds,
    start,
    stop,
    getFrequencyData,
  };
}
