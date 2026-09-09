import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { ModelFeedback } from "./ModelFeedback";
import "./ModelFeedback.css";

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

    expect(screen.getByText("模型运行中")).toBeTruthy();
    expect(screen.getByText(/最近反馈：/)).toBeTruthy();
    expect(screen.getByText("连接：连接就绪")).toBeTruthy();
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

    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByText("反馈中断，等待核对")).toBeNull();

    rerender(
      <ModelFeedback
        state="failed"
        lastObservedAt={BASE_TIME - 120_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );
    expect(screen.getByText("已失败")).toBeTruthy();

    rerender(
      <ModelFeedback
        state="cancelled"
        lastObservedAt={BASE_TIME - 120_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );
    expect(screen.getByText("已取消")).toBeTruthy();
  });

  it("shows stale/disconnected as awaiting reconciliation for non-terminal states and keeps last observed time", () => {
    vi.spyOn(Date, "now").mockReturnValue(BASE_TIME);
    render(
      <ModelFeedback
        state="running"
        lastObservedAt={BASE_TIME - 30_000}
        connection="disconnected"
        staleDurationMs={10_000}
      />,
    );

    const label = screen.getByText("反馈中断，等待核对");
    expect(label).toBeTruthy();
    const timeNode = screen.getByText("最近反馈：").closest("p") as HTMLElement;
    expect(timeNode).toContainHTML("最近反馈：");
    expect(screen.getByText("终态尚未到达，当前不应改判完成/失败/取消。")).toBeTruthy();
  });

  it("does not invent updates from timers or heartbeat data", () => {
    const timer = vi.spyOn(globalThis, "setInterval");
    render(
      <ModelFeedback
        state="idle"
        lastObservedAt={BASE_TIME}
        connection="connected"
        staleDurationMs={10_000}
      />,
    );

    expect(timer).not.toHaveBeenCalled();
  });

  it("contains reduced-motion boundary in stylesheet", () => {
    const css = fs.readFileSync(
      path.join("frontend", "src", "ModelFeedback.css"),
      "utf8",
    );
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).toContain("animation: none !important;");
  });
});