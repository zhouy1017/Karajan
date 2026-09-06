import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { RoutingSimulation, type SimulationInput } from "./RoutingSimulation";
import { RulebookPanel, type Rulebook } from "./RulebookPanel";
// These fixture reads run only in Vitest's Node process. Keep the browser
// project free of Node ambient types and load the runtime through Vitest.
const { readFileSync } = await vi.importActual<{
  readFileSync: (path: string, encoding: "utf8") => string;
}>("node:fs");
const inputText = readFileSync("../examples/routing/fixed-input.json", "utf8");
const reportText = readFileSync(
  "../examples/routing/published-report.json",
  "utf8",
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});
function input(): SimulationInput {
  return JSON.parse(inputText);
}
function draft(): Rulebook {
  return JSON.parse(inputText).policy.rulebook;
}
function report() {
  return {
    schema_version: "karajan.rulebook-simulation.v1",
    scope: "explicit_simulation",
    activation_allowed: false,
    model_calls: 0,
    result: JSON.parse(reportText).result,
  };
}
const project = { id: "project-1", name: "模拟项目" };
function mount(document: Rulebook | null = draft()) {
  const expire = vi.fn();
  const instance = render(
    <RoutingSimulation
      project={project}
      csrf="csrf-1"
      draft={document}
      onSessionExpired={expire}
    />,
  );
  fireEvent.click(screen.getByText("路由模拟 · 使用固定快照演练"));
  return { ...instance, expire };
}
type Handler = (
  path: string,
  options?: RequestInit,
) => Response | Promise<Response>;
function serve(handler?: Handler) {
  const writes: { path: string; options: RequestInit }[] = [];
  const mock = vi.fn(async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") writes.push({ path, options });
    return handler
      ? handler(path, options)
      : Response.json(path.endsWith("simulation-example") ? input() : report());
  });
  vi.stubGlobal("fetch", mock);
  return { mock, writes };
}
async function load() {
  await userEvent.click(
    screen.getByRole("button", { name: "加载固定离线示例" }),
  );
  await screen.findByLabelText("模拟角色");
}
async function run() {
  await userEvent.click(screen.getByRole("button", { name: "模拟当前编辑" }));
  await screen.findByRole("region", { name: "模拟结果" });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function upload(text: string, filename = "scenario.json") {
  const file = new File([text], filename, { type: "application/json" });
  Object.defineProperty(file, "text", { value: async () => text });
  await userEvent.upload(screen.getByLabelText("导入模拟快照"), file);
}

it("waits for explicit input and runs inside the rulebook editor without publication writes", async () => {
  const { writes, mock } = serve((path) => {
    if (path.endsWith("/configuration"))
      return Response.json({
        project_revision: 4,
        configuration_revision: 2,
        configuration: {
          rulebook: draft(),
          resources: input().policy.resources,
        },
      });
    if (path.endsWith("simulation-example")) return Response.json(input());
    if (path.endsWith("/simulate")) return Response.json(report());
    return Response.json({ items: [] });
  });
  render(
    <RulebookPanel
      project={project}
      csrf="csrf-1"
      onSessionExpired={vi.fn()}
    />,
  );
  await screen.findByLabelText("版本说明");
  expect(
    mock.mock.calls.some(([path]) => path.endsWith("simulation-example")),
  ).toBe(false);
  await userEvent.click(screen.getByText("路由模拟 · 使用固定快照演练"));
  await load();
  await run();
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe("/v1/projects/project-1/rulebook/simulate");
});

it("replaces only the current rulebook and sends frozen authorization and resources unchanged", async () => {
  const edited = draft();
  edited.revision += 1;
  edited.profile_groups.extra = [{ id: "unapproved", revision: 1 }];
  const { writes, mock } = serve();
  mount(edited);
  expect(mock).not.toHaveBeenCalled();
  await load();
  const frozen = input();
  fireEvent.change(screen.getByLabelText("模拟角色"), {
    target: { value: "commander" },
  });
  fireEvent.change(screen.getByLabelText("Commander 用途"), {
    target: { value: "advice" },
  });
  fireEvent.change(screen.getByLabelText("任务就绪情况"), {
    target: { value: "T0" },
  });
  fireEvent.change(screen.getByLabelText("任务难度"), {
    target: { value: "T3" },
  });
  fireEvent.change(screen.getByLabelText("风险标签"), {
    target: { value: "critical" },
  });
  fireEvent.change(screen.getByLabelText("所需上下文 token"), {
    target: { value: "9000" },
  });
  fireEvent.change(screen.getByLabelText("预计任务时长（秒）"), {
    target: { value: "40" },
  });
  fireEvent.change(screen.getByLabelText("涉及路径（每行一条）"), {
    target: { value: "src/auth.py\ntests/auth.py" },
  });
  await run();
  const sent = JSON.parse(String(writes[0].options.body));
  expect(sent).toEqual({
    ...frozen,
    policy: { ...frozen.policy, rulebook: edited },
    task: {
      ...frozen.task,
      role: "commander",
      purpose: "advice",
      readiness: "T0",
      complexity: "T3",
      risk: "critical",
      context_tokens: 9000,
      duration_seconds: 40,
      paths: ["src/auth.py", "tests/auth.py"],
    },
  });
  expect(sent.task.authorization.approved_groups.extra).toBeUndefined();
  expect(sent.task.authorization.ceiling_profile_refs).toEqual(
    frozen.task.authorization.ceiling_profile_refs,
  );
  const headers = new Headers(writes[0].options.headers);
  expect(headers.get("X-CSRF-Token")).toBe("csrf-1");
  expect(headers.has("If-Match")).toBe(false);
  expect(headers.has("Idempotency-Key")).toBe(false);
});

it("shows selected and rejected candidates, every reason and server-ranked components", async () => {
  const output = report();
  const first = output.result.candidates[0];
  output.result.selected_profile = { id: "z-first", revision: 1 };
  output.result.candidates = [
    { ...first, rank: 2, profile: { id: "a-second", revision: 1 } },
    { ...first, rank: 1, profile: { id: "z-first", revision: 1 } },
    {
      profile: { id: "no", revision: 2 },
      eligible: false,
      reason_codes: [
        "GROUP_PROFILE_NOT_APPROVED",
        "SOME_FUTURE_REASON",
        "QUOTA_INSUFFICIENT:pool",
      ],
    },
  ];
  serve((path) =>
    Response.json(path.endsWith("simulation-example") ? input() : output),
  );
  mount();
  await load();
  await run();
  const area = screen.getByRole("region", { name: "模拟结果" });
  const rows = within(area).getAllByRole("article");
  expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
    "候选 z-first v1",
    "候选 a-second v1",
    "淘汰 no v2",
  ]);
  expect(within(rows[0]).getByText("最紧张额度池压力")).toBeTruthy();
  expect(within(rows[0]).getByText("1 / 100")).toBeTruthy();
  expect(within(rows[2]).getByText("GROUP_PROFILE_NOT_APPROVED")).toBeTruthy();
  expect(within(rows[2]).getByText("SOME_FUTURE_REASON")).toBeTruthy();
  expect(within(rows[2]).getByText("QUOTA_INSUFFICIENT:pool")).toBeTruthy();
  expect(within(area).getByText("bounded-worker")).toBeTruthy();
  expect(within(area).getByText("T2")).toBeTruthy();
});

