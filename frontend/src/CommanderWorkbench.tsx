import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./CommanderWorkbench.css";
import {
  ModelFeedback,
  type ConnectionStatus,
  type ModelFeedbackState,
} from "./ModelFeedback";

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

async function read<T>(url: string): Promise<T> {
  const response = await fetch(url);
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
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState<{
    state: ModelFeedbackState;
    observed: number;
    connection: ConnectionStatus;
  }>({ state: "idle", observed: 0, connection: "unknown" });
  const eventSeqRef = useRef(0);
  const generation = useRef(0);
  const projectRef = useRef(projectId);
  const conversationRef = useRef(conversationId);
  const draftRef = useRef(draft);
  const draftRevisionRef = useRef<Record<string, number>>({});
  const draftOperations = useRef(new Map<string, string>());
  const draftQueues = useRef(new Map<string, Promise<boolean>>());
  const eventTimer = useRef<number | undefined>(undefined);
  const streamRef = useRef<EventSource | null>(null);
  const project = projects.find((item) => item.id === projectId) ?? projects[0];

  useEffect(() => {
    projectRef.current = projectId;
    conversationRef.current = conversationId;
    draftRef.current = draft;
  }, [projectId, conversationId, draft]);

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
      if (token === generation.current && projectRef.current === id)
        setOptions(data.items ?? []);
    } catch (cause) {
      if (token === generation.current && projectRef.current === id)
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

  const openConversation = useCallback(
    async (id: string, pid = projectId, token = ++generation.current) => {
      if (!projects.some((item) => item.id === pid)) return;
      setProjectId(pid);
      setConversationId(id);
      projectRef.current = pid;
      conversationRef.current = id;
      setError("");
      setBusy(true);
      setSnapshot(null);
      setOptions([]);
      eventSeqRef.current = 0;
      setFeedback({ state: "idle", observed: 0, connection: "unknown" });
      sessionStorage.setItem("karajan:commander-project", pid);
      sessionStorage.setItem(`karajan:commander-conversation:${pid}`, id);
      sessionStorage.setItem("karajan:commander-conversation", id);
      try {
        const data = await read<Snapshot>(
          `/v1/conversations/${encodeURIComponent(id)}/snapshot`,
        );
        if (
          token !== generation.current ||
          projectRef.current !== pid ||
          data.conversation?.project_id !== pid
        )
          return;
        setSnapshot(data);
        const localDraft = sessionStorage.getItem(
          `karajan:commander-draft:${pid}:${id}`,
        );
        setDraft(data.draft?.content ?? localDraft ?? "");
        setDraftDirty(false);
        draftRevisionRef.current[id] = data.draft?.revision ?? 0;
        setSelectedTaskId(data.draft?.selected_task_id ?? null);
        setProfile(data.conversation?.commander_profile_ref ?? "");
        setSource(data.conversation?.commander_source_ref ?? "");
        await loadOptions(pid, token);
      } catch (cause) {
        if (token === generation.current)
          setError(
            cause instanceof Error ? cause.message : "无法读取会话快照。",
          );
      } finally {
        if (token === generation.current) setBusy(false);
      }
    },
    [loadOptions, projectId, projects],
  );

  useEffect(() => {
    if (conversationId) void openConversation(conversationId, projectId);
  }, []); // restore after refresh

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
    const token = generation.current;
    let disposed = false;
    const close = () => {
      streamRef.current?.close();
      streamRef.current = null;
    };
    const recover = async () => {
      if (
        disposed ||
        token !== generation.current ||
        projectRef.current !== pid ||
        conversationRef.current !== cid
      )
        return;
      const nextToken = ++generation.current;
      await openConversation(cid, pid, nextToken);
    };
    const handle = (eventName: string, event: MessageEvent<string>) => {
      if (
        disposed ||
        token !== generation.current ||
        projectRef.current !== pid ||
        conversationRef.current !== cid
      )
        return;
      let value: Record<string, unknown>;
      try {
        value = JSON.parse(event.data) as Record<string, unknown>;
      } catch {
        void recover();
        return;
      }
      const sequence =
        typeof value.sequence === "number"
          ? value.sequence
          : typeof value.seq === "number"
            ? value.seq
            : undefined;
      if (sequence != null && sequence <= eventSeqRef.current) return;
      if (
        sequence != null &&
        sequence > eventSeqRef.current + 1 &&
        eventName !== "event_gap" &&
        eventName !== "snapshot_required"
      ) {
        void recover();
        return;
      }
      if (sequence != null) eventSeqRef.current = sequence;
      if (
        eventName === "event_gap" ||
        eventName === "snapshot_required" ||
        value.snapshot_required === true
      ) {
        void recover();
        return;
      }
      const state = feedbackState(eventName, value);
      if (state) {
        const payload =
          typeof value.payload === "object" && value.payload !== null
            ? (value.payload as Record<string, unknown>)
            : value;
        const observed = payload.observed_at ?? value.observed_at;
        const timestamp =
          typeof observed === "number"
            ? observed < 10_000_000_000
              ? observed * 1000
              : observed
            : Date.now();
        setFeedback({ state, observed: timestamp, connection: "connected" });
      }
      if (value.freshness === "stale")
        setSnapshot((old) => old && { ...old, freshness: "stale" });
      if (sequence != null)
        setSnapshot((old) => old && { ...old, snapshot_event_seq: sequence });
    };
    function connect() {
      if (disposed || token !== generation.current) return;
      close();
      const stream = new EventSource(
        `/v1/conversations/${encodeURIComponent(cid)}/events?after_seq=${eventSeqRef.current}`,
      );
      streamRef.current = stream;
      stream.onopen = () =>
        setFeedback((old) => ({ ...old, connection: "connected" }));
      [
        "conversation_created",
        "conversation_settings_saved",
        "message_created",
        "draft_saved",
        "task_draft_created",
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
        if (disposed || token !== generation.current) return;
        setFeedback((old) => ({ ...old, connection: "disconnected" }));
        window.clearTimeout(eventTimer.current);
        eventTimer.current = window.setTimeout(connect, 1000);
      };
    }
    connect();
    return () => {
      disposed = true;
      close();
      window.clearTimeout(eventTimer.current);
    };
  }, [conversationId, projectId, openConversation]);

  const saveDraft = useCallback(
    (
      value = draftRef.current,
      taskId: string | null = selectedTaskId,
      context = { pid: projectRef.current, cid: conversationRef.current },
    ): Promise<boolean> => {
      if (!context.cid) return Promise.resolve(false);
      const identity = `${context.cid}:${value}:${taskId ?? ""}`;
      const previous =
        draftQueues.current.get(context.cid) ?? Promise.resolve(true);
      const operation = previous.then(async () => {
        const operationId =
          draftOperations.current.get(identity) ?? crypto.randomUUID();
        draftOperations.current.set(identity, operationId);
        const revision = draftRevisionRef.current[context.cid] ?? 0;
        try {
          const response = await fetch(
            `/v1/conversations/${encodeURIComponent(context.cid)}/draft`,
            {
              method: "PUT",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Idempotency-Key": stableKey("draft", operationId),
                "If-Match": `"${revision}"`,
              },
              body: JSON.stringify({
                content: value,
                selected_task_id: taskId,
                base_plan_revision: null,
              }),
            },
          );
          if (!response.ok)
            throw await describeError(response, "草稿保存失败，请重试。");
          const result = (await response.json()) as { revision?: number };
          if (typeof result.revision === "number")
            draftRevisionRef.current[context.cid] = result.revision;
          sessionStorage.setItem(
            `karajan:commander-draft:${context.pid}:${context.cid}`,
            value,
          );
          if (
            projectRef.current === context.pid &&
            conversationRef.current === context.cid
          ) {
            setDraftDirty(false);
            setSnapshot(
              (old) =>
                old && {
                  ...old,
                  draft: {
                    ...(old.draft ?? { content: value }),
                    content: value,
                    selected_task_id: taskId,
                    revision: result.revision,
                  },
                },
            );
          }
          return true;
        } catch (cause) {
          sessionStorage.setItem(
            `karajan:commander-draft:${context.pid}:${context.cid}`,
            value,
          );
          if (
            projectRef.current === context.pid &&
            conversationRef.current === context.cid
          )
            setError(
              cause instanceof Error ? cause.message : "草稿保存失败，请重试。",
            );
          return false;
        }
      });
      draftQueues.current.set(context.cid, operation);
      void operation.finally(() => {
        if (draftQueues.current.get(context.cid) === operation)
          draftQueues.current.delete(context.cid);
      });
      return operation;
    },
    [csrf, selectedTaskId],
  );

  async function createConversation(task = false) {
    const targetProject = project;
    const targetConversation = conversationRef.current;
    if (!targetProject || (task && !targetConversation)) return;
    if (task && !draftRef.current.trim()) {
      setError("请先写下新任务目标。");
      return;
    }
    const operationId = crypto.randomUUID();
    const targetProjectId = targetProject.id;
    const token = generation.current;
    setBusy(true);
    setError("");
    try {
      const endpoint = task
        ? `/v1/conversations/${encodeURIComponent(targetConversation)}/task-drafts`
        : `/v1/projects/${encodeURIComponent(targetProjectId)}/conversations`;
      const body = task
        ? { requirement: draftRef.current }
        : {
            title: "新的 Commander 会话",
            commander_profile_ref: profile || null,
            commander_source_ref: source || null,
          };
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": stableKey(
            task ? "task-draft" : "conversation",
            operationId,
          ),
        },
        body: JSON.stringify(body),
      });
      if (!response.ok)
        throw await describeError(
          response,
          task ? "新任务草稿未保存，请重试。" : "新会话未创建，请重试。",
        );
      if (task) {
        setDraft("");
        setDraftDirty(false);
        await openConversation(targetConversation, targetProjectId, token);
      } else {
        const value = (await response.json()) as Conversation;
        await loadConversations(targetProjectId);
        if (token === generation.current)
          await openConversation(value.id, targetProjectId);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "新建失败。");
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage() {
    const cid = conversationRef.current;
    const pid = projectRef.current;
    const content = draftRef.current;
    if (!cid || !content.trim()) return;
    const clientMessageId = crypto.randomUUID();
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
            content,
            client_message_id: clientMessageId,
          }),
        },
      );
      if (!response.ok)
        throw await describeError(response, "消息未保存，请重试。");
      if (pid === projectRef.current && cid === conversationRef.current) {
        setDraft("");
        setDraftDirty(false);
      }
      await openConversation(cid, pid);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "消息未保存，请重试。");
    } finally {
      setBusy(false);
    }
  }
  async function saveSettings(nextProfile: string, nextSource: string) {
    if (!conversationId) return;
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
    try {
      const operationId = crypto.randomUUID();
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(cid)}/settings`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": stableKey("settings", operationId),
            "If-Match": `"${snapshot?.conversation?.revision ?? 0}"`,
          },
          body: JSON.stringify({
            commander_profile_ref: nextProfile,
            commander_source_ref: nextSource,
          }),
        },
      );
      if (!response.ok)
        throw await describeError(response, "选择保存失败，请重试。");
      const value = (await response.json()) as Conversation;
      if (pid === projectRef.current && cid === conversationRef.current)
        setSnapshot((old) => old && { ...old, conversation: value });
    } catch (cause) {
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
    const oldContext = {
      pid: projectRef.current,
      cid: conversationRef.current,
    };
    if (oldContext.cid && draftDirty)
      await saveDraft(draftRef.current, selectedTaskId, oldContext);
    const token = ++generation.current;
    setExpanded((old) => ({ ...old, [nextProjectId]: true }));
    setProjectId(nextProjectId);
    projectRef.current = nextProjectId;
    setConversationId("");
    conversationRef.current = "";
    setSnapshot(null);
    setOptions([]);
    setDraft("");
    setSelectedTaskId(null);
    setFeedback({ state: "idle", observed: 0, connection: "unknown" });
    sessionStorage.setItem("karajan:commander-project", nextProjectId);
    const items = await loadConversations(nextProjectId);
    if (token !== generation.current || projectRef.current !== nextProjectId)
      return;
    const saved = sessionStorage.getItem(
      `karajan:commander-conversation:${nextProjectId}`,
    );
    const nextConversation = items.some((item) => item.id === saved)
      ? saved
      : items[items.length - 1]?.id;
    if (nextConversation)
      await openConversation(nextConversation, nextProjectId, token);
    else await loadOptions(nextProjectId, token);
  }

  const tasks = useMemo<WorkTask[]>(
    () =>
      [
        ...(snapshot?.proposed_plan?.tasks ?? []),
        ...(snapshot?.runs?.flatMap((run) => run.tasks ?? []) ?? []),
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
  const agents = useMemo(
    () =>
      (snapshot?.runs ?? []).flatMap((run) =>
        (run.attempts ?? []).map((attempt) => ({ ...attempt, run_id: run.id })),
      ),
    [snapshot],
  );
  const selectedOption = options.find(
    (item) => item.profile_ref === profile && item.source_ref === source,
  );
  const fact =
    tab === "Diff"
      ? snapshot?.candidate
      : tab === "Checks"
        ? snapshot?.checks
        : tab === "Review"
          ? snapshot?.review
          : tab === "Logs"
            ? snapshot?.logs
            : snapshot?.dependencies;
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
            <button
              className={`project-tree-row ${item.id === project?.id ? "selected" : ""}`}
              onClick={() => void selectProject(item.id)}
            >
              <span>{expanded[item.id] ? "⌄" : "›"}</span>
              <strong>{item.name}</strong>
            </button>
            {expanded[item.id] &&
              (conversations[item.id] ?? []).map((conversation) => (
                <button
                  className={`conversation-row ${conversation.id === conversationId ? "selected" : ""}`}
                  key={conversation.id}
                  onClick={async () => {
                    if (draftDirty)
                      await saveDraft(draftRef.current, selectedTaskId);
                    await openConversation(conversation.id, item.id);
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
              state={feedback.state}
              lastObservedAt={feedback.observed}
              connection={feedback.connection}
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
                    value={draft}
                    onChange={(event) => {
                      setDraft(event.target.value);
                      setDraftDirty(true);
                      sessionStorage.setItem(
                        `karajan:commander-draft:${projectRef.current}:${conversationRef.current}`,
                        event.target.value,
                      );
                    }}
                    onBlur={() => void saveDraft()}
                    placeholder="告诉 Commander 你想完成什么…"
                    rows={4}
                  />
                  <div className="composer-actions">
                    <span className="field-help">
                      草稿保存到当前会话{draftDirty ? " · 有未保存修改" : ""}
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
                  <div className="summary-card">
                    <p className="eyebrow">Tasks</p>
                    {tasks.length ? (
                      tasks.map((task) => (
                        <button
                          className={`task-row ${task.id === selectedTaskId ? "selected" : ""}`}
                          key={task.id}
                          onClick={() => {
                            setSelectedTaskId(task.id);
                            if (
                              (snapshot?.task_drafts ?? []).some(
                                (item) => item.id === task.id,
                              )
                            )
                              void saveDraft(draftRef.current, task.id);
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
                        <div className="agent-row" key={agent.id}>
                          <strong>{agent.id}</strong>
                          <span>{agent.status ?? "待确认"}</span>
                        </div>
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
                {fact == null ? (
                  <p className="muted">
                    当前会话暂无 {tab} 事实，或证据尚未产生。
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
