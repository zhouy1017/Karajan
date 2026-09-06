import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import {
  ResourcePanel,
  type ResourcePolicy,
  type ResourcePool,
  type ResourceView,
} from "./ResourcePanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function policy(): ResourcePolicy {
  return {
    account_id: "共享账户 / 1",
    max_active_attempts: 4,
    max_attempt_duration_seconds: 45,
    observation_max_age_seconds: 30,
    require_official_observation: false,
    safety_margin: { weekly: "0.5" },
    lead_reserve: { weekly: "2", allowance: "1" },
    lead_reserved_slots: 1,
    conservative_mode: {
      enabled: true,
      max_local_active_attempts: 1,
      max_attempt_duration_seconds: 20,
      observation_max_age_seconds: 10,
      cooldown_seconds: 30,
    },
  };
}

function pool(id: string, changes: Partial<ResourcePool> = {}): ResourcePool {
  return {
    id,
    kind: "service",
    unit: "requests",
    window_kind: "fixed",
    window_id: "week-1",
    reported_remaining: "10.000000",
    reported_limit: "20.000000",
    local_uncovered: "1.000000",
    future_reserved: "2.000000",
    safety_margin: "0.500000",
    lead_reserve: "2.000000",
    available_for_worker: "4.500000",
    available_for_lead: "6.500000",
    source: "fixture",
    observed_at: 1788595200,
    received_at: 1788595201,
    reset_at: 1789199999,
    status: "observed",
    coverage_status: "uncertain",
    covered_usage_count: 0,
    ...changes,
  };
}

function view(saved = policy(), revision = 1): ResourceView {
  return {
    schema_version: "karajan.resources.view.v1",
    observed_at: 1788595202,
    accounts: [
      {
        id: saved.account_id,
        policy_revision: revision,
        policy: saved,
        active_attempts: 2,
        waiting_reconciliation: 1,
        pools: [
          pool("weekly"),
          pool("allowance", {
            kind: "platform_allowance",
            source: "local_ledger",
            coverage_status: "explicit_coverage",
            covered_usage_count: 2,
            lead_reserve: "1.000000",
          }),
        ],
      },
    ],
    live_qualification: "not_run",
    activation_allowed: false,
  };
}

async function edit() {
  await userEvent.click(
    await screen.findByRole("button", { name: "调整 共享账户 / 1 的保护量" }),
  );
  const field = screen.getByRole("textbox", {
    name: /weekly 的 Commander 保护量/,
  });
  await userEvent.clear(field);
  await userEvent.type(field, "3.25");
}

it("shows an empty account view without suggesting known quota or an editable default", async () => {
  vi.stubGlobal("fetch", async () =>
    Response.json({ ...view(), accounts: [] }),
  );
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await screen.findByRole("heading", { name: "还没有账户额度记录" });
  expect(screen.queryByRole("button", { name: "保存保护设置" })).toBeNull();
});

it("separates service quota and local allowance and withholds availability from stale or unknown reports", async () => {
  const data = view();
  data.accounts[0].pools.push(
    pool("stale", {
      status: "stale",
      available_for_worker: "777",
      available_for_lead: "888",
    }),
    pool("unknown", {
      status: "unknown",
      reported_remaining: null,
      observed_at: null,
      received_at: null,
      reset_at: null,
      available_for_worker: "999",
      available_for_lead: "1000",
      source: null,
    }),
  );
  vi.stubGlobal("fetch", async () => Response.json(data));
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  const service = await screen.findByRole("article", {
    name: "weekly 额度详情",
  });
  expect(within(service).getByText("4.5 次")).toBeTruthy();
  expect(within(service).getByText("本机样例")).toBeTruthy();
  expect(within(service).getByText(/尚未完全核对/)).toBeTruthy();
  const allowance = screen.getByRole("article", { name: "allowance 额度详情" });
  expect(within(allowance).getByText("平台分配额度")).toBeTruthy();
  expect(within(allowance).getByText("本地记录")).toBeTruthy();
  expect(
    within(allowance).getByText("报告已明确包含 2 条本地用量。"),
  ).toBeTruthy();
  expect(
    within(
      screen.getByRole("article", { name: "stale 额度详情" }),
    ).getAllByText("暂不可估算"),
  ).toHaveLength(2);
  expect(
    within(
      screen.getByRole("article", { name: "unknown 额度详情" }),
    ).getAllByText("暂不可估算"),
  ).toHaveLength(2);
  expect(screen.queryByText("777 次")).toBeNull();
  expect(screen.queryByText("999 次")).toBeNull();
});

