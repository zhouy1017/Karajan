import { useEffect, useState, type JSX } from "react";
import "./ModelFeedback.css";

type StableModelState =
  | "running"
  | "waiting_input"
  | "waiting_dependency"
  | "waiting_output"
  | "idle"
  | "completed"
  | "failed"
  | "cancelled";

export type ConnectionStatus = "connected" | "disconnected" | "unknown";

export type ModelFeedbackState = StableModelState;

export type ModelFeedbackProps = {
  /** 模型当前明确状态（终态含已完成/失败/已取消）。 */
  state: ModelFeedbackState;
  /** 最近一次可信反馈时间戳（毫秒）。 */
  lastObservedAt: number;
  /** 与后端连接健康状态。 */
  connection: ConnectionStatus;
  /** feedback 无更新即视为过期的时长（毫秒）。 */
  staleDurationMs: number;
};

const iconByState: Record<ModelFeedbackState, string> = {
  running: "⏳",
  waiting_input: "🧭",
  waiting_dependency: "🧩",
  waiting_output: "📡",
  idle: "💤",
  completed: "✅",
  failed: "❌",
  cancelled: "⏹️",
};

const labelByState: Record<ModelFeedbackState, string> = {
  running: "模型运行中",
  waiting_input: "等待用户输入",
  waiting_dependency: "等待依赖",
  waiting_output: "等待运行输出",
  idle: "空闲中",
  completed: "已完成",
  failed: "已失败",
  cancelled: "已取消",
};

const terminalStates: Set<ModelFeedbackState> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

type DisplayState = ModelFeedbackState | "awaiting_reconciliation";

const awaitingLabel = "反馈中断，等待核对";
const awaitingIcon = "⚠️";
const MAX_SAFE_FEEDBACK_TIME = 8_640_000_000_000_000;

function describeStatus(props: {
  state: ModelFeedbackState;
  stale: boolean;
  disconnected: boolean;
  observed: boolean;
}): {
  state: DisplayState;
  icon: string;
  label: string;
} {
  if (
    !terminalStates.has(props.state) &&
    (props.stale || props.disconnected || !props.observed)
  ) {
    return {
      state: "awaiting_reconciliation",
      icon: awaitingIcon,
      label: awaitingLabel,
    };
  }
  return {
    state: props.state,
    icon: iconByState[props.state],
    label: labelByState[props.state],
  };
}

function isFiniteDisplayTime(ts: number): boolean {
  return Number.isFinite(ts) && ts > 0 && ts <= MAX_SAFE_FEEDBACK_TIME;
}

function isoObservedAt(lastObservedAt: number): string | undefined {
  if (!isFiniteDisplayTime(lastObservedAt)) return undefined;

  const date = new Date(lastObservedAt);
  if (Number.isNaN(date.getTime())) return undefined;

  return date.toISOString();
}

function displayObservedAt(lastObservedAt: number): string {
  if (!isFiniteDisplayTime(lastObservedAt)) return "尚未接收反馈";

  const date = new Date(lastObservedAt);
  if (Number.isNaN(date.getTime())) return "尚未接收反馈";

  return `${date.toLocaleDateString("zh-CN")} ${date.toLocaleTimeString("zh-CN")}`;
}

function staleDurationText(staleDurationMs: number): string {
  const limitSeconds = Math.max(0, Math.round(staleDurationMs / 1000));
  return `${limitSeconds} 秒`;
}

export function ModelFeedback({
  state,
  lastObservedAt,
  connection,
  staleDurationMs,
}: ModelFeedbackProps): JSX.Element {
  const [, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (
      !isFiniteDisplayTime(lastObservedAt) ||
      !Number.isFinite(staleDurationMs)
    )
      return;
    const interval = Math.min(Math.max(staleDurationMs / 2, 1_000), 30_000);
    const timer = window.setInterval(() => setClock(Date.now()), interval);
    return () => window.clearInterval(timer);
  }, [lastObservedAt, staleDurationMs, state, connection]);
  const disconnected = connection === "disconnected";
  const unknown = connection === "unknown";
  const observed = isFiniteDisplayTime(lastObservedAt);
  const stale =
    observed &&
    Number.isFinite(staleDurationMs) &&
    Date.now() - lastObservedAt > staleDurationMs &&
    staleDurationMs >= 0;
  const display = describeStatus({
    state,
    stale,
    disconnected: disconnected || unknown,
    observed,
  });
  const observedText = displayObservedAt(lastObservedAt);
  const connectionText =
    connection === "connected"
      ? "连接就绪"
      : connection === "disconnected"
        ? "连接中断"
        : "连接状态未知";

  return (
    <section
      className={`model-feedback model-feedback--${display.state}`}
      role="status"
      aria-live="polite"
    >
      <div className="model-feedback-header">
        <span
          aria-hidden="true"
          className="model-feedback-icon"
          data-testid="model-feedback-icon"
        >
          {display.icon}
        </span>
        <span className="model-feedback-state">{display.label}</span>
      </div>
      <p className="model-feedback-meta">
        <span>最近反馈：{observedText}</span>
        <span>连接：{connectionText}</span>
      </p>
      {display.state === "awaiting_reconciliation" && (
        <p className="model-feedback-hint">
          <span>
            上次反馈超过 {staleDurationText(staleDurationMs)} 未更新，状态待核对
          </span>
        </p>
      )}
      <time dateTime={isoObservedAt(lastObservedAt)}>{observedText}</time>
    </section>
  );
}
