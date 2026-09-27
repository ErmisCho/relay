import { useEffect, useRef } from "react";

export type AgentState = "idle" | "connecting" | "listening" | "thinking" | "speaking";

const LABELS: Record<AgentState, string> = {
  idle: "Ready",
  connecting: "Connecting",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Relay is speaking",
};

/**
 * Bar visualizer modelled on the ElevenLabs UI BarVisualizer
 * (https://ui.elevenlabs.io/docs/components/bar-visualizer): same agent
 * states, bands from the SDK's frequency data while a voice session is live,
 * and a synthetic envelope otherwise (text mode, mock). The ElevenLabs
 * component itself ships as shadcn/Tailwind source, which this app does not use.
 *
 * Bars are updated through refs inside requestAnimationFrame so React does not
 * re-render 60 times a second.
 */
export function BarVisualizer({
  state,
  getFrequencyData,
  barCount = 28,
}: {
  state: AgentState;
  /** Returns 0..255 byte frequency data for the active side (agent output while speaking, mic while listening). */
  getFrequencyData?: () => Uint8Array | undefined;
  barCount?: number;
}) {
  const bars = useRef<(HTMLSpanElement | null)[]>([]);

  useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = (now - start) / 1000;
      const data = getFrequencyData?.();
      for (let i = 0; i < barCount; i++) {
        const el = bars.current[i];
        if (!el) continue;
        const x = (i - (barCount - 1) / 2) / ((barCount - 1) / 2);
        const envelope = Math.exp(-2.2 * x * x);
        let level: number;
        if (data && data.length > 0 && (state === "speaking" || state === "listening")) {
          // Use the lower half of the spectrum (voice energy), mirrored around the centre.
          const band = Math.floor((Math.abs(x) * data.length) / 2);
          level = 0.08 + 0.92 * ((data[band] ?? 0) / 255);
        } else if (reduced) {
          level = state === "speaking" ? 0.5 * envelope + 0.1 : 0.1;
        } else if (state === "speaking") {
          level = 0.12 + 0.88 * envelope * (0.55 + 0.45 * Math.abs(Math.sin(t * 7 + i * 1.7) * Math.cos(t * 3 + i * 0.6)));
        } else if (state === "listening") {
          level = 0.1 + 0.05 * Math.sin(t * 4 + i * 0.3);
        } else if (state === "thinking" || state === "connecting") {
          const sweep = (Math.sin(t * 3 - i * 0.35) + 1) / 2;
          level = 0.08 + 0.12 * sweep;
        } else {
          level = 0.06;
        }
        el.style.transform = `scaleY(${Math.max(0.04, Math.min(1, level)).toFixed(3)})`;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [state, getFrequencyData, barCount]);

  return (
    <div className={`viz viz--${state}`} role="img" aria-label={`Voice activity: ${LABELS[state]}`}>
      {Array.from({ length: barCount }, (_, i) => (
        <span key={i} className="viz__bar" ref={(el) => void (bars.current[i] = el)} />
      ))}
    </div>
  );
}

export function agentStateLabel(state: AgentState): string {
  return LABELS[state];
}
