import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ProjectRuns } from "../../../frontend/src/ProjectRuns";
import original from "./valid-view.json";
import quantity from "./quantity-view.json";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function fixture() {
  return structuredClone(original);
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
function latest(run = original.run) {
  return run.plans.at(-1)!;
}
function second() {
  const value = fixture();
  value.project.id = "different-project";
  value.project.name = "Second project";
  value.run.id = "different-run";
  value.run.project_id = value.project.id;
  value.run.requirement.goal = "Different goal";
  value.run.execution_policy_snapshot.project_id = value.project.id;
  latest(value.run).routing_binding.execution_policy.project_id =
    value.project.id;
  latest(value.run).routing_digest = "d".repeat(64);
  latest(value.run).plan_digest = "e".repeat(64);
  return value;
}
async function choose(run = original.run) {
  await userEvent.click(
    await screen.findByRole("button", { name: run.requirement.goal }),
  );
  await screen.findByRole("heading", { name: "完整执行授权（v2）" });
}
async function confirm() {
  await userEvent.click(screen.getByRole("checkbox", { name: /我已审阅/ }));
  await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
}
function show(value = fixture()) {
  return render(
    <ProjectRuns project={value.project} csrf="independent-csrf" />,
  );
}

it("permits reviewing a genuine persisted decimal-string zero written as 0e0", async () => {
  const value = structuredClone(quantity);
  expect(value.run.plans.at(-1)!.plan.authorization.currency_limits.USD).toBe(
    "0e0",
  );
  vi.stubGlobal("fetch", async (url: string) =>
    Response.json(url.includes("?") ? { items: [value.run] } : value.run),
  );
  render(<ProjectRuns project={value.project} csrf="independent-csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: value.run.requirement.goal }),
  );
  await screen.findByRole("heading", { name: "完整执行授权（v2）" });
  expect(screen.getByText(/原币限额：.*USD 0e0/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "确认 v2 授权" })).toBeTruthy();
});

it("shows plan limits separately from the larger original ceiling and keeps ordered stage identities", async () => {
  const value = fixture();
  vi.stubGlobal("fetch", async (url: string) =>
    Response.json(url.includes("?") ? { items: [value.run] } : value.run),
  );
  show(value);
  await choose(value.run);
  const scope = screen.getByRole("region", { name: "完整执行授权（v2）" });
  expect(within(scope).getByText(/单次尝试最多 25 秒/)).toBeTruthy();
  expect(within(scope).getByText(/上方原币限额是本次批准的范围/)).toBeTruthy();
  expect(
    within(scope).getByText(/质量升级第 1 组（序号 0）/).textContent,
  ).toContain("critical_qualified");
  expect(
    within(scope).getByText("mechanical-worker").closest("li")?.textContent,
  ).toContain("普通阶段：不允许");
  expect(
    within(scope).getAllByText(/输入上界 4096 token；任务时长 20 秒/),
  ).toHaveLength(2);
  expect(within(scope).getByText(/实际输入尚需执行前测量/)).toBeTruthy();
  expect(within(scope).getByText(/尚未验证执行器资格/)).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "确认 v2 授权" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

it("sends exactly the reviewed v2 identity and does not copy displayed authorization into the command", async () => {
  const value = fixture();
  const posts: RequestInit[] = [];
  let approved = false;
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(init);
      approved = true;
      return Response.json({ accepted: true });
    }
    return Response.json(
      url.includes("?")
        ? { items: [value.run] }
        : {
            ...value.run,
            active_plan_revision: approved
              ? latest(value.run).plan_revision
              : null,
          },
    );
  });
  show(value);
  await choose(value.run);
  await confirm();
  await screen.findByText(/v2 授权已确认/);
  const plan = latest(value.run);
  expect(JSON.parse(posts[0].body as string)).toEqual({
    schema_version: "karajan.approve-plan.v2",
    routing_digest: plan.routing_digest,
    term: plan.term,
    plan_revision: plan.plan_revision,
    plan_digest: plan.plan_digest,
    authorization_digest: plan.authorization_digest,
    configuration_digest: plan.configuration_digest,
  });
  expect((posts[0].headers as Record<string, string>)["X-CSRF-Token"]).toBe(
    "independent-csrf",
  );
  expect(screen.queryByRole("checkbox", { name: /我已审阅/ })).toBeNull();
});

