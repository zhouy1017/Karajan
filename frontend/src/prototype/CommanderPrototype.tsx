import { useEffect, useMemo, useRef, useState } from "react";
import "./commander-prototype.css";

// Throwaway UI prototype: three structurally different Commander workbench layouts
// on /prototype/commander, switchable with ?variant=A|B|C&scene=open|commander|plan|run|review.

type Variant = "A" | "B" | "C";
type Scene = "open" | "hub" | "commander" | "plan" | "run" | "review";
type DetailTab = "Diff" | "Checks" | "Review" | "Logs" | "Dependencies";
type TaskState = "ready" | "running" | "waiting" | "done";
type PreviewMode = "diff" | "pr";
type HubStage = "proposal" | "running" | "complete";

type Task = {
  id: string;
  title: string;
  detail: string;
  role: string;
  model: string;
  source: string;
  dependency: string;
  state: TaskState;
  result: string;
};

const sceneLabels: Record<Scene, string> = {
  open: "打开",
  hub: "Hub",
  commander: "对话",
  plan: "分工",
  run: "运行",
  review: "交付",
};

const sceneOrder: Scene[] = [
  "open",
  "hub",
  "commander",
  "plan",
  "run",
  "review",
];
const detailTabs: DetailTab[] = [
  "Diff",
  "Checks",
  "Review",
  "Logs",
  "Dependencies",
];

const initialTasks: Task[] = [
  {
    id: "csv-api",
    title: "增加 CSV 导出后端接口",
    detail: "导出订单字段并返回可下载的 CSV 文件",
    role: "Worker",
    model: "Terra",
    source: "Codex",
    dependency: "无",
    state: "ready",
    result: "准备开始",
  },
  {
    id: "csv-entry",
    title: "增加导出入口与状态反馈",
    detail: "在订单列表中加入导出操作和下载状态",
    role: "Worker",
    model: "Luna",
    source: "Codex",
    dependency: "无",
    state: "ready",
    result: "准备开始",
  },
  {
    id: "csv-review",
    title: "独立检查 CSV 变更",
    detail: "检查边界、错误处理和用户可见行为",
    role: "Reviewer",
    model: "Sol",
    source: "ChatGPT",
    dependency: "前两项",
    state: "waiting",
    result: "等待候选",
  },
];

const iconPaths: Record<string, string> = {
  repo: "M4 6h16M4 10h16M4 14h10M4 18h7",
  branch: "M7 4v11a4 4 0 1 0 2 0V9h5a3 3 0 1 1 0 6",
  chat: "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 3v-3.4A2.5 2.5 0 0 1 4 12.5z",
  task: "M5 5h14v14H5zM8 9h8M8 13h5",
  agent: "M12 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6M5 19a7 7 0 0 1 14 0",
  check: "m5 12 4 4L19 6",
  arrow: "M5 12h14m-5-5 5 5-5 5",
  play: "m9 6 9 6-9 6z",
  pause: "M8 6v12M16 6v12",
  reset: "M4 10a8 8 0 1 1 2 7M4 10V5m0 5h5",
};

function Icon({ name, size = 16 }: { name: string; size?: number }) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={iconPaths[name] ?? iconPaths.task} />
    </svg>
  );
}

function tasksForStage(stage: HubStage): Task[] {
  return initialTasks.map((task) => {
    if (stage === "complete")
      return {
        ...task,
        state: "done",
        result: task.role === "Reviewer" ? "独立审查通过" : "候选已组合",
      };
    if (stage === "running")
      return task.role === "Reviewer" || task.dependency !== "无"
        ? { ...task, state: "waiting", result: "等待 Worker 产物" }
        : { ...task, state: "running", result: "正在建立独立写区" };
    return task.role === "Reviewer"
      ? { ...task, state: "waiting", result: "等待批准后启动" }
      : { ...task, state: "ready", result: "准备开始" };
  });
}

function messagesForStage(stage: HubStage) {
  const messages = [
    {
      from: "user" as const,
      text: "我想为订单列表增加 CSV 导出。需要保留当前筛选条件，并在下载失败时给出清晰反馈。",
    },
    {
      from: "commander" as const,
      text: "明白了。我会先把导出接口与前端入口拆开，两个 Worker 可以并行处理；完成后再让独立 Reviewer 检查字段和失败反馈。",
    },
  ];
  if (stage === "running")
    messages.push({
      from: "commander" as const,
      text: "已按计划分发两个 Worker，运行中的 Profile 保持固定；完成后我会在这里汇总结果。",
    });
  if (stage === "complete")
    messages.push({
      from: "commander" as const,
      text: "本轮样例已完成 CSV 接口和列表导出入口，保留筛选并补充失败反馈。候选 #demo-01，checks 通过，独立 Reviewer 建议交付；尚未创建 PR。你可展开 diff/审查后准备 PR。",
    });
  return messages;
}

function readQuery(): { variant: Variant; scene: Scene; stage: HubStage } {
  const params = new URLSearchParams(window.location.search);
  const variant = params.get("variant");
  const scene = params.get("scene");
  const stageParam = params.get("stage");
  const stage: HubStage =
    stageParam === "running" || stageParam === "complete"
      ? stageParam
      : scene === "run"
        ? "running"
        : "proposal";
  const sceneFromStage =
    stageParam === "proposal" ||
    stageParam === "running" ||
    stageParam === "complete" ||
    scene === "commander"
      ? "hub"
      : scene;
  return {
    variant: variant === "B" || variant === "C" ? variant : "A",
    scene: sceneOrder.includes(sceneFromStage as Scene)
      ? (sceneFromStage as Scene)
      : "hub",
    stage,
  };
}

function updateUrl(variant: Variant, scene: Scene, stage?: HubStage) {
  const url = new URL(window.location.href);
  url.searchParams.set("variant", variant);
  url.searchParams.set("scene", scene);
  if (scene === "hub" && stage) url.searchParams.set("stage", stage);
  else url.searchParams.delete("stage");
  window.history.replaceState({}, "", url);
}

function StatusPill({ state }: { state: TaskState }) {
  const labels: Record<TaskState, string> = {
    ready: "待开始",
    running: "进行中",
    waiting: "等待中",
    done: "已完成",
  };
  return (
    <span className={`status-pill ${state}`}>
      {state === "running" && <i className="pulse" />}
      {labels[state]}
    </span>
  );
}

function SelectField({
  value,
  options,
  onChange,
  disabled = false,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <select
      disabled={disabled}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {options.map((option) => (
        <option key={option}>{option}</option>
      ))}
    </select>
  );
}

