import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SCENARIOS } from "../scenarios";
import type { RelayEvent } from "./contract";
import { createMockApi, MOCK_PASSCODE, SEED_IDEA_ID } from "./mock";

/** Plays a guided scenario against the mock exactly as the UI does and returns every event. */
async function play(id: string): Promise<RelayEvent[]> {
  const api = createMockApi({ reply: 10, start: 10, work: 10 });
  await api.login(MOCK_PASSCODE);
  const { session_id } = await api.createSession();
  const events: RelayEvent[] = [];
  api.subscribe(session_id, 0, (e) => events.push(e), () => undefined);
  for (const step of SCENARIOS.find((s) => s.id === id)!.steps) {
    await api.sendMessage(session_id, step);
    await vi.runAllTimersAsync();
  }
  return events;
}

const types = (events: RelayEvent[]) => events.map((e) => e.type);

describe("mock scenarios (AC#6 demo scripts)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("full flow proposes, gets an affirmative, dispatches and delivers a brief", async () => {
    const events = await play("full");
    const t = types(events);
    expect(t).toContain("proposal");
    expect(events.find((e) => e.type === "assent")?.data).toMatchObject({ label: "affirmative", utterance: "Yes, go ahead." });
    expect(t.indexOf("dispatch")).toBeGreaterThan(t.indexOf("assent"));
    expect(t).toContain("artifact_delivered");
    expect(events.map((e) => e.seq)).toEqual(events.map((_, i) => i + 1));
  });

  it("one zero-cost session delivers exactly three explicitly agreed documents", async () => {
    const api = createMockApi({ reply: 10, start: 10, work: 10 });
    await api.login(MOCK_PASSCODE);
    const { session_id } = await api.createSession();
    const events: RelayEvent[] = [];
    api.subscribe(session_id, 0, (e) => events.push(e), () => undefined);
    for (const request of [
      "Research heat pumps, leave out solar, that's everything.",
      "Compare standing desks, skip gaming desks, that's everything.",
      "Write a brief on battery recycling, leave out pricing, that's everything.",
    ]) {
      await api.sendMessage(session_id, request);
      await vi.runAllTimersAsync();
      await api.sendMessage(session_id, "Yes, go ahead.");
      await vi.runAllTimersAsync();
    }

    expect(events.filter((e) => e.type === "assent" && e.data.label === "affirmative")).toHaveLength(3);
    const dispatches = events.filter((e) => e.type === "dispatch");
    const delivered = events.filter((e) => e.type === "artifact_delivered");
    expect(dispatches).toHaveLength(3);
    expect(delivered).toHaveLength(3);
    const taskIds = new Set(dispatches.map((e) => e.data.task_id));
    const tasks = (await api.listTasks()).tasks.filter((task) => taskIds.has(task.id));
    expect(tasks.every((task) => task.status === "succeeded" && task.artifact_id)).toBe(true);
    for (const event of delivered) expect((await api.getArtifact(event.data.artifact_id)).markdown).toBeTruthy();
  });

  it("hedge scenario never dispatches", async () => {
    const events = await play("hedge");
    expect(events.find((e) => e.type === "assent")?.data).toMatchObject({ label: "hedge" });
    expect(types(events)).not.toContain("dispatch");
  });

  it("out-of-scope request is refused with the future-version wording", async () => {
    const events = await play("scope");
    expect(types(events)).toContain("scope_refusal");
    const reply = events.find((e) => e.type === "assistant_turn");
    expect(reply?.type === "assistant_turn" && reply.data.text).toMatch(/might come in a future version/);
    expect(types(events)).not.toContain("proposal");
  });

  it("recall scenario brings back the seeded earlier idea", async () => {
    const events = await play("recall");
    expect(events.find((e) => e.type === "current_idea")?.data).toMatchObject({ change: "recalled", idea_id: SEED_IDEA_ID });
  });

  it("refuses every call without the passcode", async () => {
    const api = createMockApi();
    await expect(api.createSession()).rejects.toMatchObject({ status: 401 });
    expect(await api.login("wrong")).toBe(false);
  });
});
