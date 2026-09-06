/** Independent UI checks using the full view returned by real persisted HTTP. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React from "../../../frontend/node_modules/react/index.js";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "../../../frontend/node_modules/@testing-library/react/dist/index.js";
import userEvent from "../../../frontend/node_modules/@testing-library/user-event/dist/esm/index.js";
import {
  afterEach,
  expect,
  it,
  vi,
} from "../../../frontend/node_modules/vitest/dist/index.js";
import { ProjectRuns } from "../../../frontend/src/ProjectRuns";

const captured = JSON.parse(
  readFileSync(
    resolve("examples/v2-approval-workbench/spec/inputs/valid-view.json"),
    "utf8",
  ),
);
const original = () => structuredClone(captured.run);
const project = captured.project;
const checkbox = () =>
  screen.getByRole("checkbox", { name: "我已审阅以上完整授权范围（v2）" });
const approve = () => screen.getByRole("button", { name: "确认 v2 授权" });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function arrange(
  run = original(),
  handler?: (
    path: string,
    request?: RequestInit,
  ) => Response | Promise<Response>,
) {
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, request?: RequestInit) => {
    if (request?.method === "POST") writes.push(request);
    if (handler) return handler(path, request);
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === `/v1/runs/${run.id}`) return Response.json(run);
    if (path.endsWith("/plan-approval")) {
      run.active_plan_revision = run.plans.at(-1).plan_revision;
      return Response.json({ dispatch_enabled: false });
    }
    throw new Error("Unexpected independent Spec request");
  });
  const view = render(
    React.createElement(ProjectRuns, { project, csrf: "synthetic-spec-csrf" }),
  );
  return { run, writes, view };
}

async function open(run: ReturnType<typeof original>) {
  await userEvent.click(
    await screen.findByRole("button", { name: run.requirement.goal }),
  );
}

it("renders real HTTP v2 scope and sends only its exact frozen approval after explicit review", async () => {
  const { run, writes } = arrange();
  await open(run);
  const region = within(
    await screen.findByRole("region", { name: "完整执行授权（v2）" }),
  );
  for (const value of [
    "fixture-model",
    "fixture-runtime",
    "fixture-account",
    "fixture-provider",
    "synthetic-local-fixture",
    "fixture-tools",
    "controlled_tools",
    "synthetic-report",
    "critical_qualified",
    "standard_qualified",
    "src/auth",
  ]) {
    expect(region.getAllByText(new RegExp(value)).length).toBeGreaterThan(0);
  }
  expect(region.getByText(/普通阶段：不允许/)).toBeTruthy();
  expect(region.getByText(/序号 0/)).toBeTruthy();
  expect(region.getByText(/单次尝试最多 25 秒/)).toBeTruthy();
  expect(region.getAllByText(/输入上界 4096 token/)).toHaveLength(2);
  expect(region.getByText(/另预留输出 1024 token/)).toBeTruthy();
  expect(region.getByText(/总上下文上限 8192 token/)).toBeTruthy();
  expect(region.getAllByText(/USD 0/).length).toBeGreaterThan(0);
  expect(region.getAllByText(/CNY 0/).length).toBeGreaterThan(0);
  await userEvent.click(region.getByText("核对固定版本与授权摘要"));
  const fixed = run.plans.at(-1);
  for (const digest of [
    fixed.plan_digest,
    fixed.authorization_digest,
    fixed.configuration_digest,
    fixed.routing_digest,
    run.execution_policy_snapshot.digest,
    fixed.routing_binding.rulebook.digest,
  ]) {
    expect(region.getAllByText(digest).length).toBeGreaterThan(0);
  }
  for (const name of [
    run.execution_policy_snapshot.id,
    run.execution_policy_snapshot.tool_policy.id,
    run.execution_policy_snapshot.risk_policy.id,
    run.execution_policy_snapshot.context_policy.id,
  ]) {
    expect(region.getAllByText(new RegExp(name)).length).toBeGreaterThan(0);
  }
  expect(approve().hasAttribute("disabled")).toBe(true);
  expect(writes).toHaveLength(0);
  await userEvent.click(checkbox());
  await userEvent.click(approve());
  await screen.findByText("v2 授权已确认；尚未启用实际派发。");
  const plan = run.plans.at(-1);
  expect(writes).toHaveLength(1);
  expect(JSON.parse(writes[0].body as string)).toEqual({
    schema_version: "karajan.approve-plan.v2",
    term: plan.term,
    plan_revision: plan.plan_revision,
    plan_digest: plan.plan_digest,
    authorization_digest: plan.authorization_digest,
    configuration_digest: plan.configuration_digest,
    routing_digest: plan.routing_digest,
  });
  expect(new Headers(writes[0].headers).get("X-CSRF-Token")).toBe(
    "synthetic-spec-csrf",
  );
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBeTruthy();
});

it.each([
  "routing-digest",
  "task-tools",
  "tool-permissions",
  "policy-project",
  "binding-activation",
  "unknown-schema",
])(
  "does not offer confirmation when %s material is absent or contradictory",
  async (kind) => {
    const run = original(),
      plan = run.plans.at(-1);
    if (kind === "routing-digest") delete plan.routing_digest;
    else if (kind === "task-tools") delete plan.plan.tasks[0].tools;
    else if (kind === "tool-permissions")
      delete run.execution_policy_snapshot.tool_policy.tool_permissions;
    else if (kind === "policy-project")
      run.execution_policy_snapshot.project_id = "different-project";
    else if (kind === "binding-activation")
      plan.routing_binding.activation_allowed = true;
    else run.schema_version = "karajan.run-planning.v999";
    const { writes } = arrange(run);
    await open(run);
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
    expect(writes).toHaveLength(0);
  },
);

it("does not offer the old plan after a newer Commander term is displayed", async () => {
  const run = original();
  run.commander.term = 2;
  const { writes } = arrange(run);
  await open(run);
  await screen.findByRole("region", { name: "完整执行授权（v2）" });
  expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
  expect(writes).toHaveLength(0);
});

it.each([409, 422])(
  "invalidates checked approval after HTTP %s even if reloaded plan bytes are unchanged",
  async (status) => {
    const run = original();
    const { writes } = arrange(run, async (path, request) => {
      if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
      if (request?.method === "POST")
        return Response.json(
          { reason_code: "APPROVAL_BINDING_MISMATCH" },
          { status },
        );
      return Response.json(run);
    });
    await open(run);
    await userEvent.click(checkbox());
    await userEvent.click(approve());
    await screen.findByRole("alert");
    expect((checkbox() as HTMLInputElement).checked).toBe(false);
    expect(approve().hasAttribute("disabled")).toBe(true);
    expect(writes).toHaveLength(1);
  },
);

it("removes confirmation if a stale response is followed by a failed current-plan read", async () => {
  const run = original();
  let posted = false;
  const { writes } = arrange(run, async (path, request) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (request?.method === "POST") {
      posted = true;
      return Response.json(
        { reason_code: "PLAN_REVISION_STALE" },
        { status: 409 },
      );
    }
    return posted ? new Response(null, { status: 503 }) : Response.json(run);
  });
  await open(run);
  await userEvent.click(checkbox());
  await userEvent.click(approve());
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
  expect(writes).toHaveLength(1);
});

it("requires a fresh review when the same Run is reopened", async () => {
  const { run, writes } = arrange();
  await open(run);
  await userEvent.click(checkbox());
  expect((checkbox() as HTMLInputElement).checked).toBe(true);
  await open(run);
  await waitFor(() =>
    expect((checkbox() as HTMLInputElement).checked).toBe(false),
  );
  expect(approve().hasAttribute("disabled")).toBe(true);
  expect(writes).toHaveLength(0);
});

it("clears the prior owner-session review when the CSRF session changes", async () => {
  const { run, writes, view } = arrange();
  await open(run);
  await userEvent.click(checkbox());
  view.rerender(
    React.createElement(ProjectRuns, {
      project,
      csrf: "new-synthetic-session",
    }),
  );
  await open(run);
  expect((checkbox() as HTMLInputElement).checked).toBe(false);
  expect(writes).toHaveLength(0);
});

it("keeps the real legacy v1 plan on its original approval protocol", async () => {
  const { run, writes } = arrange(structuredClone(captured.v1_run));
  await open(run);
  await userEvent.click(
    await screen.findByRole("button", { name: "确认这份计划" }),
  );
  await screen.findByText("计划已确认；执行仍需满足运行资格。");
  expect(writes).toHaveLength(1);
  const plan = run.plans.at(-1);
  expect(JSON.parse(writes[0].body as string)).toEqual({
    term: plan.term,
    plan_revision: plan.plan_revision,
    plan_digest: plan.plan_digest,
    authorization_digest: plan.authorization_digest,
    configuration_digest: plan.configuration_digest,
  });
  expect(
    screen.queryByRole("region", { name: "完整执行授权（v2）" }),
  ).toBeNull();
});
