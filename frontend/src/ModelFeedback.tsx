import { type JSX } from "react";

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

function describeStatus(
  props: { state: ModelFeedbackState; stale: boolean; disconnected: boolean },
): {
  state: DisplayState;
  icon: string;
  label: string;
  isBlocked: boolean;
} {
  if (
    !terminalStates.has(props.state) &&
    (props.stale || props.disconnected)
  ) {
    return {
      state: "awaiting_reconciliation",
      icon: awaitingIcon,
      label: awaitingLabel,
      isBlocked: true,
    };
  }
  return {
    state: props.state,
    icon: iconByState[props.state],
    label: labelByState[props.state],
    isBlocked: false,
  };
}

function formatObservedAt(lastObservedAt: number): string {
  if (!Number.isFinite(lastObservedAt) || lastObservedAt <= 0)
    return "尚未接收反馈";

  const date = new Date(lastObservedAt);
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
  const disconnected = connection === "disconnected";
  const stale =
    Number.isFinite(lastObservedAt) &&
    Date.now() - lastObservedAt > staleDurationMs &&
    staleDurationMs >= 0;
  const display = describeStatus({ state, stale, disconnected });
  const observedText = formatObservedAt(lastObservedAt);
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
      {(display.state === "awaiting_reconciliation" ||
        state === "waiting_output") && (
        <p className="model-feedback-hint">
          <span>上次反馈超过 {staleDurationText(staleDurationMs)} 未更新，状态待核对</span>
        </p>
      )}
      <time dateTime={
        Number.isFinite(lastObservedAt) && lastObservedAt > 0
          ? new Date(lastObservedAt).toISOString()
          : undefined
      }>
        {observedText}
      </time>
      {display.isBlocked && (
        <p className="model-feedback-assertion">
          终态尚未到达，当前不应改判完成/失败/取消。
        </p>
      )}
    </section>
  );
}