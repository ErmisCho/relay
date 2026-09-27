import { useMemo, useState } from "react";
import type { StreamStatus } from "../api/client";
import { describeEvent, shortId, type EventCategory } from "../state/describe";
import { latestThread, type TraceState } from "../state/trace";
import type { BriefDoc } from "./BriefReader";
import { TaskCard } from "./TaskCard";

type Filter = "all" | EventCategory;
const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["decision", "Decisions"],
  ["task", "Tasks"],
  ["turn", "Turns"],
];

function clock(ts: string): string {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

const THREAD_STATE: Record<string, string> = {
  awaiting_assent: "waiting for a clear yes",
  assented: "yes heard, dispatching",
  dropped: "withdrawn, nothing started",
  dispatched: "dispatched",
};

export function TracePanel({
  trace,
  stream,
  expanded,
  onToggleExpanded,
  onOpenBrief,
}: {
  trace: TraceState;
  stream: StreamStatus;
  expanded: boolean;
  onToggleExpanded: () => void;
  onOpenBrief: (b: BriefDoc) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | null>(null);

  const views = useMemo(() => trace.events.map((e) => ({ e, v: describeEvent(e) })), [trace.events]);
  const shown = views.filter(({ v }) => filter === "all" || v.category === filter);
  const counts = (f: Filter) => views.filter(({ v }) => f === "all" || v.category === f).length;
  const lastAnnounce = [...views].reverse().find(({ v }) => v.announce)?.v.announce ?? "";

  const thread = latestThread(trace);
  const latestTaskId = trace.taskOrder.at(-1);
  const latestTask = latestTaskId ? trace.tasks[latestTaskId] : undefined;
  const lastAssent = thread?.assents.at(-1);

  return (
    <aside className={`trace${expanded ? " trace--expanded" : ""}`} aria-labelledby="trace-title">
      <button
        type="button"
        className="trace__handle"
        aria-expanded={expanded}
        aria-controls="trace-body"
        onClick={onToggleExpanded}
      >
        <span className="trace__grip" aria-hidden="true" />
        <span className="visually-hidden">{expanded ? "Collapse" : "Expand"} decision trace</span>
      </button>
      <div className="trace__head">
        <h2 id="trace-title">Decision trace</h2>
        <span className={`mono small stream stream--${stream}`}>● {stream}</span>
        <span className="spacer" />
        <span className="mono faint small">{trace.events.length} events</span>
      </div>

      <div id="trace-body" className="trace__body">
        <dl className="summary">
          <div className="summary__cell">
            <dt>Current idea</dt>
            <dd>{trace.currentIdea ? trace.currentIdea.title : "none yet"}</dd>
            <dd className="mono small tone-idea">
              {trace.currentIdea ? `${trace.currentIdea.status} · ${trace.currentIdea.change}` : "starts when you talk"}
            </dd>
          </div>
          <div className="summary__cell">
            <dt>Ready gate</dt>
            <dd>
              {trace.readyGate ? (trace.readyGate.verdict === "ready_to_execute" ? "Ready to propose" : "Keep talking") : "not scored yet"}
            </dd>
            <dd className="mono small tone-ok">{trace.readyGate?.reason ?? "a hint, never a trigger"}</dd>
          </div>
          <div className="summary__cell">
            <dt>Proposal</dt>
            <dd>{thread ? `${shortId(thread.proposal.proposal_id)} · ${THREAD_STATE[thread.state]}` : "none"}</dd>
            <dd className="mono small tone-relay">{lastAssent ? `last answer: ${lastAssent.label.replace(/_/g, " ")}` : "needs read-back + clear yes"}</dd>
          </div>
          <div className="summary__cell">
            <dt>Scope</dt>
            <dd>Research &amp; writing only</dd>
            <dd className="mono small tone-danger">
              {trace.refusals.length} refusal{trace.refusals.length === 1 ? "" : "s"} this session
            </dd>
          </div>
        </dl>

        {thread && thread.state !== "dispatched" && (
          <div className="pending" aria-label="Pending proposal">
            <p className="eyebrow">Read-back {thread.state === "dropped" ? "(withdrawn)" : "(waiting for a clear yes)"}</p>
            <blockquote>{thread.proposal.readback}</blockquote>
          </div>
        )}

        {latestTask && (
          <div className="trace__latest-task">
            <TaskCard card={latestTask} onOpenBrief={onOpenBrief} />
          </div>
        )}

        <div className="filters" role="group" aria-label="Filter events">
          {FILTERS.map(([key, label]) => (
            <button key={key} type="button" className="chip" aria-pressed={filter === key} onClick={() => setFilter(key)}>
              {label} <span className="mono small">{counts(key)}</span>
            </button>
          ))}
        </div>

        <div className="log" tabIndex={0} aria-label="Event log, newest at the bottom">
          <ol className="log__list">
            {shown.length === 0 && <li className="faint small log__empty">Events appear here the moment relay decides something.</li>}
            {shown.map(({ e, v }) => {
              const on = selected !== null && v.thread === selected;
              const card = v.taskId ? trace.tasks[v.taskId] : undefined;
              return (
                <li key={e.seq} className={`row${on ? " row--on" : ""}`}>
                  <div className="row__meta">
                    <time className="mono faint small" dateTime={e.ts}>
                      {clock(e.ts)}
                    </time>
                    <span className={`dot tone-bg-${v.tone}`} aria-hidden="true" />
                    <span className={`mono small tone-${v.tone}`}>{v.kind}</span>
                    <span className="spacer" />
                    {v.thread && (
                      <button
                        type="button"
                        className="tag mono"
                        aria-pressed={on}
                        onClick={() => setSelected(on ? null : v.thread ?? null)}
                        title="Highlight this proposal's chain"
                      >
                        {shortId(v.thread)}
                      </button>
                    )}
                  </div>
                  <div className="row__body">
                    <p className="row__title">{v.title}</p>
                    {v.quote && <blockquote className="row__quote">&ldquo;{v.quote}&rdquo;</blockquote>}
                    {v.why && <p className="mono small muted">why → {v.why}</p>}
                    {card && <TaskCard card={card} onOpenBrief={onOpenBrief} highlighted={on} />}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
      <p className="visually-hidden" aria-live="polite">
        {lastAnnounce}
      </p>
    </aside>
  );
}
