/**
 * Folds the per-session SSE event stream into what the UI shows: the
 * transcript, the "what relay is thinking" summary, proposal threads
 * (proposal → assent(s) → dispatch) and live task cards.
 *
 * Pure and synchronous so it can be unit-tested with scripted streams.
 */
import type {
  ArtifactDeliveredData,
  AssentLabel,
  CurrentIdeaData,
  ISODateTime,
  ProposalData,
  ProposalDropReason,
  ReadyGateData,
  RelayEvent,
  ScopeRefusalData,
  TaskStatus,
} from "../api/contract";

export interface TranscriptTurn {
  turnId: string;
  role: "user" | "assistant";
  text: string;
  ts: ISODateTime;
  channel?: "voice" | "text";
  interrupted?: boolean;
  refused?: boolean;
}

export interface AssentRecord {
  label: AssentLabel;
  utterance: string;
  ts: ISODateTime;
}

export type ThreadState = "awaiting_assent" | "assented" | "dropped" | "dispatched";

export interface ProposalThread {
  proposal: ProposalData;
  proposedAt: ISODateTime;
  assents: AssentRecord[];
  state: ThreadState;
  dropReason?: ProposalDropReason;
  taskId?: string;
}

export interface TaskCardModel {
  taskId: string;
  proposalId?: string;
  commitmentId?: string;
  workflowId?: string;
  kind?: string;
  ideaId?: string;
  /** Goal and read-back from the proposal this task came from. */
  goal?: string;
  readback?: string;
  scopeExcludes?: string;
  /** The verbatim affirmative utterance that authorised the dispatch. */
  assentUtterance?: string;
  assentedAt?: ISODateTime;
  dispatchedAt?: ISODateTime;
  status: TaskStatus;
  statusAt?: ISODateTime;
  error?: string | null;
  artifact?: ArtifactDeliveredData & { deliveredAt: ISODateTime };
}

export interface TraceState {
  sessionId: string | null;
  /** Highest seq applied; events at or below it are replays and are ignored. */
  lastSeq: number;
  events: RelayEvent[];
  turns: TranscriptTurn[];
  currentIdea: CurrentIdeaData | null;
  readyGate: (ReadyGateData & { ts: ISODateTime }) | null;
  refusals: (ScopeRefusalData & { ts: ISODateTime })[];
  threads: Record<string, ProposalThread>;
  threadOrder: string[];
  tasks: Record<string, TaskCardModel>;
  taskOrder: string[];
}

export type TraceAction = { type: "event"; event: RelayEvent } | { type: "reset"; sessionId: string | null };

export function initialTrace(sessionId: string | null = null): TraceState {
  return {
    sessionId,
    lastSeq: 0,
    events: [],
    turns: [],
    currentIdea: null,
    readyGate: null,
    refusals: [],
    threads: {},
    threadOrder: [],
    tasks: {},
    taskOrder: [],
  };
}

const STATUS_RANK: Record<TaskStatus, number> = { queued: 0, running: 1, succeeded: 2, failed: 2 };

/**
 * Whether a `task_status` may replace the card's current status. Status never moves backwards,
 * and a terminal status (succeeded / failed) is final: the store never moves a finished task
 * row again, so a later terminal status is a stale or out-of-order frame, never a correction.
 * (Delivery is the exception and is handled by `artifact_delivered`, which is ground truth.)
 */
function statusMayChange(from: TaskStatus, to: TaskStatus): boolean {
  if (from === "succeeded" || from === "failed") return false;
  return (STATUS_RANK[to] ?? 0) >= (STATUS_RANK[from] ?? 0);
}

function upsertTask(state: TraceState, taskId: string, patch: Partial<TaskCardModel>): TraceState {
  const existing = state.tasks[taskId];
  const card: TaskCardModel = { ...(existing ?? { taskId, status: "queued" }), ...patch };
  return {
    ...state,
    tasks: { ...state.tasks, [taskId]: card },
    taskOrder: existing ? state.taskOrder : [...state.taskOrder, taskId],
  };
}

function updateThread(state: TraceState, id: string, patch: Partial<ProposalThread>): TraceState {
  const thread = state.threads[id];
  if (!thread) return state;
  return { ...state, threads: { ...state.threads, [id]: { ...thread, ...patch } } };
}

/** The summary card keeps showing the current idea; dispatch and delivery move its status on. */
function withIdeaStatus(state: TraceState, ideaId: string | null, status: string): TraceState {
  if (ideaId === null || state.currentIdea?.idea_id !== ideaId) return state;
  return { ...state, currentIdea: { ...state.currentIdea, status } };
}

