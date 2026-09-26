import type { TaskStatus } from "../api/contract";
import type { TaskCardModel } from "../state/trace";
import { shortId } from "../state/describe";
import type { BriefDoc } from "./BriefReader";

const STEPS: { key: TaskStatus | "delivered"; label: string }[] = [
  { key: "queued", label: "Queued" },
  { key: "running", label: "Running" },
  { key: "delivered", label: "Delivered" },
];

function stepState(card: TaskCardModel, key: string): "done" | "active" | "todo" | "failed" {
  if (card.status === "failed") return key === "queued" ? "done" : key === "running" ? "failed" : "todo";
  const reached = card.artifact ? 3 : card.status === "succeeded" ? 2 : card.status === "running" ? 1 : 0;
  const index = STEPS.findIndex((s) => s.key === key);
  if (index < reached) return "done";
  if (index === reached) return "active";
  return "todo";
}

function time(ts?: string): string {
  return ts ? new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "";
}

export function TaskCard({ card, onOpenBrief, highlighted }: { card: TaskCardModel; onOpenBrief: (b: BriefDoc) => void; highlighted?: boolean }) {
  const title = card.artifact?.title ?? card.goal ?? "Delegated task";
  return (
    <section className={`task-card${highlighted ? " task-card--on" : ""}`} aria-label={`Task ${shortId(card.taskId)}: ${card.status}`}>
      <div className="task-card__row mono small">
        <span className="tone-task strong">{shortId(card.taskId)}</span>
        <span className="faint">executor · {card.kind ?? "unknown"}</span>
        <span className="spacer" />
        {card.workflowId && <span className="faint" title="DBOS workflow id">wf {card.workflowId}</span>}
      </div>
      <h4 className="task-card__title">{title}</h4>
      {card.readback && <p className="task-card__desc">{card.readback}</p>}
      {card.assentUtterance && (
        <p className="mono faint small">
          assent &ldquo;{card.assentUtterance}&rdquo; at {time(card.assentedAt)}
        </p>
      )}
      <ol className="steps" aria-label="Progress">
        {STEPS.map((s) => {
          const st = stepState(card, s.key);
          return (
            <li key={s.key} className={`step step--${st}`}>
              <span className="dot" aria-hidden="true" />
              {st === "failed" ? "Failed" : s.label}
              <span className="visually-hidden"> ({st})</span>
            </li>
          );
        })}
      </ol>
      {card.error && <p className="tone-danger small">Error: {card.error}</p>}
      {card.artifact && (
        <button
          type="button"
          className="btn btn--accent"
          onClick={() =>
            card.artifact &&
            onOpenBrief({
              title: card.artifact.title,
              summary: card.artifact.summary,
              markdown: card.artifact.markdown,
              sources: card.artifact.sources,
              meta: `task ${shortId(card.taskId)} · delivered ${time(card.artifact.deliveredAt)}`,
            })
          }
        >
          Read the brief
        </button>
      )}
    </section>
  );
}
