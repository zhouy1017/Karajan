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
import { RulebookPanel, type Rulebook } from "./RulebookPanel";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
});

function rulebook(): Rulebook {
  return {
    schema_version: "karajan.rulebook.v1",
    id: "personal",
    revision: 1,
    description: "当前规则",
    status: "draft",
    collaboration: {
      command_mode: "lead_with_advisers",
      plan_approval: "explicit_revision",
      implementation_readiness: "ready",
      delivery_target: "pull_request",
      merge_authority: "user",
      internal_delegation: "disabled_unless_admitted_and_observable",
      max_parallel_writers_per_project: 2,
      max_quality_repair_rounds: 2,
      max_infrastructure_retries_per_root_task: 2,
    },
    global_constraints: {
      require_enabled_profile: true,
      require_passed_capabilities: true,
      require_explicit_billing_path: true,
      require_approved_data_destination: true,
      autonomous_tool_execution_min_isolation: "tool_sandboxed",
      profile_fixed_per_attempt: true,
      no_silent_model_or_billing_fallback: true,
      review_context: "fresh_non_author",
      final_review_required: true,
    },
    profile_groups: { fast: [{ id: "small", revision: 1 }], strong: [] },
    resource_policy: {
      id: "protect-lead",
      candidate_order: [
        "preference_band",
        "uncertainty_band",
        "bottleneck_quota_pressure",
        "incremental_cash_estimate",
        "completion_time_estimate",
        "profile_id",
      ],
      queue_order: [
        "lead_feedback",
        "required_review_repair_and_critical_path",
        "worker",
        "optional_adviser",
      ],
      fairness: "aging_and_run_round_robin",
      reset_preference: "only_among_equivalent_candidates_all_windows_checked",
      account_capacity_policy_binding: "current_global_revision",
      unknown_quota: "require_explicit_conservative_mode",
      planning_budget_ref: null,
      run_budget_ref: null,
      missing_cash_budget: "deny_cash_route",
      cash_budget_enforcement: "bounded_calls",
      subscription_quota_enforcement: "conservative_estimate",
      call_reservations: "slices_of_attempt_reservation",
    },
    rules: [
      {
        id: "implement",
        priority: 100,
        when: {
          role: "worker",
          readiness: "ready",
          effective_class: "T1",
          domains_all: ["python"],
        },
        eligible_groups: ["fast"],
        capabilities_all: ["bounded_code_edit", "controlled_tools"],
        quality_escalation_groups: ["strong"],
        reroute: "within_approved_set_preserve_requirements",
        profile_preferences: [
          { profile: { id: "small", revision: 1 }, band: 1 },
        ],
      },
    ],
  };
}
function configuration(doc: Rulebook | null = rulebook(), revision = 4) {
  return {
    project_revision: revision,
    configuration_revision: revision - 1,
    configuration: {
      rulebook: doc,
      resources: {
        profiles: [
          { id: "small", revision: 1, enabled: true },
          { id: "large", revision: 2, enabled: false },
        ],
      },
    },
  };
}
function preview(canPublish = true) {
  return {
    preview_id: "preview-1",
    project_revision: 4,
    can_save_draft: true,
    can_publish: canPublish,
    issues: [],
    compile_issues: [],
    warnings: [{ code: "GROUP_EMPTY", path: "profile_groups.strong" }],
    waiting_reasons: ["LIVE_QUALIFICATION_NOT_RUN"],
    rulebook_sha256: "a".repeat(64),
  };
}
function publication() {
  return {
    publication_id: "publication-1",
    project_revision: 5,
    configuration_revision: 4,
    rulebook: { id: "personal", revision: 2, rulebook_sha256: "a".repeat(64) },
    state: "waiting_qualification",
    activation_allowed: false,
    at: 1788595200,
  };
}
type Handler = (
  path: string,
  options?: RequestInit,
) => Promise<Response> | Response;
function serve(handler?: Handler) {
  const writes: { path: string; options: RequestInit }[] = [];
  const mock = vi.fn(async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") writes.push({ path, options });
    if (handler) return handler(path, options);
    if (path.endsWith("/configuration")) return Response.json(configuration());
    if (path.endsWith("/preview")) return Response.json(preview());
    if (path.endsWith("/publish")) return Response.json(publication());
    return Response.json({ items: [] });
  });
  vi.stubGlobal("fetch", mock);
  return { writes, mock };
}
function normal(path: string) {
  if (path.endsWith("/configuration")) return Response.json(configuration());
  if (path.endsWith("/preview")) return Response.json(preview());
  if (path.endsWith("/publish")) return Response.json(publication());
  return Response.json({ items: [] });
}
const project = { id: "project-1", name: "交付仓库" };
function mount(csrf = "csrf-1") {
  const expired = vi.fn();
  return {
    expired,
    ...render(
      <RulebookPanel
        project={project}
        csrf={csrf}
        onSessionExpired={expired}
      />,
    ),
  };
}
async function ready() {
  await screen.findByLabelText("版本说明");
}
async function editVersion() {
  await ready();
  fireEvent.change(screen.getByLabelText("编辑版本号"), {
    target: { value: "2" },
  });
}
async function inspect() {
  await userEvent.click(screen.getByRole("button", { name: "预览规则变更" }));
  await screen.findByRole("region", { name: "发布预览" });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function upload(document: unknown) {
  const file = new File([JSON.stringify(document)], "rulebook.json", {
    type: "application/json",
  });
  Object.defineProperty(file, "text", {
    value: async () => JSON.stringify(document),
  });
  await userEvent.upload(screen.getByLabelText("导入完整规则文件"), file);
}

it("opens the project rulebook from App and expires its owner session", async () => {
  let expired = false;
  serve((path) => {
    if (path === "/v1/session") return Response.json({ csrf_token: "csrf-1" });
    if (path === "/v1/projects")
      return Response.json({
        items: [
          {
            ...project,
            revision: 4,
            repository: {
              root: "/fixture",
              base_ref: "main",
              base_sha: "a".repeat(40),
            },
            target_branch: "main",
            configuration: {
              status: "draft",
              revision: 3,
              dispatch_eligible: false,
            },
          },
        ],
      });
    return expired ? new Response(null, { status: 401 }) : normal(path);
  });
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: "调度规则" }),
  );
  await ready();
  expired = true;
  await userEvent.click(screen.getByRole("button", { name: "刷新当前版本" }));
  await screen.findByLabelText("本机访问码");
  expect(screen.queryByLabelText("版本说明")).toBeNull();
});

