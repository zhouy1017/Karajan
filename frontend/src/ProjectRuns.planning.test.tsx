import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ProjectRuns } from "./ProjectRuns";

const project = {
  id: "project-1",
  name: "示例项目",
  revision: 2,
  target_branch: "main",
  configuration: { status: "draft" },
};

const run = {
  id: "run-1",
  schema_version: "karajan.run-planning.v1",
  requirement: { goal: "增加问候语", acceptance: ["显示问候语"] },
  commander: { term: 1, principal: "lead" },
  active_plan_revision: null,
  state: "planning",
  dispatch_enabled: false,
  plans: [],
  handoffs: [],
};

const blockedPlanning = {
  schema_version: "karajan.workbench-planning.v1",
  run,
  planning: {
    intent: {
      id: "intent-1",
      term: 1,
      principal: "lead",
      profile: { id: "commander", revision: 1 },
      budget_ref: "planning",
      state: "prepared",
    },
    execution: {
      id: "execution-1",
      binding_sha256: "b".repeat(64),
      state: "blocked",
      cancel_requested: false,
      reason_codes: ["PLANNING_TRANSPORT_UNAVAILABLE"],
    },
    availability: {
      state: "blocked",
      reason_code: "PLANNING_TRANSPORT_UNAVAILABLE",
    },
  },
};

const awaitingPlanning = {
  ...blockedPlanning,
  planning: {
    ...blockedPlanning.planning,
    availability: { state: "awaiting" },
  },
};

