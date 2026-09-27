import { useCallback, useEffect, useState } from "react";
import type { RelayApi } from "../api/client";
import type { Commitment, Idea, IdeaEdge, Task } from "../api/contract";
import { shortId } from "../state/describe";
import type { BriefDoc } from "./BriefReader";

interface Data {
  ideas: Idea[];
  edges: IdeaEdge[];
  commitments: Commitment[];
  tasks: Task[];
}

function when(ts: string): string {
  return new Date(ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

/** Ideas with status and links, and every commitment's read-back and verbatim assent (the audit trail). */
export function IdeasView({ api, refreshKey, onOpenBrief }: { api: RelayApi; refreshKey: number; onOpenBrief: (b: BriefDoc) => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [i, c, t] = await Promise.all([api.listIdeas(), api.listCommitments(), api.listTasks()]);
      setData({ ideas: i.ideas, edges: i.edges, commitments: c.commitments, tasks: t.tasks });
      setError(null);
    } catch {
      setError("Could not load ideas.");
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const openBrief = async (artifactId: string) => {
    try {
      const a = await api.getArtifact(artifactId);
      onOpenBrief({ title: a.title, summary: a.summary, markdown: a.markdown, sources: a.sources, meta: `delivered ${when(a.created_at)}` });
    } catch {
      setError("Could not load that brief.");
    }
  };

  if (!data) return <main className="ideas" id="main"><p className="faint">{error ?? "Loading ideas…"}</p></main>;

  const byId = new Map(data.ideas.map((i) => [i.id, i]));
  const ideas = [...data.ideas].sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  return (
    <main className="ideas" id="main">
      <div className="ideas__head">
        <div>
          <h1>Ideas &amp; audit trail</h1>
          <p className="muted">Every piece of work relay started, with the exact read-back it spoke and the exact words that said yes.</p>
        </div>
        <button type="button" className="btn btn--ghost" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && <p className="tone-danger" role="alert">{error}</p>}
      {ideas.length === 0 && <p className="faint">No ideas yet. Start talking.</p>}
      <ul className="idea-list">
        {ideas.map((idea) => {
          const links = data.edges.filter((e) => e.from_idea === idea.id || e.to_idea === idea.id);
          const commitments = data.commitments.filter((c) => c.idea_id === idea.id);
          return (
            <li key={idea.id} id={`idea-${idea.id}`} className="idea">
              <div className="idea__row">
                <h2 className="idea__title">{idea.title}</h2>
                <span className={`status status--${idea.status}`}>{idea.status}</span>
              </div>
              {idea.summary && <p className="muted">{idea.summary}</p>}
              <p className="mono faint small">
                {shortId(idea.id)} · updated {when(idea.updated_at)}
              </p>
              {links.length > 0 && (
                <ul className="links" aria-label="Links to other ideas">
                  {links.map((e) => {
                    const outgoing = e.from_idea === idea.id;
                    const other = byId.get(outgoing ? e.to_idea : e.from_idea);
                    const rel = e.relation.replace(/_/g, " ");
                    return (
                      <li key={`${e.from_idea}-${e.to_idea}-${e.relation}`}>
                        <span className="mono small faint">{outgoing ? rel : `${rel} (from)`}</span>{" "}
                        {other ? <a href={`#idea-${other.id}`}>{other.title}</a> : <span>{shortId(outgoing ? e.to_idea : e.from_idea)}</span>}
                      </li>
                    );
                  })}
                </ul>
              )}
              {commitments.map((c) => {
                const task = data.tasks.find((t) => t.commitment_id === c.id);
                return (
                  <section key={c.id} className="commitment" aria-label={`Commitment ${shortId(c.id)}`}>
                    <p className="eyebrow">Commitment {shortId(c.id)} · {c.artifact_kind}</p>
                    <dl className="commitment__grid">
                      <dt>Read-back</dt>
                      <dd>
                        <blockquote>{c.readback_text}</blockquote>
                      </dd>
                      <dt>Assent, verbatim</dt>
                      <dd>
                        <blockquote className="assent">&ldquo;{c.assent_utterance}&rdquo;</blockquote>
                        <span className="mono faint small">{when(c.assented_at)}</span>
                      </dd>
                      <dt>Task</dt>
                      <dd className="mono small">
                        {task ? (
                          <>
                            {shortId(task.id)} · {task.kind} · <span className={`status status--${task.status}`}>{task.status}</span>
                            {task.dbos_workflow_id && <span className="faint"> · wf {task.dbos_workflow_id}</span>}
                            {task.error && <span className="tone-danger"> · {task.error}</span>}
                          </>
                        ) : (
                          "not started yet"
                        )}
                      </dd>
                    </dl>
                    {task?.artifact_id && (
                      <button type="button" className="btn btn--accent" onClick={() => void openBrief(task.artifact_id as string)}>
                        Read the brief
                      </button>
                    )}
                  </section>
                );
              })}
            </li>
          );
        })}
      </ul>
    </main>
  );
}