it("makes blocked outcomes explicit without suggesting activation", async () => {
  const output = report();
  output.result.selected_profile = null;
  output.result.candidates = [];
  output.result.reason_codes = ["TASK_NOT_READY"];
  serve((path) =>
    Response.json(path.endsWith("simulation-example") ? input() : output),
  );
  mount();
  await load();
  await run();
  expect(
    screen.getByRole("heading", { name: "此快照下暂时阻塞" }),
  ).toBeTruthy();
  expect(screen.getByText("任务尚未就绪")).toBeTruthy();
  expect(screen.getByText(/真实执行资格未验证/)).toBeTruthy();
});

it("shows imported provenance, snapshot timestamps and unknown values without inventing availability", async () => {
  const snapshot = input();
  snapshot.capacity.pools[0].confidence = "unknown";
  snapshot.capacity.pools[0].reported_remaining = null;
  snapshot.capacity.pools[0].reset_at = null;
  snapshot.capacity.estimates[0].confidence = "unknown";
  snapshot.capacity.estimates[0].completion_seconds = null;
  serve();
  mount();
  await upload(JSON.stringify(snapshot), "unknown-snapshot.json");
  expect(screen.getByText(/导入文件：unknown-snapshot.json/)).toBeTruthy();
  expect(
    screen.getByText(/资源快照时间：1970-01-01 00:16:40 UTC/),
  ).toBeTruthy();
  await userEvent.click(screen.getByText("核对资源事实、时间与未知信息"));
  expect(screen.getAllByText(/未知/).length).toBeGreaterThan(1);
  expect(screen.getByText(/报告剩余/)).toBeTruthy();
});

