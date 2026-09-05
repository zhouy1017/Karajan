import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ProjectRuns } from "./ProjectRuns";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("keeps a Commander handoff pending until the user decides and submits only the current proposal", async () => {
  const actions: RequestInit[] = [];
  let rejected = false;
  const handoff = {
    id: "handoff-1",
    digest: "d".repeat(64),
    binding: { term: 1 },
    candidate: {
      principal: "replacement",
      profile: { id: "replacement-profile", revision: 3 },
    },
    checkpoint: {
      summary: "保留已确认需求",
      artifacts: [{ ref: "checkpoint/plan.json", sha256: "e".repeat(64) }],
    },
    resource_impact: { summary: "预算保持不变", budget_ref: "planning" },
    expires_at: Date.now() / 1000 + 300,
    state: "pending",
  };
  const run = {
    id: "run-1",
    requirement: { goal: "增加问候语", acceptance: ["显示问候语"] },
    commander: { term: 1, principal: "lead" },
    state: "planning",
    dispatch_enabled: false,
    active_plan_revision: null,
    plans: [],
    handoffs: [handoff],
    configuration_snapshot: {
      configuration: {
        resources: {
          budgets: [
            {
              id: "planning",
              currency_limits: { USD: "0", CNY: "0" },
              max_total_attempts: 3,
              max_duration_seconds: 120,
            },
          ],
        },
      },
    },
  };
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1")
      return Response.json({
        ...run,
        handoffs: [{ ...handoff, state: rejected ? "rejected" : "pending" }],
      });
    if (path.endsWith("/handoff-decision")) {
      actions.push(options!);
      rejected = true;
      return Response.json({ state: "rejected" });
    }
    throw new Error("Unexpected request");
  });
  render(
    <ProjectRuns
      project={{
        id: "project-1",
        name: "示例项目",
        revision: 2,
        target_branch: "main",
        configuration: { status: "draft" },
      }}
      csrf="csrf-fixture"
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await screen.findByText("保留已确认需求");
  expect(screen.getByText(/replacement-profile（版本 3）/)).toBeTruthy();
  expect(
    screen.getByText(/规划预算上限：USD 0，CNY 0；3 次尝试、120 秒/),
  ).toBeTruthy();
  expect(screen.getByText("checkpoint/plan.json")).toBeTruthy();
  expect(screen.getByText("e".repeat(64))).toBeTruthy();
  expect(screen.getByText(/当前预算余量和检查点文件内容尚未核对/)).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "确认交接" }).hasAttribute("disabled"),
  ).toBe(false);
  expect(actions).toHaveLength(0);
  await userEvent.click(
    screen.getByRole("button", { name: "保留当前 Commander" }),
  );
  await screen.findByText("已拒绝本次交接，继续保留当前 Commander。");
  expect(JSON.parse(actions[0].body as string)).toEqual({
    term: 1,
    handoff_id: "handoff-1",
    handoff_digest: "d".repeat(64),
    decision: "reject",
  });
  expect(screen.queryByRole("button", { name: "确认交接" })).toBeNull();
});

