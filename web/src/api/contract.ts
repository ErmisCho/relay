/**
 * The demo API contract between the relay demo website (TASK-43) and the
 * Delegator's demo endpoints (TASK-42). `web/CONTRACT.md` is the prose
 * version for the backend implementer; this file is the source of truth for
 * shapes. Every path is relative to the Delegator origin (same origin as the
 * site in production; proxied by Vite in development).
 *
 * Conventions
 * - JSON bodies, UTF-8. Timestamps are ISO 8601 strings in UTC with a `Z`
 *   suffix and millisecond precision (`2026-09-26T16:42:04.120Z`).
 * - Ids are opaque strings (the store uses UUIDs; the UI shortens them for display).
 * - Enumerations the backend may extend later (task kind, artifact kind) are
 *   plain strings on purpose: the UI must render unknown values, not crash.
 * - Every endpoint except `POST /demo/auth` requires the demo cookie and
 *   answers 401 `{ "error": "unauthorized" }` without it.
 */

export type ISODateTime = string;

/* ------------------------------------------------------------------ paths */

export const paths = {
  /** POST AuthRequest → 204 + Set-Cookie. 401 on a wrong passcode. GET → 204 if the cookie is valid, else 401. */
  auth: "/demo/auth",
  /** POST (empty body) → 201 CreateSessionResponse. */
  sessions: "/demo/sessions",
  /** GET → text/event-stream of RelayEvent (see SSE framing below). */
  events: (sessionId: string) => `/demo/sessions/${encodeURIComponent(sessionId)}/events`,
  /** POST SendMessageRequest → 202 SendMessageResponse. */
  messages: (sessionId: string) => `/demo/sessions/${encodeURIComponent(sessionId)}/messages`,
  /** POST → 200 VoiceCredentials, 409 voice_busy. DELETE → 204 (releases the voice slot). */
  voice: (sessionId: string) => `/demo/sessions/${encodeURIComponent(sessionId)}/voice`,
  /** GET → 200 IdeasResponse. */
  ideas: "/demo/ideas",
  /** GET → 200 CommitmentsResponse. */
  commitments: "/demo/commitments",
  /** GET (optional ?session_id=) → 200 TasksResponse. */
  tasks: "/demo/tasks",
  /** GET → 200 ArtifactResponse, 404 if unknown. */
  artifact: (artifactId: string) => `/demo/artifacts/${encodeURIComponent(artifactId)}`,
} as const;

/** Name of the HttpOnly session cookie set by POST /demo/auth (informational; the browser never reads it). */
export const AUTH_COOKIE = "relay_demo";

/* ------------------------------------------------------------------ errors */

export type ApiErrorCode =
  | "unauthorized" // 401: missing/invalid cookie or wrong passcode
  | "not_found" // 404: unknown session, artifact, ...
  | "voice_busy" // 409: another live voice session holds the single slot
  | "voice_unavailable" // 503: ElevenLabs not configured / token issuance failed
  | "rate_limited" // 429: too many passcode attempts or messages
  | "invalid_request"; // 400/422: malformed body

export interface ApiErrorBody {
  error: ApiErrorCode;
  /** Human-readable, safe to show to a viewer. Never contains secrets. */
  message?: string;
}

/* -------------------------------------------------------------- auth + session */

export interface AuthRequest {
  passcode: string;
}

export interface CreateSessionResponse {
  session_id: string;
  created_at: ISODateTime;
  /** Server-enforced cap for one voice session, in seconds (e.g. 600). */
  voice_max_seconds: number;
}

/**
 * Short-lived credentials for the private ElevenLabs agent. The ElevenLabs
 * API key never leaves the server; only the token/URL does.
 * - "webrtc": from ElevenLabs `GET /v1/convai/conversation/token` (preferred:
 *   browser echo cancellation makes barge-in reliable).
 * - "websocket": from `GET /v1/convai/conversation/get-signed-url`.
 * The client passes `session_id` to the agent as a dynamic variable AND in
 * `custom_llm_extra_body`, exactly like the Python client, so the Delegator
 * attributes the voice turns to this demo session.
 */
export type VoiceCredentials = (
  | { transport: "webrtc"; conversation_token: string }
  | { transport: "websocket"; signed_url: string }
) & {
  expires_at: ISODateTime;
  /** Seconds this voice session may run; the client ends it at 0 and the server enforces it too. */
  max_duration_s: number;
};

export interface SendMessageRequest {
  /** 1..2000 characters. Goes through the same Delegator path as a voice turn. */
  text: string;
}

export interface SendMessageResponse {
  /** Id of the persisted user turn; the matching `user_turn` event carries it. */
  turn_id: string;
}

/* ------------------------------------------------------------------ events */

/**
 * SSE framing on GET paths.events(sessionId):
 *
 *   id: <seq>\n
 *   data: <RelayEvent JSON>\n
 *   \n
 *
 * - No `event:` field: the type is inside the JSON, so one `onmessage` handler sees everything.
 * - `seq` is a per-session, strictly increasing integer and equals the SSE `id`.
 * - On connect the server first replays every stored event of the session with
 *   seq > (Last-Event-ID header ?? `after` query param ?? 0), then streams live.
 *   A page reload therefore rebuilds the whole trace.
 * - Events are emitted in the order the decisions happened (TASK-42 AC#2).
 * - A comment line `: keepalive` every 15 s keeps ngrok and proxies from closing the stream.
 * - Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
 */
export interface EventEnvelope<T extends string, D> {
  seq: number;
  session_id: string;
  /** When the decision happened on the server (not when it was sent). */
  ts: ISODateTime;
  type: T;
  data: D;
}

export type TurnChannel = "voice" | "text";

export interface UserTurnData {
  turn_id: string;
  text: string;
  channel: TurnChannel;
}