it("exports exactly the composed input and unchanged complete server report", async () => {
  const blobs: Blob[] = [];
  vi.stubGlobal("URL", {
    createObjectURL: (blob: Blob) => {
      blobs.push(blob);
      return "blob:fixture";
    },
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const edited = draft();
  edited.revision = 7;
  const { writes } = serve();
  mount(edited);
  await load();
  await userEvent.click(
    screen.getByRole("button", { name: "导出当前模拟输入" }),
  );
  await run();
  await userEvent.click(
    screen.getByRole("button", { name: "导出完整模拟报告" }),
  );
  async function parse(blob: Blob) {
    return JSON.parse(
      await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blob);
      }),
    );
  }
  expect(await parse(blobs[0])).toEqual(
    JSON.parse(String(writes[0].options.body)),
  );
  expect(await parse(blobs[1])).toEqual(report());
});

it("sends identical bytes on repeat simulation with identical facts", async () => {
  const { writes } = serve();
  mount();
  await load();
  await run();
  const first = screen.getByRole("region", { name: "模拟结果" }).textContent;
  await run();
  expect(writes[1].options).toEqual(writes[0].options);
  expect(screen.getByRole("region", { name: "模拟结果" }).textContent).toBe(
    first,
  );
});

it.each(["task", "draft", "file", "example"])(
  "invalidates a completed result after changing %s",
  async (change) => {
    serve();
    const instance = mount();
    await load();
    await run();
    if (change === "task")
      fireEvent.change(screen.getByLabelText("风险标签"), {
        target: { value: "critical" },
      });
    else if (change === "draft")
      instance.rerender(
        <RoutingSimulation
          project={project}
          csrf="csrf-1"
          draft={{ ...draft(), revision: 2 }}
          onSessionExpired={instance.expire}
        />,
      );
    else if (change === "file") await upload(inputText);
    else await load();
    expect(screen.queryByRole("region", { name: "模拟结果" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "导出完整模拟报告" }),
    ).toBeNull();
  },
);

it.each(["duplicate", "escaped duplicate", "invalid", "missing"])(
  "rejects %s file data before any simulation request",
  async (mode) => {
    const { writes } = serve();
    mount();
    const text =
      mode === "duplicate"
        ? inputText.replace(
            '"role": "worker"',
            '"role": "reviewer", "role": "worker"',
          )
        : mode === "escaped duplicate"
          ? inputText.replace(
              '"role": "worker"',
              '"r\\u006fle": "reviewer", "role": "worker"',
            )
          : mode === "invalid"
            ? "{"
            : JSON.stringify({ task: {} });
    await upload(text);
    await screen.findByRole("alert");
    expect(writes).toHaveLength(0);
    expect(screen.queryByLabelText("模拟角色")).toBeNull();
  },
);