function renderRuns() {
  render(<ProjectRuns project={project} csrf="csrf-fixture" />);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("starts persisted planning with an empty body and shows the actual blocked state", async () => {
  const writes: RequestInit[] = [];
  let started = false;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-1/planning")
      return Response.json(
        started ? blockedPlanning : { ...blockedPlanning, planning: null },
      );
    if (path === "/v1/runs/run-1/planning-start") {
      writes.push(options!);
      started = true;
      return Response.json({ state: "prepared" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "准备规划" }),
  );
  expect(screen.getAllByText("规划已准备，执行服务尚未就绪。")).toHaveLength(1);
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toBe("{}");
  expect(new Headers(writes[0].headers).get("X-CSRF-Token")).toBe(
    "csrf-fixture",
  );
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBeTruthy();
});

it("reuses the same idempotency key after an unknown preparation response", async () => {
  const writes: RequestInit[] = [];
  let attempts = 0;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-1/planning")
      return Response.json({ ...blockedPlanning, planning: null });
    if (path === "/v1/runs/run-1/planning-start") {
      writes.push(options!);
      attempts += 1;
      if (attempts === 1) throw new TypeError("network lost");
      return Response.json({ state: "prepared" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  const button = await screen.findByRole("button", { name: "准备规划" });
  await userEvent.click(button);
  await screen.findByText("network lost");
  await userEvent.click(screen.getByRole("button", { name: "准备规划" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(new Headers(writes[1].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[0].headers).get("Idempotency-Key"),
  );
});

it("keeps an existing plan's exact approval action available", async () => {
  const existingPlan = {
    ...run,
    active_plan_revision: null,
    state: "awaiting_approval",
    configuration_snapshot: {
      configuration: {
        resources: {
          budgets: [
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
    plans: [
      {
        term: 1,
        plan_revision: 1,
        plan_digest: "a".repeat(64),
        authorization_digest: "b".repeat(64),
        configuration_digest: "c".repeat(64),
        plan: {
          summary: "现有计划",
          authorization: {
            profile_refs: [{ id: "worker", revision: 1 }],
            read_paths: ["."],
            write_paths: ["src"],
            checks: ["unit-tests"],
            budget_ref: "planning",
            delivery: "pull_request",
            target_branch: "main",
          },
          tasks: [],
        },
      },
    ],
  };
  vi.stubGlobal("fetch", async (path: string) => {
    if (path.startsWith("/v1/runs?"))
      return Response.json({ items: [existingPlan] });
    if (path === "/v1/runs/run-1") return Response.json(existingPlan);
    if (path === "/v1/runs/run-1/planning")
      return Response.json({
        ...blockedPlanning,
        run: existingPlan,
        planning: null,
      });
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await screen.findByText("现有计划");
  expect(screen.queryByRole("button", { name: "准备规划" })).toBeNull();
  expect(screen.getByRole("button", { name: "确认这份计划" })).toBeTruthy();
});

it("disables preparation when the persisted planning status cannot be read", async () => {
  vi.stubGlobal("fetch", async (path: string) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-1/planning")
      throw new TypeError("planning read failed");
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  expect(
    (await screen.findAllByText("规划状态读取失败，请重新读取。")).length,
  ).toBeGreaterThan(1);
  expect(screen.queryByRole("button", { name: "准备规划" })).toBeNull();
});

it("restores preparation after a failed planning read succeeds on retry", async () => {
  let planningReads = 0;
  vi.stubGlobal("fetch", async (path: string) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-1/planning") {
      planningReads += 1;
      if (planningReads === 1) throw new TypeError("planning read failed");
      return Response.json({ ...blockedPlanning, planning: null });
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  const runButton = await screen.findByRole("button", { name: "增加问候语" });
  await userEvent.click(runButton);
  expect(
    (await screen.findAllByText("规划状态读取失败，请重新读取。")).length,
  ).toBeGreaterThan(1);
  expect(screen.queryByRole("button", { name: "准备规划" })).toBeNull();

  await userEvent.click(runButton);
  expect(
    (
      (await screen.findByRole("button", {
        name: "准备规划",
      })) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
  expect(planningReads).toBe(2);
});

it.each([
  ["preparation", "准备规划", "/v1/runs/run-1/planning-start"],
  ["execution", "生成计划", "/v1/runs/run-1/planning-execute"],
])(
  "recovers busy state when %s command storage fails",
  async (_label, buttonName, endpoint) => {
    vi.stubGlobal("sessionStorage", {
      getItem() {
        throw new Error("session storage unavailable");
      },
      setItem() {
        throw new Error("session storage unavailable");
      },
      removeItem() {
        throw new Error("session storage unavailable");
      },
    });
    vi.stubGlobal("fetch", async (path: string) => {
      if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
      if (path === "/v1/runs/run-1") return Response.json(run);
      if (path === "/v1/runs/run-1/planning")
        return Response.json(
          endpoint.endsWith("start")
            ? { ...blockedPlanning, planning: null }
            : awaitingPlanning,
        );
      if (path === endpoint) throw new Error("command should not be sent");
      throw new Error(`Unexpected request: ${path}`);
    });

    renderRuns();
    await userEvent.click(
      await screen.findByRole("button", { name: "增加问候语" }),
    );
    const button = await screen.findByRole("button", { name: buttonName });
    await userEvent.click(button);
    expect(await screen.findByText("session storage unavailable")).toBeTruthy();
    expect((button as HTMLButtonElement).disabled).toBe(false);
  },
);

it("ignores a late planning read after the project selection changes", async () => {
  let resolveFirstPlanning: ((response: Response) => void) | undefined;
  const firstPlanning = new Promise<Response>((resolve) => {
    resolveFirstPlanning = resolve;
  });
  const secondRun = {
    ...run,
    id: "run-2",
    requirement: { ...run.requirement, goal: "第二个需求" },
  };
  vi.stubGlobal("fetch", async (path: string) => {
    if (path === "/v1/runs?project_id=project-1")
      return Response.json({ items: [run] });
    if (path === "/v1/runs?project_id=project-2")
      return Response.json({ items: [secondRun] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-2") return Response.json(secondRun);
    if (path === "/v1/runs/run-1/planning") return firstPlanning;
    if (path === "/v1/runs/run-2/planning")
      return Response.json({
        ...blockedPlanning,
        run: secondRun,
        planning: null,
      });
    throw new Error(`Unexpected request: ${path}`);
  });

  const view = render(<ProjectRuns project={project} csrf="csrf-fixture" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  view.rerender(
    <ProjectRuns
      project={{ ...project, id: "project-2", name: "第二个项目" }}
      csrf="csrf-fixture"
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "第二个需求" }),
  );
  expect(screen.getAllByText("第二个需求").length).toBeGreaterThan(1);
  resolveFirstPlanning?.(Response.json({ ...blockedPlanning, planning: null }));
  await waitFor(() =>
    expect(screen.getAllByText("第二个需求").length).toBeGreaterThan(1),
  );
  expect(screen.queryByText("增加问候语")).toBeNull();
});

it("submits execution only after preparation and keeps the persisted plan approval", async () => {
  const writes: RequestInit[] = [];
  let executed = false;
  const generatedRun = {
    ...run,
    state: "awaiting_approval",
    configuration_snapshot: {
      configuration: {
        resources: {
          budgets: [
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
    plans: [
      {
        term: 1,
        plan_revision: 1,
        plan_digest: "a".repeat(64),
        authorization_digest: "b".repeat(64),
        configuration_digest: "c".repeat(64),
        plan: {
          summary: "生成的持久计划",
          authorization: {
            profile_refs: [{ id: "worker", revision: 1 }],
            read_paths: ["."],
            write_paths: ["src"],
            checks: ["unit-tests"],
            budget_ref: "planning",
            delivery: "pull_request",
            target_branch: "main",
          },
          tasks: [],
        },
      },
    ],
  };
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1")
      return Response.json(executed ? generatedRun : run);
    if (path === "/v1/runs/run-1/planning")
      return Response.json(awaitingPlanning);
    if (path === "/v1/runs/run-1/planning-execute") {
      writes.push(options!);
      executed = true;
      return Response.json({ ...awaitingPlanning, run: generatedRun });
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  expect(await screen.findByRole("button", { name: "生成计划" })).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "生成计划" }));
  await screen.findByText("生成的持久计划");
  expect(writes).toHaveLength(1);
  expect(writes[0].method).toBe("POST");
  expect(writes[0].body).toBe("{}");
  expect(new Headers(writes[0].headers).get("X-CSRF-Token")).toBe(
    "csrf-fixture",
  );
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBeTruthy();
  expect(screen.getByRole("button", { name: "确认这份计划" })).toBeTruthy();
});

it("shows an execution block without offering a second execution", async () => {
  let execute = false;
  vi.stubGlobal("fetch", async (path: string) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-1/planning")
      return Response.json(
        execute
          ? {
              ...blockedPlanning,
              planning: {
                ...blockedPlanning.planning,
                availability: {
                  state: "blocked",
                  reason_code: "PLANNING_EXECUTION_CANCELLED",
                },
              },
            }
          : awaitingPlanning,
      );
    if (path === "/v1/runs/run-1/planning-execute") {
      execute = true;
      return Response.json(blockedPlanning);
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "生成计划" }),
  );
  expect(await screen.findByText("规划已取消。")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "生成计划" })).toBeNull();
});

it.each([
  [
    "COMMANDER_QUALIFICATION_REQUIRED",
    "当前 Commander 资格不可用，请先完成或更新资格核验。",
  ],
  [
    "PLANNING_BUDGET_EXHAUSTED",
    "当前规划预算不足或已失效，请检查预算范围和剩余额度。",
  ],
  [
    "PLANNING_REASON_FROM_SERVER",
    "当前规划暂不能执行（服务端代码：PLANNING_REASON_FROM_SERVER）。",
  ],
])(
  "explains blocked planning reason %s without offering execution",
  async (reasonCode, message) => {
    let execute = false;
    vi.stubGlobal("fetch", async (path: string) => {
      if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
      if (path === "/v1/runs/run-1") return Response.json(run);
      if (path === "/v1/runs/run-1/planning")
        return Response.json({
          ...blockedPlanning,
          planning: {
            ...blockedPlanning.planning,
            availability: { state: "blocked", reason_code: reasonCode },
          },
        });
      if (path === "/v1/runs/run-1/planning-execute") {
        execute = true;
        throw new Error("blocked planning must not execute");
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    renderRuns();
    await userEvent.click(
      await screen.findByRole("button", { name: "增加问候语" }),
    );
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "生成计划" })).toBeNull();
    expect(execute).toBe(false);
  },
);

it("reuses an unknown execution key after the workbench is remounted", async () => {
  const writes: RequestInit[] = [];
  let attempts = 0;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-1") return Response.json(run);
    if (path === "/v1/runs/run-1/planning")
      return Response.json(awaitingPlanning);
    if (path === "/v1/runs/run-1/planning-execute") {
      writes.push(options!);
      attempts += 1;
      if (attempts === 1) throw new TypeError("network lost");
      return Response.json(awaitingPlanning);
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  const view = render(<ProjectRuns project={project} csrf="csrf-fixture" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "生成计划" }),
  );
  await screen.findByText("network lost");
  view.unmount();
  renderRuns();
  await userEvent.click(
    await screen.findByRole("button", { name: "增加问候语" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "生成计划" }),
  );
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(new Headers(writes[1].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[0].headers).get("Idempotency-Key"),
  );
});