it("edits matrix groups, registered members, priority, limits and ordering while retaining advanced fields", async () => {
  const { writes } = serve();
  mount();
  await editVersion();
  fireEvent.change(screen.getByLabelText("implement 优先级"), {
    target: { value: "210" },
  });
  await userEvent.click(screen.getByLabelText("implement 候选组 strong"));
  await userEvent.click(screen.getByLabelText("implement 升级组 strong"));
  await userEvent.click(screen.getByLabelText("strong 成员 large v2"));
  await userEvent.click(
    screen.getByRole("button", { name: "上移信息确定程度" }),
  );
  fireEvent.change(screen.getByLabelText("每 Run 质量修复轮数上限"), {
    target: { value: "4" },
  });
  await inspect();
  const doc = JSON.parse(String(writes[0].options.body));
  expect(doc.rules[0]).toEqual({
    ...rulebook().rules[0],
    priority: 210,
    eligible_groups: ["fast", "strong"],
    quality_escalation_groups: [],
  });
  expect(doc.profile_groups.strong).toEqual([{ id: "large", revision: 2 }]);
  expect(doc.collaboration.max_quality_repair_rounds).toBe(4);
  expect(doc.global_constraints).toEqual(rulebook().global_constraints);
  expect(doc.resource_policy.candidate_order.slice(0, 2)).toEqual([
    "uncertainty_band",
    "preference_band",
  ]);
  expect(
    screen
      .getByRole("button", { name: "上移配置标识（稳定顺序）" })
      .hasAttribute("disabled"),
  ).toBe(true);
  const headers = new Headers(writes[0].options.headers);
  expect(headers.get("If-Match")).toBe('"4"');
  expect(headers.get("X-CSRF-Token")).toBe("csrf-1");
  expect(headers.get("Idempotency-Key")).toBeTruthy();
  expect(screen.getByText("规则.revision")).toBeTruthy();
  expect(screen.getByText(/尚未验证真实执行资格/)).toBeTruthy();
});

