import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ProjectRuns } from "./ProjectRuns";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const project = {
  id: "project-v2",
  name: "授权示例",
  revision: 2,
  target_branch: "main",
  configuration: { status: "offline_valid", digest: "c".repeat(64) },
};

function fixture() {
  const refs = [
    { id: "worker-alpha", revision: 2 },
    { id: "review-beta", revision: 3 },
  ];
  const tasks = [
    {
      id: "implement",
      revision: 2,
      role: "worker",
      purpose: null,
      readiness: "ready",
      complexity: "T2",
      risk: "standard",
      paths: ["src/feature.py"],
      domains: ["domain-python"],
      required_capabilities: ["task-code-edit"],
      tools: ["read-tool", "edit-tool"],
      context_tokens: 4096,
      duration_seconds: 45,
      depends_on: [],
      required: true,
      acceptance: ["功能通过测试"],
    },
  ];
  const policy = {
    schema_version: "karajan.execution-policy.v1",
    id: "owner-policy",
    revision: 4,
    digest: "e".repeat(64),
    project_id: project.id,
    registered_by: "owner",
    configuration_digest: "c".repeat(64),
    constraints: {
      profile_refs: refs,
      channel_ids: ["channel-alpha", "channel-beta"],
      tools: ["read-tool", "edit-tool"],
      data_destinations: ["destination-alpha", "destination-beta"],
      required_capabilities: ["controlled_tools"],
      min_isolation: "tool_sandboxed",
    },
    channel_destinations: {
      "channel-alpha": "destination-alpha",
      "channel-beta": "destination-beta",
    },
    tool_policy: {
      id: "tool-policy",
      revision: 2,
      tool_permissions: {
        "read-tool": ["filesystem.read"],
        "edit-tool": ["filesystem.edit"],
      },
    },
    risk_policy: {
      id: "risk-policy",
      revision: 1,
      mapping: { standard: "T1", critical: "T3" },
      path_floors: [{ prefix: "src/auth", minimum_class: "T3" }],
    },
    context_policy: {
      id: "context-policy",
      revision: 1,
      input_accounting: "explicit_approved_upper_bound",
      reserved_output_tokens: 1024,
    },
    max_context_tokens: 8192,
  };
  const authorization = {
    read_paths: ["src", "tests"],
    write_paths: ["src"],
    checks: ["unit-tests", "independent_review"],
    budget_ref: "run",
    delivery: "pull_request",
    target_branch: "main",
    ...policy.constraints,
    currency_limits: { USD: "1.25", CNY: "0.000001" },
    max_attempt_duration_seconds: 60,
    max_quality_repair_rounds: 1,
    stage_permissions: {
      "bounded-worker": { normal: true, quality_indices: [0] },
      "standard-review": { normal: false, quality_indices: [] },
    },
  };
  return {
    schema_version: "karajan.run-planning.v2",
    id: "run-v2",
    owner: "owner",
    project_id: project.id,
    requirement: { goal: "审阅 v2 授权", acceptance: ["功能通过测试"] },
    commander: { term: 2, principal: "lead" },
    active_plan_revision: null as number | null,
    state: "awaiting_approval",
    dispatch_enabled: false,
    handoffs: [],
    execution_policy_snapshot: policy,
    configuration_snapshot: {
      digest: "c".repeat(64),
      configuration: {
        resources: {
          budgets: [
            {
              id: "run",
              currency_limits: { USD: "10", CNY: "99" },
              max_total_attempts: 8,
              max_duration_seconds: 300,
            },
          ],
          accounts: [
            { id: "account-alpha", provider_id: "service-alpha" },
            { id: "account-beta", provider_id: "service-beta" },
          ],
          channels: [
            {
              id: "channel-alpha",
              account_id: "account-alpha",
              billing_path: "subscription_only",
            },
            {
              id: "channel-beta",
              account_id: "account-beta",
              billing_path: "api_cash",
            },
          ],
          profiles: refs.map((ref, index) => ({
            ...ref,
            profile: {
              ...ref,
              binding: {
                model_id: index ? "model-beta" : "model-alpha",
                channel_id: index ? "channel-beta" : "channel-alpha",
                account_id: index ? "account-beta" : "account-alpha",
                runtime_kind: "fixture-runtime",
                runtime_version: "1.0",
                auth_mode: "fixture",
                billing_path: index ? "api_cash" : "subscription_only",
                native_settings: {},
              },
            },
          })),
        },
      },
    },
    plans: [
      {
        term: 2,
        plan_revision: 3,
        plan_digest: "a".repeat(64),
        authorization_digest: "b".repeat(64),
        configuration_digest: "c".repeat(64),
        routing_digest: "d".repeat(64),
        plan: { summary: "限定来源与工具的实施计划", authorization, tasks },
        routing_binding: {
          schema_version: "karajan.approved-routing-binding.v1",
          configuration_digest: "c".repeat(64),
          execution_policy: {
            id: policy.id,
            revision: policy.revision,
            digest: policy.digest,
            project_id: project.id,
            registered_by: "owner",
          },
          rulebook: {
            id: "personal-rules",
            revision: 7,
            digest: "f".repeat(64),
          },
          authorization_ceiling_digest: "0".repeat(64),
          activation_allowed: false,
          stage_grants: {
            "bounded-worker": {
              normal: { standard_group: [refs[0]] },
              quality: [
                { index: 0, group: "critical_group", profiles: [refs[1]] },
              ],
            },
            "standard-review": { normal: {}, quality: [] },
          },
          task_requirements: {
            implement: {
              revision: 2,
              role: "worker",
              purpose: null,
              readiness: "ready",
              complexity: "T2",
              risk: "standard",
              paths: ["src/feature.py"],
              domains: ["domain-python"],
              required_capabilities: ["task-code-edit"],
              tools: ["read-tool", "edit-tool"],
              context_tokens: 4096,
              duration_seconds: 45,
            },
          },
        },
      },
    ],
  };
}