it.each(["profile", "budget"])(
  "prevents handoff approval when fixed %s material is missing",
  async (missing) => {
    const run = {
      id: "run-1",
      requirement: { goal: "交接待核对", acceptance: [] },
      commander: { term: 1, principal: "lead" },
      state: "planning",
      dispatch_enabled: false,
      active_plan_revision: null,
      plans: [],
      handoffs: [
        {
          id: "handoff-1",
          digest: "d".repeat(64),
          binding: { term: 1 },
          candidate: {
            principal: "replacement",
            ...(missing === "profile"
              ? {}
              : { profile: { id: "candidate", revision: 1 } }),
          },
          checkpoint: { summary: "待核对", artifacts: [] },
          resource_impact: { summary: "提案说明", budget_ref: "planning" },
          expires_at: Date.now() / 1000 + 300,
          state: "pending",
        },
      ],
      configuration_snapshot: {
        configuration: {
          resources: {
            budgets:
              missing === "budget"
                ? []
                : [
                    {
                      id: "planning",
                      currency_limits: { USD: "0" },
                      max_total_attempts: 3,
                      max_duration_seconds: 120,
                    },
                  ],
          },
        },
      },
    };
    vi.stubGlobal("fetch", async (path: string) =>
      Response.json(path.startsWith("/v1/runs?") ? { items: [run] } : run),
    );
    render(
      <ProjectRuns
        project={{
          id: "project-1",
          name: "示例项目",
          revision: 2,
          target_branch: "main",
          configuration: { status: "draft" },
        }}
        csrf="csrf-fixture"
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "交接待核对" }),
    );
    await screen.findByText("Commander 交接提案");
    expect(
      screen.getByRole("button", { name: "确认交接" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(
      screen
        .getByRole("button", { name: "保留当前 Commander" })
        .hasAttribute("disabled"),
    ).toBe(false);
  },
);

it("shows a persisted requirement and approves the exact displayed plan without sending model commands", async () => {
  const actions: { path: string; options?: RequestInit }[] = [];
  const run = {
    id: "run-1",
    requirement: { goal: "增加问候语", acceptance: ["页面显示问候语"] },
    commander: { term: 1, principal: "lead" },
    active_plan_revision: null,
    state: "awaiting_approval",
    configuration_snapshot: {
      configuration: {
        resources: {
          budgets: [
            {
              id: "run",
              currency_limits: { USD: "0", CNY: "0" },
              max_total_attempts: 8,
              max_duration_seconds: 300,
            },
          ],
        },
      },
    },
    dispatch_enabled: false,
    plans: [
      {
        term: 1,
        plan_revision: 2,
        plan_digest: "a".repeat(64),
        authorization_digest: "b".repeat(64),
        configuration_digest: "c".repeat(64),
        plan: {
          summary: "在已有页面增加问候语",
          authorization: {
            profile_refs: [
              { id: "lead-profile", revision: 1 },
              { id: "worker-profile", revision: 2 },
            ],
            read_paths: ["src", "tests"],
            write_paths: ["src"],
            checks: ["unit-tests", "independent_review"],
            budget_ref: "run",
            delivery: "pull_request",
            target_branch: "main",
          },
          tasks: [
            {
              id: "task-1",
              role: "worker",
              readiness: "ready",
              complexity: "T1",
              risk: "standard",
              paths: ["src/home.tsx"],
              depends_on: [],
              required: true,
              acceptance: ["页面显示问候语"],
            },
          ],
        },
      },
    ],
    handoffs: [],
  };
  let approved = false;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1")
      return Response.json({
        ...run,
        active_plan_revision: approved ? 2 : null,
      });
    if (path.endsWith("/configuration"))
      return Response.json({ configuration: null });
    actions.push({ path, options });
    if (path.endsWith("/plan-approval")) {
      approved = true;
      return Response.json({ dispatch_enabled: false });
    }
    throw new Error("Unexpected request");
  });
  render(
    <ProjectRuns
      project={{
        id: "project-1",
        name: "示例项目",
        revision: 2,
        target_branch: "main",
        configuration: { status: "draft", digest: "c".repeat(64) },
      }}
      csrf="csrf-fixture"
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await screen.findByText("在已有页面增加问候语");
  expect(screen.getByText("前置任务：无")).toBeTruthy();
  await screen.findByText("允许修改：src");
  await screen.findByText(/预算上限：USD 0，CNY 0/);
  await userEvent.click(screen.getByRole("button", { name: "确认这份计划" }));
  await screen.findByText("计划已确认；执行仍需满足运行资格。");
  expect(screen.getByText("这份计划已确认，等待满足执行条件。")).toBeTruthy();
  expect(actions).toHaveLength(1);
  expect(actions[0].path).toBe("/v1/runs/run-1/plan-approval");
  expect(JSON.parse(actions[0].options?.body as string)).toEqual({
    term: 1,
    plan_revision: 2,
    plan_digest: "a".repeat(64),
    authorization_digest: "b".repeat(64),
    configuration_digest: "c".repeat(64),
  });
  expect(new Headers(actions[0].options?.headers).get("X-CSRF-Token")).toBe(
    "csrf-fixture",
  );
});
