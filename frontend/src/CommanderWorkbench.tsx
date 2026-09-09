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
  commander_profile_ref?: string;
  commander_source_ref?: string;
  draft_revision?: number;
};
type Snapshot = {
  conversation?: Conversation;
  messages?: { id: string; role: string; content?: string; text?: string }[];
  draft?: { content: string; revision?: number };
  runs?: {
    id: string;
    status?: string;
    requirement?: { goal?: string };
    tasks?: { id: string; title?: string; role?: string; status?: string }[];
  }[];
  run_summaries?: { id: string; state?: string; snapshot_event_seq?: number }[];
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
  if (!response.ok)
    throw new Error(
      response.status === 404
        ? "此项目还没有 Commander 会话。"
        : "暂时无法读取工作台状态。",
    );
  return response.json();
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
  const [conversationId, setConversationId] = useState(
    () => sessionStorage.getItem("karajan:commander-conversation") ?? "",
  );
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [tab, setTab] = useState<Tab>("Hub");
  const [draft, setDraft] = useState("");
  const [profile, setProfile] = useState("commander");
  const [source, setSource] = useState("configured");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState<{
    state: ModelFeedbackState;
    observed: number;
    connection: ConnectionStatus;
  }>({ state: "idle", observed: 0, connection: "unknown" });
  const [eventSeq, setEventSeq] = useState(0);
  const eventSeqRef = useRef(0);
  const generation = useRef(0);
  const project = projects.find((item) => item.id === projectId) ?? projects[0];

  const loadConversations = useCallback(async (id: string) => {
    try {
      const data = await read<{ items?: Conversation[] }>(
        `/v1/projects/${encodeURIComponent(id)}/conversations`,
      );
      setConversations((old) => ({ ...old, [id]: data.items ?? [] }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取会话。");
    }
  }, []);

  useEffect(() => {
    projects.forEach((item) => void loadConversations(item.id));
  }, [projects, loadConversations]);

  const openConversation = useCallback(
    async (id: string, pid = projectId) => {
      const token = ++generation.current;
      setProjectId(pid);
      setConversationId(id);
      setError("");
      setBusy(true);
      sessionStorage.setItem("karajan:commander-project", pid);
      sessionStorage.setItem("karajan:commander-conversation", id);
      try {
        const data = await read<Snapshot>(
          `/v1/conversations/${encodeURIComponent(id)}/snapshot`,
        );
        if (token !== generation.current) return;
        setSnapshot(data);
        setDraft(data.draft?.content ?? "");
        setProfile(data.conversation?.commander_profile_ref ?? "commander");
        setSource(data.conversation?.commander_source_ref ?? "configured");
      } catch (cause) {
        if (token === generation.current)
          setError(
            cause instanceof Error ? cause.message : "无法读取会话快照。",
          );
      } finally {
        if (token === generation.current) setBusy(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    if (conversationId) void openConversation(conversationId, projectId);
  }, []); // restore after refresh

  useEffect(() => {
    if (!conversationId) return;
    const stream = new EventSource(
      `/v1/conversations/${encodeURIComponent(conversationId)}/events?after_seq=${eventSeqRef.current}`,
    );
    setFeedback((old) => ({ ...old, connection: "connected" }));
    stream.onmessage = (message) => {
      try {
        const value = JSON.parse(message.data) as {
          seq?: number;
          event_type?: string;
          observed_at?: number;
          state?: ModelFeedbackState;
          freshness?: string;
        };
        if (value.seq && value.seq <= eventSeqRef.current) return;
        if (value.seq) {
          eventSeqRef.current = value.seq;
          setEventSeq(value.seq);
        }
        if (value.state)
          setFeedback({
            state: value.state,
            observed:
              (value.observed_at ?? Math.floor(Date.now() / 1000)) * 1000,
            connection: "connected",
          });
        if (
          value.event_type === "event_gap" ||
          value.freshness === "snapshot_required"
        )
          void openConversation(conversationId, projectId);
      } catch {
        /* snapshot recovery handles malformed events */
      }
    };
    stream.onerror = () =>
      setFeedback((old) => ({ ...old, connection: "disconnected" }));
    return () => stream.close();
  }, [conversationId, projectId, openConversation]);

  async function createConversation(task = false) {
    if (!project) return;
    setBusy(true);
    setError("");
    try {
      const endpoint = task
        ? `/v1/conversations/${encodeURIComponent(conversationId)}/task-drafts`
        : `/v1/projects/${encodeURIComponent(project.id)}/conversations`;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify(
          task
            ? { requirement: { goal: draft } }
            : {
                title: "新的 Commander 会话",
                commander_profile_ref: profile,
                commander_source_ref: source,
              },
        ),
      });
      if (!response.ok) throw new Error("新建请求未被接受，请重试。");
      if (task) {
        setDraft("");
        if (conversationId) await openConversation(conversationId);
      } else {
        const value = (await response.json()) as Conversation;
        await loadConversations(project.id);
        await openConversation(value.id, project.id);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "新建失败。");
    } finally {
      setBusy(false);
    }
  }

  async function saveDraft(value = draft) {
    if (!conversationId) return;
    setDraft(value);
    sessionStorage.setItem(`karajan:commander-draft:${conversationId}`, value);
    try {
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(conversationId)}/draft`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": crypto.randomUUID(),
            "If-Match": `"${snapshot?.draft?.revision ?? 0}"`,
          },
          body: JSON.stringify({
            content: value,
            selected_task_id: null,
            base_plan_revision: null,
          }),
        },
      );
      if (!response.ok) throw new Error("草稿保存失败，请重试。");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "草稿保存失败，请重试。",
      );
    }
  }

  async function sendMessage() {
    if (!conversationId || !draft.trim()) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({
            content: draft,
            client_message_id: crypto.randomUUID(),
          }),
        },
      );
      if (!response.ok) throw new Error("消息未保存，请重试。");
      setDraft("");
      await openConversation(conversationId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "消息未保存，请重试。");
    } finally {
      setBusy(false);
    }
  }
  async function saveSettings(kind: "profile" | "source", value: string) {
    if (!conversationId) return;
    if (kind === "profile") setProfile(value);
    else setSource(value);
    try {
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(conversationId)}/settings`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": crypto.randomUUID(),
            "If-Match": `"${snapshot?.conversation?.revision ?? 0}"`,
          },
          body: JSON.stringify(
            kind === "profile"
              ? { commander_profile_ref: value }
              : { commander_source_ref: value },
          ),
        },
      );
      if (!response.ok) throw new Error("选择保存失败，请重试。");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "选择保存失败，请重试。",
      );
    }
  }

  const activeConversations = conversations[project?.id ?? ""] ?? [];
  const tasks = useMemo<WorkTask[]>(
    () =>
      snapshot?.proposed_plan?.tasks ??
      (snapshot?.runs?.flatMap((run) => run.tasks ?? []) as
        WorkTask[] | undefined) ??
      [],
    [snapshot],
  );
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
              onClick={() => {
                setExpanded((old) => ({ ...old, [item.id]: !old[item.id] }));
                setProjectId(item.id);
              }}
            >
              <span>{expanded[item.id] ? "⌄" : "›"}</span>
              <strong>{item.name}</strong>
            </button>
            {expanded[item.id] &&
              (conversations[item.id] ?? []).map((conversation) => (
                <button
                  className={`conversation-row ${conversation.id === conversationId ? "selected" : ""}`}
                  key={conversation.id}
                  onClick={() =>
                    void openConversation(conversation.id, item.id)
                  }
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
                value={profile}
                onChange={(event) =>
                  void saveSettings("profile", event.target.value)
                }
              >
                <option value="commander">Commander</option>
                <option value="gpt-5.6-luna">gpt-5.6-luna</option>
              </select>
            </label>
            <label>
              来源
              <select
                value={source}
                onChange={(event) =>
                  void saveSettings("source", event.target.value)
                }
              >
                <option value="configured">已配置来源</option>
                <option value="local">本机来源</option>
              </select>
            </label>
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
              选择模型和来源，描述你希望完成的工作。新建只保存会话或任务草稿，不会启动执行。
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
                    onChange={(event) => setDraft(event.target.value)}
                    onBlur={() => void saveDraft()}
                    placeholder="告诉 Commander 你想完成什么…"
                    rows={4}
                  />
                  <div className="composer-actions">
                    <span className="field-help">草稿自动保存到当前会话</span>
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
                    <p className="eyebrow">任务摘要</p>
                    {tasks.length ? (
                      tasks.map((task) => (
                        <div className="task-row" key={task.id}>
                          <span className="task-status">
                            {task.status === "running" ? "◌" : "○"}
                          </span>
                          <div>
                            <strong>{task.title ?? task.id}</strong>
                            <small>
                              {task.role ?? "任务"} · {task.status ?? "待确认"}
                            </small>
                          </div>
                        </div>
                      ))
                    ) : (
                      <p className="muted">
                        还没有任务。使用“新任务草稿”交给 Commander
                        形成待确认建议。
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}
            {tab !== "Hub" && (
              <div className="detail-panel">
                <p className="eyebrow">{tab.toUpperCase()}</p>
                <h2>{tab}</h2>
                {tasks.length ? (
                  <div className="detail-list">
                    {tasks.map((task) => (
                      <article key={task.id}>
                        <strong>{task.title ?? task.id}</strong>
                        <span>
                          {task.role ?? "任务"} · {task.model ?? "模型待指定"} ·{" "}
                          {task.source ?? "来源待指定"}
                        </span>
                        <small>
                          依赖：{task.dependencies?.join("、") || "无"}
                        </small>
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="muted">当前会话暂无可展示事实。</p>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