it("publishes empty groups as waiting configuration and displays retained version history", async () => {
  let published = false;
  const { writes } = serve((path) => {
    if (path.endsWith("/publish")) {
      published = true;
      return Response.json(publication());
    }
    if (path.endsWith("/versions"))
      return Response.json({
        items: published ? [publication().rulebook] : [],
      });
    if (path.endsWith("/publications"))
      return Response.json({ items: published ? [publication()] : [] });
    if (path.endsWith("/configuration") && published)
      return Response.json(configuration({ ...rulebook(), revision: 2 }, 5));
    return normal(path);
  });
  mount();
  await editVersion();
  await inspect();
  expect(screen.getByText(/尚无成员 · 等待配置/)).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  await screen.findByText(/规则版本已发布，等待执行资格验证/);
  await screen.findByText(/项目修订 5/);
  expect(writes.map((item) => item.path)).toEqual([
    "/v1/projects/project-1/rulebook/preview",
    "/v1/projects/project-1/rulebook/publish",
  ]);
  expect(JSON.parse(String(writes[1].options.body))).toEqual({
    preview_id: "preview-1",
  });
  await userEvent.click(screen.getByText("版本与发布历史（1 个版本）"));
  expect(screen.getByText(/personal · v2 ·.*等待执行资格验证/)).toBeTruthy();
});

it("invalidates a preview after any form change", async () => {
  const { writes } = serve();
  mount();
  await ready();
  await inspect();
  fireEvent.change(screen.getByLabelText("版本说明"), {
    target: { value: "修改后说明" },
  });
  expect(screen.queryByRole("button", { name: "确认发布此版本" })).toBeNull();
  await inspect();
  expect(
    new Headers(writes[1].options.headers).get("Idempotency-Key"),
  ).not.toBe(new Headers(writes[0].options.headers).get("Idempotency-Key"));
});

