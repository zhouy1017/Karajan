import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./CommanderWorkbench.css";
import {
  ModelFeedback,
  type ConnectionStatus,
  type ModelFeedbackState,
} from "./ModelFeedback";
import {
  ConversationDraftLedger,
  type DraftContext,
  type DraftSelection,
  type DraftState,
} from "./conversationDraft";
import { CommandRegistry } from "./commandRegistry";
import {
  candidateSelections,
  currentAttemptId,
  feedbackFromSnapshot,
  relatedEvidence,
} from "./conversationFacts";
import { SnapshotRecovery, type RecoveryContext } from "./snapshotRecovery";

type Project = {
  id: string;
  name: string;
  repository: { root: string; base_ref: string };
  target_branch: string;
};
type Conversation = {
  id: string;
  project_id: string;
  title: string;
  state?: string;
  revision?: number;
  commander_profile_ref?: string | null;
  commander_source_ref?: string | null;
  draft_revision?: number;
};
type Snapshot = {
  conversation?: Conversation;
  messages?: { id: string; role: string; content?: string; text?: string }[];
  draft?: {
    content: string;
    revision?: number;
    selected_task_id?: string | null;
  };
  task_drafts?: { id: string; requirement?: string; state?: string }[];
  runs?: {
    id: string;
    status?: string;
    requirement?: string | { goal?: string };
    tasks?: WorkTask[];
    attempts?: { id: string; status?: string; task_id?: string }[];
  }[];
  run_summaries?: {
    id: string;
    state?: string;
    snapshot_event_seq?: number;
    tasks?: WorkTask[];
    attempts?: { id: string; status?: string; task_id?: string }[];
  }[];
  proposed_plan?: {
    summary?: string;
    proposal_revision?: number;
    status?: string;
    tasks?: {
      id: string;
      title?: string;
      role?: string;
      model?: string;
      source?: string;
      dependencies?: string[];
      status?: string;
    }[];
  };
  candidate?: unknown;
  checks?: unknown;
  review?: unknown;
  logs?: unknown;
  dependencies?: unknown;
  snapshot_event_seq?: number;
  freshness?: string;
};
type WorkTask = {
  id: string;
  title?: string;
  role?: string;
  model?: string;
  source?: string;
  dependencies?: string[];
  status?: string;
  attempt_id?: string;
};

type Attempt = {
  id: string;
  status?: string;
  task_id?: string;
  run_id?: string;
};
type Selection = DraftSelection;
type FeedbackByAttempt = Record<
  string,
  { state: ModelFeedbackState; observed: number }
>;

type CommanderOption = {
  profile_ref: string;
  profile_revision?: number;
  source_ref: string;
  source_label?: string;
};

const tabs = [
  "Hub",
  "Diff",
  "Checks",
  "Review",
  "Logs",
  "Dependencies",
] as const;
type Tab = (typeof tabs)[number];

async function read<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.reason_code === "string"
        ? body.reason_code
        : response.status === 404
          ? "此项目还没有 Commander 会话。"
          : "暂时无法读取工作台状态。",
    );
  }
  return response.json() as Promise<T>;
}

async function describeError(
  response: Response,
  fallback: string,
): Promise<Error> {
  const body = await response.json().catch(() => ({}));
  const reason =
    typeof body.reason_code === "string" ? `（${body.reason_code}）` : "";
  const revision =
    typeof body.current_revision === "number"
      ? ` 当前版本：${body.current_revision}。`
      : "";
  return new Error(`${fallback}${reason}${revision}`);
}

function stableKey(prefix: string, value: string): string {
  return `${prefix}:${value}`.slice(0, 200);
}

function contextKey(context: DraftContext): string {
  return `${context.projectId}:${context.conversationId}`;
}

function feedbackState(
  eventName: string,
  value: Record<string, unknown>,
): ModelFeedbackState | null {
  const payload =
    typeof value.payload === "object" && value.payload !== null
      ? (value.payload as Record<string, unknown>)
      : value;
  const state = payload.state ?? value.state;
  if (
    state === "running" ||
    state === "waiting_input" ||
    state === "waiting_dependency" ||
    state === "waiting_output" ||
    state === "idle" ||
    state === "completed" ||
    state === "failed" ||
    state === "cancelled"
  )
    return state;
  if (
    eventName === "waiting_input" ||
    eventName === "waiting_dependency" ||
    eventName === "waiting_output" ||
    eventName === "completed" ||
    eventName === "failed" ||
    eventName === "cancelled"
  )
    return eventName;
  if (
    eventName === "progress" ||
    eventName === "feedback" ||
    eventName === "model_feedback"
  )
    return "running";
  return null;
}

function settingsStorageKey(pid: string): string {
  return `karajan:commander-creation-settings:${pid}`;
}

function eventAttemptId(value: Record<string, unknown>): string | undefined {
  const payload =
    typeof value.payload === "object" && value.payload !== null
      ? (value.payload as Record<string, unknown>)
      : value;
  const attemptId = payload.attempt_id ?? value.attempt_id;
  return typeof attemptId === "string" ? attemptId : undefined;
}

function observedTimestamp(value: Record<string, unknown>): number {
  const payload =
    typeof value.payload === "object" && value.payload !== null
      ? (value.payload as Record<string, unknown>)
      : value;
  const observed = payload.observed_at ?? value.observed_at;
  if (typeof observed !== "number" || !Number.isFinite(observed)) return 0;
  return observed < 10_000_000_000 ? observed * 1000 : observed;
}

