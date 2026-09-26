import { ConversationProvider } from "@elevenlabs/react";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { ApiError, type RelayApi, type StreamStatus } from "./api/client";
import type { RelayEvent } from "./api/contract";
import { createApi } from "./api";
import type { AgentState } from "./components/BarVisualizer";
import { BriefReader, type BriefDoc } from "./components/BriefReader";
import { IdeasView } from "./components/IdeasView";
import { PasscodeGate } from "./components/PasscodeGate";
import { TracePanel } from "./components/TracePanel";
import { Composer, Stage } from "./components/VoicePanel";
import { useVoice } from "./hooks/useVoice";
import type { Scenario } from "./scenarios";
import { shortId } from "./state/describe";
import { initialTrace, traceReducer } from "./state/trace";

type Theme = "light" | "dark" | "system";

function readTheme(): Theme {
  try {
    const t = localStorage.getItem("relay.theme");
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("relay.theme", theme);
    } catch {
      /* storage blocked */
    }
  }, [theme]);
  const cycle = () => setTheme((t) => (t === "system" ? "dark" : t === "dark" ? "light" : "system"));
  return [theme, cycle];
}

export function App() {
  const [api, setApi] = useState<RelayApi | null>(null);
  const [auth, setAuth] = useState<"checking" | "locked" | "ok">("checking");

  useEffect(() => {
    let cancelled = false;
    void createApi().then(async (a) => {
      const ok = await a.checkAuth().catch(() => false);
      if (cancelled) return;
      setApi(a);
      setAuth(ok ? "ok" : "locked");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!api || auth === "checking") return <p className="boot">Loading relay…</p>;
  if (auth !== "ok") return <PasscodeGate api={api} onUnlocked={() => setAuth("ok")} />;
  return (
    <ConversationProvider>
      <Console api={api} onUnauthorized={() => setAuth("locked")} />
    </ConversationProvider>
  );
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function Console({ api, onUnauthorized }: { api: RelayApi; onUnauthorized: () => void }) {
  const [trace, dispatch] = useReducer(traceReducer, null, () => initialTrace());
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [stream, setStream] = useState<StreamStatus>("connecting");
  const [view, setView] = useState<"conversation" | "ideas">("conversation");
  const [sheetOpen, setSheetOpen] = useState(false);
  const [brief, setBrief] = useState<BriefDoc | null>(null);
  const [waitingReply, setWaitingReply] = useState(false);
  const [speakingUntil, setSpeakingUntil] = useState(0);
  const [scenario, setScenario] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [theme, cycleTheme] = useTheme();

  const unsubscribe = useRef<(() => void) | null>(null);
  const listeners = useRef(new Set<(e: RelayEvent) => void>());
  const scenarioAbort = useRef<{ aborted: boolean } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const voice = useVoice(api, sessionId);

  const handleError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.status === 401) onUnauthorized();
      else setError(err instanceof ApiError && err.message ? err.message : "Something went wrong talking to relay.");
    },
    [onUnauthorized],
  );

  const onEvent = useCallback((e: RelayEvent) => {
    dispatch({ type: "event", event: e });
    if (e.type === "assistant_turn") {
      setWaitingReply(false);
      // Text mode has no audio: animate "replied" for roughly the time it would take to say it.
      setSpeakingUntil(Date.now() + Math.min(6000, 60 * e.data.text.split(/\s+/).length));
    }
    listeners.current.forEach((l) => l(e));
  }, []);

  /** Starts a fresh demo session and subscribes to its event stream. */
  const newSession = useCallback(async (): Promise<string | null> => {
    try {
      const s = await api.createSession();
      unsubscribe.current?.();
      dispatch({ type: "reset", sessionId: s.session_id });
      setSessionId(s.session_id);
      setError(null);
      unsubscribe.current = api.subscribe(s.session_id, 0, onEvent, setStream);
      return s.session_id;
    } catch (err) {
      handleError(err);
      return null;
    }
  }, [api, onEvent, handleError]);

  useEffect(() => {
    void newSession();
    return () => unsubscribe.current?.();
  }, [newSession]);

  // Re-render when the text-mode "replied" animation should end.
  useEffect(() => {
    const left = speakingUntil - Date.now();
    if (left <= 0) return;
    const id = setTimeout(() => setSpeakingUntil(0), left);
    return () => clearTimeout(id);
  }, [speakingUntil]);

  const send = useCallback(
    async (text: string, sid = sessionId) => {
      if (!sid) return;
      setWaitingReply(true);
      try {
        await api.sendMessage(sid, text);
      } catch (err) {
        setWaitingReply(false);
        handleError(err);
      }
    },
    [api, sessionId, handleError],
  );

  const nextAssistantTurn = (timeoutMs: number) =>
    new Promise<boolean>((resolve) => {
      const done = (ok: boolean) => {
        listeners.current.delete(listener);
        clearTimeout(timer);
        resolve(ok);
      };
      const listener = (e: RelayEvent) => e.type === "assistant_turn" && done(true);
      const timer = setTimeout(() => done(false), timeoutMs);
      listeners.current.add(listener);
    });

  const runScenario = async (s: Scenario) => {
    if (voice.active) voice.stop();
    const token = { aborted: false };
    scenarioAbort.current = token;
    setScenario(s.id);
    setView("conversation");
    try {
      const sid = await newSession();
      if (!sid) return;
      for (const step of s.steps) {
        if (token.aborted) break;
        const replied = nextAssistantTurn(60_000);
        await send(step, sid);
        if (!(await replied)) {
          setError("relay did not answer within a minute; the scenario stopped.");
          break;
        }
        await sleep(1500);
      }
    } finally {
      if (scenarioAbort.current === token) scenarioAbort.current = null;
      setScenario(null);
    }
  };

  const stopScenario = () => {
    if (scenarioAbort.current) scenarioAbort.current.aborted = true;
  };

  // Type-to-talk takes over when voice cannot work.
  useEffect(() => {
    if (voice.problem) inputRef.current?.focus();
  }, [voice.problem]);

  const textState: AgentState = waitingReply ? "thinking" : speakingUntil > Date.now() ? "speaking" : "idle";
  const timer =
    voice.secondsLeft !== null && voice.maxSeconds !== null
      ? `${fmt(voice.maxSeconds - voice.secondsLeft)} / ${fmt(voice.maxSeconds)}`
      : null;

  return (
    <div className={`app app--${view}`}>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <span className="brand">relay</span>
        <span className="mono faint small hide-sm">{sessionId ? `session ${shortId(sessionId)}` : "no session"}</span>
        {api.mode === "mock" && <span className="badge">mock data</span>}
        <nav className="tabs" aria-label="Views">
          <button type="button" className="tab" aria-current={view === "conversation" ? "page" : undefined} onClick={() => setView("conversation")}>
            Conversation
          </button>
          <button type="button" className="tab" aria-current={view === "ideas" ? "page" : undefined} onClick={() => setView("ideas")}>
            Ideas &amp; audit
          </button>
        </nav>
        <span className="spacer" />
        {timer && (
          <span className="mono small timer" aria-label="Voice time used">
            {timer}
          </span>
        )}
        <button type="button" className="btn btn--ghost btn--small hide-sm" onClick={() => void newSession()} disabled={scenario !== null || voice.active}>
          New session
        </button>
        <button type="button" className="btn btn--ghost btn--small" onClick={cycleTheme} aria-label={`Theme: ${theme}. Change theme`}>
          {theme === "system" ? "Auto" : theme === "dark" ? "Dark" : "Light"}
        </button>
      </header>

      {error && (
        <div className="banner" role="alert">
          {error}
          <button type="button" className="btn btn--ghost btn--small" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}
      {stream === "offline" && (
        <div className="banner" role="alert">
          The live event feed stopped.
          <button type="button" className="btn btn--ghost btn--small" onClick={() => void newSession()}>
            Start a new session
          </button>
        </div>
      )}

      {view === "conversation" ? (
        <div className="workspace">
          <Stage
            trace={trace}
            voice={voice}
            textState={textState}
            scenarioRunning={scenario}
            onRunScenario={(s) => void runScenario(s)}
            onStopScenario={stopScenario}
            onFocusTask={() => setSheetOpen(true)}
          />
          <TracePanel trace={trace} stream={stream} expanded={sheetOpen} onToggleExpanded={() => setSheetOpen((o) => !o)} onOpenBrief={setBrief} />
          <Composer voice={voice} disabled={!sessionId || scenario !== null} onSend={(t) => void send(t)} inputRef={inputRef} />
        </div>
      ) : (
        <IdeasView api={api} refreshKey={trace.taskOrder.length + trace.events.filter((e) => e.type === "task_status" || e.type === "artifact_delivered").length} onOpenBrief={setBrief} />
      )}
      <BriefReader brief={brief} onClose={() => setBrief(null)} />
    </div>
  );
}

function fmt(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