function TopBar({
  variant,
  scene,
  onScene,
  onReset,
  resourceOpen,
  setResourceOpen,
}: {
  variant: Variant;
  scene: Scene;
  onScene: (scene: Scene) => void;
  onReset: () => void;
  resourceOpen: boolean;
  setResourceOpen: (open: boolean) => void;
}) {
  return (
    <header className="prototype-topbar">
      <div className="topbar-identity">
        <div className="topbar-repo">
          <span className="repo-mark">
            <Icon name="repo" size={14} />
          </span>
          <strong>Karajan</strong>
          <span className="slash">/</span>
          <span>feature/csv-export</span>
        </div>
        <span className="prototype-badge">交互原型 · 样例数据</span>
      </div>
      <nav className="scene-nav" aria-label="演示场景">
        <span>演示场景</span>
        {sceneOrder
          .filter((item) => item !== "commander")
          .map((item, index) => (
            <button
              className={scene === item ? "active" : ""}
              key={item}
              onClick={() => onScene(item)}
            >
              <b>{index + 1}</b>
              {sceneLabels[item]}
            </button>
          ))}
      </nav>
      <div className="topbar-actions">
        <div className="resource-wrap">
          <button
            className="resource-chip"
            onClick={() => setResourceOpen(!resourceOpen)}
          >
            <span className="resource-dot" />
            订阅优先 <span className="chevron">⌄</span>
          </button>
          {resourceOpen && (
            <div className="resource-popover">
              <strong>资源摘要</strong>
              <p>当前可用额度</p>
              <b>未知（示例）</b>
              <small>固定选择，不会静默替换</small>
            </div>
          )}
        </div>
        <span className="variant-readout">Variant {variant}</span>
        <button
          className="icon-button"
          aria-label="重置演示"
          title="重置演示"
          onClick={onReset}
        >
          <Icon name="reset" size={15} />
        </button>
      </div>
    </header>
  );
}

function Sidebar({
  scene,
  onScene,
  selectedTask,
  tasks,
  onTask,
}: {
  scene: Scene;
  onScene: (scene: Scene) => void;
  selectedTask: Task;
  tasks: Task[];
  onTask: (task: Task) => void;
}) {
  return (
    <aside className="workbench-sidebar">
      <div className="sidebar-title">
        <span className="brand-symbol">K</span>
        <div>
          <strong>工作台</strong>
          <small>Commander</small>
        </div>
      </div>
      <div className="sidebar-session">
        <small>当前会话</small>
        <strong>为订单增加 CSV 导出</strong>
        <span>
          <span className="online-dot" /> 高级 Commander
        </span>
      </div>
      <div className="sidebar-group">
        <small className="sidebar-label">工作流</small>
        {sceneOrder
          .filter((item) => item !== "open" && item !== "commander")
          .map((item) => (
            <button
              className={`sidebar-nav ${scene === item ? "selected" : ""}`}
              onClick={() => onScene(item)}
              key={item}
            >
              <Icon
                name={
                  item === "run"
                    ? "agent"
                    : item === "review"
                      ? "check"
                      : item === "hub"
                        ? "chat"
                        : "task"
                }
                size={16}
              />
              <span>{sceneLabels[item]}</span>
              {item === "plan" && <em>3</em>}
            </button>
          ))}
      </div>
      <div className="sidebar-group task-nav">
        <small className="sidebar-label">任务</small>
        {["csv-api", "csv-entry", "csv-review"].map((id) => (
          <button
            key={id}
            onClick={() =>
              onTask(tasks.find((task) => task.id === id) ?? selectedTask)
            }
            className={
              selectedTask.id === id
                ? "task-nav-item selected"
                : "task-nav-item"
            }
          >
            <span className="task-dot" />
            <span>
              {id === "csv-api"
                ? "后端 CSV 接口"
                : id === "csv-entry"
                  ? "前端导出入口"
                  : "独立审查"}
            </span>
          </button>
        ))}
      </div>
      <div className="sidebar-footer">
        <span className="online-dot" /> 示例会话已连接
      </div>
    </aside>
  );
}