it("saves only Commander reserve changes with the current revision and refreshes the quota view", async () => {
  const writes: { path: string; options: RequestInit }[] = [];
  const original = policy();
  let saved = original;
  let revision = 1;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      writes.push({ path, options });
      saved = JSON.parse(options.body as string).policy;
      revision = 2;
      return Response.json({ revision, policy: saved });
    }
    return Response.json(view(saved, revision));
  });
  render(<ResourcePanel csrf="csrf-fixture" onSessionExpired={vi.fn()} />);
  await edit();
  const slots = screen.getByRole("textbox", {
    name: "为主 Commander 保留的并发名额",
  });
  await userEvent.clear(slots);
  await userEvent.type(slots, "2");
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("保护设置已保存，后续分配会使用新设置。");
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe(
    `/v1/resources/policy?account_id=${encodeURIComponent(original.account_id)}`,
  );
  expect(new Headers(writes[0].options.headers).get("X-CSRF-Token")).toBe(
    "csrf-fixture",
  );
  expect(new Headers(writes[0].options.headers).get("If-Match")).toBe('"1"');
  expect(saved).toEqual({
    ...original,
    lead_reserved_slots: 2,
    lead_reserve: { ...original.lead_reserve, weekly: "3.25" },
  });
  expect(screen.queryByRole("button", { name: "保存保护设置" })).toBeNull();
});

it("reuses the command identity when the same settings are retried after a lost response", async () => {
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      writes.push(options);
      if (writes.length === 1) throw new TypeError("Disconnected");
      return Response.json({
        revision: 2,
        policy: JSON.parse(options.body as string).policy,
      });
    }
    return Response.json(view());
  });
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await edit();
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("尚未确认操作结果，可重试同一份设置。");
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("保护设置已保存，后续分配会使用新设置。");
  expect(writes).toHaveLength(2);
  expect(writes[0].body).toBe(writes[1].body);
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[1].headers).get("Idempotency-Key"),
  );
});

it("refreshes conflicting settings and waits for a new user submission instead of overwriting them", async () => {
  const writes: RequestInit[] = [];
  const current = {
    ...policy(),
    lead_reserved_slots: 2,
    lead_reserve: { weekly: "5", allowance: "4" },
    max_attempt_duration_seconds: 25,
  };
  vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      writes.push(options);
      if (writes.length === 1)
        return Response.json(
          { reason_code: "CAPACITY_POLICY_STALE" },
          { status: 409 },
        );
      return Response.json({
        revision: 3,
        policy: JSON.parse(options.body as string).policy,
      });
    }
    return Response.json(writes.length ? view(current, 2) : view());
  });
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await edit();
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("额度设置已变化，已载入当前值。请重新调整后保存。");
  expect(writes).toHaveLength(1);
  const reserve = screen.getByRole("textbox", {
    name: /weekly 的 Commander 保护量/,
  }) as HTMLInputElement;
  expect(reserve.value).toBe("5");
  await userEvent.clear(reserve);
  await userEvent.type(reserve, "6");
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("保护设置已保存，后续分配会使用新设置。");
  expect(new Headers(writes[1].headers).get("If-Match")).toBe('"2"');
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).not.toBe(
    new Headers(writes[1].headers).get("Idempotency-Key"),
  );
  expect(JSON.parse(writes[1].body as string).policy).toEqual({
    ...current,
    lead_reserve: { ...current.lead_reserve, weekly: "6" },
  });
});

it("prevents saving an old policy when reading the replacement after a conflict fails", async () => {
  let conflict = false;
  vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      conflict = true;
      return new Response("{}", { status: 409 });
    }
    if (conflict) throw new TypeError("Disconnected");
    return Response.json(view());
  });
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await edit();
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("无法连接本地工作台，请重试。");
  expect(screen.queryByRole("button", { name: "保存保护设置" })).toBeNull();
  expect(screen.queryByText("4.5 次")).toBeNull();
  expect(screen.getByRole("button", { name: "刷新额度" })).toBeTruthy();
});

