import { describe, expect, it } from "vitest";
import type { RelayEvent } from "../api/contract";
import { initialTrace, pendingProposal, traceReducer, type TraceState } from "./trace";

const S = "sess-1";
let seq = 0;
function ev<T extends RelayEvent["type"]>(type: T, data: Extract<RelayEvent, { type: T }>["data"], s = S): RelayEvent {
  seq += 1;
  return { seq, session_id: s, ts: `2026-09-26T16:42:${String(seq).padStart(2, "0")}.000Z`, type, data } as RelayEvent;
}
function fold(events: RelayEvent[], start: TraceState = initialTrace(S)): TraceState {
  return events.reduce((st, event) => traceReducer(st, { type: "event", event }), start);
}

const proposal = {
  proposal_id: "p1",
  idea_id: "i1",
  goal: "write a brief on heat pumps",
  scope_excludes: "solar",
  artifact_kind: "document",
  readback: "Just to confirm: I'll write a brief on heat pumps, leaving out solar, and I'll leave it as a document for you to read. Should I start on it?",
};
const dispatch = { proposal_id: "p1", commitment_id: "c1", task_id: "t1", workflow_id: "task-t1", kind: "research", idea_id: "i1" };

describe("traceReducer", () => {
  it("assembles a task card from proposal → hedge → affirmative → dispatch → delivery", () => {
    seq = 0;
    const st = fold([
      ev("proposal", proposal),
      ev("assent", { proposal_id: "p1", turn_id: "u1", label: "hedge", utterance: "sure, I guess" }),
      ev("proposal", proposal), // re-proposed after the hedge
      ev("assent", { proposal_id: "p1", turn_id: "u2", label: "affirmative", utterance: "yes, go ahead" }),
      ev("dispatch", dispatch),
      ev("task_status", { task_id: "t1", status: "running" }),
      ev("artifact_delivered", { artifact_id: "a1", task_id: "t1", idea_id: "i1", title: "Heat pumps", summary: "ready", markdown: "# hi", sources: [] }),
    ]);
    const card = st.tasks.t1;
    // The card must quote the affirmative words, not the earlier hedge.
    expect(card.assentUtterance).toBe("yes, go ahead");
    expect(card.readback).toBe(proposal.readback);
    expect(card.workflowId).toBe("task-t1");
    expect(card.kind).toBe("research");
    expect(card.status).toBe("succeeded");
    expect(card.artifact?.artifact_id).toBe("a1");
    expect(st.threads.p1.state).toBe("dispatched");
    expect(st.threads.p1.assents.map((a) => a.label)).toEqual(["hedge", "affirmative"]);
    expect(pendingProposal(st)).toBeNull();
  });

  it("a hedge drops the proposal and creates no task", () => {
    seq = 0;
    const st = fold([
      ev("proposal", proposal),
      ev("assent", { proposal_id: "p1", turn_id: "u1", label: "hedge", utterance: "sure, I guess" }),
      ev("proposal_dropped", { proposal_id: "p1", reason: "not_affirmative" }),
    ]);
    expect(st.threads.p1.state).toBe("dropped");
    expect(st.taskOrder).toEqual([]);
    expect(pendingProposal(st)).toBeNull();
  });

  it("ignores replayed events after an SSE reconnect", () => {
    seq = 0;
    const events = [
      ev("user_turn", { turn_id: "u1", text: "hello", channel: "text" }),
      ev("proposal", proposal),
      ev("assent", { proposal_id: "p1", turn_id: "u2", label: "affirmative", utterance: "yes" }),
    ];
    const st = fold([...events, ...events]);
    expect(st.turns).toHaveLength(1);
    expect(st.threads.p1.assents).toHaveLength(1);
    expect(st.events).toHaveLength(3);
  });

  it("keeps a worker status that arrives before the dispatch event", () => {
    seq = 0;
    const st = fold([
      ev("proposal", proposal),
      ev("assent", { proposal_id: "p1", turn_id: "u2", label: "affirmative", utterance: "yes" }),
      ev("task_status", { task_id: "t1", status: "running" }),
      ev("dispatch", dispatch),
      ev("task_status", { task_id: "t1", status: "queued" }), // stale, must not regress
    ]);
    expect(st.tasks.t1.status).toBe("running");
    expect(st.tasks.t1.assentUtterance).toBe("yes");
    expect(st.taskOrder).toEqual(["t1"]);
  });

  it("ignores events of another session", () => {
    seq = 0;
    const st = fold([ev("user_turn", { turn_id: "u1", text: "hi", channel: "text" }, "other-session")]);
    expect(st.turns).toHaveLength(0);
  });
});