export function CommanderWorkbench({
  projects,
  csrf,
}: {
  projects: Project[];
  csrf: string;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [conversations, setConversations] = useState<
    Record<string, Conversation[]>
  >({});
  const [projectId, setProjectId] = useState(
    () =>
      sessionStorage.getItem("karajan:commander-project") ??
      projects[0]?.id ??
      "",
  );
  const [conversationId, setConversationId] = useState(() => {
    const pid =
      sessionStorage.getItem("karajan:commander-project") ??
      projects[0]?.id ??
      "";
    return (
      sessionStorage.getItem(`karajan:commander-conversation:${pid}`) ?? ""
    );
  });
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [options, setOptions] = useState<CommanderOption[]>([]);
  const [tab, setTab] = useState<Tab>("Hub");
  const [draft, setDraft] = useState("");
  const [draftDirty, setDraftDirty] = useState(false);
  const [profile, setProfile] = useState("");
  const [source, setSource] = useState("");
  const [selection, setSelection] = useState<Selection>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedbackByAttempt, setFeedbackByAttempt] = useState<FeedbackByAttempt>(
    {},
  );
  const [connection, setConnection] = useState<ConnectionStatus>("unknown");
  const [subscriptionVersion, setSubscriptionVersion] = useState(0);
  const eventSeqRef = useRef(0);
  // Only an explicit project/conversation change advances this generation.
  const navigation = useRef(0);
  const projectRef = useRef(projectId);
  const conversationRef = useRef(conversationId);
  const draftRef = useRef(draft);
  const selectionRef = useRef<Selection>(selection);
  const draftQueues = useRef(new Map<string, Promise<boolean>>());
  const draftLedger = useRef(
    new ConversationDraftLedger(sessionStorage, () => crypto.randomUUID()),
  );
  const commandRegistry = useRef(
    new CommandRegistry(() => crypto.randomUUID(), sessionStorage),
  );
  const creationSettings = useRef<
    Record<string, { profile: string; source: string }>
  >({});
  const streamRef = useRef<EventSource | null>(null);
  const streamContext = useRef<RecoveryContext | null>(null);
  const snapshotReader = useRef<
    (context: RecoveryContext, signal: AbortSignal) => Promise<Snapshot>
  >(async () => {
    throw new Error("会话恢复尚未初始化。");
  });
  const snapshotApplier = useRef<
    (context: RecoveryContext, snapshot: Snapshot) => void
  >(() => undefined);
  const snapshotError = useRef<
    (context: RecoveryContext, error: Error) => void
  >(() => undefined);
  const recovery = useRef<SnapshotRecovery<Snapshot> | null>(null);
  if (!recovery.current)
    recovery.current = new SnapshotRecovery(
      (context, signal) => snapshotReader.current(context, signal),
      (context, value) => snapshotApplier.current(context, value),
      (context, error) => snapshotError.current(context, error),
    );
  const project = projects.find((item) => item.id === projectId) ?? projects[0];

  useEffect(() => () => recovery.current?.cancel(), []);

  useEffect(() => {
    projectRef.current = projectId;
    conversationRef.current = conversationId;
    draftRef.current = draft;
    selectionRef.current = selection;
  }, [projectId, conversationId, draft, selection]);

  const presentDraft = useCallback((state: DraftState | undefined) => {
    if (!state) return;
    setDraft(state.content);
    setDraftDirty(state.dirty);
    setSelection(state.selection);
    // A draft acknowledgement is not authority to clear an unrelated command
    // error.  In particular, a blur-save can complete after a message POST
    // loses its response body.
    if (state.error) setError(state.error);
  }, []);

  const rememberCreationSettings = useCallback(
    (pid: string, nextProfile: string, nextSource: string) => {
      creationSettings.current[pid] = {
        profile: nextProfile,
        source: nextSource,
      };
      sessionStorage.setItem(
        settingsStorageKey(pid),
        JSON.stringify({ profile: nextProfile, source: nextSource }),
      );
    },
    [],
  );

  const loadConversations = useCallback(async (id: string) => {
    try {
      const data = await read<{ items?: Conversation[] }>(
        `/v1/projects/${encodeURIComponent(id)}/conversations`,
      );
      setConversations((old) => ({ ...old, [id]: data.items ?? [] }));
      return data.items ?? [];
    } catch (cause) {
      if (projectRef.current === id)
        setError(cause instanceof Error ? cause.message : "无法读取会话。");
      return [];
    }
  }, []);

  const loadOptions = useCallback(async (id: string, token: number) => {
    try {
      const data = await read<{ items?: CommanderOption[] }>(
        `/v1/projects/${encodeURIComponent(id)}/commander-options`,
      );
      if (token === navigation.current && projectRef.current === id)
        setOptions(data.items ?? []);
    } catch (cause) {
      if (token === navigation.current && projectRef.current === id)
        setError(
          cause instanceof Error
            ? cause.message
            : "无法读取已配置的 Commander 选项。",
        );
    }
  }, []);

  useEffect(() => {
    projects.forEach((item) => void loadConversations(item.id));
  }, [projects, loadConversations]);

  const active = useCallback(
    (context: RecoveryContext) =>
      context.navigation === navigation.current &&
      context.projectId === projectRef.current &&
      context.conversationId === conversationRef.current,
    [],
  );

  snapshotReader.current = async (context, signal) => {
    const data = await read<Snapshot>(
      `/v1/conversations/${encodeURIComponent(context.conversationId)}/snapshot`,
      { signal },
    );
    if (data.conversation?.project_id !== context.projectId)
      throw new Error("CROSS_PROJECT_REFERENCE");
    return data;
  };
  snapshotApplier.current = (context, data) => {
    if (!active(context)) return;
    setSnapshot(data);
    eventSeqRef.current = data.snapshot_event_seq ?? 0;
    const restoredId = data.draft?.selected_task_id ?? null;
    const selectionFromServer: DraftSelection = restoredId
      ? [...(data.runs ?? []), ...(data.run_summaries ?? [])].some((run) =>
          (run.attempts ?? []).some((attempt) => attempt.id === restoredId),
        )
        ? { kind: "attempt", id: restoredId }
        : candidateSelections(data).some((item) => item.id === restoredId)
          ? { kind: "candidate", id: restoredId }
          : { kind: "task", id: restoredId }
      : null;
    presentDraft(
      draftLedger.current.open(
        {
          projectId: context.projectId,
          conversationId: context.conversationId,
        },
        {
          content: data.draft?.content ?? "",
          revision: data.draft?.revision ?? 0,
          selection: selectionFromServer,
        },
      ),
    );
    setFeedbackByAttempt(feedbackFromSnapshot(data));
    setProfile(data.conversation?.commander_profile_ref ?? "");
    setSource(data.conversation?.commander_source_ref ?? "");
    setBusy(false);
    streamContext.current = { ...context };
    void loadOptions(context.projectId, context.navigation);
    setSubscriptionVersion((old) => old + 1);
  };
  snapshotError.current = (context, cause) => {
    if (!active(context)) return;
    setBusy(false);
    setConnection("disconnected");
    setError(cause.message || "无法读取会话快照，将自动重试。");
  };

  const openConversation = useCallback(
    (id: string, pid = projectId) => {
      if (!projects.some((item) => item.id === pid)) return;
      const context: RecoveryContext = {
        navigation: ++navigation.current,
        projectId: pid,
        conversationId: id,
      };
      streamRef.current?.close();
      streamRef.current = null;
      streamContext.current = null;
      setProjectId(pid);
      setConversationId(id);
      projectRef.current = pid;
      conversationRef.current = id;
      setError("");
      setBusy(true);
      setSnapshot(null);
      setOptions([]);
      eventSeqRef.current = 0;
      setFeedbackByAttempt({});
      setConnection("unknown");
      setDraft("");
      setDraftDirty(false);
      setSelection(null);
      sessionStorage.setItem("karajan:commander-project", pid);
      sessionStorage.setItem(`karajan:commander-conversation:${pid}`, id);
      sessionStorage.setItem("karajan:commander-conversation", id);
      recovery.current?.navigate(context);
    },
    [projectId, projects],
  );

  useEffect(() => {
    if (conversationId) void openConversation(conversationId, projectId);
  }, []); // restore after refresh

  useEffect(() => {
    if (
      !conversationId &&
      projectId &&
      conversations[projectId]?.length === 0
    ) {
      const saved = sessionStorage.getItem(settingsStorageKey(projectId));
      if (saved) {
        try {
          const value = JSON.parse(saved) as {
            profile?: string;
            source?: string;
          };
          setProfile(value.profile ?? "");
          setSource(value.source ?? "");
        } catch {
          setProfile("");
          setSource("");
        }
      }
      void loadOptions(projectId, navigation.current);
    }
  }, [conversationId, conversations, loadOptions, projectId]);

  useEffect(() => {
    if (conversationId || !projectId) return;
    const items = conversations[projectId];
    if (!items?.length) return;
    const saved = sessionStorage.getItem(
      `karajan:commander-conversation:${projectId}`,
    );
    const next = items.some((item) => item.id === saved)
      ? saved
      : items[items.length - 1].id;
    if (!next) return;
    void openConversation(next, projectId);
  }, [conversationId, conversations, openConversation, projectId]);

  useEffect(() => {
    if (!conversationId || !projectId) return;
    const pid = projectId;
    const cid = conversationId;
    const context: RecoveryContext = {
      navigation: navigation.current,
      projectId: pid,
      conversationId: cid,
    };
    if (
      !streamContext.current ||
      streamContext.current.navigation !== context.navigation ||
      streamContext.current.projectId !== pid ||
      streamContext.current.conversationId !== cid
    )
      return;
    let disposed = false;
    const close = () => {
      streamRef.current?.close();
      streamRef.current = null;
    };
    const recover = () => {
      if (disposed || !active(context)) return;
      close();
      setConnection("disconnected");
      recovery.current?.recover(context);
    };
    const handle = (eventName: string, event: MessageEvent<string>) => {
      if (disposed || !active(context)) return;
      let value: Record<string, unknown>;
      try {
        value = JSON.parse(event.data) as Record<string, unknown>;
      } catch {
        recover();
        return;
      }
      const sequence =
        typeof value.sequence === "number"
          ? value.sequence
          : typeof value.seq === "number"
            ? value.seq
            : undefined;
      if (
        eventName === "event_gap" ||
        eventName === "snapshot_required" ||
        value.snapshot_required === true
      ) {
        recover();
        return;
      }
      if (sequence != null && sequence <= eventSeqRef.current) return;
      if (sequence != null && sequence > eventSeqRef.current + 1) {
        recover();
        return;
      }
      const state = feedbackState(eventName, value);
      if (state) {
        const attemptId = eventAttemptId(value);
        if (attemptId)
          setFeedbackByAttempt((old) => ({
            ...old,
            [attemptId]: {
              state,
              observed: observedTimestamp(value),
            },
          }));
      }
      if (value.freshness === "stale")
        setSnapshot((old) => old && { ...old, freshness: "stale" });
      // Any named state transition is only a notification.  The snapshot is
      // the authoritative projection, including when the event has feedback.
      const feedbackOnly = ["feedback", "model_feedback", "progress"].includes(
        eventName,
      );
      const eventChangesFacts = !feedbackOnly;
      if (eventChangesFacts) {
        recover();
        return;
      }
      if (sequence != null)
        setSnapshot((old) => old && { ...old, snapshot_event_seq: sequence });
    };
    function connect() {
      if (disposed || !active(context)) return;
      close();
      const stream = new EventSource(
        `/v1/conversations/${encodeURIComponent(cid)}/events?after_seq=${eventSeqRef.current}`,
      );
      streamRef.current = stream;
      stream.onopen = () => {
        if (!disposed && active(context)) setConnection("connected");
      };
      stream.onmessage = (event) => handle("message", event);
      [
        "conversation_created",
        "conversation_settings_saved",
        "message_created",
        "draft_saved",
        "task_draft_created",
        "conversation_updated",
        "task_updated",
        "attempt_updated",
        "run_updated",
        "candidate_updated",
        "checks_updated",
        "review_updated",
        "delivery_updated",
        "feedback",
        "model_feedback",
        "progress",
        "waiting_input",
        "waiting_dependency",
        "waiting_output",
        "completed",
        "failed",
        "cancelled",
        "event_gap",
        "snapshot_required",
      ].forEach((name) =>
        stream.addEventListener(name, (event) =>
          handle(name, event as MessageEvent<string>),
        ),
      );
      stream.onerror = () => {
        stream.close();
        if (disposed || !active(context)) return;
        recover();
      };
    }
    connect();
    return () => {
      disposed = true;
      close();
    };
  }, [active, conversationId, projectId, subscriptionVersion]);

  const saveDraft = useCallback(
    (
      context = {
        projectId: projectRef.current,
        conversationId: conversationRef.current,
      },
    ): Promise<boolean> => {
      if (!context.conversationId) return Promise.resolve(false);
      const previous =
        draftQueues.current.get(context.conversationId) ??
        Promise.resolve(true);
      const operation = previous.then(async () => {
        while (true) {
          const command = draftLedger.current.prepareSave(context);
          if (!command) return true;
          try {
            const response = await fetch(
              `/v1/conversations/${encodeURIComponent(command.context.conversationId)}/draft`,
              {
                method: "PUT",
                headers: {
                  "Content-Type": "application/json",
                  "X-CSRF-Token": csrf,
                  "Idempotency-Key": stableKey("draft", command.id),
                  "If-Match": `"${command.baseRevision}"`,
                },
                body: JSON.stringify(command.body),
              },
            );
            if (!response.ok)
              throw await describeError(response, "草稿保存失败，请重试。");
            // Do not retire the key until the entire response is available.
            const result = (await response.json()) as { revision?: number };
            if (typeof result.revision !== "number")
              throw new Error("草稿保存响应缺少 revision，请重试。");
            const next = draftLedger.current.acknowledgeSave(
              command,
              result.revision,
            );
            if (
              projectRef.current === command.context.projectId &&
              conversationRef.current === command.context.conversationId
            )
              presentDraft(next);
            // A later edit was made while this command was in flight. Send it
            // with the newly acknowledged revision in the same explicit save.
            if (next?.dirty && next.editVersion !== command.editVersion)
              continue;
            return true;
          } catch (cause) {
            const message =
              cause instanceof Error ? cause.message : "草稿保存失败，请重试。";
            const next = draftLedger.current.rejectSave(command, message);
            if (
              projectRef.current === command.context.projectId &&
              conversationRef.current === command.context.conversationId
            ) {
              presentDraft(next);
              setError(message);
            }
            return false;
          }
        }
      });
      draftQueues.current.set(context.conversationId, operation);
      void operation.finally(() => {
        if (draftQueues.current.get(context.conversationId) === operation)
          draftQueues.current.delete(context.conversationId);
      });
      return operation;
    },
    [csrf, presentDraft],
  );

  const pendingDraftNotice = useCallback((context: DraftContext) => {
    const failed = draftLedger.current.current(context)?.error;
    return `上一会话草稿尚未保存到服务器${failed ? `（${failed}）` : ""}；本地草稿已保留，返回后可重试。`;
  }, []);

  async function createConversation(
    task = false,
    targetProjectId = projectRef.current,
  ) {
    const targetProject = projects.find((item) => item.id === targetProjectId);
    const origin: RecoveryContext = {
      navigation: navigation.current,
      projectId: projectRef.current,
      conversationId: conversationRef.current,
    };
    const submitted = draftLedger.current.current({
      projectId: origin.projectId,
      conversationId: origin.conversationId,
    });
    const targetConversation = origin.conversationId;
    const content = submitted?.content ?? draftRef.current;
    if (!targetProject || (task && !targetConversation)) return;
    if (task && !content.trim()) {
      setError("请先写下新任务目标。");
      return;
    }
    let originDraftSaved = true;
    if (!task && submitted?.dirty) {
      originDraftSaved = await saveDraft({
        projectId: origin.projectId,
        conversationId: origin.conversationId,
      });
      if (!active(origin)) return;
    }
    setBusy(true);
    setError("");
    try {
      const endpoint = task
        ? `/v1/conversations/${encodeURIComponent(targetConversation)}/task-drafts`
        : `/v1/projects/${encodeURIComponent(targetProjectId)}/conversations`;
      const targetSettings =
        targetProjectId === projectRef.current
          ? { profile, source }
          : (creationSettings.current[targetProjectId] ?? {
              profile: "",
              source: "",
            });
      const body = task
        ? { requirement: content }
        : {
            title: "新的 Commander 会话",
            commander_profile_ref: targetSettings.profile || null,
            commander_source_ref: targetSettings.source || null,
          };
      const command = commandRegistry.current.prepare(
        task ? "task-draft" : "conversation",
        task ? targetConversation : targetProjectId,
        body,
      );
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": stableKey(
            task ? "task-draft" : "conversation",
            command.id,
          ),
        },
        body: JSON.stringify(command.payload),
      });
      if (!response.ok)
        throw await describeError(
          response,
          task ? "新任务草稿未保存，请重试。" : "新会话未创建，请重试。",
        );
      // A complete and validated body is the commit point for the client key.
      const value = (await response.json()) as {
        id?: string;
        project_id?: string;
      };
      if (typeof value.id !== "string")
        throw new Error("新建响应缺少对象 ID，请重试。");
      if (!task && value.project_id && value.project_id !== targetProjectId)
        throw new Error("CROSS_PROJECT_REFERENCE");
      commandRegistry.current.complete(command);
      if (task) {
        if (active(origin)) {
          presentDraft(
            draftLedger.current.clearSubmitted(
              {
                projectId: origin.projectId,
                conversationId: targetConversation,
              },
              submitted?.editVersion ?? -1,
            ),
          );
          recovery.current?.recover(origin);
        }
      } else {
        await loadConversations(targetProjectId);
        if (active(origin)) {
          await openConversation(value.id, targetProjectId);
          if (!originDraftSaved)
            setError(
              pendingDraftNotice({
                projectId: origin.projectId,
                conversationId: origin.conversationId,
              }),
            );
        }
      }
    } catch (cause) {
      if (active(origin))
        setError(cause instanceof Error ? cause.message : "新建失败。");
    } finally {
      if (active(origin)) setBusy(false);
    }
  }

  async function sendMessage() {
    const cid = conversationRef.current;
    const pid = projectRef.current;
    const context: RecoveryContext = {
      navigation: navigation.current,
      projectId: pid,
      conversationId: cid,
    };
    const submitted = draftLedger.current.current({
      projectId: pid,
      conversationId: cid,
    });
    const content = submitted?.content ?? draftRef.current;
    if (!cid || !content.trim()) return;
    const body = { content };
    const command = commandRegistry.current.prepare("message", cid, body);
    const clientMessageId = command.id;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(cid)}/messages`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": stableKey("message", clientMessageId),
          },
          body: JSON.stringify({
            content: body.content,
            client_message_id: clientMessageId,
          }),
        },
      );
      if (!response.ok)
        throw await describeError(response, "消息未保存，请重试。");
      const result = (await response.json()) as {
        client_message_id?: string;
        conversation_id?: string;
      };
      if (
        (result.client_message_id &&
          result.client_message_id !== clientMessageId) ||
        (result.conversation_id && result.conversation_id !== cid)
      )
        throw new Error("消息响应身份不匹配，请重试。");
      commandRegistry.current.complete(command);
      if (active(context)) {
        presentDraft(
          draftLedger.current.clearSubmitted(
            { projectId: pid, conversationId: cid },
            submitted?.editVersion ?? -1,
          ),
        );
        recovery.current?.recover(context);
      }
    } catch (cause) {
      if (active(context))
        setError(
          cause instanceof Error ? cause.message : "消息未保存，请重试。",
        );
    } finally {
      if (active(context)) setBusy(false);
    }
  }
  async function saveSettings(nextProfile: string, nextSource: string) {
    if (
      !options.some(
        (item) =>
          item.profile_ref === nextProfile && item.source_ref === nextSource,
      )
    ) {
      setError("只能选择该项目已配置的 Commander 模型与来源。");
      return;
    }
    const pid = projectRef.current;
    const cid = conversationRef.current;
    setProfile(nextProfile);
    setSource(nextSource);
    rememberCreationSettings(pid, nextProfile, nextSource);
    // An empty project has no Conversation resource to update yet.  This is a
    // creation choice only; it is sent when the user explicitly creates one.
    if (!cid) return;
    const context: RecoveryContext = {
      navigation: navigation.current,
      projectId: pid,
      conversationId: cid,
    };
    try {
      const body = {
        commander_profile_ref: nextProfile,
        commander_source_ref: nextSource,
      };
      const revision = snapshot?.conversation?.revision ?? 0;
      const command = commandRegistry.current.prepare(
        "settings",
        cid,
        body,
        revision,
      );
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(cid)}/settings`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": stableKey("settings", command.id),
            "If-Match": `"${revision}"`,
          },
          body: JSON.stringify(command.payload),
        },
      );
      if (!response.ok)
        throw await describeError(response, "选择保存失败，请重试。");
      const value = (await response.json()) as Conversation;
      if (value.id && value.id !== cid)
        throw new Error("CONVERSATION_ID_MISMATCH");
      commandRegistry.current.complete(command);
      if (active(context))
        setSnapshot((old) => old && { ...old, conversation: value });
    } catch (cause) {
      if (active(context))
        setError(
          cause instanceof Error ? cause.message : "选择保存失败，请重试。",
        );
    }
  }

  async function selectProject(nextProjectId: string) {
    if (nextProjectId === projectRef.current) {
      setExpanded((old) => ({ ...old, [nextProjectId]: !old[nextProjectId] }));
      return;
    }
    const oldContext: DraftContext = {
      projectId: projectRef.current,
      conversationId: conversationRef.current,
    };
    const oldDraftSaved =
      !oldContext.conversationId ||
      !draftDirty ||
      (await saveDraft(oldContext));
    const token = ++navigation.current;
    recovery.current?.cancel();
    streamRef.current?.close();
    streamRef.current = null;
    streamContext.current = null;
    setExpanded((old) => ({ ...old, [nextProjectId]: true }));
    setProjectId(nextProjectId);
    projectRef.current = nextProjectId;
    setConversationId("");
    conversationRef.current = "";
    setSnapshot(null);
    setOptions([]);
    setDraft("");
    setDraftDirty(false);
    setSelection(null);
    setFeedbackByAttempt({});
    setConnection("unknown");
    sessionStorage.setItem("karajan:commander-project", nextProjectId);
    const items = await loadConversations(nextProjectId);
    if (token !== navigation.current || projectRef.current !== nextProjectId)
      return;
    const saved = sessionStorage.getItem(
      `karajan:commander-conversation:${nextProjectId}`,
    );
    const nextConversation = items.some((item) => item.id === saved)
      ? saved
      : items[items.length - 1]?.id;
    if (nextConversation) {
      await openConversation(nextConversation, nextProjectId);
      if (!oldDraftSaved) setError(pendingDraftNotice(oldContext));
    } else {
      const savedSettings = sessionStorage.getItem(
        settingsStorageKey(nextProjectId),
      );
      if (savedSettings) {
        try {
          const value = JSON.parse(savedSettings) as {
            profile?: string;
            source?: string;
          };
          setProfile(value.profile ?? "");
          setSource(value.source ?? "");
        } catch {
          setProfile("");
          setSource("");
        }
      } else {
        setProfile("");
        setSource("");
      }
      await loadOptions(nextProjectId, token);
      if (!oldDraftSaved) setError(pendingDraftNotice(oldContext));
    }
  }

  const tasks = useMemo<WorkTask[]>(
    () =>
      [
        ...(snapshot?.proposed_plan?.tasks ?? []),
        ...(snapshot?.runs?.flatMap((run) => run.tasks ?? []) ?? []),
        ...(snapshot?.run_summaries?.flatMap((run) => run.tasks ?? []) ?? []),
        ...(snapshot?.task_drafts ?? []).map((item) => ({
          id: item.id,
          title: item.requirement,
          role: "需求草稿",
          status: item.state ?? "draft",
        })),
      ].filter(
        (item, index, all) =>
          all.findIndex((candidate) => candidate.id === item.id) === index,
      ),
    [snapshot],
  );
  const agents = useMemo<Attempt[]>(
    () =>
      [...(snapshot?.runs ?? []), ...(snapshot?.run_summaries ?? [])].flatMap(
        (run) =>
          (run.attempts ?? []).map((attempt) => ({
            ...attempt,
            run_id: run.id,
          })),
      ),
    [snapshot],
  );
  const selectedOption = options.find(
    (item) => item.profile_ref === profile && item.source_ref === source,
  );
  const selectedAttemptId = currentAttemptId(snapshot, selection);
  const selectedFeedback = selectedAttemptId
    ? (feedbackByAttempt[selectedAttemptId] ?? {
        state: "idle" as const,
        observed: 0,
      })
    : { state: "idle" as const, observed: 0 };
  const conversationFact =
    tab === "Diff"
      ? snapshot?.candidate
      : tab === "Checks"
        ? snapshot?.checks
        : tab === "Review"
          ? snapshot?.review
          : tab === "Logs"
            ? snapshot?.logs
            : snapshot?.dependencies;
  const fact = relatedEvidence(snapshot, conversationFact, selection);
  const visibleDraftContext: DraftContext = { projectId, conversationId };
  return (
    <section className="commander-workbench" aria-label="Commander 工作台">
      <aside className="commander-sidebar">
        <div className="commander-sidebar-head">
          <div>
            <p className="eyebrow">COMMANDER</p>
            <h2>工作台</h2>
          </div>
          <button
            className="icon-button"
            aria-label="新对话"
            onClick={() => void createConversation()}
            disabled={busy || !project}
          >
            ＋
          </button>
        </div>
        <button
          className="new-task-link"
          onClick={() => void createConversation(true)}
          disabled={busy || !conversationId}
        >
          ＋ 新任务草稿
        </button>
        {projects.map((item) => (
          <div className="project-tree" key={item.id}>
            <div className="project-tree-row-wrap">
              <button
                className={`project-tree-row ${item.id === project?.id ? "selected" : ""}`}
                onClick={() => void selectProject(item.id)}
              >
                <span>{expanded[item.id] ? "⌄" : "›"}</span>
                <strong>{item.name}</strong>
              </button>
              <button
                className="project-create-button"
                aria-label={`在 ${item.name} 新建对话`}
                onClick={(event) => {
                  event.stopPropagation();
                  void createConversation(false, item.id);
                }}
                disabled={busy}
              >
                ＋
              </button>
            </div>
            {expanded[item.id] &&
              (conversations[item.id] ?? []).map((conversation) => (
                <button
                  className={`conversation-row ${conversation.id === conversationId ? "selected" : ""}`}
                  key={conversation.id}
                  onClick={async () => {
                    const oldContext: DraftContext = {
                      projectId: projectRef.current,
                      conversationId: conversationRef.current,
                    };
                    const oldDraftSaved = !draftDirty || (await saveDraft());
                    await openConversation(conversation.id, item.id);
                    if (!oldDraftSaved)
                      setError(pendingDraftNotice(oldContext));
                  }}
                >
                  ◌ {conversation.title || "Commander 会话"}
                </button>
              ))}
            {expanded[item.id] && !(conversations[item.id] ?? []).length && (
              <p className="sidebar-empty">还没有会话，点击 ＋ 开始。</p>
            )}
          </div>
        ))}
      </aside>
      <div className="commander-main">
        <header className="commander-header">
          <div>
            <p className="eyebrow">{project?.name ?? "项目"} / COMMANDER HUB</p>
            <h1>{snapshot?.conversation?.title ?? "持续协作会话"}</h1>
            <p className="muted">
              {project?.repository?.root} · {project?.repository?.base_ref} →{" "}
              {project?.target_branch}
            </p>
          </div>
          <div className="commander-controls">
            <label>
              模型
              <select
                aria-label="已配置 Commander 模型"
                value={profile}
                disabled={!options.length || busy}
                onChange={(event) => {
                  const next =
                    options.find(
                      (item) =>
                        item.profile_ref === event.target.value &&
                        item.source_ref === source,
                    ) ??
                    options.find(
                      (item) => item.profile_ref === event.target.value,
                    );
                  if (next)
                    void saveSettings(next.profile_ref, next.source_ref);
                }}
              >
                <option value="">
                  {options.length ? "选择已配置模型" : "无已配置模型"}
                </option>
                {[
                  ...new Map(
                    options.map((item) => [item.profile_ref, item]),
                  ).values(),
                ].map((item) => (
                  <option
                    key={`${item.profile_ref}:${item.profile_revision ?? ""}`}
                    value={item.profile_ref}
                  >
                    {item.profile_ref}
                  </option>
                ))}
              </select>
            </label>
            <label>
              来源
              <select
                aria-label="已配置 Commander 来源"
                value={source}
                disabled={!options.length || busy}
                onChange={(event) => {
                  const next =
                    options.find(
                      (item) =>
                        item.source_ref === event.target.value &&
                        item.profile_ref === profile,
                    ) ??
                    options.find(
                      (item) => item.source_ref === event.target.value,
                    );
                  if (next)
                    void saveSettings(next.profile_ref, next.source_ref);
                }}
              >
                <option value="">
                  {options.length ? "选择已配置来源" : "无已配置来源"}
                </option>
                {[
                  ...new Map(
                    options.map((item) => [item.source_ref, item]),
                  ).values(),
                ].map((item) => (
                  <option key={item.source_ref} value={item.source_ref}>
                    {item.source_label ?? item.source_ref}
                  </option>
                ))}
              </select>
            </label>
            {selectedOption && <span className="configured-mark">已配置</span>}
          </div>
        </header>
        {error && (
          <p role="alert" className="notice error">
            {error}
          </p>
        )}
        {!conversationId ? (
          <div className="commander-empty">
            <span>✦</span>
            <h2>从 Commander 开始</h2>
            <p>
              选择该项目已配置的模型和来源，描述你希望完成的工作。新建只保存会话或任务草稿，不会启动执行。
            </p>
            <button
              onClick={() => void createConversation()}
              disabled={busy || !project}
            >
              与 Commander 开始
            </button>
          </div>
        ) : (
          <>
            <nav className="commander-tabs" aria-label="工作台详情">
              <div>
                {tabs.map((item) => (
                  <button
                    key={item}
                    className={tab === item ? "active" : ""}
                    onClick={() => setTab(item)}
                  >
                    {item}
                  </button>
                ))}
              </div>
              <span className="freshness">
                {snapshot?.freshness === "stale" ? "待核对" : "● 已同步"}
              </span>
            </nav>
            <ModelFeedback
              state={selectedFeedback.state}
              lastObservedAt={selectedFeedback.observed}
              connection={connection}
              staleDurationMs={120000}
            />
            {tab === "Hub" && (
              <div className="hub-layout">
                <div className="conversation-panel">
                  <div className="message-list">
                    {snapshot?.messages?.length ? (
                      snapshot.messages.map((message) => (
                        <article
                          className={`message ${message.role}`}
                          key={message.id}
                        >
                          <span>
                            {message.role === "user" ? "你" : "Commander"}
                          </span>
                          <p>{message.content ?? message.text}</p>
                        </article>
                      ))
                    ) : (
                      <div className="message-placeholder">
                        <h2>描述下一次交付</h2>
                        <p>Commander 会在这里持续保留需求、问题与计划版本。</p>
                      </div>
                    )}
                  </div>
                  <textarea
                    aria-label="消息草稿"
                    data-draft-context={contextKey(visibleDraftContext)}
                    key={`${visibleDraftContext.projectId}:${visibleDraftContext.conversationId}`}
                    value={draft}
                    onChange={(event) => {
                      if (
                        event.currentTarget.dataset.draftContext !==
                          contextKey(visibleDraftContext) ||
                        projectRef.current !== visibleDraftContext.projectId ||
                        conversationRef.current !==
                          visibleDraftContext.conversationId
                      )
                        return;
                      presentDraft(
                        draftLedger.current.edit(
                          visibleDraftContext,
                          event.target.value,
                          selectionRef.current,
                        ),
                      );
                    }}
                    onBlur={(event) => {
                      if (
                        event.currentTarget.dataset.draftContext ===
                        contextKey(visibleDraftContext)
                      )
                        void saveDraft(visibleDraftContext);
                    }}
                    placeholder="告诉 Commander 你想完成什么…"
                    rows={4}
                  />
                  <div className="composer-actions">
                    <span className="field-help">
                      {draftDirty
                        ? "未发送的本地草稿待保存"
                        : "草稿已由服务器确认"}
                    </span>
                    <button
                      onClick={() => void sendMessage()}
                      disabled={!draft.trim() || busy}
                    >
                      发送给 Commander
                    </button>
                    <button
                      className="secondary"
                      onClick={() => void saveDraft()}
                      disabled={!draft.trim() || busy}
                    >
                      保存草稿
                    </button>
                  </div>
                </div>
                <div className="summary-column">
                  <div className="summary-card">
                    <p className="eyebrow">当前计划</p>
                    <h3>
                      {snapshot?.proposed_plan?.summary ?? "尚未形成计划"}
                    </h3>
                    <span>
                      {snapshot?.proposed_plan
                        ? `版本 ${snapshot.proposed_plan.proposal_revision ?? "—"} · ${snapshot.proposed_plan.status ?? "待确认"}`
                        : "需求会先保存，计划由 Commander 提出"}
                    </span>
                  </div>
                  {candidateSelections(snapshot).map((candidate) => (
                    <button
                      className={`task-row ${selection?.kind === "candidate" && selection.id === candidate.id ? "selected" : ""}`}
                      key={candidate.id}
                      onClick={() => setSelection(candidate)}
                    >
                      <span className="task-status">候选</span>
                      <span>
                        <strong>{candidate.id}</strong>
                        <small>当前候选证据</small>
                      </span>
                    </button>
                  ))}
                  <div className="summary-card">
                    <p className="eyebrow">Tasks</p>
                    {tasks.length ? (
                      tasks.map((task) => (
                        <button
                          className={`task-row ${selection?.kind === "task" && task.id === selection.id ? "selected" : ""}`}
                          key={task.id}
                          onClick={() => {
                            presentDraft(
                              draftLedger.current.edit(
                                {
                                  projectId: projectRef.current,
                                  conversationId: conversationRef.current,
                                },
                                draftRef.current,
                                { kind: "task", id: task.id },
                              ),
                            );
                            void saveDraft();
                          }}
                        >
                          <span className="task-status">
                            {task.status ?? "待确认"}
                          </span>
                          <span>
                            <strong>{task.title ?? task.id}</strong>
                            <small>
                              {task.role ?? "任务"}
                              {task.dependencies?.length
                                ? ` · 依赖 ${task.dependencies.join("、")}`
                                : ""}
                            </small>
                          </span>
                        </button>
                      ))
                    ) : (
                      <p className="muted">
                        还没有任务。使用“新任务草稿”交给 Commander
                        形成待确认建议。
                      </p>
                    )}
                  </div>
                  <div className="summary-card">
                    <p className="eyebrow">Agents</p>
                    {agents.length ? (
                      agents.map((agent) => (
                        <button
                          className={`agent-row ${selection?.kind === "attempt" && agent.id === selection.id ? "selected" : ""}`}
                          key={agent.id}
                          onClick={() => {
                            presentDraft(
                              draftLedger.current.edit(
                                {
                                  projectId: projectRef.current,
                                  conversationId: conversationRef.current,
                                },
                                draftRef.current,
                                { kind: "attempt", id: agent.id },
                              ),
                            );
                            void saveDraft();
                          }}
                        >
                          <strong>{agent.id}</strong>
                          <span>{agent.status ?? "待确认"}</span>
                        </button>
                      ))
                    ) : (
                      <p className="muted">当前会话暂无 Agent Attempt 事实。</p>
                    )}
                  </div>
                </div>
              </div>
            )}
            {tab !== "Hub" && (
              <div className="detail-panel">
                <p className="eyebrow">{tab.toUpperCase()}</p>
                <h2>{tab}</h2>
                {!selection ? (
                  <p className="muted">
                    选择 Task 或 Agent 后查看其 {tab} 事实。
                  </p>
                ) : fact == null ? (
                  <p className="muted">
                    所选对象暂无 {tab} 事实，或证据尚未产生。
                  </p>
                ) : (
                  <pre className="fact-view">
                    {JSON.stringify(fact, null, 2)}
                  </pre>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
