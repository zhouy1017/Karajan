import { expect, it } from "vitest";
import {
  candidateSelections,
  currentAttemptId,
  feedbackFromSnapshot,
  relatedEvidence,
} from "./snapshotFacts";
import { adaptSnapshot } from "./conversationSnapshot";

const snapshot = adaptSnapshot({
  conversation: { id: "conversation-a", project_id: "project-a" },
  messages: [],
  draft: { content: "", revision: 1 },
  task_drafts: [],
  runs: ["run-a"],
  run_summaries: [{ id: "run-a", state: "running" }],
  tasks: [
    {
      id: "task-a",
      run_id: "run-a",
      state: "ready",
      readiness: "ready",
      depends_on: [],
      checks: [],
    },
    {
      id: "task-b",
      run_id: "run-a",
      state: "ready",
      readiness: "ready",
      depends_on: [],
      checks: [],
    },
  ],
  attempts: [
    { id: "attempt-a", run_id: "run-a", task_id: "task-a", state: "running" },
    { id: "attempt-b", run_id: "run-a", task_id: "task-b", state: "completed" },
  ],
  agents: [],
  blockers: [],
  candidate: {
    items: [
      { id: "candidate-a", version: 2 },
      { id: "candidate-a", version: 1 },
    ],
  },
  snapshot_event_seq: 8,
});

it("adapts flat backend Tasks and Planning Attempts without using nested Run collections", () => {
  expect(snapshot.runs).toEqual(["run-a"]);
  expect(snapshot.tasks.map((task) => task.id)).toEqual(["task-a", "task-b"]);
  expect(snapshot.attempts.map((attempt) => attempt.state)).toEqual([
    "running",
    "completed",
  ]);
  expect(currentAttemptId(snapshot, { kind: "task", id: "task-a" })).toBe(
    "attempt-a",
  );
  expect(
    relatedEvidence(
      snapshot,
      {
        items: [
          { attempt_id: "attempt-a", result: "current Attempt of A" },
          { task_id: "task-a", result: "only A" },
          { attempt_id: "attempt-b", result: "historical or sibling B" },
          { task_id: "task-b", result: "only B" },
        ],
      },
      { kind: "task", id: "task-a" },
    ),
  ).toEqual({
    items: [
      { attempt_id: "attempt-a", result: "current Attempt of A" },
      { task_id: "task-a", result: "only A" },
    ],
  });
});

it("keeps a selected child directional and candidate evidence version-exact", () => {
  expect(
    relatedEvidence(
      snapshot,
      {
        items: [
          { attempt_id: "attempt-a", result: "attempt A" },
          { task_id: "task-a", result: "parent task A" },
          { attempt_id: "attempt-b", result: "sibling B" },
          { task_id: "task-b", result: "sibling task B" },
        ],
      },
      { kind: "attempt", id: "attempt-a" },
    ),
  ).toEqual({
    items: [
      { attempt_id: "attempt-a", result: "attempt A" },
      { task_id: "task-a", result: "parent task A" },
    ],
  });
  expect(
    relatedEvidence(
      snapshot,
      {
        items: [
          { candidate_id: "candidate-a", candidate_version: 2, result: "new" },
          { candidate_id: "candidate-a", candidate_version: 1, result: "old" },
        ],
      },
      { kind: "candidate", id: "candidate-a", version: 2 },
    ),
  ).toEqual({
    items: [
      { candidate_id: "candidate-a", candidate_version: 2, result: "new" },
    ],
  });
  expect(candidateSelections(snapshot)).toEqual([
    { kind: "candidate", id: "candidate-a", version: 2 },
    { kind: "candidate", id: "candidate-a", version: 1 },
  ]);
});

it("rebuilds known Attempt truth from snapshots without inventing a time", () => {
  expect(feedbackFromSnapshot(snapshot)).toEqual({
    "attempt-a": { state: "running", observed: 0 },
    "attempt-b": { state: "completed", observed: 0 },
  });
});
