/**
 * In-browser stand-in for the TASK-42 demo API (VITE_API_MODE=mock).
 *
 * It is a small scripted Delegator: typed messages are classified with
 * deterministic rules that mirror the real protocol (scope refusal, recall,
 * read-back proposal, hedge/negative/affirmative assent, dispatch, task
 * progress, delivered brief) and answered with the same events the real
 * SSE feed emits. Guided scenarios send the same text to either backend.
 *
 * Deliberately naive keyword rules: fine for the four scripted scenarios and
 * light free typing; it is not a model and does not try to be one.
 */
import { ApiError, type RelayApi, type StreamStatus } from "./client";
import type {
  ArtifactResponse,
  Commitment,
  CurrentIdeaData,
  Idea,
  IdeaEdge,
  ProposalData,
  RelayEvent,
  Source,
  Task,
} from "./contract";

export const MOCK_PASSCODE = "relay-demo";
/** The seeded earlier idea the recall scenario brings back. */
export const SEED_IDEA_ID = "4d5e6f7a-0000-4000-8000-000000000001";

type Emit = (e: Omit<RelayEvent, "seq" | "session_id" | "ts">) => void;

interface MockSession {
  id: string;
  events: RelayEvent[];
  listeners: Set<(e: RelayEvent) => void>;
  currentIdeaId: string | null;
  pending: ProposalData | null;
  timers: ReturnType<typeof setTimeout>[];
}

export interface MockTiming {
  /** Delay before the assistant answers, ms. */
  reply: number;
  /** Delay between dispatch and `running`, ms. */
  start: number;
  /** Delay between `running` and delivery, ms. */
  work: number;
}

const DEFAULT_TIMING: MockTiming = { reply: 900, start: 1200, work: 5000 };