function applyEvent(state: TraceState, e: RelayEvent): TraceState {
  switch (e.type) {
    case "user_turn":
      return {
        ...state,
        turns: [...state.turns, { turnId: e.data.turn_id, role: "user", text: e.data.text, ts: e.ts, channel: e.data.channel }],
      };
    case "assistant_turn":
      return {
        ...state,
        turns: [
          ...state.turns,
          // turn_id is null only when the store was unreachable; the seq still keys the turn uniquely.
          { turnId: e.data.turn_id ?? `seq-${e.seq}`, role: "assistant", text: e.data.text, ts: e.ts, interrupted: e.data.interrupted },
        ],
      };
    case "scope_refusal":
      return {
        ...state,
        refusals: [...state.refusals, { ...e.data, ts: e.ts }],
        turns: state.turns.map((t) => (t.turnId === e.data.turn_id ? { ...t, refused: true } : t)),
      };
    case "current_idea":
      return { ...state, currentIdea: e.data };
    case "ready_gate":
      return { ...state, readyGate: { ...e.data, ts: e.ts } };
    case "proposal": {
      const id = e.data.proposal_id;
      const known = state.threads[id];
      return {
        ...state,
        threads: {
          ...state.threads,
          // A re-proposal of a known id (e.g. after a hedge) is a fresh read-back waiting for a
          // yes: it leaves the dropped state unless the proposal was already dispatched.
          [id]: known
            ? known.state === "dispatched"
              ? { ...known, proposal: e.data }
              : { ...known, proposal: e.data, proposedAt: e.ts, state: "awaiting_assent", dropReason: undefined }
            : { proposal: e.data, proposedAt: e.ts, assents: [], state: "awaiting_assent" },
        },
        threadOrder: known ? state.threadOrder : [...state.threadOrder, id],
      };
    }
    case "assent": {
      const thread = state.threads[e.data.proposal_id];
      if (!thread) return state;
      const affirmative = e.data.label === "affirmative";
      return updateThread(state, e.data.proposal_id, {
        assents: [...thread.assents, { label: e.data.label, utterance: e.data.utterance, ts: e.ts }],
        // Anything but affirmative cancels the proposal (architecture.md, commitment protocol step 3).
        state: thread.state === "dispatched" ? "dispatched" : affirmative ? "assented" : "dropped",
        dropReason: thread.state === "dispatched" ? undefined : affirmative ? undefined : "not_affirmative",
      });
    }
    case "proposal_dropped": {
      const thread = state.threads[e.data.proposal_id];
      if (!thread || thread.state === "dispatched") return state;
      return updateThread(state, e.data.proposal_id, { state: "dropped", dropReason: e.data.reason });
    }
    case "dispatch": {
      const d = e.data;
      const thread = state.threads[d.proposal_id];
      const assent = thread?.assents.filter((a) => a.label === "affirmative").at(-1);
      let next = updateThread(state, d.proposal_id, { state: "dispatched", taskId: d.task_id, dropReason: undefined });
      next = upsertTask(next, d.task_id, {
        proposalId: d.proposal_id,
        commitmentId: d.commitment_id,
        workflowId: d.workflow_id,
        kind: d.kind,
        ideaId: d.idea_id ?? undefined,
        goal: thread?.proposal.goal,
        readback: thread?.proposal.readback,
        scopeExcludes: thread?.proposal.scope_excludes,
        assentUtterance: assent?.utterance,
        assentedAt: assent?.ts,
        dispatchedAt: e.ts,
        // The worker may report `running` before the Delegator emits `dispatch`; never move status backwards.
        ...(next.tasks[d.task_id] ? {} : { status: "queued" as const, statusAt: e.ts }),
      });
      return withIdeaStatus(next, d.idea_id, "committed");
    }
    case "task_status": {
      const existing = state.tasks[e.data.task_id];
      if (existing && !statusMayChange(existing.status, e.data.status)) return state;
      return upsertTask(state, e.data.task_id, { status: e.data.status, statusAt: e.ts, error: e.data.error ?? null });
    }
    case "artifact_delivered":
      return upsertTask(withIdeaStatus(state, e.data.idea_id, "delivered"), e.data.task_id, {
        artifact: { ...e.data, deliveredAt: e.ts },
        status: "succeeded",
        ideaId: state.tasks[e.data.task_id]?.ideaId ?? e.data.idea_id ?? undefined,
      });
    default:
      return state;
  }
}

export function traceReducer(state: TraceState, action: TraceAction): TraceState {
  if (action.type === "reset") return initialTrace(action.sessionId);
  const e = action.event;
  if (state.sessionId !== null && e.session_id !== state.sessionId) return state;
  if (e.seq <= state.lastSeq) return state; // replay after reconnect
  const next = applyEvent(state, e);
  return { ...next, sessionId: state.sessionId ?? e.session_id, lastSeq: e.seq, events: [...state.events, e] };
}

/** The proposal waiting for a yes/no, if any. */
export function pendingProposal(state: TraceState): ProposalThread | null {
  for (let i = state.threadOrder.length - 1; i >= 0; i--) {
    const t = state.threads[state.threadOrder[i]];
    if (t.state === "awaiting_assent" || t.state === "assented") return t;
  }
  return null;
}

export function latestThread(state: TraceState): ProposalThread | null {
  const id = state.threadOrder.at(-1);
  return id ? state.threads[id] : null;
}