it("accepts the same field names in different objects and commas/braces inside strings", async () => {
  serve();
  mount();
  const snapshot = input();
  snapshot.task.risk = 'quoted {a,b} "value"';
  await upload(JSON.stringify(snapshot));
  await screen.findByLabelText("模拟角色");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("checks the composed UTF-8 body size rather than file characters", async () => {
  const { writes } = serve();
  mount();
  const snapshot = input();
  snapshot.task.paths = Array.from({ length: 600 }, () => "界".repeat(50));
  const text = JSON.stringify(snapshot);
  expect(text.length).toBeLessThan(65536);
  expect(new TextEncoder().encode(text).byteLength).toBeGreaterThan(65536);
  await upload(text);
  await userEvent.click(screen.getByRole("button", { name: "模拟当前编辑" }));
  await screen.findByText(/超过 64 KiB 请求上限/);
  expect(writes).toHaveLength(0);
});

it.each(["422", "malformed", "wrong scope", "network"])(
  "clears the old result and handles a %s simulation failure",
  async (mode) => {
    let fail = false;
    serve((path) => {
      if (path.endsWith("simulation-example")) return Response.json(input());
      if (!fail) return Response.json(report());
      if (mode === "422")
        return Response.json(
          {
            reason_code: "TASK_SNAPSHOT_INVALID",
            issues: [{ path: "task.paths", code: "INVALID" }],
          },
          { status: 422 },
        );
      if (mode === "network") throw new TypeError("offline");
      return mode === "malformed"
        ? new Response("{")
        : Response.json({ ...report(), activation_allowed: true });
    });
    mount();
    await load();
    await run();
    fail = true;
    await userEvent.click(screen.getByRole("button", { name: "模拟当前编辑" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("region", { name: "模拟结果" })).toBeNull();
    if (mode === "422")
      expect(
        screen.getByText(/TASK_SNAPSHOT_INVALID.*task.paths: INVALID/),
      ).toBeTruthy();
  },
);

it.each(["example", "simulate"])(
  "expires only the active session on %s 401",
  async (stage) => {
    let unauthorized = false;
    serve((path) =>
      unauthorized
        ? new Response(null, { status: 401 })
        : Response.json(
            path.endsWith("simulation-example") ? input() : report(),
          ),
    );
    const mounted = mount();
    if (stage === "simulate") await load();
    unauthorized = true;
    await userEvent.click(
      screen.getByRole("button", {
        name: stage === "example" ? "加载固定离线示例" : "模拟当前编辑",
      }),
    );
    await waitFor(() => expect(mounted.expire).toHaveBeenCalledTimes(1));
  },
);

it.each(["example", "simulate"])(
  "discards late %s headers after mount, session, project, draft or task changes",
  async (stage) => {
    for (const change of [
      "unmount",
      "csrf",
      "project",
      "draft",
      ...(stage === "simulate" ? ["task"] : []),
    ]) {
      for (const outcome of ["success", "401", "network"]) {
        const delayed = deferred<Response>();
        let hold = false;
        serve((path) =>
          hold
            ? delayed.promise
            : Response.json(
                path.endsWith("simulation-example") ? input() : report(),
              ),
        );
        const mounted = mount();
        if (stage === "simulate") await load();
        hold = true;
        await userEvent.click(
          screen.getByRole("button", {
            name: stage === "example" ? "加载固定离线示例" : "模拟当前编辑",
          }),
        );
        if (change === "unmount") mounted.unmount();
        else if (change === "task")
          fireEvent.change(screen.getByLabelText("风险标签"), {
            target: { value: "new-risk" },
          });
        else
          mounted.rerender(
            <RoutingSimulation
              project={
                change === "project"
                  ? { ...project, id: "new-project" }
                  : project
              }
              csrf={change === "csrf" ? "new-csrf" : "csrf-1"}
              draft={change === "draft" ? { ...draft(), revision: 8 } : draft()}
              onSessionExpired={mounted.expire}
            />,
          );
        await act(async () => {
          if (outcome === "network") delayed.reject(new TypeError("old error"));
          else
            delayed.resolve(
              outcome === "401"
                ? new Response(null, { status: 401 })
                : Response.json(stage === "example" ? input() : report()),
            );
        });
        expect(mounted.expire).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).toBeNull();
        expect(screen.queryByRole("region", { name: "模拟结果" })).toBeNull();
        mounted.unmount();
      }
    }
  },
);

it.each(["example", "simulate"])(
  "rechecks edit scope after delayed %s JSON parsing",
  async (stage) => {
    const parsed = deferred<unknown>();
    let hold = false;
    serve((path) => {
      const response = Response.json(
        path.endsWith("simulation-example") ? input() : report(),
      );
      if (hold)
        vi.spyOn(response, "json").mockImplementation(() => parsed.promise);
      return response;
    });
    const mounted = mount();
    if (stage === "simulate") await load();
    hold = true;
    await userEvent.click(
      screen.getByRole("button", {
        name: stage === "example" ? "加载固定离线示例" : "模拟当前编辑",
      }),
    );
    mounted.rerender(
      <RoutingSimulation
        project={project}
        csrf="csrf-1"
        draft={{ ...draft(), revision: 9 }}
        onSessionExpired={mounted.expire}
      />,
    );
    await act(async () =>
      parsed.resolve(stage === "example" ? input() : report()),
    );
    expect(screen.queryByRole("region", { name: "模拟结果" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    if (stage === "example")
      expect(screen.queryByLabelText("模拟角色")).toBeNull();
  },
);

it.each(["unmount", "csrf", "project", "draft", "task"])(
  "discards delayed imported text after %s changes",
  async (change) => {
    serve();
    const mounted = mount();
    if (change === "task") await load();
    const delayed = deferred<string>();
    const file = new File([""], "late.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: () => delayed.promise });
    await userEvent.upload(screen.getByLabelText("导入模拟快照"), file);
    if (change === "unmount") mounted.unmount();
    else if (change === "task")
      fireEvent.change(screen.getByLabelText("风险标签"), {
        target: { value: "new-risk" },
      });
    else
      mounted.rerender(
        <RoutingSimulation
          project={
            change === "project" ? { ...project, id: "new-project" } : project
          }
          csrf={change === "csrf" ? "new-csrf" : "csrf-1"}
          draft={change === "draft" ? { ...draft(), revision: 8 } : draft()}
          onSessionExpired={mounted.expire}
        />,
      );
    await act(async () => delayed.resolve(inputText));
    expect(screen.queryByText(/导入文件：late.json/)).toBeNull();
  },
);

it("does not mix a late old result into a newer result", async () => {
  const old = deferred<Response>();
  let calls = 0;
  const newest = report();
  newest.result.rule_id = "new-rule";
  serve((path) =>
    path.endsWith("simulation-example")
      ? Response.json(input())
      : ++calls === 1
        ? old.promise
        : Response.json(newest),
  );
  const mounted = mount();
  await load();
  await userEvent.click(screen.getByRole("button", { name: "模拟当前编辑" }));
  mounted.rerender(
    <RoutingSimulation
      project={project}
      csrf="csrf-1"
      draft={{ ...draft(), revision: 3 }}
      onSessionExpired={mounted.expire}
    />,
  );
  await run();
  await act(async () => old.resolve(Response.json(report())));
  expect(
    screen.getByRole("region", { name: "模拟结果" }).textContent,
  ).toContain("new-rule");
});

it("renders the original prototype-like evidence reference without crashing", async () => {
  serve();
  mount();
  await upload(
    readFileSync(
      "../examples/routing-workbench/review-fixes/label-prototype.input.json",
      "utf8",
    ),
  );
  await userEvent.click(screen.getByText("核对资源事实、时间与未知信息"));
  expect(screen.getByText(/__proto__/)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

it.each(["constructor", "toString"])(
  "keeps %s literal in evidence, reason and field labels",
  async (name) => {
    const snapshot = input();
    snapshot.policy.profile_facts[0].evidence_ref = name;
    const output = report();
    output.result.candidates = [
      {
        profile: { id: "refused", revision: 1 },
        eligible: false,
        reason_codes: [name],
        [name]: "literal-field-value",
      },
    ];
    output.result.selected_profile = null;
    serve((path) =>
      Response.json(path.endsWith("simulation-example") ? snapshot : output),
    );
    mount();
    await load();
    await run();
    expect(screen.getAllByText(new RegExp(name)).length).toBeGreaterThan(1);
    expect(screen.getByText("literal-field-value")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  },
);

it("rejects the original overflowing cooldown file without sending a null replacement", async () => {
  const { writes } = serve();
  mount();
  await upload(
    readFileSync(
      "../examples/routing-workbench/review-fixes/ui-standards-overflow.input.json",
      "utf8",
    ),
  );
  await screen.findByText(/数字超出可靠范围/);
  expect(writes).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "模拟当前编辑" })).toBeNull();
});

it.each(["9007199254740993", "-1e400", "9007199254740992.1"])(
  "rejects the imported non-finite or unsafe integer %s",
  async (number) => {
    const { writes } = serve();
    mount();
    await upload(
      inputText.replace(
        '"cooldown_until": null',
        `"cooldown_until": ${number}`,
      ),
    );
    await screen.findByRole("alert");
    expect(writes).toHaveLength(0);
    expect(screen.queryByLabelText("模拟角色")).toBeNull();
  },
);

it.each([
  "1.0",
  "1e3",
  "0.1",
  "1.2500",
  "5e-324",
  "0.0e400",
  "0.1234567890123456789",
  "1e-400",
])(
  "accepts finite floating input %s under the existing IEEE contract",
  async (number) => {
    const { writes } = serve();
    mount();
    await upload(
      inputText.replace(
        '"cooldown_until": null',
        `"cooldown_until": ${number}`,
      ),
    );
    await screen.findByLabelText("模拟角色");
    await run();
    expect(
      JSON.parse(String(writes[0].options.body)).capacity.accounts[0]
        .cooldown_until,
    ).toBe(Number(number));
  },
);
