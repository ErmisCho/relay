import type { RelayEvent } from "../api/contract";

export type EventCategory = "turn" | "decision" | "task";
export type Tone = "turn" | "relay" | "idea" | "ok" | "warn" | "danger" | "task";

export interface EventView {
  category: EventCategory;
  kind: string;
  tone: Tone;
  title: string;
  quote?: string;
  why?: string;
  /** proposal id when the event belongs to a proposal → assent → dispatch chain */
  thread?: string;
  /** task id when a task card should render under this row */
  taskId?: string;
  /** Short text for the screen-reader live region; only key decisions get one. */
  announce?: string;
}

export function shortId(id: string): string {
  // The store uses UUIDs; the first 8 hex digits are enough to tell rows apart in a demo.
  return id.replace(/-/g, "").slice(0, 8);
}

const DROP_REASON: Record<string, string> = {
  readback_interrupted: "the read-back was interrupted, so it was never fully heard",
  not_affirmative: "only a clear yes can start work",
  scope_guard: "the plan failed the scope check",
  expired: "the session ended before a yes",
};

export function describeEvent(e: RelayEvent): EventView {
  switch (e.type) {
    case "user_turn":
      return { category: "turn", kind: "you", tone: "turn", title: e.data.channel === "voice" ? "You said" : "You typed", quote: e.data.text };
    case "assistant_turn":
      return {
        category: "turn",
        kind: "relay",
        tone: "relay",
        title: e.data.interrupted ? "Relay (cut off by barge-in)" : "Relay",
        quote: e.data.text,
      };
    case "scope_refusal":
      return {
        category: "decision",
        kind: "scope refusal",
        tone: "danger",
        title: `Out of scope: ${e.data.category}`,
        why: e.data.reason,
        announce: `Refused as out of scope: ${e.data.category}.`,
      };
    case "current_idea": {
      const verb = e.data.change === "created" ? "New idea" : e.data.change === "recalled" ? "Recalled idea" : "Switched to idea";
      const links = e.data.edges.map((x) => x.relation.replace(/_/g, " ")).join(", ");
      return {
        category: "decision",
        kind: "current idea",
        tone: "idea",
        title: `${verb}: ${e.data.title}`,
        why: links ? `status ${e.data.status}; linked: ${links}` : `status ${e.data.status}`,
        announce: e.data.change === "recalled" ? `Recalled idea ${e.data.title}.` : undefined,
      };
    }
    case "ready_gate":
      return {
        category: "decision",
        kind: "ready gate",
        tone: e.data.verdict === "ready_to_execute" ? "ok" : "turn",
        title: e.data.verdict === "ready_to_execute" ? "Ready to propose" : "Keep talking",
        why: e.data.reason ?? undefined,
      };
    case "proposal":
      return {
        category: "decision",
        kind: "proposal",
        tone: "relay",
        title: "Read-back: relay proposes a plan and waits for a clear yes",
        quote: e.data.readback,
        thread: e.data.proposal_id,
        announce: "Relay proposed a plan and is waiting for a clear yes.",
      };
    case "assent": {
      const yes = e.data.label === "affirmative";
      return {
        category: "decision",
        kind: `assent: ${e.data.label.replace(/_/g, " ")}`,
        tone: yes ? "ok" : "warn",
        title: yes ? "Clear yes: dispatch allowed" : "Not a clear yes: nothing starts",
        quote: e.data.utterance,
        thread: e.data.proposal_id,
        announce: yes ? "Heard a clear yes." : `Classified as ${e.data.label.replace(/_/g, " ")}; nothing was started.`,
      };
    }
    case "proposal_dropped":
      return {
        category: "decision",
        kind: "proposal dropped",
        tone: "warn",
        title: "Proposal withdrawn",
        why: DROP_REASON[e.data.reason] ?? e.data.reason,
        thread: e.data.proposal_id,
      };
    case "dispatch":
      return {
        category: "task",
        kind: "dispatch",
        tone: "task",
        title: `Handed to the executor (${e.data.kind})`,
        thread: e.data.proposal_id,
        taskId: e.data.task_id,
        announce: `Task ${shortId(e.data.task_id)} dispatched.`,
      };
    case "task_status":
      return {
        category: "task",
        kind: "task status",
        tone: e.data.status === "failed" ? "danger" : "task",
        title: `Task ${shortId(e.data.task_id)}: ${e.data.status}`,
        why: e.data.error ?? undefined,
        announce: e.data.status === "failed" ? `Task ${shortId(e.data.task_id)} failed.` : undefined,
      };
    case "artifact_delivered":
      return {
        category: "task",
        kind: "delivered",
        tone: "ok",
        title: `Delivered: ${e.data.title}`,
        why: e.data.summary,
        announce: `Brief delivered: ${e.data.title}.`,
      };
  }
}