it("a conflict followed by a failed read revokes approval until the plan is freshly reviewed", async () => {
  const value = fixture();
  const posts: RequestInit[] = [];
  let readFail = false;
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(init);
      readFail = true;
      return Response.json(
        { reason_code: "APPROVAL_BINDING_MISMATCH" },
        { status: 409 },
      );
    }
    if (url.includes("?")) return Response.json({ items: [value.run] });
    return readFail
      ? new Response(null, { status: 503 })
      : Response.json(value.run);
  });
  show(value);
  await choose(value.run);
  await confirm();
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
  readFail = false;
  await choose(value.run);
  expect(
    (screen.getByRole("checkbox", { name: /我已审阅/ }) as HTMLInputElement)
      .checked,
  ).toBe(false);
  await confirm();
  expect(
    (posts[0].headers as Record<string, string>)["Idempotency-Key"],
  ).not.toBe((posts[1].headers as Record<string, string>)["Idempotency-Key"]);
});

it("an uncertain retry preserves the exact body and idempotency key", async () => {
  const value = fixture();
  const posts: RequestInit[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(init);
      throw new TypeError("synthetic lost response");
    }
    return Response.json(
      url.includes("?") ? { items: [value.run] } : value.run,
    );
  });
  show(value);
  await choose(value.run);
  await confirm();
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "确认 v2 授权" }));
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
});

it("late detail from an old project cannot overwrite the newly reviewed project", async () => {
  const first = fixture(),
    next = second(),
    delayed = deferred<Response>();
  vi.stubGlobal("fetch", async (url: string) => {
    if (url.includes("?"))
      return Response.json({
        items: [url.includes(next.project.id) ? next.run : first.run],
      });
    if (url.endsWith(first.run.id)) return delayed.promise;
    return Response.json(next.run);
  });
  const view = show(first);
  await userEvent.click(
    await screen.findByRole("button", { name: first.run.requirement.goal }),
  );
  view.rerender(<ProjectRuns project={next.project} csrf="independent-csrf" />);
  await choose(next.run);
  await act(async () => {
    delayed.resolve(Response.json(first.run));
  });
  expect(
    screen.queryByRole("heading", { name: first.run.requirement.goal }),
  ).toBeNull();
  expect(
    screen.getByRole("heading", { name: next.run.requirement.goal }),
  ).toBeTruthy();
});

it("late failed approval from an old login view cannot create an error in the replacement view", async () => {
  const value = fixture(),
    delayed = deferred<Response>();
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return delayed.promise;
    return Response.json(
      url.includes("?") ? { items: [value.run] } : value.run,
    );
  });
  const view = show(value);
  await choose(value.run);
  await confirm();
  view.rerender(
    <ProjectRuns project={value.project} csrf="replacement-login-csrf" />,
  );
  await choose(value.run);
  await act(async () => {
    delayed.reject(new TypeError("old lost response"));
  });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(
    (screen.getByRole("checkbox", { name: /我已审阅/ }) as HTMLInputElement)
      .checked,
  ).toBe(false);
});

it("switching Run and returning never retains the earlier review checkbox", async () => {
  const first = fixture(),
    next = fixture();
  next.run.id = "second-run-same-project";
  next.run.requirement.goal = "Second Run";
  vi.stubGlobal("fetch", async (url: string) =>
    Response.json(
      url.includes("?")
        ? { items: [first.run, next.run] }
        : url.endsWith(next.run.id)
          ? next.run
          : first.run,
    ),
  );
  show(first);
  await choose(first.run);
  await userEvent.click(screen.getByRole("checkbox", { name: /我已审阅/ }));
  await choose(next.run);
  await choose(first.run);
  expect(
    (screen.getByRole("checkbox", { name: /我已审阅/ }) as HTMLInputElement)
      .checked,
  ).toBe(false);
});

it.each(["missing-routing-digest", "unknown-version", "foreign-policy-owner"])(
  "incomplete authority is non-actionable: %s",
  async (variant) => {
    const value = fixture();
    if (variant === "missing-routing-digest")
      latest(value.run).routing_digest = "";
    if (variant === "unknown-version")
      value.run.schema_version = "karajan.run-planning.v9";
    if (variant === "foreign-policy-owner")
      value.run.execution_policy_snapshot.registered_by = "other-owner";
    vi.stubGlobal("fetch", async (url: string) =>
      Response.json(url.includes("?") ? { items: [value.run] } : value.run),
    );
    show(value);
    await userEvent.click(
      await screen.findByRole("button", { name: value.run.requirement.goal }),
    );
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "确认 v2 授权" })).toBeNull();
    expect(screen.queryByRole("button", { name: "确认这份计划" })).toBeNull();
  },
);