it("shows compiler problems and does not publish an invalid rulebook", async () => {
  const { writes } = serve((path) =>
    path.endsWith("/preview")
      ? Response.json({
          ...preview(false),
          compile_issues: [{ code: "RULE_AMBIGUOUS", path: "rules" }],
        })
      : normal(path),
  );
  mount();
  await ready();
  await inspect();
  expect(screen.getByText("RULE_AMBIGUOUS · rules")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  expect(writes).toHaveLength(1);
});

it.each(["network", "server", "json", "incomplete"])(
  "retries the identical publication after an unknown %s outcome",
  async (failure) => {
    let calls = 0;
    const { writes } = serve((path) => {
      if (path.endsWith("/publish") && calls++ === 0) {
        if (failure === "network") throw new TypeError("offline");
        if (failure === "server")
          return new Response("unavailable", { status: 503 });
        if (failure === "json") return new Response("{");
        return Response.json({});
      }
      return normal(path);
    });
    mount();
    await ready();
    await inspect();
    await userEvent.click(
      screen.getByRole("button", { name: "确认发布此版本" }),
    );
    await screen.findByRole("button", { name: "重试原发布请求" });
    expect(
      screen.getByLabelText("版本说明").closest("fieldset")?.disabled,
    ).toBe(true);
    expect(
      screen
        .getByRole("button", { name: "刷新当前版本" })
        .hasAttribute("disabled"),
    ).toBe(true);
    await userEvent.click(
      screen.getByRole("button", { name: "重试原发布请求" }),
    );
    await screen.findByText(/规则版本已发布/);
    expect(writes[1].options).toEqual(writes[2].options);
  },
);

it.each(["preview", "publish"])(
  "refreshes authority after a %s conflict and requires a new preview",
  async (stage) => {
    let conflict = false;
    const { writes } = serve((path) => {
      if (path.endsWith(`/${stage}`) && !conflict) {
        conflict = true;
        return new Response(null, { status: 409 });
      }
      if (path.endsWith("/configuration"))
        return Response.json(configuration(rulebook(), conflict ? 8 : 4));
      return normal(path);
    });
    mount();
    await editVersion();
    if (stage === "publish") {
      await inspect();
      await userEvent.click(
        screen.getByRole("button", { name: "确认发布此版本" }),
      );
    } else
      await userEvent.click(
        screen.getByRole("button", { name: "预览规则变更" }),
      );
    await screen.findByText(/项目或预览版本已变化/);
    expect(screen.queryByRole("button", { name: "确认发布此版本" })).toBeNull();
    expect(
      (screen.getByLabelText("编辑版本号") as HTMLInputElement).value,
    ).toBe("2");
    await inspect();
    const command = writes.at(-1)!;
    expect(command.path.endsWith("/preview")).toBe(true);
    expect(new Headers(command.options.headers).get("If-Match")).toBe('"8"');
    expect(
      new Headers(command.options.headers).get("Idempotency-Key"),
    ).not.toBe(new Headers(writes[0].options.headers).get("Idempotency-Key"));
  },
);

it("keeps publication unavailable if the conflict refresh fails", async () => {
  let conflict = false;
  serve((path) => {
    if (path.endsWith("/publish")) {
      conflict = true;
      return new Response(null, { status: 409 });
    }
    if (conflict && path.endsWith("/configuration"))
      throw new TypeError("offline");
    return normal(path);
  });
  mount();
  await ready();
  await inspect();
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "确认发布此版本" })).toBeNull();
  expect(screen.queryByRole("button", { name: "重试原发布请求" })).toBeNull();
  expect(screen.getByLabelText("版本说明").closest("fieldset")?.disabled).toBe(
    true,
  );
});

it("imports advanced conditions and configurable limits while invalidating the previous preview", async () => {
  const { writes } = serve();
  mount();
  await ready();
  await inspect();
  const doc = rulebook();
  doc.revision = 2;
  doc.rules[0].when.domains_all = ["typescript"];
  doc.collaboration.max_parallel_writers_per_project = 3;
  await upload(doc);
  await screen.findByText(/文件已载入表单/);
  expect(screen.queryByRole("button", { name: "确认发布此版本" })).toBeNull();
  await inspect();
  expect(JSON.parse(String(writes.at(-1)!.options.body))).toEqual(doc);
});

it.each(["global", "collaboration"])(
  "rejects imported changes to fixed %s rules without replacing the editor",
  async (field) => {
    serve();
    mount();
    await ready();
    const doc = rulebook();
    doc.description = "不应载入";
    if (field === "global")
      doc.global_constraints.final_review_required = false;
    else doc.collaboration.merge_authority = "agent";
    await upload(doc);
    await screen.findByText(/导入不能改变已确认/);
    expect(
      (screen.getByLabelText("版本说明") as HTMLTextAreaElement).value,
    ).toBe("当前规则");
  },
);

it("imports into an unconfigured project and rejects non Rulebook documents", async () => {
  serve((path) =>
    path.endsWith("/configuration")
      ? Response.json(configuration(null))
      : normal(path),
  );
  mount();
  await screen.findByText(/还没有可编辑的规则/);
  await upload({ something: true });
  await screen.findByText(/文件需要包含完整/);
  await upload(rulebook());
  await ready();
});

it("exports the complete edited document", async () => {
  const blobs: Blob[] = [];
  vi.stubGlobal("URL", {
    createObjectURL: (blob: Blob) => {
      blobs.push(blob);
      return "blob:fixture";
    },
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  serve();
  mount();
  await editVersion();
  await userEvent.click(screen.getByRole("button", { name: "导出当前编辑" }));
  expect(blobs).toHaveLength(1);
  const text = await new Promise<string>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.readAsText(blobs[0]);
  });
  expect(JSON.parse(text)).toEqual({ ...rulebook(), revision: 2 });
});