it("shows every v2 authorization dimension before explicitly approving its exact frozen revision", async () => {
  const run = fixture();
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-v2") return Response.json(run);
    if (path.endsWith("/plan-approval")) {
      writes.push(options!);
      run.active_plan_revision = 3;
      return Response.json({ dispatch_enabled: false });
    }
    throw new Error("Unexpected request");
  });
  render(<ProjectRuns project={project} csrf="fixture-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  const scope = within(
    await screen.findByRole("region", { name: "完整执行授权（v2）" }),
  );
  for (const value of [
    "service-alpha",
    "service-beta",
    "model-alpha",
    "model-beta",
    "destination-alpha",
    "destination-beta",
    "filesystem.read",
    "filesystem.edit",
    "controlled_tools",
    "standard_group",
    "critical_group",
    "domain-python",
    "task-code-edit",
  ]) {
    expect(scope.getAllByText(new RegExp(value)).length).toBeGreaterThan(0);
  }
  expect(scope.getByText(/USD 1.25/)).toBeTruthy();
  expect(scope.getByText(/CNY 0.000001/)).toBeTruthy();
  expect(scope.getByText(/单次尝试最多 60 秒/)).toBeTruthy();
  expect(scope.getByText(/质量修复最多 1 轮/)).toBeTruthy();
  expect(scope.getByText(/输入上界 4096 token/)).toBeTruthy();
  expect(scope.getByText(/任务时长 45 秒/)).toBeTruthy();
  expect(scope.getByText(/普通阶段：不允许/)).toBeTruthy();
  expect(
    scope.getByText(/所引用的运行预算上限：USD 10，CNY 99；8 次尝试、300 秒/),
  ).toBeTruthy();
  expect(
    scope.getByText(/输出 1024 token；总上下文上限 8192 token/),
  ).toBeTruthy();
  expect(scope.getByText(/src\/auth → T3/)).toBeTruthy();
  await userEvent.click(scope.getByText("核对固定版本与授权摘要"));
  for (const digest of ["a", "b", "c", "d", "e", "f"])
    expect(scope.getByText(digest.repeat(64))).toBeTruthy();
  expect(
    scope.getByText(/owner-policy（版本 4）；登记人：owner；项目：project-v2/),
  ).toBeTruthy();
  expect(scope.getByText(/工具政策：tool-policy（版本 2）/)).toBeTruthy();
  expect(
    screen
      .getByRole("button", { name: "确认 v2 授权" })
      .hasAttribute("disabled"),
  ).toBe(true);
  expect(writes).toHaveLength(0);
  await userEvent.click(
    screen.getByRole("checkbox", { name: "我已审阅以上完整授权范围（v2）" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
  await screen.findByText("v2 授权已确认；尚未启用实际派发。");
  expect(writes).toHaveLength(1);
  expect(JSON.parse(writes[0].body as string)).toEqual({
    schema_version: "karajan.approve-plan.v2",
    term: 2,
    plan_revision: 3,
    plan_digest: "a".repeat(64),
    authorization_digest: "b".repeat(64),
    configuration_digest: "c".repeat(64),
    routing_digest: "d".repeat(64),
  });
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBeTruthy();
  expect(new Headers(writes[0].headers).get("X-CSRF-Token")).toBe(
    "fixture-csrf",
  );
});

it.each([
  "missing-routing",
  "missing-binding",
  "different-task",
  "different-owner",
  "unknown-version",
  "missing-version",
  "v2-under-v1",
])("never offers a legacy approval for %s material", async (kind) => {
  const run = fixture();
  if (kind === "missing-routing")
    Reflect.deleteProperty(run.plans[0], "routing_digest");
  if (kind === "missing-binding")
    Reflect.deleteProperty(run.plans[0], "routing_binding");
  if (kind === "different-task")
    run.plans[0].routing_binding.task_requirements.implement.tools = [];
  if (kind === "different-owner")
    run.execution_policy_snapshot.registered_by = "another-owner";
  if (kind === "unknown-version")
    run.schema_version = "karajan.run-planning.v3";
  if (kind === "missing-version") Reflect.deleteProperty(run, "schema_version");
  if (kind === "v2-under-v1") run.schema_version = "karajan.run-planning.v1";
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") writes.push(options);
    return Response.json(path.startsWith("/v1/runs?") ? { items: [run] } : run);
  });
  render(<ProjectRuns project={project} csrf="fixture-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
  expect(screen.queryByRole("button", { name: "确认这份计划" })).toBeNull();
  expect(writes).toHaveLength(0);
});

