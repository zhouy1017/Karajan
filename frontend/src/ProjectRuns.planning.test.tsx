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

function renderRuns() {
  render(<ProjectRuns project={project} csrf="csrf-fixture" />);
}

afterEach(() => {
  cleanup();
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
  expect(screen.getAllByText("规划已准备，执行服务尚未就绪。")).toHaveLength(2);
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
