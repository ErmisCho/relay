/**
 * Guided scenarios (TASK-43 AC#6). Each is a list of user utterances sent
 * through the text endpoint, one per assistant reply, so the same buttons work
 * against the mock and against the real Delegator.
 */
export interface Scenario {
  id: string;
  title: string;
  /** What the viewer should watch for. */
  caption: string;
  steps: string[];
}

export const SCENARIOS: Scenario[] = [
  {
    id: "full",
    title: "Full flow",
    caption: "Talk it through, hear the read-back, say yes, get a brief back.",
    steps: [
      "I've been wondering whether a heat pump makes sense for my old flat in Madrid.",
      "Mostly running costs, noise rules and subsidies. Leave out new builds and solar. That's everything, you can start.",
      "Yes, go ahead.",
    ],
  },
  {
    id: "hedge",
    title: "A hedge is not a yes",
    caption: "Relay proposes, you answer “sure, I guess”, and nothing is dispatched.",
    steps: [
      "Could you look into standing desks for our small office? Skip the gaming ones, that's all.",
      "Sure, I guess.",
    ],
  },
  {
    id: "scope",
    title: "Out of scope",
    caption: "Ask it to book something: a clear refusal, no partial attempt.",
    steps: ["Can you book me a heat pump installer for next week?"],
  },
  {
    id: "recall",
    title: "Recall an earlier idea",
    caption: "Bring back an idea from a previous conversation.",
    steps: ["Let's go back to the idea from earlier about cycling to work in winter."],
  },
];