it("shows account cooldown and exhaustion and renders resource names as plain text", async () => {
  const data = view();
  const name = '<img src=x onerror="alert(1)">';
  data.accounts[0].pools[0].id = name;
  data.accounts[0].blockers = [
    { reason_code: "ACCOUNT_COOLDOWN", until: 1788595230 },
    { reason_code: "EXHAUSTION_REQUIRES_NEW_OBSERVATION:weekly", until: null },
  ];
  vi.stubGlobal("fetch", async () => Response.json(data));
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await screen.findByText(/账户正在等待冷却/);
  expect(screen.getByText("额度已耗尽，等待新的有效报告。")).toBeTruthy();
  expect(screen.getByRole("heading", { name })).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

it("rejects negative protection and excess reserved slots before any write", async () => {
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
    if (options?.method === "POST") writes.push(options);
    return Response.json(view());
  });
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await edit();
  const reserve = screen.getByRole("textbox", {
    name: /weekly 的 Commander 保护量/,
  });
  await userEvent.clear(reserve);
  await userEvent.type(reserve, "-1");
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByRole("alert");
  await userEvent.clear(reserve);
  await userEvent.type(reserve, "2");
  const slots = screen.getByRole("textbox", {
    name: "为主 Commander 保留的并发名额",
  });
  await userEvent.clear(slots);
  await userEvent.type(slots, "5");
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  expect(writes).toHaveLength(0);
});

it.each(["read", "save"])(
  "notifies the parent when the session expires during %s",
  async (stage) => {
    const expired = vi.fn();
    vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
      if (stage === "read" || options?.method === "POST")
        return new Response("{}", { status: 401 });
      return Response.json(view());
    });
    render(<ResourcePanel csrf="csrf" onSessionExpired={expired} />);
    if (stage === "save") {
      await edit();
      await userEvent.click(
        screen.getByRole("button", { name: "保存保护设置" }),
      );
    }
    await screen.findByText("会话已过期，请重新登录。");
    expect(expired).toHaveBeenCalledOnce();
  },
);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

it.each(
  ["read", "refresh", "save"].flatMap((stage) =>
    ["401", "ok", "network"].map((outcome) => ({ stage, outcome })),
  ),
)(
  "ignores a late $outcome from the previous session's $stage operation",
  async ({ stage, outcome }) => {
    const old = deferred<Response>();
    const expired = vi.fn();
    let generation = "old";
    let firstRead = true;
    const next = view({ ...policy(), account_id: "新会话账户" });
    vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
      if (generation === "new") return Response.json(next);
      if (stage === "save" && options?.method === "POST") return old.promise;
      if (stage === "read" || (stage === "refresh" && !firstRead))
        return old.promise;
      firstRead = false;
      return Response.json(view());
    });
    const panel = render(
      <ResourcePanel csrf="old-csrf" onSessionExpired={expired} />,
    );
    if (stage === "refresh") {
      await screen.findByRole("heading", { name: "共享账户 / 1" });
      await userEvent.click(screen.getByRole("button", { name: "刷新额度" }));
    }
    if (stage === "save") {
      await edit();
      await userEvent.click(
        screen.getByRole("button", { name: "保存保护设置" }),
      );
    }
    generation = "new";
    panel.rerender(
      <ResourcePanel csrf="new-csrf" onSessionExpired={expired} />,
    );
    await screen.findByRole("heading", { name: "新会话账户" });
    await act(async () => {
      if (outcome === "network") old.reject(new TypeError("Disconnected"));
      else
        old.resolve(
          outcome === "401"
            ? new Response("{}", { status: 401 })
            : Response.json(
                stage === "save" ? { revision: 2, policy: policy() } : view(),
              ),
        );
    });
    expect(expired).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "新会话账户" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "共享账户 / 1" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      screen.queryByText("保护设置已保存，后续分配会使用新设置。"),
    ).toBeNull();
    expect(
      (screen.getByRole("button", { name: "刷新额度" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  },
);

it.each(["read", "save"])(
  "ignores the previous session's delayed %s response body",
  async (stage) => {
    let finish!: () => void;
    const response = new Response(
      new ReadableStream({
        start(controller) {
          finish = () => {
            controller.enqueue(
              new TextEncoder().encode(
                JSON.stringify(
                  stage === "save" ? { revision: 2, policy: policy() } : view(),
                ),
              ),
            );
            controller.close();
          };
        },
      }),
      { headers: { "Content-Type": "application/json" } },
    );
    let generation = "old";
    const expired = vi.fn();
    vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
      if (generation === "new")
        return Response.json(view({ ...policy(), account_id: "新会话账户" }));
      if (stage === "read" || options?.method === "POST") return response;
      return Response.json(view());
    });
    const panel = render(
      <ResourcePanel csrf="old" onSessionExpired={expired} />,
    );
    if (stage === "save") {
      await edit();
      await userEvent.click(
        screen.getByRole("button", { name: "保存保护设置" }),
      );
    }
    generation = "new";
    panel.rerender(<ResourcePanel csrf="new" onSessionExpired={expired} />);
    await screen.findByRole("heading", { name: "新会话账户" });
    await act(async () => {
      finish();
    });
    expect(expired).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "新会话账户" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      screen.queryByText("保护设置已保存，后续分配会使用新设置。"),
    ).toBeNull();
  },
);

