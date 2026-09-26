import {
  paths,
  type ApiErrorBody,
  type ApiErrorCode,
  type ArtifactResponse,
  type CommitmentsResponse,
  type CreateSessionResponse,
  type IdeasResponse,
  type RelayEvent,
  type SendMessageResponse,
  type TasksResponse,
  type VoiceCredentials,
} from "./contract";

/** offline = the server refused the stream (unknown session, 401) and EventSource gave up. */
export type StreamStatus = "connecting" | "live" | "reconnecting" | "offline";

/** Everything the UI needs from a backend. Implemented by the HTTP client and the mock. */
export interface RelayApi {
  readonly mode: "live" | "mock";
  /** Only the mock sets this: its fixed, non-secret passcode, shown on the gate. */
  readonly passcodeHint?: string;
  checkAuth(): Promise<boolean>;
  /** Resolves false on a wrong passcode; throws ApiError for anything else. */
  login(passcode: string): Promise<boolean>;
  createSession(): Promise<CreateSessionResponse>;
  /** Streams the session's events (replayed from `afterSeq`, then live). Returns an unsubscribe function. */
  subscribe(
    sessionId: string,
    afterSeq: number,
    onEvent: (event: RelayEvent) => void,
    onStatus: (status: StreamStatus) => void,
  ): () => void;
  sendMessage(sessionId: string, text: string): Promise<SendMessageResponse>;
  getVoiceCredentials(sessionId: string): Promise<VoiceCredentials>;
  endVoice(sessionId: string): Promise<void>;
  listIdeas(): Promise<IdeasResponse>;
  listCommitments(): Promise<CommitmentsResponse>;
  listTasks(): Promise<TasksResponse>;
  getArtifact(artifactId: string): Promise<ArtifactResponse>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: ApiErrorCode | "network",
    message?: string,
  ) {
    super(message ?? code);
  }
}

// ngrok's free tier shows an HTML interstitial to browser traffic; this header skips it for fetch calls.
const BASE_HEADERS = { "ngrok-skip-browser-warning": "1" };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      credentials: "same-origin",
      ...init,
      headers: { ...BASE_HEADERS, ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
    });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the relay server.");
  }
  if (!res.ok) {
    let body: Partial<ApiErrorBody> = {};
    try {
      body = (await res.json()) as ApiErrorBody;
    } catch {
      /* non-JSON error body */
    }
    const code = body.error ?? (res.status === 401 ? "unauthorized" : res.status === 404 ? "not_found" : "invalid_request");
    throw new ApiError(res.status, code, body.message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function createHttpApi(): RelayApi {
  return {
    mode: "live",
    async checkAuth() {
      try {
        await request<void>(paths.auth);
        return true;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return false;
        throw e;
      }
    },
    async login(passcode) {
      try {
        await request<void>(paths.auth, { method: "POST", body: JSON.stringify({ passcode }) });
        return true;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return false;
        throw e;
      }
    },
    createSession: () => request(paths.sessions, { method: "POST" }),
    subscribe(sessionId, afterSeq, onEvent, onStatus) {
      onStatus("connecting");
      // EventSource sends Last-Event-ID itself on automatic reconnects; `after` covers the first connect.
      const source = new EventSource(`${paths.events(sessionId)}?after=${afterSeq}`, { withCredentials: true });
      source.onopen = () => onStatus("live");
      source.onerror = () => onStatus(source.readyState === EventSource.CLOSED ? "offline" : "reconnecting");
      source.onmessage = (msg: MessageEvent<string>) => {
        try {
          onEvent(JSON.parse(msg.data) as RelayEvent);
        } catch {
          /* ignore a malformed frame rather than killing the stream */
        }
      };
      return () => source.close();
    },
    sendMessage: (sessionId, text) =>
      request(paths.messages(sessionId), { method: "POST", body: JSON.stringify({ text }) }),
    getVoiceCredentials: (sessionId) => request(paths.voice(sessionId), { method: "POST" }),
    endVoice: (sessionId) => request(paths.voice(sessionId), { method: "DELETE" }),
    listIdeas: () => request(paths.ideas),
    listCommitments: () => request(paths.commitments),
    listTasks: () => request(paths.tasks),
    getArtifact: (id) => request(paths.artifact(id)),
  };
}
