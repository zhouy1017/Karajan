import type { ModelFeedbackState } from "./ModelFeedback";
import type { DraftSelection } from "./conversationDraft";

type RecordValue = Record<string, unknown>;

export type AttemptFact = {
  id: string;
  taskId?: string;
  runId?: string;
  status?: string;
  current?: boolean;
  observed?: number;
};
export type FeedbackFact = Record<
  string,
  { state: ModelFeedbackState; observed: number }
>;

function record(value: unknown): RecordValue | undefined {
  return typeof value === "object" && value !== null
    ? (value as RecordValue)
    : undefined;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : typeof value === "string"
      ? [value]
      : [];
}

function stableState(value: unknown): ModelFeedbackState | undefined {
  return value === "running" ||
    value === "waiting_input" ||
    value === "waiting_dependency" ||
    value === "waiting_output" ||
    value === "idle" ||
    value === "completed" ||
    value === "failed" ||
    value === "cancelled"
    ? value
    : undefined;
}

function observedAt(item: RecordValue): number {
  const feedback = record(item.feedback);
  const value =
    item.observed_at ??
    item.last_feedback_at ??
    feedback?.observed_at ??
    feedback?.last_observed_at;
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return value < 10_000_000_000 ? value * 1000 : value;
}

function flattenAttempts(snapshot: unknown): AttemptFact[] {
  const root = record(snapshot);
  const runs = Array.isArray(root?.runs) ? root.runs : [];
  const summaries = Array.isArray(root?.run_summaries)
    ? root.run_summaries
    : [];
  return [...runs, ...summaries].flatMap((run) => {
    const runRecord = record(run);
    const runId = typeof runRecord?.id === "string" ? runRecord.id : undefined;
    return (
      Array.isArray(runRecord?.attempts) ? runRecord.attempts : []
    ).flatMap((attempt) => {
      const item = record(attempt);
      if (!item || typeof item.id !== "string") return [];
      const feedback = record(item.feedback);
      return [
        {
          id: item.id,
          taskId: typeof item.task_id === "string" ? item.task_id : undefined,
          runId,
          status:
            typeof item.status === "string"
              ? item.status
              : typeof feedback?.state === "string"
                ? feedback.state
                : undefined,
          current: item.current === true || item.is_current === true,
          observed: observedAt(item),
        },
      ];
    });
  });
}

function relatedValues(item: RecordValue): string[] {
  return [
    ...strings(item.id),
    ...strings(item.task_id),
    ...strings(item.source_task_id),
    ...strings(item.attempt_id),
    ...strings(item.run_id),
    ...strings(item.candidate_id),
    ...strings(item.task_ids),
    ...strings(item.attempt_ids),
    ...strings(item.run_ids),
    ...strings(item.source_task_ids),
  ];
}

function candidates(snapshot: unknown): RecordValue[] {
  const root = record(snapshot);
  const candidate = root?.candidate;
  if (Array.isArray(candidate))
    return candidate.flatMap((item) => {
      const value = record(item);
      return value ? [value] : [];
    });
  const value = record(candidate);
  if (!value) return [];
  if (Array.isArray(value.items))
    return value.items.flatMap((item) => {
      const nested = record(item);
      return nested ? [nested] : [];
    });
  return [value];
}

export function candidateSelections(
  snapshot: unknown,
): { kind: "candidate"; id: string }[] {
  return candidates(snapshot).flatMap((candidate) =>
    typeof candidate.id === "string"
      ? [{ kind: "candidate" as const, id: candidate.id }]
      : [],
  );
}

export function currentAttemptId(
  snapshot: unknown,
  selection: DraftSelection,
): string | undefined {
  if (selection?.kind === "attempt") return selection.id;
  if (selection?.kind !== "task") return undefined;
  const root = record(snapshot);
  const tasks = [
    ...((record(root?.proposed_plan)?.tasks as unknown[] | undefined) ?? []),
    ...(Array.isArray(root?.runs)
      ? root.runs.flatMap((run) => {
          const item = record(run);
          return Array.isArray(item?.tasks) ? item.tasks : [];
        })
      : []),
  ];
  const task = tasks.map(record).find((item) => item?.id === selection.id);
  const explicit = task?.current_attempt_id ?? task?.attempt_id;
  if (typeof explicit === "string") return explicit;
  const related = flattenAttempts(snapshot).filter(
    (attempt) => attempt.taskId === selection.id,
  );
  const current = related.filter((attempt) => attempt.current);
  if (current.length === 1) return current[0].id;
  const running = related.filter((attempt) => attempt.status === "running");
  return running.length === 1 ? running[0].id : undefined;
}

export function feedbackFromSnapshot(snapshot: unknown): FeedbackFact {
  return Object.fromEntries(
    flattenAttempts(snapshot).flatMap((attempt) => {
      const state = stableState(attempt.status);
      return state
        ? [[attempt.id, { state, observed: attempt.observed ?? 0 }]]
        : [];
    }),
  );
}

function closure(snapshot: unknown, selection: DraftSelection): Set<string> {
  if (!selection) return new Set();
  const values = new Set([selection.id]);
  const attempts = flattenAttempts(snapshot);
  const pending = [selection.id];
  const add = (value: string | undefined) => {
    if (value && !values.has(value)) {
      values.add(value);
      pending.push(value);
    }
  };
  while (pending.length) {
    const id = pending.shift()!;
    for (const attempt of attempts) {
      if (attempt.id === id || attempt.taskId === id || attempt.runId === id) {
        add(attempt.id);
        add(attempt.taskId);
        add(attempt.runId);
      }
    }
    for (const candidate of candidates(snapshot)) {
      if (relatedValues(candidate).includes(id))
        relatedValues(candidate).forEach(add);
    }
  }
  return values;
}

function matches(item: RecordValue, ids: Set<string>): boolean {
  return relatedValues(item).some((value) => ids.has(value));
}

/** Returns only evidence connected to the selected Task/Attempt/Candidate. */
export function relatedEvidence(
  snapshot: unknown,
  fact: unknown,
  selection: DraftSelection,
): unknown {
  if (!selection || fact == null) return null;
  const ids = closure(snapshot, selection);
  if (Array.isArray(fact)) {
    const items = fact.filter(
      (item) => record(item) && matches(record(item)!, ids),
    );
    return items.length ? items : null;
  }
  const value = record(fact);
  if (!value) return null;
  if (matches(value, ids)) return value;
  for (const key of [
    "items",
    "results",
    "entries",
    "events",
    "checks",
    "reviews",
  ]) {
    if (!Array.isArray(value[key])) continue;
    const items = value[key].filter(
      (item) => record(item) && matches(record(item)!, ids),
    );
    if (items.length) return { ...value, [key]: items };
  }
  return null;
}