it("does not expire a newly mounted panel when an unmounted panel receives 401", async () => {
  const old = deferred<Response>();
  let first = true;
  const expired = vi.fn();
  vi.stubGlobal("fetch", async () => {
    if (first) {
      first = false;
      return old.promise;
    }
    return Response.json(view());
  });
  const firstPanel = render(
    <ResourcePanel csrf="old" onSessionExpired={expired} />,
  );
  firstPanel.unmount();
  render(<ResourcePanel csrf="new" onSessionExpired={expired} />);
  await screen.findByRole("heading", { name: "共享账户 / 1" });
  await act(async () => {
    old.resolve(new Response("{}", { status: 401 }));
  });
  expect(expired).not.toHaveBeenCalled();
});

it.each(
  ["conflict", "saved"].flatMap((stage) =>
    ["401", "ok", "network"].map((outcome) => ({ stage, outcome })),
  ),
)(
  "ignores a late $outcome while reading the previous session's $stage policy back",
  async ({ stage, outcome }) => {
    const old = deferred<Response>();
    const expired = vi.fn();
    let generation = "old";
    let wrote = false;
    let readingBack = false;
    vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
      if (generation === "new")
        return Response.json(view({ ...policy(), account_id: "新会话账户" }));
      if (options?.method === "POST") {
        wrote = true;
        return stage === "conflict"
          ? new Response("{}", { status: 409 })
          : Response.json({ revision: 2, policy: policy() });
      }
      if (wrote) {
        readingBack = true;
        return old.promise;
      }
      return Response.json(view());
    });
    const panel = render(
      <ResourcePanel csrf="old" onSessionExpired={expired} />,
    );
    await edit();
    await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
    await waitFor(() => expect(readingBack).toBe(true));
    generation = "new";
    panel.rerender(<ResourcePanel csrf="new" onSessionExpired={expired} />);
    await screen.findByRole("heading", { name: "新会话账户" });
    await act(async () => {
      if (outcome === "network") old.reject(new TypeError("Disconnected"));
      else
        old.resolve(
          outcome === "401"
            ? new Response("{}", { status: 401 })
            : Response.json(view()),
        );
    });
    expect(expired).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "新会话账户" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "共享账户 / 1" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "保存保护设置" })).toBeNull();
  },
);

it("preserves a dot-segment account identity in the policy query", async () => {
  const writes: { url: URL; body: unknown }[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      writes.push({
        url: new URL(path, "http://127.0.0.1"),
        body: JSON.parse(options.body as string),
      });
      return Response.json({
        revision: 2,
        policy: { ...policy(), account_id: ".." },
      });
    }
    return Response.json(view({ ...policy(), account_id: ".." }));
  });
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "调整 .. 的保护量" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("保护设置已保存，后续分配会使用新设置。");
  expect(writes[0].url.pathname).toBe("/v1/resources/policy");
  expect(writes[0].url.searchParams.get("account_id")).toBe("..");
  expect(writes[0].body).toEqual({ policy: { ...policy(), account_id: ".." } });
});

it("shows reported limits and rejects protection above an exact decimal limit", async () => {
  const data = view();
  data.accounts[0].pools[0].reported_limit = "9007199254.740990";
  data.accounts[0].pools[1].reported_limit = null;
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (_path: string, options?: RequestInit) => {
    if (options?.method === "POST") writes.push(options);
    return Response.json(data);
  });
  render(<ResourcePanel csrf="csrf" onSessionExpired={vi.fn()} />);
  const known = await screen.findByRole("article", { name: "weekly 额度详情" });
  expect(within(known).getByText("已报告池上限")).toBeTruthy();
  expect(within(known).getByText("9007199254.74099 次")).toBeTruthy();
  expect(
    within(
      screen.getByRole("article", { name: "allowance 额度详情" }),
    ).getByText("上限未知"),
  ).toBeTruthy();
  await edit();
  const reserve = screen.getByRole("textbox", {
    name: /weekly 的 Commander 保护量/,
  });
  await userEvent.clear(reserve);
  await userEvent.type(reserve, "9007199254.740991");
  await userEvent.click(screen.getByRole("button", { name: "保存保护设置" }));
  await screen.findByText("weekly 的保护量不能超过已报告池上限。");
  expect(writes).toHaveLength(0);
});