it.each(["csrf", "project", "unmount"])(
  "ignores a delayed import after %s changes",
  async (change) => {
    serve();
    const mounted = mount();
    await ready();
    const contents = deferred<string>();
    const file = new File([""], "rules.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: () => contents.promise });
    await userEvent.upload(screen.getByLabelText("导入完整规则文件"), file);
    if (change === "unmount") mounted.unmount();
    else
      mounted.rerender(
        <RulebookPanel
          project={
            change === "project" ? { ...project, id: "project-2" } : project
          }
          csrf={change === "csrf" ? "csrf-2" : "csrf-1"}
          onSessionExpired={mounted.expired}
        />,
      );
    await act(async () =>
      contents.resolve(
        JSON.stringify({ ...rulebook(), description: "旧文件" }),
      ),
    );
    if (change !== "unmount") {
      await ready();
      expect(
        (screen.getByLabelText("版本说明") as HTMLTextAreaElement).value,
      ).toBe("当前规则");
    }
    expect(screen.queryByDisplayValue("旧文件")).toBeNull();
  },
);

it.each(["read", "refresh", "preview", "publish"])(
  "ignores late %s headers, including 401, after scope changes",
  async (stage) => {
    for (const change of ["csrf", "project", "unmount"]) {
      for (const outcome of ["success", "401", "network"]) {
        sessionStorage.clear();
        const delayed = deferred<Response>();
        let hold = stage === "read";
        const pathToHold =
          stage === "read" || stage === "refresh"
            ? "/configuration"
            : `/${stage}`;
        const { mock } = serve((path) => {
          if (hold && path.endsWith(pathToHold)) {
            hold = false;
            return delayed.promise;
          }
          return normal(path);
        });
        const mounted = mount();
        if (stage === "read")
          await waitFor(() =>
            expect(mock).toHaveBeenCalledWith(
              expect.stringContaining(pathToHold),
            ),
          );
        if (stage !== "read") {
          await ready();
          if (stage === "publish") await inspect();
          hold = true;
          await userEvent.click(
            screen.getByRole("button", {
              name:
                stage === "refresh"
                  ? "刷新当前版本"
                  : stage === "preview"
                    ? "预览规则变更"
                    : "确认发布此版本",
            }),
          );
        }
        if (change === "unmount") mounted.unmount();
        else
          mounted.rerender(
            <RulebookPanel
              project={
                change === "project" ? { ...project, id: "project-2" } : project
              }
              csrf={change === "csrf" ? "csrf-2" : "csrf-1"}
              onSessionExpired={mounted.expired}
            />,
          );
        await act(async () => {
          if (outcome === "network")
            delayed.reject(new TypeError("old failure"));
          else
            delayed.resolve(
              outcome === "401"
                ? new Response(null, { status: 401 })
                : normal(pathToHold),
            );
        });
        if (change !== "unmount") {
          await ready();
          expect(screen.queryByRole("region", { name: "发布预览" })).toBeNull();
          expect(screen.queryByRole("alert")).toBeNull();
        }
        expect(mounted.expired).not.toHaveBeenCalled();
        mounted.unmount();
      }
    }
  },
);

it.each(["read", "refresh", "preview", "publish"])(
  "rechecks scope after delayed %s JSON parsing",
  async (stage) => {
    const delayed = deferred<unknown>();
    let hold = stage === "read";
    const pathToHold =
      stage === "read" || stage === "refresh" ? "/configuration" : `/${stage}`;
    const { mock } = serve((path) => {
      if (hold && path.endsWith(pathToHold)) {
        hold = false;
        const response = Response.json({});
        vi.spyOn(response, "json").mockImplementation(() => delayed.promise);
        return response;
      }
      return normal(path);
    });
    const mounted = mount();
    if (stage === "read")
      await waitFor(() =>
        expect(mock).toHaveBeenCalledWith(expect.stringContaining(pathToHold)),
      );
    if (stage !== "read") {
      await ready();
      if (stage === "publish") await inspect();
      hold = true;
      await userEvent.click(
        screen.getByRole("button", {
          name:
            stage === "refresh"
              ? "刷新当前版本"
              : stage === "preview"
                ? "预览规则变更"
                : "确认发布此版本",
        }),
      );
    }
    await act(async () => {});
    mounted.rerender(
      <RulebookPanel
        project={{ ...project, id: "project-2" }}
        csrf="csrf-2"
        onSessionExpired={mounted.expired}
      />,
    );
    await ready();
    await act(async () =>
      delayed.resolve(
        stage === "publish"
          ? publication()
          : stage === "preview"
            ? preview()
            : configuration({ ...rulebook(), description: "旧请求" }),
      ),
    );
    expect(screen.queryByDisplayValue("旧请求")).toBeNull();
    expect(screen.queryByRole("region", { name: "发布预览" })).toBeNull();
    expect(screen.queryByText(/规则版本已发布/)).toBeNull();
    expect(mounted.expired).not.toHaveBeenCalled();
  },
);

it("prevents duplicate publication while its first response is pending", async () => {
  const delayed = deferred<Response>();
  const { writes } = serve((path) =>
    path.endsWith("/publish") ? delayed.promise : normal(path),
  );
  mount();
  await ready();
  await inspect();
  const button = screen.getByRole("button", { name: "确认发布此版本" });
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(() =>
    expect(
      writes.filter((item) => item.path.endsWith("/publish")),
    ).toHaveLength(1),
  );
  await act(async () => delayed.resolve(Response.json(publication())));
});

it("restores an unresolved draft and exact command after reopening with a newer server configuration", async () => {
  let newer = false;
  let accept = false;
  const { writes } = serve((path) => {
    if (path.endsWith("/publish") && !accept) throw new TypeError("lost");
    if (path.endsWith("/configuration") && newer)
      return Response.json(
        configuration(
          { ...rulebook(), revision: 9, description: "新服务器版本" },
          12,
        ),
      );
    return normal(path);
  });
  const first = mount();
  await editVersion();
  fireEvent.change(screen.getByLabelText("版本说明"), {
    target: { value: "未决编辑内容" },
  });
  await inspect();
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  await screen.findByRole("button", { name: "重试原发布请求" });
  const firstCommand = writes[1].options;
  expect(sessionStorage.length).toBe(1);
  expect(sessionStorage.key(0)).not.toContain("csrf-1");
  first.unmount();
  newer = true;
  mount();
  await screen.findByText(/项目修订 12/);
  expect((screen.getByLabelText("版本说明") as HTMLTextAreaElement).value).toBe(
    "未决编辑内容",
  );
  expect((screen.getByLabelText("编辑版本号") as HTMLInputElement).value).toBe(
    "2",
  );
  expect(writes).toHaveLength(2);
  accept = true;
  await userEvent.click(screen.getByRole("button", { name: "重试原发布请求" }));
  await screen.findByText(/规则版本已发布/);
  expect(writes[2].options).toEqual(firstCommand);
  expect(sessionStorage.length).toBe(0);
});

it.each(["project", "csrf"])(
  "isolates an unresolved record from a different %s and restores only when returning to its scope",
  async (change) => {
    const { writes } = serve((path) => {
      if (path.endsWith("/publish")) throw new TypeError("lost");
      return normal(path);
    });
    const mounted = mount();
    await editVersion();
    await inspect();
    await userEvent.click(
      screen.getByRole("button", { name: "确认发布此版本" }),
    );
    await screen.findByRole("button", { name: "重试原发布请求" });
    mounted.rerender(
      <RulebookPanel
        project={
          change === "project" ? { ...project, id: "other-project" } : project
        }
        csrf={change === "csrf" ? "other-session" : "csrf-1"}
        onSessionExpired={mounted.expired}
      />,
    );
    await ready();
    expect(screen.queryByRole("button", { name: "重试原发布请求" })).toBeNull();
    expect(
      (screen.getByLabelText("编辑版本号") as HTMLInputElement).value,
    ).toBe("1");
    mounted.rerender(
      <RulebookPanel
        project={project}
        csrf="csrf-1"
        onSessionExpired={mounted.expired}
      />,
    );
    await screen.findByRole("button", { name: "重试原发布请求" });
    expect(
      (screen.getByLabelText("编辑版本号") as HTMLInputElement).value,
    ).toBe("2");
    expect(writes).toHaveLength(2);
  },
);

it("does not send a publication until the full pending identity is durably recorded", async () => {
  const { writes } = serve();
  mount();
  await ready();
  await inspect();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  await screen.findByText(/无法保存本标签页的发布身份/);
  expect(writes.filter((item) => item.path.endsWith("/publish"))).toHaveLength(
    0,
  );
});

it("blocks malformed pending records without overwriting or sending them", async () => {
  serve((path) => {
    if (path.endsWith("/publish")) throw new TypeError("lost");
    return normal(path);
  });
  const first = mount();
  await ready();
  await inspect();
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  await screen.findByRole("button", { name: "重试原发布请求" });
  const key = sessionStorage.key(0)!;
  first.unmount();
  sessionStorage.setItem(key, "{broken");
  const { writes } = serve();
  mount();
  await ready();
  await screen.findByText(/无法读取本标签页的待核对记录/);
  expect(screen.getByLabelText("版本说明").closest("fieldset")?.disabled).toBe(
    true,
  );
  expect(writes).toHaveLength(0);
  expect(sessionStorage.getItem(key)).toBe("{broken");
});

it("does not let an unmounted successful response erase a reopened pending record", async () => {
  const delayed = deferred<Response>();
  let wait = true;
  const { writes } = serve((path) =>
    path.endsWith("/publish") && wait ? delayed.promise : normal(path),
  );
  const first = mount();
  await ready();
  await inspect();
  await userEvent.click(screen.getByRole("button", { name: "确认发布此版本" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  first.unmount();
  mount();
  await screen.findByRole("button", { name: "重试原发布请求" });
  const stored = sessionStorage.getItem(sessionStorage.key(0)!);
  await act(async () => delayed.resolve(Response.json(publication())));
  expect(sessionStorage.getItem(sessionStorage.key(0)!)).toBe(stored);
  expect(screen.queryByText(/规则版本已发布/)).toBeNull();
  wait = false;
  await waitFor(() =>
    expect(
      screen
        .getByRole("button", { name: "重试原发布请求" })
        .hasAttribute("disabled"),
    ).toBe(false),
  );
  await userEvent.click(screen.getByRole("button", { name: "重试原发布请求" }));
  await screen.findByText(/规则版本已发布/);
  expect(writes[2].options).toEqual(writes[1].options);
});

it.each(["资源与配额", "检查配置", "需求与计划", "另一项目"])(
  "retains the original publication across App navigation to %s",
  async (destination) => {
    let accepted = false;
    const projects = [project, { id: "project-2", name: "第二仓库" }].map(
      (item) => ({
        ...item,
        revision: 4,
        repository: {
          root: "/fixture",
          base_ref: "main",
          base_sha: "a".repeat(40),
        },
        target_branch: "main",
        configuration: {
          status: "draft",
          revision: 3,
          dispatch_eligible: false,
        },
      }),
    );
    const { writes } = serve((path) => {
      if (path === "/v1/session")
        return Response.json({ csrf_token: "csrf-1" });
      if (path === "/v1/projects") return Response.json({ items: projects });
      if (path === "/v1/resources") return Response.json({ accounts: [] });
      if (path.endsWith("/publish") && !accepted) throw new TypeError("lost");
      return normal(path);
    });
    render(<App />);
    await screen.findByRole("heading", { name: "交付仓库" });
    const card = screen
      .getByRole("heading", { name: "交付仓库" })
      .closest("article")!;
    await userEvent.click(
      within(card).getByRole("button", { name: "调度规则" }),
    );
    await editVersion();
    fireEvent.change(screen.getByLabelText("版本说明"), {
      target: { value: "保留的原编辑" },
    });
    await inspect();
    await userEvent.click(
      screen.getByRole("button", { name: "确认发布此版本" }),
    );
    await screen.findByRole("button", { name: "重试原发布请求" });
    if (destination === "资源与配额") {
      await userEvent.click(screen.getByRole("button", { name: destination }));
      await screen.findByRole("heading", { name: "还没有账户额度记录" });
      await userEvent.click(screen.getByRole("button", { name: "项目工作台" }));
    } else if (destination === "另一项目") {
      const other = screen
        .getByRole("heading", { name: "第二仓库" })
        .closest("article")!;
      await userEvent.click(
        within(other).getByRole("button", { name: "调度规则" }),
      );
      await screen.findByRole("heading", { name: "第二仓库 · 调度规则" });
      await ready();
      await userEvent.click(
        within(card).getByRole("button", { name: "调度规则" }),
      );
    } else {
      await userEvent.click(
        within(card).getByRole("button", { name: destination }),
      );
      await waitFor(() =>
        expect(
          screen.queryByRole("heading", { name: "交付仓库 · 调度规则" }),
        ).toBeNull(),
      );
      await userEvent.click(
        within(card).getByRole("button", { name: "调度规则" }),
      );
    }
    await screen.findByRole("button", { name: "重试原发布请求" });
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "重试原发布请求" })
          .hasAttribute("disabled"),
      ).toBe(false),
    );
    expect(
      (screen.getByLabelText("版本说明") as HTMLTextAreaElement).value,
    ).toBe("保留的原编辑");
    accepted = true;
    await userEvent.click(
      screen.getByRole("button", { name: "重试原发布请求" }),
    );
    await screen.findByText(/规则版本已发布/);
    const attempts = writes.filter((write) => write.path.endsWith("/publish"));
    expect(attempts).toHaveLength(2);
    expect(attempts[1].options).toEqual(attempts[0].options);
  },
);