it.each(["PLAN_REVISION_STALE", "RUN_PROTOCOL_VERSION_MISMATCH"])(
  "requires fresh review after %s and submits only the newly displayed scope",
  async (reason) => {
    const run = fixture();
    const writes: RequestInit[] = [];
    vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
      if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
      if (path === "/v1/runs/run-v2") return Response.json(run);
      writes.push(options!);
      if (writes.length === 1) {
        run.commander.term = 3;
        run.plans[0].term = 3;
        run.plans[0].plan_revision = 4;
        run.plans[0].routing_digest = "1".repeat(64);
        run.plans[0].plan_digest = "2".repeat(64);
        run.plans[0].authorization_digest = "3".repeat(64);
        run.plans[0].plan.authorization.currency_limits.USD = "0.50";
        return Response.json({ reason_code: reason }, { status: 409 });
      }
      run.active_plan_revision = 4;
      return Response.json({ dispatch_enabled: false });
    });
    render(<ProjectRuns project={project} csrf="fixture-csrf" />);
    await userEvent.click(
      await screen.findByRole("button", { name: "审阅 v2 授权" }),
    );
    await userEvent.click(
      await screen.findByRole("checkbox", {
        name: "我已审阅以上完整授权范围（v2）",
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
    await screen.findByText(/USD 0.50/);
    expect(writes).toHaveLength(1);
    expect(screen.getByRole("checkbox")).toHaveProperty("checked", false);
    expect(
      screen
        .getByRole("button", { name: "确认 v2 授权" })
        .hasAttribute("disabled"),
    ).toBe(true);
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
    await screen.findByText("v2 授权已确认；尚未启用实际派发。");
    expect(JSON.parse(writes[1].body as string)).toMatchObject({
      schema_version: "karajan.approve-plan.v2",
      term: 3,
      plan_revision: 4,
      routing_digest: "1".repeat(64),
      plan_digest: "2".repeat(64),
      authorization_digest: "3".repeat(64),
    });
    expect(new Headers(writes[1].headers).get("Idempotency-Key")).not.toBe(
      new Headers(writes[0].headers).get("Idempotency-Key"),
    );
  },
);

it("preserves domain-validated exponent notation in the original currency amount", async () => {
  const run = fixture();
  run.plans[0].plan.authorization.currency_limits.USD = "0e0";
  vi.stubGlobal("fetch", async (path: string) =>
    Response.json(path.startsWith("/v1/runs?") ? { items: [run] } : run),
  );
  render(<ProjectRuns project={project} csrf="fixture-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  await screen.findByText(/USD 0e0/);
  expect(await screen.findByRole("checkbox")).toHaveProperty("checked", false);
  expect(screen.getByRole("button", { name: "确认 v2 授权" })).toBeTruthy();
});

it("retries an uncertain approval with its original body and idempotency key", async () => {
  const run = fixture();
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-v2") return Response.json(run);
    writes.push(options!);
    if (writes.length === 1) return new Response(null, { status: 503 });
    run.active_plan_revision = 3;
    return Response.json({ dispatch_enabled: false });
  });
  render(<ProjectRuns project={project} csrf="fixture-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
  await screen.findByText("尚未确认批准结果，可重试同一操作。");
  await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
  await screen.findByText("v2 授权已确认；尚未启用实际派发。");
  expect(writes[1].body).toBe(writes[0].body);
  expect(new Headers(writes[1].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[0].headers).get("Idempotency-Key"),
  );
});

it("does not carry consent when switching Runs", async () => {
  const first = fixture(),
    second = fixture();
  second.id = "run-other";
  second.requirement.goal = "另一份需求";
  vi.stubGlobal("fetch", async (path: string) =>
    Response.json(
      path.startsWith("/v1/runs?")
        ? { items: [first, second] }
        : path.endsWith("run-other")
          ? second
          : first,
    ),
  );
  render(<ProjectRuns project={project} csrf="fixture-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  await userEvent.click(await screen.findByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "另一份需求" }));
  await screen.findByRole("heading", { name: "另一份需求" });
  expect(screen.getByRole("checkbox")).toHaveProperty("checked", false);
  expect(
    screen
      .getByRole("button", { name: "确认 v2 授权" })
      .hasAttribute("disabled"),
  ).toBe(true);
});

it("ignores a late Run response from the previously selected project", async () => {
  const first = fixture(),
    second = fixture();
  second.id = "run-next";
  second.project_id = "project-next";
  second.requirement.goal = "新项目需求";
  second.execution_policy_snapshot.project_id = second.project_id;
  second.plans[0].routing_binding.execution_policy.project_id =
    second.project_id;
  let release: (response: Response) => void = () => {};
  const late = new Promise<Response>((resolve) => {
    release = resolve;
  });
  vi.stubGlobal("fetch", async (path: string) => {
    if (path.startsWith("/v1/runs?"))
      return Response.json({
        items: [path.includes("project-next") ? second : first],
      });
    if (path.endsWith("run-v2")) return late;
    return Response.json(second);
  });
  const rendered = render(
    <ProjectRuns project={project} csrf="fixture-csrf" />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  rendered.rerender(
    <ProjectRuns
      project={{ ...project, id: "project-next" }}
      csrf="fixture-csrf"
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "新项目需求" }),
  );
  await screen.findByRole("heading", { name: "新项目需求" });
  await act(async () => {
    release(Response.json(first));
  });
  expect(screen.queryByRole("heading", { name: "审阅 v2 授权" })).toBeNull();
  expect(screen.getByRole("heading", { name: "新项目需求" })).toBeTruthy();
  expect(screen.getByRole("checkbox")).toHaveProperty("checked", false);
});

it("revokes the displayed confirmation when a stale approval cannot reload its current plan", async () => {
  const run = fixture();
  let stale = false;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [run] });
    if (path === "/v1/runs/run-v2")
      return stale ? new Response(null, { status: 503 }) : Response.json(run);
    if (options?.method === "POST") {
      stale = true;
      return Response.json(
        { reason_code: "PLAN_REVISION_STALE" },
        { status: 409 },
      );
    }
    throw new Error("Unexpected request");
  });
  render(<ProjectRuns project={project} csrf="fixture-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: "审阅 v2 授权" }),
  );
  await userEvent.click(
    await screen.findByRole("checkbox", {
      name: "我已审阅以上完整授权范围（v2）",
    }),
  );
  await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
});