const OUT_OF_SCOPE: [RegExp, string, string][] = [
  [/\b(e-?mail|inbox)\b/i, "email", "sending or drafting email acts on your behalf"],
  [/\b(book|reserve|appointment|calendar|schedule)\b/i, "calendar", "booking and scheduling act in the world"],
  [/\b(buy|order|purchase|pay)\b/i, "purchase", "purchases are irreversible"],
  [/\b(text|whatsapp|message|call)\s+(my|him|her|them)\b/i, "messaging", "messaging people acts on your behalf"],
];
const RECALL = /\b(earlier|last time|the other day|remember|go back to|pick up)\b/i;
const NEGATIVE = /\b(no|nope|don'?t|do not|stop|not yet|hold on|wait)\b/i;
const HEDGE = /\b(i guess|maybe|i suppose|probably|sort of|kind of|not sure|perhaps)\b|\?|\.\.\./i;
const AFFIRM = /^\s*(yes|yeah|yep|yup|sure|go ahead|do it|please do|start|ok|okay|sounds good)\b/i;
const READY = /\b(that'?s (it|everything|all)|write (it|that) up|leave out|skip|you can start)\b/i;

interface Topic {
  match: RegExp;
  title: string;
  goal: string;
  brief: { title: string; markdown: string; summary: string; sources: Source[] };
}

const TOPICS: Topic[] = [
  {
    match: /heat ?pump/i,
    title: "Heat pumps for pre-1980 Madrid flats",
    goal: "write a two-page brief on air-to-water heat pumps versus gas boilers for 1960s–70s Madrid flats, covering running costs, noise rules and subsidies",
    brief: {
      title: "Heat pumps vs gas boilers for pre-1980 Madrid flats",
      summary: "The heat-pump brief is ready: worth it with insulation work first, and subsidies cover a real share.",
      markdown: `> **Demo content.** This brief was written by the demo mock, not by the relay executor. Its structure matches a real delivery.

## Short answer

For a poorly insulated 1960s–70s flat, an air-to-water heat pump pays off **only after basic insulation work** (windows and the façade side you control). Without that, running costs sit close to a modern gas boiler.

## What drives the numbers

| Factor | Heat pump | Gas boiler |
|---|---|---|
| Efficiency | 250–350 % (seasonal COP 2.5–3.5) | 90–95 % |
| Sensitivity to insulation | High | Medium |
| Outdoor unit | Needed; façade rules apply | None |

## Building rules

- Outdoor units on a shared façade usually need the owners' community approval.
- Municipal noise limits apply to night-time operation; check the unit's rating.

## Subsidies

Regional and national programmes have covered part of the cost of heat pumps in existing homes. Check what is open before buying.

## Left out

New builds and solar, as agreed.
`,
      sources: [
        { title: "Heat pump (Wikipedia)", url: "https://en.wikipedia.org/wiki/Heat_pump" },
        { title: "IDAE, Spanish energy agency", url: "https://www.idae.es/" },
      ],
    },
  },
  {
    match: /standing desk/i,
    title: "Standing desks for a small office",
    goal: "compare standing desks for a five-person office",
    brief: {
      title: "Standing desks for a small office",
      summary: "The standing-desk comparison is ready.",
      markdown: "> **Demo content.** Written by the demo mock.\n\n## Summary\n\nA shortlist of three desk types with trade-offs.\n",
      sources: [{ title: "Standing desk (Wikipedia)", url: "https://en.wikipedia.org/wiki/Standing_desk" }],
    },
  },
];

function nowIso(): string {
  return new Date().toISOString();
}

/** UUIDs, like the store. */
function newId(): string {
  return crypto.randomUUID();
}

function topicFor(text: string): Topic | null {
  return TOPICS.find((t) => t.match.test(text)) ?? null;
}

function titleFrom(text: string): string {
  const words = text.replace(/[^\p{L}\p{N}\s'-]/gu, "").split(/\s+/).filter(Boolean).slice(0, 7).join(" ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function excludesFrom(text: string): string | null {
  const m = /\b(?:leave out|skip|without|not)\s+([^.,;]+)/i.exec(text);
  return m ? m[1].trim() : null;
}

/** The server's read-back template (src/relay/delegator/commitment/readback.py). */
export function buildReadback(goal: string, excludes: string): string {
  return `Just to confirm: I'll ${goal}, leaving out ${excludes}, and I'll leave it as a document for you to read. Should I start on it?`;
}

export function createMockApi(timing: MockTiming = DEFAULT_TIMING): RelayApi {
  let authed = false;
  const sessions = new Map<string, MockSession>();

  const yesterday = new Date(Date.now() - 86_400_000).toISOString();
  const ideas = new Map<string, Idea>();
  const edges: IdeaEdge[] = [];
  const commitments: Commitment[] = [];
  const tasks = new Map<string, Task>();
  const artifacts = new Map<string, ArtifactResponse>();

  // Seed: an earlier open idea to recall, and one delivered commitment so the audit trail is never empty.
  const bike: Idea = {
    id: SEED_IDEA_ID,
    title: "Winter bike commute across Madrid",
    summary: "Whether cycling to work through the winter is realistic: routes, clothing, and what to do on rainy days.",
    status: "exploring",
    created_at: yesterday,
    updated_at: yesterday,
  };
  const insurance: Idea = {
    id: "5e6f7a8b-0000-4000-8000-000000000002",
    title: "E-bike insurance options",
    summary: "Compare theft and liability cover for a mid-range e-bike.",
    status: "delivered",
    created_at: yesterday,
    updated_at: yesterday,
  };
  ideas.set(bike.id, bike);
  ideas.set(insurance.id, insurance);
  edges.push({ from_idea: insurance.id, to_idea: bike.id, relation: "spun_off_from" });
  commitments.push({
    id: "6a7b8c9d-0000-4000-8000-000000000003",
    idea_id: insurance.id,
    goal: "compare theft and liability cover for a mid-range e-bike in Spain",
    scope_excludes: "car insurance bundles",
    artifact_kind: "document",
    readback_text: buildReadback("compare theft and liability cover for a mid-range e-bike in Spain", "car insurance bundles"),
    assent_utterance: "Yes, please do that.",
    assented_at: yesterday,
    created_at: yesterday,
  });
  tasks.set("7b8c9d0e-0000-4000-8000-000000000004", {
    id: "7b8c9d0e-0000-4000-8000-000000000004",
    commitment_id: "6a7b8c9d-0000-4000-8000-000000000003",
    kind: "research",
    status: "succeeded",
    dbos_workflow_id: "task-7b8c9d0e-0000-4000-8000-000000000004",
    started_at: yesterday,
    finished_at: yesterday,
    error: null,
    artifact_id: "8c9d0e1f-0000-4000-8000-000000000005",
  });
  artifacts.set("8c9d0e1f-0000-4000-8000-000000000005", {
    id: "8c9d0e1f-0000-4000-8000-000000000005",
    task_id: "7b8c9d0e-0000-4000-8000-000000000004",
    idea_id: insurance.id,
    kind: "document",
    title: "E-bike insurance: theft and liability cover",
    summary: "Theft cover is the expensive part; liability is often already in home insurance.",
    markdown:
      "> **Demo content.** Written by the demo mock.\n\n## Summary\n\nTheft cover is the expensive part; liability is often already included in home insurance.\n",
    sources: [{ title: "Bicycle insurance (Wikipedia)", url: "https://en.wikipedia.org/wiki/Bicycle_insurance" }],
    created_at: yesterday,
  });

  function requireAuth(): void {
    if (!authed) throw new ApiError(401, "unauthorized");
  }

  function session(id: string): MockSession {
    const s = sessions.get(id);
    if (!s) throw new ApiError(404, "not_found", "Unknown session");
    return s;
  }

  function emitter(s: MockSession): Emit {
    return (partial) => {
      const event = { ...partial, seq: s.events.length + 1, session_id: s.id, ts: nowIso() } as RelayEvent;
      s.events.push(event);
      s.listeners.forEach((l) => l(event));
    };
  }

  function later(s: MockSession, ms: number, fn: () => void): void {
    s.timers.push(setTimeout(fn, ms));
  }

  function ideaEvent(idea: Idea, change: CurrentIdeaData["change"]): Omit<RelayEvent, "seq" | "session_id" | "ts"> {
    return {
      type: "current_idea",
      data: {
        idea_id: idea.id,
        title: idea.title,
        status: idea.status,
        change,
        edges: edges.filter((e) => e.from_idea === idea.id || e.to_idea === idea.id),
      },
    };
  }

  function say(s: MockSession, emit: Emit, text: string, delay = timing.reply): void {
    later(s, delay, () => emit({ type: "assistant_turn", data: { turn_id: newId(), text, interrupted: false } }));
  }

  function dispatch(s: MockSession, emit: Emit, proposal: ProposalData, utterance: string): void {
    const commitmentId = newId();
    const taskId = newId();
    const workflowId = `task-${taskId}`;
    const at = nowIso();
    commitments.push({
      id: commitmentId,
      // The store's commitments.idea_id is NOT NULL; the mock always focuses an idea before proposing.
      idea_id: proposal.idea_id ?? newId(),
      goal: proposal.goal,
      scope_excludes: proposal.scope_excludes,
      artifact_kind: proposal.artifact_kind,
      readback_text: proposal.readback,
      assent_utterance: utterance,
      assented_at: at,
      created_at: at,
    });
    const task: Task = {
      id: taskId,
      commitment_id: commitmentId,
      kind: "research",
      status: "queued",
      dbos_workflow_id: workflowId,
      started_at: null,
      finished_at: null,
      error: null,
      artifact_id: null,
    };
    tasks.set(taskId, task);
    const idea = proposal.idea_id ? ideas.get(proposal.idea_id) : undefined;
    if (idea) Object.assign(idea, { status: "committed", updated_at: at });
    emit({
      type: "dispatch",
      data: { proposal_id: proposal.proposal_id, commitment_id: commitmentId, task_id: taskId, workflow_id: workflowId, kind: task.kind, idea_id: proposal.idea_id },
    });
    emit({ type: "task_status", data: { task_id: taskId, status: "queued" } });
    say(s, emit, "Great, I've started on it. I'll let you know when the document is ready — we can keep talking meanwhile.", timing.reply / 2);

    later(s, timing.start, () => {
      task.status = "running";
      task.started_at = nowIso();
      if (idea) Object.assign(idea, { status: "executing", updated_at: nowIso() });
      emit({ type: "task_status", data: { task_id: taskId, status: "running" } });
    });
    later(s, timing.start + timing.work, () => {
      const topic = idea ? topicFor(idea.title) : null;
      const brief = topic?.brief ?? {
        title: idea?.title ?? "Brief",
        summary: "Your document is ready.",
        markdown: `> **Demo content.** Written by the demo mock.\n\n## ${idea?.title ?? "Brief"}\n\nGoal: ${proposal.goal}.\n\nLeft out: ${proposal.scope_excludes}.\n`,
        sources: [],
      };
      const artifactId = newId();
      const artifact: ArtifactResponse = {
        id: artifactId,
        task_id: taskId,
        idea_id: proposal.idea_id,
        kind: "document",
        title: brief.title,
        summary: brief.summary,
        markdown: brief.markdown,
        sources: brief.sources,
        created_at: nowIso(),
      };
      artifacts.set(artifactId, artifact);
      task.status = "succeeded";
      task.finished_at = nowIso();
      task.artifact_id = artifactId;
      if (idea) Object.assign(idea, { status: "delivered", updated_at: nowIso() });
      emit({ type: "task_status", data: { task_id: taskId, status: "succeeded" } });
      emit({
        type: "artifact_delivered",
        data: { artifact_id: artifactId, task_id: taskId, idea_id: proposal.idea_id, title: brief.title, summary: brief.summary, markdown: brief.markdown, sources: brief.sources },
      });
    });
  }

  function respond(s: MockSession, text: string): string {
    const emit = emitter(s);
    const turnId = newId();

    const refusal = OUT_OF_SCOPE.find(([re]) => re.test(text));
    emit({ type: "user_turn", data: { turn_id: turnId, text, channel: "text" } });

    // 1. A pending proposal: this turn is the answer to the read-back.
    if (s.pending) {
      const proposal = s.pending;
      s.pending = null;
      const label = NEGATIVE.test(text) ? "negative" : HEDGE.test(text) ? "hedge" : AFFIRM.test(text) ? "affirmative" : "new_information";
      emit({ type: "assent", data: { proposal_id: proposal.proposal_id, turn_id: turnId, label, utterance: text } });
      if (label === "affirmative") {
        dispatch(s, emit, proposal, text);
      } else {
        emit({ type: "proposal_dropped", data: { proposal_id: proposal.proposal_id, reason: "not_affirmative" } });
        say(
          s,
          emit,
          label === "hedge"
            ? "That didn't sound like a clear yes, so I haven't started anything. What would you want to change?"
            : label === "negative"
              ? "Okay, I won't start it. Let's keep talking."
              : "Got it, I haven't started anything. Tell me more and I'll read the plan back again.",
        );
      }
      return turnId;
    }

    // 2. Out of scope: hard refusal, no partial attempt.
    if (refusal) {
      const [, category, reason] = refusal;
      emit({ type: "scope_refusal", data: { turn_id: turnId, utterance: text, category, reason } });
      say(s, emit, `I can't do that yet — ${category === "email" ? "email" : category === "calendar" ? "booking things" : "that"} isn't something I can do, though that might come in a future version. I'm happy to keep thinking it through with you.`);
      return turnId;
    }

    // 3. Recall an earlier idea.
    if (RECALL.test(text)) {
      const found = [...ideas.values()].find((i) => i.status === "exploring" && i.id !== s.currentIdeaId);
      if (found) {
        s.currentIdeaId = found.id;
        emit(ideaEvent(found, "recalled"));
        emit({ type: "ready_gate", data: { idea_id: found.id, verdict: "keep_talking", reason: "recalled idea still has no agreed goal" } });
        say(s, emit, `Yes — "${found.title}". Last time: ${found.summary} Where do you want to pick it up?`);
        return turnId;
      }
    }

    // 4. Exploring or ready: create/keep the current idea, run the ready gate.
    let idea = s.currentIdeaId ? ideas.get(s.currentIdeaId) ?? null : null;
    const topic = topicFor(text);
    if (!idea || (topic && idea.title !== topic.title)) {
      const at = nowIso();
      idea = { id: newId(), title: topic?.title ?? titleFrom(text), summary: text, status: "exploring", created_at: at, updated_at: at };
      ideas.set(idea.id, idea);
      s.currentIdeaId = idea.id;
      emit(ideaEvent(idea, "created"));
    }
    if (READY.test(text)) {
      emit({ type: "ready_gate", data: { idea_id: idea.id, verdict: "ready_to_execute", reason: "goal and exclusions are stated and you asked to proceed" } });
      const goal = topicFor(idea.title)?.goal ?? `write a short brief on ${idea.title.toLowerCase()}`;
      const proposal: ProposalData = {
        proposal_id: newId(),
        idea_id: idea.id,
        goal,
        scope_excludes: excludesFrom(text) ?? "anything we haven't discussed",
        artifact_kind: "document",
        readback: "",
      };
      proposal.readback = buildReadback(proposal.goal, proposal.scope_excludes);
      s.pending = proposal;
      emit({ type: "proposal", data: proposal });
      say(s, emit, proposal.readback);
    } else {
      emit({ type: "ready_gate", data: { idea_id: idea.id, verdict: "keep_talking", reason: "no agreed goal or exclusions yet" } });
      say(s, emit, "Interesting. What matters most to you here, and is there anything I should leave out?");
    }
    return turnId;
  }

  return {
    mode: "mock",
    passcodeHint: MOCK_PASSCODE,
    async checkAuth() {
      return authed;
    },
    async login(passcode) {
      authed = passcode.trim() === MOCK_PASSCODE;
      return authed;
    },
    async createSession() {
      requireAuth();
      const id = newId();
      sessions.set(id, { id, events: [], listeners: new Set(), currentIdeaId: null, pending: null, timers: [] });
      return { session_id: id, created_at: nowIso(), voice_max_seconds: 600 };
    },
    subscribe(sessionId, afterSeq, onEvent, onStatus: (s: StreamStatus) => void) {
      const s = session(sessionId);
      onStatus("live");
      s.events.filter((e) => e.seq > afterSeq).forEach(onEvent);
      s.listeners.add(onEvent);
      return () => s.listeners.delete(onEvent);
    },
    async sendMessage(sessionId, text) {
      requireAuth();
      const trimmed = text.trim();
      if (!trimmed || trimmed.length > 2000) throw new ApiError(422, "invalid_request", "Message must be 1–2000 characters.");
      return { turn_id: respond(session(sessionId), trimmed) };
    },
    async getVoiceCredentials() {
      requireAuth();
      throw new ApiError(503, "voice_unavailable", "Voice needs the live relay server. In this mock demo, type or run a scenario.");
    },
    async endVoice() {},
    async listIdeas() {
      requireAuth();
      return { ideas: [...ideas.values()], edges: [...edges] };
    },
    async listCommitments() {
      requireAuth();
      return { commitments: [...commitments] };
    },
    async listTasks() {
      requireAuth();
      return { tasks: [...tasks.values()] };
    },
    async getArtifact(id) {
      requireAuth();
      const a = artifacts.get(id);
      if (!a) throw new ApiError(404, "not_found");
      return a;
    },
  };
}
