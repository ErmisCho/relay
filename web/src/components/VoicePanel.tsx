import { useEffect, useRef, useState, type FormEvent } from "react";
import type { TraceState } from "../state/trace";
import { SCENARIOS, type Scenario } from "../scenarios";
import type { VoiceControls } from "../hooks/useVoice";
import { BarVisualizer, agentStateLabel, type AgentState } from "./BarVisualizer";

const SUBLABEL: Record<AgentState, string> = {
  idle: "start a conversation, type below, or try a scenario",
  connecting: "opening the microphone",
  listening: "talk any time; silence ends your turn",
  thinking: "scope check → idea → ready gate",
  speaking: "barge in any time, just talk",
};

export function Stage({
  trace,
  voice,
  textState,
  scenarioRunning,
  onRunScenario,
  onStopScenario,
  onFocusTask,
}: {
  trace: TraceState;
  voice: VoiceControls;
  /** Visualizer state while in type-to-talk mode. */
  textState: AgentState;
  scenarioRunning: string | null;
  onRunScenario: (s: Scenario) => void;
  onStopScenario: () => void;
  onFocusTask: () => void;
}) {
  const state = voice.active ? voice.agentState : textState;
  const label = voice.active
    ? agentStateLabel(state)
    : state === "thinking"
      ? "Thinking"
      : state === "speaking"
        ? "Relay replied"
        : "Ready";
  const turns = trace.turns.slice(-6);
  const running = trace.taskOrder.map((id) => trace.tasks[id]).filter((t) => t.status === "queued" || t.status === "running");
  const transcriptEnd = useRef<HTMLLIElement>(null);

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ block: "nearest" });
  }, [trace.turns.length, voice.caption]);

  return (
    <main className="stage" id="main">
      <section className="intro" aria-labelledby="intro-title">
        <h1 id="intro-title" className="intro__title">
          Think out loud. relay only starts work after it reads the plan back and you say a clear yes.
        </h1>
        <div className="scenarios" role="group" aria-label="Guided scenarios">
          {SCENARIOS.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`scenario${scenarioRunning === s.id ? " scenario--on" : ""}`}
              onClick={() => onRunScenario(s)}
              disabled={scenarioRunning !== null}
              aria-describedby={`sc-${s.id}`}
            >
              <span className="scenario__title">{s.title}</span>
              <span className="scenario__caption" id={`sc-${s.id}`}>
                {s.caption}
              </span>
            </button>
          ))}
          {scenarioRunning && (
            <button type="button" className="btn btn--ghost" onClick={onStopScenario}>
              Stop scenario
            </button>
          )}
        </div>
      </section>

      <div className="stage__center">
        <BarVisualizer state={state} getFrequencyData={voice.active ? voice.getFrequencyData : undefined} />
        <div className="stage__label">
          <p className="stage__state">{label}</p>
          <p className="mono faint small">{voice.active ? SUBLABEL[state] : SUBLABEL[state === "speaking" ? "idle" : state]}</p>
        </div>
      </div>

      <ol className="transcript" role="log" aria-label="Transcript" aria-live="polite">
        {turns.length === 0 && !voice.caption && <li className="faint small">The conversation appears here.</li>}
        {turns.map((t) => (
          <li key={`${t.role}-${t.turnId}`} className={`turn turn--${t.role}`}>
            <span className="turn__who mono">{t.role === "user" ? "You" : "Relay"}</span>
            <span className="turn__text">
              {t.text}
              {t.refused && <span className="badge badge--danger">refused: out of scope</span>}
              {t.interrupted && <span className="badge">cut off by barge-in</span>}
            </span>
          </li>
        ))}
        {voice.active && voice.caption && (
          <li className={`turn turn--${voice.caption.role === "user" ? "user" : "assistant"} turn--live`}>
            <span className="turn__who mono">{voice.caption.role === "user" ? "You" : "Relay"} · live</span>
            <span className="turn__text">{voice.caption.text}</span>
          </li>
        )}
        <li ref={transcriptEnd} aria-hidden="true" className="transcript__end" />
      </ol>

      {running.length > 0 && (
        <button type="button" className="pill" onClick={onFocusTask}>
          <span className="dot tone-bg-task pulse" aria-hidden="true" />
          {running.length} task{running.length > 1 ? "s" : ""} running
          <span className="muted">{running[0].goal ?? ""}</span>
        </button>
      )}
    </main>
  );
}

export function Composer({
  voice,
  disabled,
  onSend,
  inputRef,
}: {
  voice: VoiceControls;
  disabled: boolean;
  onSend: (text: string) => void;
  inputRef: React.RefObject<HTMLInputElement | null>;
}) {
  const [text, setText] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setText("");
  };
  return (
    <div className="composer">
      {voice.problem && (
        <p className="composer__notice" role="status">
          {voice.problem}
        </p>
      )}
      <form className="composer__row" onSubmit={submit}>
        <button
          type="button"
          className={`btn btn--call${voice.active ? " btn--call-on" : ""}`}
          onClick={() => (voice.active ? voice.stop() : void voice.start())}
          aria-pressed={voice.active}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="9" y="3" width="6" height="12" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0" />
            <path d="M12 18v3" />
          </svg>
          <span className="btn--call__label">{voice.active ? "End conversation" : "Start conversation"}</span>
        </button>
        <label htmlFor="type-to-talk" className="visually-hidden">
          Type to talk
        </label>
        <input
          id="type-to-talk"
          ref={inputRef}
          className="input"
          type="text"
          autoComplete="off"
          maxLength={2000}
          placeholder={voice.active ? "Voice is on; end it to type" : "Type instead of speaking…"}
          value={text}
          disabled={voice.active}
          onChange={(e) => setText(e.target.value)}
        />
        <button type="submit" className="btn btn--send" aria-label="Send" disabled={disabled || voice.active || !text.trim()}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M5 12h14" />
            <path d="M13 6l6 6-6 6" />
          </svg>
        </button>
      </form>
    </div>
  );
}
