import type { ModelFeedbackState } from "./ModelFeedback";
import type { CommanderSnapshot } from "./conversationSnapshot";
import type { DraftSelection } from "./conversationDraft";

type RecordValue = Record<string, unknown>;

export type FeedbackFact = Record<
  string,
  { state: ModelFeedbackState; observed: number }
>;

function record(value: unknown): RecordValue | undefined {
  return typeof value === "object" && value !== null
    ? (value as RecordValue)
    : undefined;
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

function observedAt(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return value < 10_000_000_000 ? value * 1000 : value;
}

function candidateRecords(snapshot: CommanderSnapshot | null): RecordValue[] {
  const candidate = snapshot?.candidate;
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

function versionOf(item: RecordValue): number | undefined {
  const value = item.candidate_version ?? item.version;
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

export function candidateSelections(
  snapshot: CommanderSnapshot | null,
): { kind: "candidate"; id: string; version?: number }[] {
  return candidateRecords(snapshot).flatMap((candidate) =>
    typeof candidate.id === "string"
      ? [
          {
            kind: "candidate" as const,
            id: candidate.id,
            version: versionOf(candidate),
          },
        ]
      : [],
  );
}

/** A Task may name an Attempt; Run ancestry alone is never enough evidence. */
export function currentAttemptId(
  snapshot: CommanderSnapshot | null,
  selection: DraftSelection,
): string | undefined {
  if (!snapshot) return undefined;
  if (selection?.kind === "attempt")
    return snapshot.attempts.some((attempt) => attempt.id === selection.id)
      ? selection.id
      : undefined;
  if (selection?.kind !== "task") return undefined;
  const task = snapshot.tasks.find((item) => item.id === selection.id);
  const explicit = task?.current_attempt_id ?? task?.attempt_id;
  if (
    typeof explicit === "string" &&
    snapshot.attempts.some((attempt) => attempt.id === explicit)
  )
    return explicit;
  const linked = snapshot.attempts.filter(
    (attempt) => attempt.task_id === selection.id,
  );
  return linked.length === 1 ? linked[0].id : undefined;
}

/** Only an explicit, canonical backend Attempt state is feedback truth. */
export function feedbackFromSnapshot(
  snapshot: CommanderSnapshot | null,
): FeedbackFact {
  return Object.fromEntries(
    (snapshot?.attempts ?? []).flatMap((attempt) => {
      const state = stableState(attempt.state);
      return state
        ? [[attempt.id, { state, observed: observedAt(attempt.observed_at) }]]
        : [];
    }),
  );
}

type SelectionFacts = {
  taskIds: Set<string>;
  attemptIds: Set<string>;
};

function selectionFacts(
  snapshot: CommanderSnapshot | null,
  selection: DraftSelection,
): SelectionFacts {
  const facts: SelectionFacts = { taskIds: new Set(), attemptIds: new Set() };
  if (!selection || selection.kind === "candidate") return facts;
  if (selection.kind === "task") {
    facts.taskIds.add(selection.id);
    const current = currentAttemptId(snapshot, selection);
    if (current) facts.attemptIds.add(current);
    return facts;
  }
  facts.attemptIds.add(selection.id);
  const attempt = snapshot?.attempts.find((item) => item.id === selection.id);
  if (attempt?.task_id) facts.taskIds.add(attempt.task_id);
  return facts;
}

function directMatch(
  item: RecordValue,
  selection: DraftSelection,
  facts: SelectionFacts,
): boolean {
  if (!selection) return false;
  if (selection.kind === "candidate") {
    const candidateId = item.candidate_id ?? item.id;
    if (candidateId !== selection.id) return false;
    // Candidate/version evidence is intentionally exact: a versionless
    // selection does not claim a historical version belongs to it.
    return versionOf(item) === selection.version;
  }
  if (
    (typeof item.attempt_id === "string" &&
      facts.attemptIds.has(item.attempt_id)) ||
    (typeof item.id === "string" && facts.attemptIds.has(item.id))
  )
    return true;
  return (
    (typeof item.task_id === "string" && facts.taskIds.has(item.task_id)) ||
    (typeof item.source_task_id === "string" &&
      facts.taskIds.has(item.source_task_id)) ||
    (typeof item.id === "string" && facts.taskIds.has(item.id))
  );
}

function filteredItems(
  value: unknown[],
  selection: DraftSelection,
  facts: SelectionFacts,
): RecordValue[] {
  return value.flatMap((item) => {
    const itemRecord = record(item);
    return itemRecord && directMatch(itemRecord, selection, facts)
      ? [itemRecord]
      : [];
  });
}

/**
 * Evidence is directional: children can display their parent context, but a
 * selected child never widens through that parent to sibling Tasks/Attempts.
 */
export function relatedEvidence(
  snapshot: CommanderSnapshot | null,
  fact: unknown,
  selection: DraftSelection,
): unknown {
  if (!selection || fact == null) return null;
  const facts = selectionFacts(snapshot, selection);
  if (Array.isArray(fact)) {
    const items = filteredItems(fact, selection, facts);
    return items.length ? items : null;
  }
  const value = record(fact);
  if (!value) return null;
  if (directMatch(value, selection, facts)) return value;
  for (const key of [
    "items",
    "results",
    "entries",
    "events",
    "checks",
    "reviews",
  ]) {
    if (!Array.isArray(value[key])) continue;
    const items = filteredItems(value[key], selection, facts);
    if (items.length) return { ...value, [key]: items };
  }
  return null;
}
