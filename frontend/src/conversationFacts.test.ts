import { expect, it } from "vitest";
import {
  currentAttemptId,
  feedbackFromSnapshot,
  relatedEvidence,
} from "./conversationFacts";

const snapshot = {
  runs: [
    {
      id: "run-a",
      tasks: [{ id: "task-a", current_attempt_id: "attempt-current" }],
      attempts: [
        { id: "attempt-old", task_id: "task-a", status: "completed" },
        {
          id: "attempt-current",
          task_id: "task-a",
          status: "running",
          current: true,
        },
      ],
    },
  ],
  candidate: { id: "candidate-a", run_id: "run-a", task_ids: ["task-a"] },
};

it("follows Task to its current Attempt and related Candidate evidence", () => {
  expect(currentAttemptId(snapshot, { kind: "task", id: "task-a" })).toBe(
    "attempt-current",
  );
  expect(
    relatedEvidence(
      snapshot,
      {
        items: [
          { attempt_id: "attempt-current", result: "current" },
          { attempt_id: "other" },
        ],
      },
      { kind: "task", id: "task-a" },
    ),
  ).toEqual({ items: [{ attempt_id: "attempt-current", result: "current" }] });
});

it("follows Candidate evidence through its Run and Task to the Attempt", () => {
  expect(
    relatedEvidence(
      snapshot,
      { items: [{ attempt_id: "attempt-current", result: "current" }] },
      { kind: "candidate", id: "candidate-a" },
    ),
  ).toEqual({ items: [{ attempt_id: "attempt-current", result: "current" }] });
});

it("rebuilds known Attempt truth from snapshots without inventing a time", () => {
  expect(feedbackFromSnapshot(snapshot)).toEqual({
    "attempt-old": { state: "completed", observed: 0 },
    "attempt-current": { state: "running", observed: 0 },
  });
});