export interface AssistantTurnData {
  turn_id: string;
  /** What the agent said. For voice, the text as ElevenLabs recorded it (truncated on barge-in). */
  text: string;
  /** True when the user barged in before the answer finished. */
  interrupted: boolean;
  /** Time to first token, when known. */
  ttft_ms?: number | null;
}

export interface ScopeRefusalData {
  /** The user turn that was refused. */
  turn_id: string;
  utterance: string;
  /** Short machine-ish reason, e.g. "email", "calendar", "purchase", "browser". */
  category: string;
  /** One sentence a viewer can read: why this is out of v1 scope. */
  reason: string;
}

export type IdeaStatus = "exploring" | "committed" | "executing" | "delivered" | "abandoned";
export type EdgeRelation = "refines" | "supersedes" | "blocks" | "spun_off_from";

export interface IdeaEdge {
  from_idea: string;
  to_idea: string;
  relation: EdgeRelation | string;
}

export interface CurrentIdeaData {
  idea_id: string;
  title: string;
  status: IdeaStatus | string;
  /** created: a new idea; switched: focus moved to another open idea; recalled: brought back via `recall`. */
  change: "created" | "switched" | "recalled";
  /** Edges touching this idea, so the trace can show "refines I-09". */
  edges: IdeaEdge[];
}

export type ReadyVerdict = "keep_talking" | "ready_to_execute";

export interface ReadyGateData {
  idea_id: string | null;
  verdict: ReadyVerdict;
  /** One sentence of evidence, when the scorer gives one. */
  reason?: string | null;
}

export interface ProposalData {
  /** Server-side id of the in-memory pending proposal (not a commitment id yet). */
  proposal_id: string;
  idea_id: string;
  goal: string;
  scope_excludes: string;
  artifact_kind: string; // "document" | "pull_request" | future kinds
  /** The server-built read-back the agent must speak word for word. */
  readback: string;
}

export type AssentLabel = "affirmative" | "hedge" | "negative" | "new_information";

export interface AssentData {
  proposal_id: string;
  turn_id: string;
  label: AssentLabel;
  /** The user's words, verbatim. */
  utterance: string;
}

export type ProposalDropReason =
  | "readback_interrupted" // barge-in cut the read-back, so it was never fully heard
  | "not_affirmative" // assent label was hedge / negative / new_information
  | "scope_guard" // the stored proposal failed the scope guard at dispatch time
  | "expired"; // session ended, restart or eviction

export interface ProposalDroppedData {
  proposal_id: string;
  reason: ProposalDropReason;
}

export interface DispatchData {
  proposal_id: string;
  commitment_id: string;
  task_id: string;
  /** DBOS workflow id (`task-<id>`). */
  workflow_id: string;
  /** Executor capability, e.g. "research" or "code". Rendered as-is. */
  kind: string;
  idea_id: string;
}

export type TaskStatus = "queued" | "running" | "succeeded" | "failed";

export interface TaskStatusData {
  task_id: string;
  status: TaskStatus;
  error?: string | null;
}

export interface Source {
  title: string;
  url: string;
}

export interface ArtifactDeliveredData {
  artifact_id: string;
  task_id: string;
  idea_id: string;
  title: string;
  /** The one-sentence summary relay speaks when it reports back. */
  summary: string;
  /** The brief, as Markdown. The client renders it with raw HTML disabled. */
  markdown: string;
  sources: Source[];
}

export type RelayEvent =
  | EventEnvelope<"user_turn", UserTurnData>
  | EventEnvelope<"assistant_turn", AssistantTurnData>
  | EventEnvelope<"scope_refusal", ScopeRefusalData>
  | EventEnvelope<"current_idea", CurrentIdeaData>
  | EventEnvelope<"ready_gate", ReadyGateData>
  | EventEnvelope<"proposal", ProposalData>
  | EventEnvelope<"assent", AssentData>
  | EventEnvelope<"proposal_dropped", ProposalDroppedData>
  | EventEnvelope<"dispatch", DispatchData>
  | EventEnvelope<"task_status", TaskStatusData>
  | EventEnvelope<"artifact_delivered", ArtifactDeliveredData>;

export type RelayEventType = RelayEvent["type"];

export const EVENT_TYPES: readonly RelayEventType[] = [
  "user_turn",
  "assistant_turn",
  "scope_refusal",
  "current_idea",
  "ready_gate",
  "proposal",
  "assent",
  "proposal_dropped",
  "dispatch",
  "task_status",
  "artifact_delivered",
];

/* ------------------------------------------------------------ read endpoints */

export interface Idea {
  id: string;
  title: string;
  summary: string | null;
  status: IdeaStatus | string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface IdeasResponse {
  ideas: Idea[];
  edges: IdeaEdge[];
}

export interface Commitment {
  id: string;
  idea_id: string;
  goal: string;
  scope_excludes: string;
  artifact_kind: string;
  readback_text: string;
  /** Verbatim words that constituted assent (NOT NULL in the store). */
  assent_utterance: string;
  assented_at: ISODateTime;
  created_at: ISODateTime;
}

export interface CommitmentsResponse {
  commitments: Commitment[];
}

export interface Task {
  id: string;
  commitment_id: string;
  kind: string;
  status: TaskStatus;
  dbos_workflow_id: string | null;
  started_at: ISODateTime | null;
  finished_at: ISODateTime | null;
  error: string | null;
  /** Set once the task delivered an artifact. */
  artifact_id: string | null;
}

export interface TasksResponse {
  tasks: Task[];
}

export interface ArtifactResponse {
  id: string;
  task_id: string;
  idea_id: string;
  kind: string;
  title: string;
  summary: string | null;
  markdown: string;
  sources: Source[];
  created_at: ISODateTime;
}
