/**
 * The one client boundary for the Commander snapshot endpoint.
 *
 * `ConversationStore.snapshot` returns flat, top-level task and attempt
 * facts.  Keep that transport shape here so renderers and relationship logic
 * cannot accidentally reinterpret nested Run fixtures as backend data.
 */
type RecordValue = Record<string, unknown>;

export type ConversationFact = {
  id: string;
  project_id: string;
  title?: string;
  state?: string;
  revision?: number;
  commander_profile_ref?: string | null;
  commander_source_ref?: string | null;
};

export type DraftFact = {
  content: string;
  revision?: number;
  selected_task_id?: string | null;
};

export type MessageFact = {
  id: string;
  role: string;
  content?: string;
  text?: string;
};

export type TaskDraftFact = {
  id: string;
  requirement?: string;
  state?: string;
};

export type TaskFact = {
  id: string;
  requirement?: string;
  run_id?: string;
  current_attempt_id?: string;
  attempt_id?: string;
  plan_revision?: number;
  revision?: number;
  role?: string;
  state?: string;
  readiness?: string;
  depends_on?: string[];
  checks?: unknown[];
};

export type AttemptFact = {
  id: string;
  run_id?: string;
  task_id?: string;
  kind?: string;
  state?: string;
  term?: number;
  principal?: string;
  profile?: RecordValue;
  intent_id?: string;
  execution_id?: string;
  admission_state?: string | null;
  reason_codes?: string[];
  next_action?: string | null;
  observed_at?: number;
};

export type AgentFact = {
  run_id?: string;
  principal?: string;
  role?: string;
  profile?: RecordValue;
};

export type CommanderSnapshot = {
  conversation?: ConversationFact;
  messages: MessageFact[];
  draft?: DraftFact;
  task_drafts: TaskDraftFact[];
  runs: string[];
  run_summaries: RecordValue[];
  tasks: TaskFact[];
  attempts: AttemptFact[];
  agents: AgentFact[];
  blockers: RecordValue[];
  candidate?: unknown;
  checks?: unknown;
  review?: unknown;
  logs?: unknown;
  dependencies?: unknown;
  snapshot_event_seq: number;
  freshness?: string;
};

function record(value: unknown): RecordValue | undefined {
  return typeof value === "object" && value !== null
    ? (value as RecordValue)
    : undefined;
}

function records(value: unknown): RecordValue[] {
  return Array.isArray(value)
    ? value.flatMap((item) => {
        const itemRecord = record(item);
        return itemRecord ? [itemRecord] : [];
      })
    : [];
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

function strings(value: unknown): string[] | undefined {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value
    : undefined;
}

function conversation(value: unknown): ConversationFact | undefined {
  const item = record(value);
  if (
    !item ||
    typeof item.id !== "string" ||
    typeof item.project_id !== "string"
  )
    return undefined;
  return {
    id: item.id,
    project_id: item.project_id,
    title: stringValue(item.title),
    state: stringValue(item.state),
    revision: numberValue(item.revision),
    commander_profile_ref:
      typeof item.commander_profile_ref === "string" ||
      item.commander_profile_ref === null
        ? item.commander_profile_ref
        : undefined,
    commander_source_ref:
      typeof item.commander_source_ref === "string" ||
      item.commander_source_ref === null
        ? item.commander_source_ref
        : undefined,
  };
}

function draft(value: unknown): DraftFact | undefined {
  const item = record(value);
  if (!item || typeof item.content !== "string") return undefined;
  return {
    content: item.content,
    revision: numberValue(item.revision),
    selected_task_id:
      typeof item.selected_task_id === "string" ||
      item.selected_task_id === null
        ? item.selected_task_id
        : undefined,
  };
}

function messages(value: unknown): MessageFact[] {
  return records(value).flatMap((item) =>
    typeof item.id === "string" && typeof item.role === "string"
      ? [
          {
            id: item.id,
            role: item.role,
            content: stringValue(item.content),
            text: stringValue(item.text),
          },
        ]
      : [],
  );
}

function taskDrafts(value: unknown): TaskDraftFact[] {
  return records(value).flatMap((item) =>
    typeof item.id === "string"
      ? [
          {
            id: item.id,
            requirement: stringValue(item.requirement),
            state: stringValue(item.state),
          },
        ]
      : [],
  );
}

function tasks(value: unknown): TaskFact[] {
  return records(value).flatMap((item) => {
    if (typeof item.id !== "string") return [];
    return [
      {
        id: item.id,
        run_id: stringValue(item.run_id),
        current_attempt_id: stringValue(item.current_attempt_id),
        attempt_id: stringValue(item.attempt_id),
        plan_revision: numberValue(item.plan_revision),
        revision: numberValue(item.revision),
        role: stringValue(item.role),
        state: stringValue(item.state),
        readiness: stringValue(item.readiness),
        depends_on: strings(item.depends_on),
        checks: Array.isArray(item.checks) ? item.checks : undefined,
      },
    ];
  });
}

function attempts(value: unknown): AttemptFact[] {
  return records(value).flatMap((item) => {
    if (typeof item.id !== "string") return [];
    const profile = record(item.profile);
    return [
      {
        id: item.id,
        run_id: stringValue(item.run_id),
        task_id: stringValue(item.task_id),
        kind: stringValue(item.kind),
        state: stringValue(item.state),
        term: numberValue(item.term),
        principal: stringValue(item.principal),
        profile,
        intent_id: stringValue(item.intent_id),
        execution_id: stringValue(item.execution_id),
        admission_state:
          typeof item.admission_state === "string" ||
          item.admission_state === null
            ? item.admission_state
            : undefined,
        reason_codes: strings(item.reason_codes),
        next_action:
          typeof item.next_action === "string" || item.next_action === null
            ? item.next_action
            : undefined,
        observed_at: numberValue(item.observed_at),
      },
    ];
  });
}

function agents(value: unknown): AgentFact[] {
  return records(value).map((item) => ({
    run_id: stringValue(item.run_id),
    principal: stringValue(item.principal),
    role: stringValue(item.role),
    profile: record(item.profile),
  }));
}

/** Adapt the flat backend projection exactly once at the HTTP boundary. */
export function adaptSnapshot(value: unknown): CommanderSnapshot {
  const root = record(value) ?? {};
  return {
    conversation: conversation(root.conversation),
    messages: messages(root.messages),
    draft: draft(root.draft),
    task_drafts: taskDrafts(root.task_drafts),
    runs: Array.isArray(root.runs)
      ? root.runs.filter((run): run is string => typeof run === "string")
      : [],
    run_summaries: records(root.run_summaries),
    tasks: tasks(root.tasks),
    attempts: attempts(root.attempts),
    agents: agents(root.agents),
    blockers: records(root.blockers),
    candidate: root.candidate,
    checks: root.checks,
    review: root.review,
    logs: root.logs,
    dependencies: root.dependencies,
    snapshot_event_seq: numberValue(root.snapshot_event_seq) ?? 0,
    freshness: stringValue(root.freshness),
  };
}
