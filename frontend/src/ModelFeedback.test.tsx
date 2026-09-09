import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelFeedback } from "./ModelFeedback";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const BASE_TIME = new Date("2026-09-09T10:00:00.000Z").getTime();

describe("ModelFeedback", () => {
  it("shows explicit state text and latest observed time", () => {
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME);
    render(
      <ModelFeedback
        state="running"
        lastObservedAt={BASE_TIME - 5_000}
        connection="connected"
        staleDurationMs={20_000}
      />,
    );

    const status = screen.getByRole("status");
    expect(status.textContent).toContain("模型运行中");
    expect(status.textContent).toContain("最近反馈：");
    expect(status.textContent).toContain("连接：连接就绪");
  });

  it("does not downgrade completed/failed/cancelled when stale or disconnected", () => {
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME);
    const { rerender } = render(
      <ModelFeedback
        state="completed"
        lastObservedAt={BASE_TIME - 120_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain("已完成");
    expect(screen.queryByText("反馈中断，等待核对")).toBeNull();

    rerender(
      <ModelFeedback
        state="failed"
        lastObservedAt={BASE_TIME - 120_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain("已失败");

    rerender(
      <ModelFeedback
        state="cancelled"
        lastObservedAt={BASE_TIME - 120_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain("已取消");
  });

  it("renders awaiting reconciliation only for stale or disconnected non-terminal states and keeps last observed time", () => {
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME);
    render(
      <ModelFeedback
        state="running"
        lastObservedAt={BASE_TIME - 30_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain(
      "反馈中断，等待核对",
    );
    expect(screen.getByRole("status").textContent).toContain("最近反馈：");
    expect(
      screen.getByText("上次反馈超过 10 秒 未更新，状态待核对"),
    ).toBeTruthy();
  });

  it("treats unknown connection as awaiting reconciliation for running states", () => {
    render(
      <ModelFeedback
        state="running"
        lastObservedAt={BASE_TIME}
        connection="unknown"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain(
      "反馈中断，等待核对",
    );
    expect(screen.getByRole("status").textContent).toContain(
      "连接：连接状态未知",
    );
  });

  it("does not show stale warning for waiting_output when feedback is fresh", () => {
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME);
    render(
      <ModelFeedback
        state="waiting_output"
        lastObservedAt={BASE_TIME - 500}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain("等待运行输出");
    expect(screen.queryByText("状态待核对")).toBeNull();
  });

  it("switches to awaiting reconciliation after stale clock advance without timers", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(BASE_TIME);
    const { rerender } = render(
      <ModelFeedback
        state="waiting_output"
        lastObservedAt={BASE_TIME - 500}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );
    expect(screen.queryByText("反馈中断，等待核对")).toBeNull();

    now.mockReturnValue(BASE_TIME + 15_000);
    rerender(
      <ModelFeedback
        state="waiting_output"
        lastObservedAt={BASE_TIME - 500}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain(
      "反馈中断，等待核对",
    );
  });

  it("ages a connected state into reconciliation without changing its observed time", () => {
    vi.useFakeTimers();
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME);
    render(
      <ModelFeedback
        state="waiting_output"
        lastObservedAt={BASE_TIME - 500}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain("等待运行输出");
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME + 15_000);
    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByRole("status").textContent).toContain(
      "反馈中断，等待核对",
    );
    expect(screen.getByRole("status").textContent).toContain("最近反馈：");
  });

  it("treats invalid/overflow timestamps as unknown and renders stable fallback", () => {
    render(
      <ModelFeedback
        state="running"
        lastObservedAt={Number.POSITIVE_INFINITY}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );

    expect(screen.getByText("尚未接收反馈")).toBeTruthy();
    const time = screen.getByText("尚未接收反馈");
    expect(time.closest("time")?.getAttribute("datetime")).toBeNull();
  });

  it("renders accessible status classes and icons", () => {
    render(
      <ModelFeedback
        state="running"
        lastObservedAt={BASE_TIME - 500}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );

    const status = screen.getByRole("status");
    expect(status.className).toContain("model-feedback");
    expect(status.className).toContain("model-feedback--running");
    expect(screen.getByTestId("model-feedback-icon").textContent).toBe("⏳");
  });
});
