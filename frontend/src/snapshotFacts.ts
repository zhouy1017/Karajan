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

function headOf(item: RecordValue): string | undefined {
  const value = item.candidate_head ?? item.head ?? item.head_sha;
  return typeof value === "string" ? value : undefined;
}

export function candidateSelections(
  snapshot: CommanderSnapshot | null,
): { kind: "candidate"; id: string; version?: number; head?: string }[] {
  return candidateRecords(snapshot).flatMap((candidate) =>
    typeof candidate.id === "string"
      ? [
          {
            kind: "candidate" as const,
            id: candidate.id,
            version: versionOf(candidate),
            head: headOf(candidate),
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
  knownAttemptIds: Set<string>;
  candidate?: { id: string; version?: number; head?: string };
};

function authoritativeCandidate(
  snapshot: CommanderSnapshot | null,
): { id: string; version?: number; head?: string } | undefined {
  const root = record(snapshot?.candidate);
  const records = candidateRecords(snapshot);
  const selected =
    root && !Array.isArray(root.items) && typeof root.id === "string"
      ? root
      : (records.find(
          (item) => item.current === true || item.is_current === true,
        ) ?? (records.length === 1 ? records[0] : undefined));
  return selected && typeof selected.id === "string"
    ? { id: selected.id, version: versionOf(selected), head: headOf(selected) }
    : undefined;
}

function selectionFacts(
  snapshot: CommanderSnapshot | null,
  selection: DraftSelection,
): SelectionFacts {
  const facts: SelectionFacts = {
    taskIds: new Set(),
    attemptIds: new Set(),
    knownAttemptIds: new Set(snapshot?.attempts.map((attempt) => attempt.id)),
    candidate: authoritativeCandidate(snapshot),
  };
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

function explicitAttemptIds(
  item: RecordValue,
  facts: SelectionFacts,
): string[] {
  const values = typeof item.attempt_id === "string" ? [item.attempt_id] : [];
  if (typeof item.id === "string" && facts.knownAttemptIds.has(item.id))
    values.push(item.id);
  return values;
}

function explicitTaskIds(item: RecordValue): string[] {
  return [item.task_id, item.source_task_id].filter(
    (value): value is string => typeof value === "string",
  );
}

function matchesCandidate(
  item: RecordValue,
  selection: DraftSelection,
  candidate: SelectionFacts["candidate"],
): boolean {
  const itemCandidateId =
    typeof item.candidate_id === "string"
      ? item.candidate_id
      : selection?.kind === "candidate" && typeof item.id === "string"
        ? item.id
        : undefined;
  const expected =
    selection?.kind === "candidate"
      ? { id: selection.id, version: selection.version, head: selection.head }
      : candidate;
  if (!expected) return selection?.kind !== "candidate";
  if (selection?.kind === "candidate" && itemCandidateId === undefined)
    return false;
  if (itemCandidateId !== undefined && itemCandidateId !== expected.id)
    return false;
  const hasVersion =
    typeof item.candidate_version === "number" ||
    (itemCandidateId !== undefined && typeof item.version === "number");
  if (hasVersion && versionOf(item) !== expected.version) return false;
  const hasHead =
    typeof item.candidate_head === "string" ||
    (itemCandidateId !== undefined &&
      (typeof item.head === "string" || typeof item.head_sha === "string"));
  if (hasHead && headOf(item) !== expected.head) return false;
  return true;
}

function directMatch(
  item: RecordValue,
  selection: DraftSelection,
  facts: SelectionFacts,
): boolean {
  if (!selection) return false;
  if (!matchesCandidate(item, selection, facts.candidate)) return false;
  if (selection.kind === "candidate") return true;
  const attemptIds = explicitAttemptIds(item, facts);
  const taskIds = explicitTaskIds(item);
  const hasMatchingTask = taskIds.some((id) => facts.taskIds.has(id));
  if (taskIds.length && !hasMatchingTask) return false;
  if (attemptIds.length) {
    // An explicit child identity is more specific than the parent Task. A
    // Task without a definitive current Attempt cannot import a retry.
    return (
      facts.attemptIds.size > 0 &&
      attemptIds.some((id) => facts.attemptIds.has(id))
    );
  }
  return (
    hasMatchingTask ||
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