it("compares rules by identity and displays only a changed priority field", async () => {
  serve((path) =>
    path.endsWith("/preview")
      ? Response.json({
          ...preview(),
          issues: [{ code: "PROFILE_GROUP_EMPTY", path: "strong" }],
        })
      : normal(path),
  );
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("implement 优先级"), {
    target: { value: "101" },
  });
  await inspect();
  const area = screen.getByRole("region", { name: "发布预览" });
  const row = within(area)
    .getByText("规则.rules.implement.优先级")
    .closest("li")!;
  expect(within(row).getByText("100")).toBeTruthy();
  expect(within(row).getByText("101")).toBeTruthy();
  expect(within(area).queryByText("规则.rules")).toBeNull();
  expect(within(area).queryByText(/bounded_code_edit/)).toBeNull();
  expect(within(area).getByText("能力组尚无成员 · strong")).toBeTruthy();
});

it("reports rule additions, removals and ordering explicitly", async () => {
  serve();
  mount();
  await ready();
  const document = rulebook();
  document.rules = [{ ...document.rules[0], id: "replacement" }];
  await upload(document);
  await inspect();
  const area = screen.getByRole("region", { name: "发布预览" });
  expect(within(area).getByText("规则.rules.implement")).toBeTruthy();
  expect(within(area).getByText("规则.rules.replacement")).toBeTruthy();
  expect(
    within(area).getByText("规则.rules.规则顺序（含新增或移除）"),
  ).toBeTruthy();
});

it("displays escalation stage order and preserves it through a form round trip", async () => {
  const document = rulebook();
  document.rules[0].quality_escalation_groups = ["strong", "fast"];
  const { writes } = serve((path) =>
    path.endsWith("/configuration")
      ? Response.json(configuration(document))
      : normal(path),
  );
  mount();
  await ready();
  expect(
    screen.getByLabelText("implement 升级组 strong").parentElement?.textContent,
  ).toBe("strong · 第 1 阶段");
  expect(
    screen.getByLabelText("implement 升级组 fast").parentElement?.textContent,
  ).toBe("fast · 第 2 阶段");
  fireEvent.change(screen.getByLabelText("版本说明"), {
    target: { value: "保留阶段顺序" },
  });
  await inspect();
  expect(
    JSON.parse(String(writes[0].options.body)).rules[0]
      .quality_escalation_groups,
  ).toEqual(["strong", "fast"]);
});