function TaskDetail({
  task,
  detailTab,
  setDetailTab,
  variant,
}: {
  task: Task;
  detailTab: DetailTab;
  setDetailTab: (tab: DetailTab) => void;
  variant: Variant;
}) {
  return (
    <aside className={`detail-panel detail-${variant}`}>
      <div className="detail-header">
        <div>
          <span className="eyebrow">任务详情</span>
          <h2>{task.title}</h2>
        </div>
        <StatusPill state={task.state} />
      </div>
      <p className="detail-description">{task.detail}</p>
      <div className="profile-row">
        <span className="avatar small">
          {task.role === "Reviewer" ? "S" : task.model[0]}
        </span>
        <div>
          <strong>
            {task.role} · {task.model}
          </strong>
          <small>
            {task.source} · Attempt {task.state === "ready" ? "—" : "01"}
          </small>
        </div>
      </div>
      <div className="detail-tabs">
        {detailTabs.map((tab) => (
          <button
            key={tab}
            className={detailTab === tab ? "active" : ""}
            onClick={() => setDetailTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>
      <div className="detail-content">
        {detailTab === "Diff" && (
          <>
            <div className="diff-stat">
              <span>候选变更</span>
              <strong>{task.state === "done" ? "+84 −12" : "尚无候选"}</strong>
            </div>
            <div className="file-line">
              <span className="file-badge">M</span>
              <span>
                {task.id === "csv-entry" ? "OrderList.tsx" : "orders/export.ts"}
              </span>
              <span className="file-change">
                {task.state === "done" ? "+42" : "—"}
              </span>
            </div>
            <p className="empty-note">
              {task.state === "done"
                ? "相对批准基准的样例 diff"
                : "任务完成后将在此显示候选变更"}
            </p>
          </>
        )}
        {detailTab === "Checks" && (
          <div className="evidence-list">
            <Evidence
              label="类型检查"
              value={task.state === "done" ? "通过" : "等待运行"}
              ok={task.state === "done"}
            />
            <Evidence
              label="导出行为检查"
              value={task.state === "done" ? "通过" : "未开始"}
              ok={task.state === "done"}
            />
            <Evidence label="证据版本" value="候选 #demo-01" />
          </div>
        )}
        {detailTab === "Review" && (
          <div className="review-empty">
            <span className="review-mark">S</span>
            <strong>
              {task.role === "Reviewer" ? "独立审查等待中" : "尚未开始审查"}
            </strong>
            <p>审查从冻结候选和独立上下文开始。</p>
          </div>
        )}
        {detailTab === "Logs" && (
          <div className="log-list">
            <div>
              <time>10:42:08</time>
              <span>
                profile 固定为 {task.model} / {task.source}
              </span>
            </div>
            <div>
              <time>10:42:12</time>
              <span>
                任务状态变为 {task.state === "running" ? "running" : task.state}
              </span>
            </div>
            <div>
              <time>10:42:16</time>
              <span>资源：订阅优先 · 未知（示例）</span>
            </div>
          </div>
        )}
        {detailTab === "Dependencies" && (
          <div className="dependency-detail">
            <span className="dependency-icon">↳</span>
            <div>
              <strong>
                {task.dependency === "无" ? "无前置任务" : "等待两个 Worker"}
              </strong>
              <p>
                {task.dependency === "无"
                  ? "可独立启动，写区与其他任务隔离。"
                  : "后端 CSV 接口、前端导出入口完成后可运行。"}
              </p>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

function Evidence({
  label,
  value,
  ok = false,
}: {
  label: string;
  value: string;
  ok?: boolean;
}) {
  return (
    <div className="evidence-row">
      <span className={ok ? "evidence-ok" : "evidence-wait"}>
        {ok ? "✓" : "·"}
      </span>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function CommanderPanel({
  compact = false,
  messages,
  draft,
  setDraft,
  onSend,
  onContinue,
  model,
  source,
}: {
  compact?: boolean;
  messages: { from: "user" | "commander"; text: string }[];
  draft: string;
  setDraft: (value: string) => void;
  onSend: () => void;
  onContinue: () => void;
  model: string;
  source: string;
}) {
  const chatRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (chatRef.current)
      chatRef.current.scrollTo({
        top: chatRef.current.scrollHeight,
        behavior: "smooth",
      });
  }, [messages.length]);
  return (
    <section
      className={`commander-panel ${compact ? "commander-compact" : ""}`}
    >
      <div className="commander-heading">
        <div className="commander-avatar">✦</div>
        <div>
          <strong>Commander</strong>
          <small>
            {model} · {source}
          </small>
        </div>
        <span className="live-label">
          <i />
          已连接
        </span>
      </div>
      <div className="chat-stream" ref={chatRef}>
        {messages.map((message, index) => (
          <div
            className={`chat-message ${message.from}`}
            key={`${message.from}-${index}`}
          >
            <span className="chat-label">
              {message.from === "user" ? "你" : "Commander"}
            </span>
            <p>{message.text}</p>
          </div>
        ))}
      </div>
      <div className="chat-input">
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="继续告诉 Commander 你的约束…"
          rows={compact ? 2 : 3}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter")
              onSend();
          }}
        />
        <div className="chat-actions">
          <small>⌘ Enter 发送</small>
          <button
            className="send-button"
            onClick={onSend}
            disabled={!draft.trim()}
          >
            发送 <Icon name="arrow" size={14} />
          </button>
        </div>
      </div>
      {messages.length > 2 && (
        <button className="continue-link" onClick={onContinue}>
          查看分工建议 <Icon name="arrow" size={13} />
        </button>
      )}
    </section>
  );
}

function OpenScene({
  onStart,
  model,
  setModel,
  source,
  setSource,
}: {
  onStart: () => void;
  model: string;
  setModel: (value: string) => void;
  source: string;
  setSource: (value: string) => void;
}) {
  return (
    <div className="scene-content open-scene">
      <div className="open-hero">
        <span className="eyebrow">WORKSPACE / 01</span>
        <h1>
          打开仓库，开始一段
          <br />
          <em>可控的协作</em>
        </h1>
        <p>选择仓库与高级 Commander，会话将从这里接续。</p>
      </div>
      <div className="open-grid">
        <section className="repo-section">
          <div className="section-title">
            <div>
              <span className="eyebrow">最近仓库</span>
              <h2>你的工作空间</h2>
            </div>
            <button className="text-button" onClick={onStart}>
              查看全部 <Icon name="arrow" size={13} />
            </button>
          </div>
          <div className="repo-list">
            <RepoCard
              name="Karajan"
              path="zhouy1017 / Karajan"
              branch="feature/csv-export"
              active
            />
            <RepoCard name="Sundial" path="zhouy1017 / Sundial" branch="main" />
            <RepoCard
              name="Ledger UI"
              path="zhouy1017 / ledger-ui"
              branch="release / 2.4"
            />
          </div>
          <button className="open-example" onClick={onStart}>
            <span>＋</span> 打开示例仓库 <Icon name="arrow" size={15} />
          </button>
        </section>
        <section className="commander-setup">
          <div className="setup-kicker">
            <span className="setup-icon">✦</span>
            <div>
              <span className="eyebrow">高级会话</span>
              <h2>选择 Commander</h2>
            </div>
          </div>
          <p>Commander 负责理解需求、提出分工，并在执行前等待你的批准。</p>
          <label>
            模型
            <SelectField
              value={model}
              options={["Astra", "Sol"]}
              onChange={setModel}
            />
          </label>
          <label>
            来源
            <SelectField
              value={source}
              options={["Codex", "ChatGPT"]}
              onChange={setSource}
            />
          </label>
          <div className="setup-fact">
            <span className="online-dot" />
            <div>
              <strong>可用 · 示例配置</strong>
              <small>选择会保留在本次演示中</small>
            </div>
          </div>
          <button className="primary wide" onClick={onStart}>
            进入 Commander 对话 <Icon name="arrow" size={15} />
          </button>
        </section>
      </div>
    </div>
  );
}

function RepoCard({
  name,
  path,
  branch,
  active = false,
}: {
  name: string;
  path: string;
  branch: string;
  active?: boolean;
}) {
  return (
    <div className={`repo-card ${active ? "active" : ""}`}>
      <div className="repo-card-top">
        <span className="repo-card-icon">
          <Icon name="repo" size={17} />
        </span>
        {active && <span className="active-label">当前</span>}
      </div>
      <strong>{name}</strong>
      <small>{path}</small>
      <span className="branch-line">
        <Icon name="branch" size={12} /> {branch}
      </span>
    </div>
  );
}

function CommanderScene({
  messages,
  draft,
  setDraft,
  onSend,
  onContinue,
  model,
  setModel,
  source,
  setSource,
}: {
  messages: { from: "user" | "commander"; text: string }[];
  draft: string;
  setDraft: (value: string) => void;
  onSend: () => void;
  onContinue: () => void;
  model: string;
  setModel: (value: string) => void;
  source: string;
  setSource: (value: string) => void;
}) {
  return (
    <div className="scene-content commander-scene">
      <div className="conversation-column">
        <div className="scene-title-row">
          <div>
            <span className="eyebrow">WORKSPACE / 02</span>
            <h1>和 Commander 讨论需求</h1>
            <p>先把目标说清楚，再决定如何分工。</p>
          </div>
          <span className="version-label">计划 v0 · 未批准</span>
        </div>
        <CommanderPanel
          messages={messages}
          draft={draft}
          setDraft={setDraft}
          onSend={onSend}
          onContinue={onContinue}
          model={model}
          source={source}
        />
      </div>
      <aside className="context-column">
        <div className="context-card repository-context">
          <span className="eyebrow">当前上下文</span>
          <div className="context-repo">
            <span className="repo-card-icon">
              <Icon name="repo" size={16} />
            </span>
            <div>
              <strong>Karajan</strong>
              <small>feature/csv-export</small>
            </div>
          </div>
          <div className="context-lines">
            <span>
              <b>目标</b> 为订单增加 CSV 导出
            </span>
            <span>
              <b>基准</b> main · 示例快照
            </span>
            <span>
              <b>状态</b> <i className="online-dot" /> 只读上下文已就绪
            </span>
          </div>
        </div>
        <div className="context-card routing-card">
          <span className="eyebrow">Commander 配置</span>
          <label>
            模型
            <SelectField
              value={model}
              options={["Astra", "Sol"]}
              onChange={setModel}
            />
          </label>
          <label>
            来源
            <SelectField
              value={source}
              options={["Codex", "ChatGPT"]}
              onChange={setSource}
            />
          </label>
          <small className="routing-note">高级模型只负责判断与验收。</small>
        </div>
        <div className="context-card hint-card">
          <span className="hint-number">01</span>
          <strong>说出你在意的边界</strong>
          <p>例如：字段顺序、失败反馈、是否需要导出按钮。</p>
        </div>
      </aside>
    </div>
  );
}

function HubScene({
  stage,
  tasks,
  setTasks,
  messages,
  draft,
  setDraft,
  onSend,
  onContinue,
  onConfirm,
  onSimulate,
  onOpenDetails,
  model,
  setModel,
  source,
  setSource,
}: {
  stage: HubStage;
  tasks: Task[];
  setTasks: (tasks: Task[]) => void;
  messages: { from: "user" | "commander"; text: string }[];
  draft: string;
  setDraft: (value: string) => void;
  onSend: () => void;
  onContinue: () => void;
  onConfirm: () => void;
  onSimulate: (id: string) => void;
  onOpenDetails: (scene: Scene, taskId?: string) => void;
  model: string;
  setModel: (value: string) => void;
  source: string;
  setSource: (value: string) => void;
}) {
  const updateTask = (
    id: string,
    field: "role" | "model" | "source" | "dependency",
    value: string,
  ) =>
    setTasks(
      tasks.map((task) =>
        task.id === id ? { ...task, [field]: value } : task,
      ),
    );
  return (
    <div className="scene-content hub-scene">
      <div className="hub-heading">
        <div>
          <span className="eyebrow">COMMANDER HUB</span>
          <h1>把协作留在一个工作面</h1>
          <p>Commander 拆解、分发、汇总；你随时可以展开任一详情。</p>
        </div>
        <span className={`hub-stage stage-${stage}`}>
          {stage === "proposal"
            ? "建议待确认"
            : stage === "running"
              ? "任务进行中"
              : "可交付候选"}
        </span>
      </div>
      <div className="hub-layout">
        <section className="hub-conversation">
          <div className="hub-panel-label">
            <span>持续对话</span>
            <small>
              {model} · {source}
            </small>
          </div>
          <CommanderPanel
            messages={messages}
            draft={draft}
            setDraft={setDraft}
            onSend={onSend}
            onContinue={onContinue}
            model={model}
            source={source}
          />
        </section>
        <aside className="hub-workspace">
          <div className="hub-panel-label">
            <span>
              {stage === "proposal"
                ? "建议分工"
                : stage === "running"
                  ? "运行摘要"
                  : "结果汇总"}
            </span>
            <button
              className="text-button"
              onClick={() =>
                onOpenDetails(
                  stage === "proposal"
                    ? "plan"
                    : stage === "running"
                      ? "run"
                      : "review",
                )
              }
            >
              展开详情 <Icon name="arrow" size={12} />
            </button>
          </div>
          {stage === "proposal" ? (
            <HubPlanCard
              tasks={tasks}
              onUpdate={updateTask}
              onConfirm={onConfirm}
            />
          ) : stage === "running" ? (
            <HubRunCards
              tasks={tasks}
              onSimulate={onSimulate}
              onOpenDetails={(taskId) => onOpenDetails("run", taskId)}
            />
          ) : (
            <HubCompleteCard onOpenDetails={() => onOpenDetails("review")} />
          )}
        </aside>
      </div>
    </div>
  );
}

function HubPlanCard({
  tasks,
  onUpdate,
  onConfirm,
}: {
  tasks: Task[];
  onUpdate: (
    id: string,
    field: "role" | "model" | "source" | "dependency",
    value: string,
  ) => void;
  onConfirm: () => void;
}) {
  const parallelCount = tasks.filter(
    (task) => task.role === "Worker" && task.dependency === "无",
  ).length;
  return (
    <section className="hub-plan" id="hub-plan">
      <div className="hub-card-head">
        <div>
          <strong>Commander 的初步分工</strong>
          <small>可编辑配置 · 计划 v1 · {parallelCount} 项可并行</small>
        </div>
        <span className="version-chip">待确认</span>
      </div>
      <div className="hub-plan-rows">
        {tasks.map((task) => (
          <div className="hub-plan-row" key={task.id}>
            <div className="hub-plan-main">
              <span
                className={`task-index ${task.role === "Reviewer" ? "reviewer" : "worker"}`}
              >
                {task.role === "Reviewer" ? "S" : "W"}
              </span>
              <div className="hub-task-copy">
                <strong>{task.title}</strong>
                <small>{task.detail}</small>
              </div>
            </div>
            <div className="hub-plan-controls">
              <label>
                角色
                <SelectField
                  value={task.role}
                  options={["Worker", "Reviewer"]}
                  onChange={(value) => onUpdate(task.id, "role", value)}
                />
              </label>
              <label>
                模型
                <SelectField
                  value={task.model}
                  options={["Terra", "Luna", "Sol"]}
                  onChange={(value) => onUpdate(task.id, "model", value)}
                />
              </label>
              <label>
                来源
                <SelectField
                  value={task.source}
                  options={["Codex", "ChatGPT"]}
                  onChange={(value) => onUpdate(task.id, "source", value)}
                />
              </label>
              <label>
                依赖
                <SelectField
                  value={task.dependency}
                  options={
                    task.id === "csv-api"
                      ? ["无"]
                      : task.id === "csv-entry"
                        ? ["无", "后端接口"]
                        : ["前两项"]
                  }
                  onChange={(value) => onUpdate(task.id, "dependency", value)}
                />
              </label>
            </div>
          </div>
        ))}
      </div>
      <div className="hub-card-foot">
        <small>确认后才会分发；运行中的 Profile 保持固定。</small>
        <button className="primary" onClick={onConfirm}>
          确认并分发 <Icon name="arrow" size={14} />
        </button>
      </div>
    </section>
  );
}

function HubRunCards({
  tasks,
  onSimulate,
  onOpenDetails,
}: {
  tasks: Task[];
  onSimulate: (id: string) => void;
  onOpenDetails: (taskId?: string) => void;
}) {
  const runningCount = tasks.filter((task) => task.state === "running").length;
  const waitingCount = tasks.filter((task) => task.state === "waiting").length;
  return (
    <section className="hub-run-cards">
      <div className="hub-run-head">
        <span>
          <i className="pulse" /> {runningCount} 个运行中 · {waitingCount}{" "}
          个等待
        </span>
        <small>
          {runningCount} 个运行中 · {waitingCount} 个等待依赖 · 样例动作推进
        </small>
      </div>
      {tasks.map((task) => (
        <div
          className={`hub-run-card ${task.role === "Reviewer" ? "reviewer" : ""}`}
          key={task.id}
          onClick={() => onOpenDetails(task.id)}
        >
          <span
            className={`task-index ${task.role === "Reviewer" ? "reviewer" : "worker"}`}
          >
            {task.role === "Reviewer" ? "S" : "W"}
          </span>
          <div className="hub-task-copy">
            <strong>{task.title}</strong>
            <small>
              {task.model} · {task.source} · {task.result}
            </small>
          </div>
          <StatusPill state={task.state} />
          {task.state === "running" && (
            <button
              className="simulate-button"
              onClick={(event) => {
                event.stopPropagation();
                onSimulate(task.id);
              }}
            >
              模拟任务完成
            </button>
          )}
        </div>
      ))}
      <button className="detail-link" onClick={() => onOpenDetails()}>
        查看运行详情 <Icon name="arrow" size={12} />
      </button>
    </section>
  );
}

function HubCompleteCard({ onOpenDetails }: { onOpenDetails: () => void }) {
  return (
    <section className="hub-complete-card">
      <div className="complete-mark">✓</div>
      <div>
        <strong>本轮样例已完成 CSV 接口和列表导出入口</strong>
        <p>
          保留筛选并补充失败反馈 · 候选 #demo-01 · checks 通过 · Reviewer
          建议交付 · 尚未创建 PR
        </p>
      </div>
      <button className="primary wide" onClick={onOpenDetails}>
        展开检查与交付 <Icon name="arrow" size={14} />
      </button>
    </section>
  );
}

function PlanScene({
  tasks,
  setTasks,
  onStart,
  onBackToHub,
  stage,
}: {
  tasks: Task[];
  setTasks: (tasks: Task[]) => void;
  onStart: () => void;
  onBackToHub: () => void;
  stage: HubStage;
}) {
  const parallelCount = tasks.filter(
    (task) => task.role === "Worker" && task.dependency === "无",
  ).length;
  const updateTask = (
    id: string,
    field: "role" | "model" | "source" | "dependency",
    value: string,
  ) =>
    setTasks(
      tasks.map((task) =>
        task.id === id ? { ...task, [field]: value } : task,
      ),
    );
  return (
    <div className="scene-content plan-scene">
      <div className="scene-title-row">
        <div>
          <span className="eyebrow">WORKSPACE / 03</span>
          <h1>确认任务分工</h1>
          <p>你可以调整角色、模型和来源，再接受这份计划。</p>
        </div>
        <div className="detail-title-actions">
          <button className="back-hub" onClick={onBackToHub}>
            ← 返回 Commander Hub
          </button>
          <div className="plan-summary">
            <span className="summary-number">{parallelCount}</span>
            <div>
              <strong>项可并行</strong>
              <small>独立写区 · 固定配置</small>
            </div>
          </div>
        </div>
      </div>
      <section className="plan-card">
        <div className="plan-card-head">
          <div>
            <strong>
              {stage === "proposal" ? "建议任务图" : "已批准任务图"}
            </strong>
            <small>
              {stage === "proposal"
                ? "由 Commander 根据当前需求生成 · 计划版本 v1"
                : "当前运行绑定 · 计划版本 v1"}
            </small>
          </div>
          <span className="version-chip">
            {stage === "proposal" ? "待批准" : "已批准版本"}
          </span>
        </div>
        <div className="plan-table-wrap">
          <table className="plan-table">
            <thead>
              <tr>
                <th>任务</th>
                <th>角色</th>
                <th>模型</th>
                <th>来源</th>
                <th>依赖</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id}>
                  <td>
                    <div className="task-name">
                      <span
                        className={`task-index ${task.role === "Reviewer" ? "reviewer" : "worker"}`}
                      >
                        {task.role === "Reviewer" ? "S" : "W"}
                      </span>
                      <div>
                        <strong>{task.title}</strong>
                        <small>{task.detail}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    <SelectField
                      value={task.role}
                      options={["Worker", "Reviewer"]}
                      disabled={stage !== "proposal"}
                      onChange={(value) => updateTask(task.id, "role", value)}
                    />
                  </td>
                  <td>
                    <SelectField
                      value={task.model}
                      options={["Terra", "Luna", "Sol"]}
                      disabled={stage !== "proposal"}
                      onChange={(value) => updateTask(task.id, "model", value)}
                    />
                  </td>
                  <td>
                    <SelectField
                      value={task.source}
                      options={["Codex", "ChatGPT"]}
                      disabled={stage !== "proposal"}
                      onChange={(value) => updateTask(task.id, "source", value)}
                    />
                  </td>
                  <td>
                    <SelectField
                      value={task.dependency}
                      options={
                        task.id === "csv-api"
                          ? ["无"]
                          : task.id === "csv-entry"
                            ? ["无", "后端接口"]
                            : ["前两项"]
                      }
                      disabled={stage !== "proposal"}
                      onChange={(value) =>
                        updateTask(task.id, "dependency", value)
                      }
                    />
                  </td>
                  <td>
                    <StatusPill state={task.state} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="plan-card-foot">
          <div className="approval-note">
            <span className="check-circle">✓</span>
            <span>
              <strong>范围清楚</strong>
              <small>
                {stage === "proposal"
                  ? "接受后将固定这份计划与配置"
                  : "配置已固定，运行中的 Profile 不可更改"}
              </small>
            </span>
          </div>
          <button
            className="primary"
            onClick={onStart}
            disabled={stage !== "proposal"}
          >
            {stage === "proposal" ? "接受并启动" : "已批准，返回 Hub"}{" "}
            <Icon name="arrow" size={15} />
          </button>
        </div>
      </section>
      <div className="plan-note">
        <span>i</span>{" "}
        {stage === "proposal"
          ? "模型和来源是演示选项。未启动任务可以继续调整，运行后配置将保持不变。"
          : "模型、来源和依赖已随批准版本固定；详情页仅供查看。"}
      </div>
    </div>
  );
}

function RunTaskCard({
  task,
  selected,
  onSelect,
  onComplete,
}: {
  task: Task;
  selected: boolean;
  onSelect: () => void;
  onComplete?: () => void;
}) {
  return (
    <button
      className={`run-task-card ${selected ? "selected" : ""}`}
      onClick={onSelect}
    >
      <div className="run-task-head">
        <span className="task-index worker">W</span>
        <StatusPill state={task.state} />
      </div>
      <strong>{task.title}</strong>
      <small>
        {task.model} · {task.source} · 独立写区
      </small>
      <div className="run-task-progress">
        <span
          style={{
            width:
              task.state === "done"
                ? "100%"
                : task.state === "running"
                  ? "58%"
                  : "8%",
          }}
        />
      </div>
      <span className="run-task-result">{task.result}</span>
      {task.state !== "done" && onComplete && (
        <span
          className="run-simulate"
          onClick={(event) => {
            event.stopPropagation();
            onComplete();
          }}
        >
          模拟任务完成
        </span>
      )}
    </button>
  );
}

function RunScene({
  tasks,
  selectedTask,
  setSelectedTask,
  detailTab,
  setDetailTab,
  paused,
  setPaused,
  variant,
  runView,
  setRunView,
  onBackToHub,
  onSimulate,
}: {
  tasks: Task[];
  selectedTask: Task;
  setSelectedTask: (task: Task) => void;
  detailTab: DetailTab;
  setDetailTab: (tab: DetailTab) => void;
  paused: boolean;
  setPaused: (paused: boolean) => void;
  variant: Variant;
  runView: "tasks" | "agents";
  setRunView: (view: "tasks" | "agents") => void;
  onBackToHub: () => void;
  onSimulate: (id: string) => void;
}) {
  const workers = tasks.filter((task) => task.role !== "Reviewer");
  const reviewer = tasks.find((task) => task.role === "Reviewer") ?? tasks[2];
  return (
    <div className="scene-content run-scene">
      <div className="scene-title-row run-title">
        <div>
          <span className="eyebrow">WORKSPACE / 04</span>
          <h1>{variant === "C" ? "Agent 工作区" : "并行运行中"}</h1>
          <p>
            {paused
              ? "派发已暂停 · 当前 Attempt 保持不变"
              : "两个 Worker 正在独立写区中工作"}
          </p>
        </div>
        <div className="run-controls">
          <button className="back-hub" onClick={onBackToHub}>
            ← 返回 Commander Hub
          </button>
          <span className="run-clock">
            <i className="pulse" /> 示例运行 · 00:42
          </span>
          <button className="secondary" onClick={() => setPaused(!paused)}>
            <Icon name={paused ? "play" : "pause"} size={14} />{" "}
            {paused ? "继续派发" : "暂停派发"}
          </button>
        </div>
      </div>
      <div className="run-layout">
        <section className="run-main">
          <div className="view-switch">
            <button
              className={runView === "tasks" ? "active" : ""}
              onClick={() => setRunView("tasks")}
            >
              <Icon name="task" size={14} /> Tasks
            </button>
            <button
              className={runView === "agents" ? "active" : ""}
              onClick={() => setRunView("agents")}
            >
              <Icon name="agent" size={14} /> Agents
            </button>
            <span>按状态筛选</span>
          </div>
          {variant === "C" || runView === "agents" ? (
            <div className="agent-columns">
              <AgentColumn
                title="Workers"
                accent="teal"
                tasks={workers}
                selectedTask={selectedTask}
                onSelect={setSelectedTask}
              />
              <AgentColumn
                title="Reviewer"
                accent="amber"
                tasks={[reviewer]}
                selectedTask={selectedTask}
                onSelect={setSelectedTask}
              />
              <AgentColumn
                title="Evidence"
                accent="blue"
                tasks={[]}
                selectedTask={selectedTask}
                onSelect={setSelectedTask}
              />
            </div>
          ) : variant === "B" ? (
            <ParallelBoard
              tasks={workers}
              selectedTask={selectedTask}
              onSelect={setSelectedTask}
              reviewer={reviewer}
            />
          ) : (
            <div className="run-task-stack">
              {workers.map((task) => (
                <RunTaskCard
                  key={task.id}
                  task={task}
                  selected={selectedTask.id === task.id}
                  onSelect={() => setSelectedTask(task)}
                  onComplete={() => onSimulate(task.id)}
                />
              ))}
              <div
                className="reviewer-waiting"
                onClick={() => setSelectedTask(reviewer)}
              >
                <span className="task-index reviewer">S</span>
                <div>
                  <strong>{reviewer.title}</strong>
                  <small>
                    {reviewer.model} · {reviewer.source} · {reviewer.dependency}
                    后开始
                  </small>
                </div>
                <StatusPill state="waiting" />
                <Icon name="arrow" size={15} />
              </div>
            </div>
          )}
          <div className="run-foot-note">
            <span className="lock-symbol">⌁</span> 已批准版本 v1 · Profile
            在运行中保持固定 · 资源摘要未知（示例）
          </div>
        </section>
        {variant === "B" ? (
          <aside className="run-drawer">
            <TaskDetail
              task={selectedTask}
              detailTab={detailTab}
              setDetailTab={setDetailTab}
              variant={variant}
            />
          </aside>
        ) : (
          <TaskDetail
            task={selectedTask}
            detailTab={detailTab}
            setDetailTab={setDetailTab}
            variant={variant}
          />
        )}
      </div>
    </div>
  );
}

function ParallelBoard({
  tasks,
  selectedTask,
  onSelect,
  reviewer,
}: {
  tasks: Task[];
  selectedTask: Task;
  onSelect: (task: Task) => void;
  reviewer: Task;
}) {
  const columns: { key: TaskState; label: string; hint: string }[] = [
    { key: "ready", label: "待开始", hint: "已批准，等待派发" },
    { key: "running", label: "进行中", hint: "独立写区" },
    { key: "waiting", label: "检查中", hint: "等待候选" },
    { key: "done", label: "完成", hint: "已产生证据" },
  ];
  return (
    <div className="parallel-board">
      {columns.map((column) => (
        <div className={`board-column board-${column.key}`} key={column.key}>
          <div className="board-column-head">
            <span>{column.label}</span>
            <b>{tasks.filter((task) => task.state === column.key).length}</b>
          </div>
          <small className="board-hint">{column.hint}</small>
          {tasks
            .filter((task) => task.state === column.key)
            .map((task) => (
              <button
                key={task.id}
                className={
                  selectedTask.id === task.id
                    ? "board-card selected"
                    : "board-card"
                }
                onClick={() => onSelect(task)}
              >
                <span className="task-index worker">W</span>
                <strong>{task.title}</strong>
                <small>
                  {task.model} · {task.source}
                </small>
                <StatusPill state={task.state} />
              </button>
            ))}
          {column.key === "waiting" && (
            <button
              className={
                selectedTask.id === reviewer.id
                  ? "board-card reviewer-card selected"
                  : "board-card reviewer-card"
              }
              onClick={() => onSelect(reviewer)}
            >
              <span className="task-index reviewer">S</span>
              <strong>{reviewer.title}</strong>
              <small>
                {reviewer.model} · {reviewer.source}
              </small>
              <StatusPill state="waiting" />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function AgentColumn({
  title,
  accent,
  tasks,
  selectedTask,
  onSelect,
}: {
  title: string;
  accent: string;
  tasks: Task[];
  selectedTask: Task;
  onSelect: (task: Task) => void;
}) {
  return (
    <div className={`agent-column accent-${accent}`}>
      <div className="agent-column-head">
        <span className="column-dot" />
        <strong>{title}</strong>
        <span>{tasks.length || "—"}</span>
      </div>
      {tasks.length ? (
        tasks.map((task) => (
          <button
            className={
              selectedTask.id === task.id ? "agent-card selected" : "agent-card"
            }
            key={task.id}
            onClick={() => onSelect(task)}
          >
            <div>
              <span className="avatar">{task.model[0]}</span>
              <span>
                <strong>
                  {task.model} {task.role}
                </strong>
                <small>{task.source} · Attempt 01</small>
              </span>
            </div>
            <b>
              {task.state === "running"
                ? "运行中"
                : task.state === "waiting"
                  ? "等待"
                  : task.state}
            </b>
            <p>{task.title}</p>
            <div className="mini-progress">
              <span />
            </div>
          </button>
        ))
      ) : (
        <div className="column-empty">候选产物将在此汇总</div>
      )}
    </div>
  );
}

function ReviewScene({
  onPrepare,
  onViewDiff,
  onBackToHub,
  stage,
  reviewer,
}: {
  onPrepare: () => void;
  onViewDiff: () => void;
  onBackToHub: () => void;
  stage: HubStage;
  reviewer: Task;
}) {
  return (
    <div className="scene-content review-scene">
      <div className="scene-title-row">
        <div>
          <span className="eyebrow">WORKSPACE / 05</span>
          <h1>检查并准备交付</h1>
          <p>所有证据都绑定到候选 #demo-01，提交前仍由你决定。</p>
        </div>
        <div className="detail-title-actions">
          <button className="back-hub" onClick={onBackToHub}>
            ← 返回 Commander Hub
          </button>
          <span
            className={
              stage === "complete"
                ? "ready-chip"
                : "ready-chip review-waiting-chip"
            }
          >
            <span>{stage === "complete" ? "✓" : "·"}</span>{" "}
            {stage === "complete" ? "可准备 PR" : "等待审查完成"}
          </span>
        </div>
      </div>
      <div className="review-grid">
        <section className="review-main">
          <div className="candidate-banner">
            <div className="candidate-icon">
              {stage === "complete" ? "✓" : "·"}
            </div>
            <div>
              <strong>
                {stage === "complete" ? "候选 #demo-01 已冻结" : "等待候选汇总"}
              </strong>
              <small>
                {stage === "complete"
                  ? "组合顺序：后端 CSV 接口 → 前端导出入口"
                  : "两个 Worker 完成后才会冻结组合候选"}
              </small>
            </div>
            <span className="candidate-date">刚刚 · 示例</span>
          </div>
          <div className="evidence-card">
            <div className="evidence-card-head">
              <div>
                <span className="eyebrow">验收证据</span>
                <h2>Checks</h2>
              </div>
              <span
                className={
                  stage === "complete" ? "pass-label" : "waiting-label"
                }
              >
                {stage === "complete" ? "全部通过" : "等待候选"}
              </span>
            </div>
            <div className="checks-grid">
              <Evidence
                label="类型检查"
                value={stage === "complete" ? "通过 · 42s" : "等待运行"}
                ok={stage === "complete"}
              />
              <Evidence
                label="行为检查"
                value={stage === "complete" ? "通过 · 18s" : "未开始"}
                ok={stage === "complete"}
              />
              <Evidence
                label="候选完整性"
                value={stage === "complete" ? "通过 · 2s" : "等待候选"}
                ok={stage === "complete"}
              />
              <Evidence
                label="工作区隔离"
                value={stage === "complete" ? "已核对" : "未开始"}
                ok={stage === "complete"}
              />
            </div>
          </div>
          <div className="evidence-card diff-card">
            <div className="evidence-card-head">
              <div>
                <span className="eyebrow">候选变更</span>
                <h2>Diff</h2>
              </div>
              <button
                className="text-button"
                onClick={onViewDiff}
                disabled={stage !== "complete"}
              >
                {stage === "complete" ? "查看完整 diff" : "Diff 待候选"}{" "}
                <Icon name="arrow" size={13} />
              </button>
            </div>
            {stage === "complete" ? (
              <div className="diff-files">
                <div>
                  <span className="file-badge">M</span>
                  <strong>orders/export.ts</strong>
                  <span>+42 −7</span>
                </div>
                <div>
                  <span className="file-badge">M</span>
                  <strong>OrderList.tsx</strong>
                  <span>+42 −5</span>
                </div>
                <div>
                  <span className="file-badge new">A</span>
                  <strong>export.test.ts</strong>
                  <span>+18</span>
                </div>
              </div>
            ) : (
              <div className="review-pending">
                候选尚未冻结，Diff 会在 Worker 结果和独立审查完成后出现。
              </div>
            )}
          </div>
        </section>
        <aside className="review-side">
          <div className="independent-review">
            <div className="reviewer-heading">
              <span className="avatar reviewer-avatar">S</span>
              <div>
                <strong>独立 Reviewer</strong>
                <small>
                  {reviewer.model} · {reviewer.source}
                </small>
              </div>
              <span
                className={
                  stage === "complete" ? "pass-label" : "waiting-label"
                }
              >
                {stage === "complete" ? "通过" : "等待中"}
              </span>
            </div>
            <div className="review-verdict">
              <span>{stage === "complete" ? "✓" : "·"}</span>
              <div>
                <strong>
                  {stage === "complete" ? "建议交付" : "等待候选"}
                </strong>
                <p>
                  {stage === "complete"
                    ? "未发现阻塞问题。CSV 字段顺序与失败反馈符合需求。"
                    : "两个 Worker 完成后，Reviewer 才会读取冻结候选。"}
                </p>
              </div>
            </div>
            <div className="review-meta">
              <span>输入候选</span>
              <b>#demo-01</b>
              <span>审查上下文</span>
              <b>独立 · 新会话</b>
            </div>
          </div>
          <div className="delivery-card">
            <span className="eyebrow">交付</span>
            <h3>准备一个 PR</h3>
            <p>演示动作只生成本地交付预览，不会写入 GitHub。</p>
            <button
              className="primary wide"
              onClick={onPrepare}
              disabled={stage !== "complete"}
            >
              准备 PR <Icon name="arrow" size={15} />
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}

function ReviewPreview({
  mode,
  onClose,
}: {
  mode: PreviewMode;
  onClose: () => void;
}) {
  return (
    <div
      className="preview-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        className="preview-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="preview-title"
      >
        <div className="preview-modal-head">
          <div>
            <span className="eyebrow">本地演示预览</span>
            <h2 id="preview-title">
              {mode === "diff" ? "候选 Diff 片段" : "Pull Request 预览"}
            </h2>
          </div>
          <button
            className="preview-close"
            onClick={onClose}
            aria-label="关闭预览"
          >
            ×
          </button>
        </div>
        {mode === "diff" ? (
          <div className="preview-diff">
            <p className="preview-note">
              样例片段 · 候选 #demo-01 · 仅用于讨论
            </p>
            <div className="code-preview">
              <div>
                <span className="line-no">18</span>
                <span className="code-context">
                  {" "}
                  const rows = orders.map(toCsvRow);
                </span>
              </div>
              <div>
                <span className="line-no">19</span>
                <span className="code-add">
                  + return new Response(toCsv(rows), &#123; headers &#125;);
                </span>
              </div>
              <div>
                <span className="line-no">20</span>
                <span className="code-context"> &#125;</span>
              </div>
              <div>
                <span className="line-no">21</span>
                <span className="code-add">
                  + toast.success("CSV 下载已开始");
                </span>
              </div>
            </div>
            <div className="preview-file-summary">
              <span>
                <b>M</b> orders/export.ts
              </span>
              <span>+42 −7</span>
            </div>
            <div className="preview-file-summary">
              <span>
                <b>M</b> OrderList.tsx
              </span>
              <span>+42 −5</span>
            </div>
          </div>
        ) : (
          <div className="pr-preview">
            <span className="preview-status">仅本地预览，未创建</span>
            <label>标题</label>
            <div className="pr-title">feat: add CSV export for orders</div>
            <label>正文</label>
            <div className="pr-body">
              <strong>Summary</strong>
              <p>
                Adds a CSV export action to the order list while preserving
                active filters.
              </p>
              <strong>Checks</strong>
              <p>
                ✓ Type checks
                <br />✓ Export behavior checks
                <br />✓ Independent review
              </p>
            </div>
          </div>
        )}
        <div className="preview-modal-foot">
          <span>演示动作不会写入 GitHub</span>
          <button className="secondary" onClick={onClose}>
            关闭
          </button>
        </div>
      </section>
    </div>
  );
}

function PrototypeSwitcher({
  variant,
  setVariant,
}: {
  variant: Variant;
  setVariant: (variant: Variant) => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target?.matches("input, textarea, select, [contenteditable=true]"))
        return;
      if (event.key === "ArrowLeft")
        setVariant(variant === "A" ? "C" : variant === "B" ? "A" : "B");
      if (event.key === "ArrowRight")
        setVariant(variant === "C" ? "A" : variant === "A" ? "B" : "C");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [variant, setVariant]);
  return (
    <div className="prototype-switcher">
      <button
        aria-label="上一个变体"
        onClick={() =>
          setVariant(variant === "A" ? "C" : variant === "B" ? "A" : "B")
        }
      >
        ←
      </button>
      <span>
        <b>{variant}</b> ·{" "}
        {variant === "A"
          ? "任务工作台"
          : variant === "B"
            ? "并行看板"
            : "Agent 工作区"}
      </span>
      <button
        aria-label="下一个变体"
        onClick={() =>
          setVariant(variant === "C" ? "A" : variant === "A" ? "B" : "C")
        }
      >
        →
      </button>
    </div>
  );
}

export function CommanderPrototype() {
  const query = useMemo(readQuery, []);
  const queryTasks = tasksForStage(query.stage);
  const [variant, setVariantState] = useState<Variant>(query.variant);
  const [scene, setSceneState] = useState<Scene>(query.scene);
  const [stage, setStage] = useState<HubStage>(query.stage);
  const [tasks, setTasks] = useState<Task[]>(queryTasks);
  const [selectedTask, setSelectedTask] = useState<Task>(queryTasks[0]);
  const [detailTab, setDetailTab] = useState<DetailTab>("Diff");
  const [messages, setMessages] = useState(messagesForStage(query.stage));
  const [draft, setDraft] = useState("");
  const [model, setModel] = useState("Astra");
  const [source, setSource] = useState("Codex");
  const [paused, setPaused] = useState(false);
  const [resourceOpen, setResourceOpen] = useState(false);
  const [preview, setPreview] = useState<PreviewMode | null>(null);
  const [runView, setRunView] = useState<"tasks" | "agents">("tasks");

  const setVariant = (next: Variant) => {
    setVariantState(next);
    updateUrl(next, scene, stage);
  };
  const setScene = (next: Scene) => {
    setSceneState(next);
    updateUrl(variant, next, stage);
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  const setHubStage = (next: HubStage) => {
    setStage(next);
    setSceneState("hub");
    updateUrl(variant, "hub", next);
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  const reset = () => {
    setVariantState("A");
    setSceneState("hub");
    setStage("proposal");
    setTasks(tasksForStage("proposal"));
    setSelectedTask(tasksForStage("proposal")[0]);
    setMessages(messagesForStage("proposal"));
    setDraft("");
    setPaused(false);
    setPreview(null);
    updateUrl("A", "hub", "proposal");
  };
  const sendMessage = () => {
    if (!draft.trim()) return;
    setMessages((current) => [
      ...current,
      { from: "user", text: draft.trim() },
      {
        from: "commander",
        text: "已记录，准备分工。我会把这个约束带入任务验收条件。",
      },
    ]);
    setDraft("");
  };
  const startRun = () => {
    if (stage !== "proposal") return;
    const nextTasks: Task[] = tasks.map((task) =>
      task.role === "Reviewer" || task.dependency !== "无"
        ? { ...task, state: "waiting", result: "等待 Worker 产物" }
        : { ...task, state: "running", result: "正在建立独立写区" },
    );
    setTasks(nextTasks);
    setSelectedTask(
      nextTasks.find((task) => task.id === selectedTask.id) ?? nextTasks[0],
    );
    setMessages((currentMessages) => [
      ...currentMessages,
      {
        from: "commander",
        text: `已按计划分发：${nextTasks
          .filter(
            (task) => task.role !== "Reviewer" && task.state === "running",
          )
          .map((task) => `${task.title}（${task.model}）`)
          .join("、")}。Reviewer 将在依赖完成后回到 Hub 汇总。`,
      },
    ]);
    setStage("running");
    setSceneState("hub");
    updateUrl(variant, "hub", "running");
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  const simulateTask = (id: string) => {
    const current = tasks.find((task) => task.id === id);
    if (!current || current.state !== "running") return;
    let nextTasks: Task[] = tasks.map((task) =>
      task.id === id
        ? {
            ...task,
            state: "done",
            result: task.role === "Reviewer" ? "独立审查通过" : "样例任务完成",
          }
        : task,
    );
    const workersReady = nextTasks
      .filter((task) => task.role !== "Reviewer")
      .every((task) => task.state === "done");
    nextTasks = nextTasks.map((task) => {
      if (task.state !== "waiting") return task;
      if (task.role === "Reviewer" && workersReady)
        return { ...task, state: "running", result: "正在独立审查候选" };
      if (
        task.dependency === "后端接口" &&
        nextTasks.find((candidate) => candidate.id === "csv-api")?.state ===
          "done"
      )
        return { ...task, state: "running", result: "依赖已满足，准备写入" };
      return task;
    });
    setTasks(nextTasks);
    setMessages((currentMessages) => [
      ...currentMessages,
      {
        from: "commander",
        text:
          workersReady && current.role !== "Reviewer"
            ? "两个 Worker 的样例结果已就绪，Reviewer 已开始独立检查。"
            : current.role === "Reviewer"
              ? "独立 Reviewer 的样例检查已完成，候选可以回到 Hub 汇总。"
              : `${current.title} 的样例任务已完成，其他任务继续保留在当前状态。`,
      },
    ]);
    if (nextTasks.every((task) => task.state === "done"))
      setHubStage("complete");
  };
  const preparePr = () => setPreview("pr");

  useEffect(() => {
    const onPop = () => {
      const next = readQuery();
      setVariantState(next.variant);
      setSceneState(next.scene);
      setStage(next.stage);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  let content: React.ReactNode;
  if (scene === "open")
    content = (
      <OpenScene
        onStart={() => setScene("hub")}
        model={model}
        setModel={setModel}
        source={source}
        setSource={setSource}
      />
    );
  else if (scene === "hub")
    content = (
      <HubScene
        stage={stage}
        tasks={tasks}
        setTasks={setTasks}
        messages={messages}
        draft={draft}
        setDraft={setDraft}
        onSend={sendMessage}
        onContinue={() => {
          window.setTimeout(
            () =>
              (stage === "proposal"
                ? document.getElementById("hub-plan")
                : document.querySelector(".hub-workspace")
              )?.scrollIntoView({ behavior: "smooth", block: "center" }),
            0,
          );
        }}
        onConfirm={startRun}
        onSimulate={simulateTask}
        onOpenDetails={(next, taskId) => {
          if (taskId)
            setSelectedTask(
              tasks.find((task) => task.id === taskId) ?? selectedTask,
            );
          setScene(next);
        }}
        model={model}
        setModel={setModel}
        source={source}
        setSource={setSource}
      />
    );
  else if (scene === "commander")
    content = (
      <CommanderScene
        messages={messages}
        draft={draft}
        setDraft={setDraft}
        onSend={sendMessage}
        onContinue={() => setScene("plan")}
        model={model}
        setModel={setModel}
        source={source}
        setSource={setSource}
      />
    );
  else if (scene === "plan")
    content = (
      <PlanScene
        tasks={tasks}
        setTasks={setTasks}
        onStart={startRun}
        onBackToHub={() => setScene("hub")}
        stage={stage}
      />
    );
  else if (scene === "run")
    content = (
      <RunScene
        tasks={tasks}
        selectedTask={
          tasks.find((task) => task.id === selectedTask.id) ?? selectedTask
        }
        setSelectedTask={(task) =>
          setSelectedTask(
            tasks.find((candidate) => candidate.id === task.id) ?? task,
          )
        }
        detailTab={detailTab}
        setDetailTab={setDetailTab}
        paused={paused}
        setPaused={setPaused}
        variant={variant}
        runView={runView}
        setRunView={setRunView}
        onBackToHub={() => setScene("hub")}
        onSimulate={simulateTask}
      />
    );
  else
    content = (
      <ReviewScene
        onPrepare={preparePr}
        onViewDiff={() => setPreview("diff")}
        onBackToHub={() => setScene("hub")}
        stage={stage}
        reviewer={tasks.find((task) => task.role === "Reviewer") ?? tasks[2]}
      />
    );

  return (
    <div
      className={`prototype-shell variant-${variant.toLowerCase()} scene-${scene}`}
    >
      <TopBar
        variant={variant}
        scene={scene}
        onScene={setScene}
        onReset={reset}
        resourceOpen={resourceOpen}
        setResourceOpen={setResourceOpen}
      />
      {variant === "A" && (
        <Sidebar
          scene={scene}
          onScene={setScene}
          selectedTask={selectedTask}
          tasks={tasks}
          onTask={setSelectedTask}
        />
      )}
      <main className="prototype-main">{content}</main>
      {scene !== "open" && scene !== "hub" && variant === "A" && (
        <div className="commander-peek">
          <span className="commander-avatar">✦</span>
          <span>
            <strong>Commander</strong>
            <small>随时可以继续讨论</small>
          </span>
          <button onClick={() => setScene("hub")} aria-label="打开 Commander">
            ↗
          </button>
        </div>
      )}
      {preview && (
        <ReviewPreview mode={preview} onClose={() => setPreview(null)} />
      )}
      <PrototypeSwitcher variant={variant} setVariant={setVariant} />
    </div>
  );
}
