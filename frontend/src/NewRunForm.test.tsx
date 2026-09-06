import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { NewRunForm } from "./NewRunForm";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

const project = {
  id: "project-1",
  name: "Sample",
  revision: 2,
  target_branch: "main",
  configuration: { status: "offline_valid", digest: "c".repeat(64) },
};
function configured(revision = 2) {
  return Response.json({
    project_revision: revision,
    configuration: {
      approved_profile_refs: [{ id: "lead-profile", revision: 1 }],
      rulebook: {
        profile_groups: {
          commander_qualified: [{ id: "lead-profile", revision: 1 }],
        },
        resource_policy: { run_budget_ref: "run" },
      },
      resources: {
        budgets: [
          {
            id: "run",
            currency_limits: { USD: "0" },
            max_total_attempts: 8,
            max_duration_seconds: 300,
          },
        ],
      },
    },
  });
}
async function fillRequirement() {
  await waitFor(() =>
    expect(screen.getByLabelText("希望完成什么").matches(":disabled")).toBe(
      false,
    ),
  );
  await userEvent.type(
    await screen.findByLabelText("希望完成什么"),
    "增加问候语",
  );
  await userEvent.type(
    screen.getByLabelText("验收标准（每行一条）"),
    "页面显示问候语",
  );
  await userEvent.selectOptions(screen.getByLabelText("主 Commander"), "0");
  await userEvent.type(
    screen.getByLabelText("允许读取的路径（每行一条）"),
    "src",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存需求" }));
}

it("recovers the same pending request after reopening even when the project configuration changed", async () => {
  const writes: RequestInit[] = [];
  const saved = vi.fn();
  let revision = 2;
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration")) return configured(revision);
    writes.push(options!);
    if (writes.length === 1) throw new TypeError("Response lost after commit");
    return Response.json({ id: "run-1" }, { status: 201 });
  });
  const view = render(
    <NewRunForm project={project} csrf="same-session" onSaved={saved} />,
  );
  await fillRequirement();
  await screen.findByRole("alert");
  view.unmount();
  revision = 3;
  render(
    <NewRunForm
      project={{ ...project, revision: 3 }}
      csrf="same-session"
      onSaved={saved}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "核对保存结果" }),
  );
  expect(saved).toHaveBeenCalledWith("run-1");
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toBe(writes[0].body);
  expect(new Headers(writes[1].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[0].headers).get("Idempotency-Key"),
  );
});

it("does not send a request when the session binding cannot be established", async () => {
  const writes: RequestInit[] = [];
  vi.stubGlobal("crypto", {
    randomUUID: () => "00000000-0000-4000-8000-000000000001",
  });
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration")) return configured();
    writes.push(options!);
    return Response.json({ id: "unexpected" });
  });
  render(
    <NewRunForm
      project={project}
      csrf="session-unavailable"
      onSaved={vi.fn()}
    />,
  );
  expect((await screen.findByRole("alert")).textContent).toContain(
    "暂不能发送需求",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存需求" }));
  expect(writes).toHaveLength(0);
});

it("does not send until the original request identity is durably stored in this tab", async () => {
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration")) return configured();
    writes.push(options!);
    return Response.json({ id: "unexpected" });
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("storage full");
  });
  render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  await fillRequirement();
  expect((await screen.findByRole("alert")).textContent).toContain(
    "尚未发送新请求",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存需求" }));
  expect(writes).toHaveLength(0);
});

it("keeps an unreadable pending record blocked instead of replacing it with a new request", async () => {
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration")) return configured();
    writes.push(options!);
    throw new TypeError("unknown result");
  });
  const view = render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  await fillRequirement();
  await screen.findByRole("alert");
  view.unmount();
  const storageKey = sessionStorage.key(0)!;
  sessionStorage.setItem(storageKey, "{broken");
  render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  expect((await screen.findByRole("alert")).textContent).toContain(
    "暂不能发送需求",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存需求" }));
  expect(writes).toHaveLength(1);
  expect(sessionStorage.getItem(storageKey)).toBe("{broken");
});

it.each(["project", "session"])(
  "isolates pending commands by %s and only retries them on an explicit click",
  async (variant) => {
    const writes: RequestInit[] = [];
    vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
      if (path.endsWith("/configuration")) return configured();
      writes.push(options!);
      throw new TypeError("unknown result");
    });
    const view = render(
      <NewRunForm
        project={project}
        csrf="original-session-secret"
        onSaved={vi.fn()}
      />,
    );
    await fillRequirement();
    await screen.findByRole("alert");
    view.rerender(
      <NewRunForm
        project={
          variant === "project" ? { ...project, id: "project-2" } : project
        }
        csrf={
          variant === "session"
            ? "new-session-secret"
            : "original-session-secret"
        }
        onSaved={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("希望完成什么").matches(":disabled")).toBe(
        false,
      ),
    );
    expect(
      (screen.getByLabelText("希望完成什么") as HTMLTextAreaElement).value,
    ).toBe("");
    expect(screen.queryByRole("button", { name: "核对保存结果" })).toBeNull();
    expect(writes).toHaveLength(1);
    expect(sessionStorage.getItem(sessionStorage.key(0)!)).not.toContain(
      "original-session-secret",
    );
    expect(sessionStorage.key(0)).not.toContain("original-session-secret");
    view.rerender(
      <NewRunForm
        project={project}
        csrf="original-session-secret"
        onSaved={vi.fn()}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "核对保存结果" }),
    );
    expect(writes).toHaveLength(2);
    expect(writes[1].body).toBe(writes[0].body);
    expect(new Headers(writes[1].headers).get("Idempotency-Key")).toBe(
      new Headers(writes[0].headers).get("Idempotency-Key"),
    );
  },
);

it("retains malformed success as unknown and removes pending identity only after a usable confirmation", async () => {
  const writes: RequestInit[] = [];
  const saved = vi.fn();
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration")) return configured();
    writes.push(options!);
    return Response.json(writes.length === 1 ? {} : { id: "run-1" }, {
      status: 201,
    });
  });
  const view = render(
    <NewRunForm project={project} csrf="same-session" onSaved={saved} />,
  );
  await fillRequirement();
  await screen.findByRole("alert");
  expect(saved).not.toHaveBeenCalled();
  view.unmount();
  const restored = render(
    <NewRunForm project={project} csrf="same-session" onSaved={saved} />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "核对保存结果" }),
  );
  expect(saved).toHaveBeenCalledWith("run-1");
  expect(writes[1].body).toBe(writes[0].body);
  restored.unmount();
  render(<NewRunForm project={project} csrf="same-session" onSaved={saved} />);
  await waitFor(() =>
    expect(screen.getByLabelText("希望完成什么").matches(":disabled")).toBe(
      false,
    ),
  );
  expect(
    (screen.getByLabelText("希望完成什么") as HTMLTextAreaElement).value,
  ).toBe("");
  expect(screen.queryByRole("button", { name: "核对保存结果" })).toBeNull();
  expect(writes).toHaveLength(2);
});

it("does not let an old unmounted response remove a newer pending requirement", async () => {
  const writes: RequestInit[] = [];
  let finishOld: (response: Response) => void = () => {};
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration")) return configured();
    writes.push(options!);
    if (writes.length === 1)
      return new Promise<Response>((resolve) => {
        finishOld = resolve;
      });
    if (writes.length === 2)
      return Response.json({ id: "run-1" }, { status: 201 });
    throw new TypeError("new request outcome unknown");
  });
  const first = render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  await fillRequirement();
  await screen.findByRole("status");
  first.unmount();
  const retry = render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "核对保存结果" }),
  );
  retry.unmount();
  const next = render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  await fillRequirement();
  await screen.findByRole("alert");
  await act(async () => {
    finishOld(Response.json({ id: "run-1" }, { status: 201 }));
  });
  next.unmount();
  render(
    <NewRunForm project={project} csrf="same-session" onSaved={vi.fn()} />,
  );
  await screen.findByRole("button", { name: "核对保存结果" });
  expect(writes).toHaveLength(3);
  expect(new Headers(writes[2].headers).get("Idempotency-Key")).not.toBe(
    new Headers(writes[0].headers).get("Idempotency-Key"),
  );
});
it("saves the users requirement against the displayed project snapshot and reuses an uncertain request", async () => {
  const writes: RequestInit[] = [];
  const saved = vi.fn();
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path.endsWith("/configuration"))
      return Response.json({
        project_revision: 2,
        configuration: {
          approved_profile_refs: [
            { id: "lead-profile", revision: 1 },
            { id: "worker-profile", revision: 2 },
          ],
          rulebook: {
            profile_groups: {
              commander_qualified: [{ id: "lead-profile", revision: 1 }],
            },
            resource_policy: { run_budget_ref: "run" },
          },
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
      });
    if (path === "/v1/runs") {
      writes.push(options!);
      if (writes.length === 1) throw new TypeError("Connection lost");
      return Response.json({ id: "run-1" }, { status: 201 });
    }
    throw new Error("Unexpected request");
  });
  render(
    <NewRunForm
      project={{
        id: "project-1",
        name: "Sample",
        revision: 2,
        target_branch: "main",
        configuration: { status: "offline_valid", digest: "c".repeat(64) },
      }}
      csrf="csrf-fixture"
      onSaved={saved}
    />,
  );
  await userEvent.type(
    await screen.findByLabelText("希望完成什么"),
    "增加问候语",
  );
  await userEvent.type(
    screen.getByLabelText("验收标准（每行一条）"),
    "页面显示问候语",
  );
  await userEvent.selectOptions(screen.getByLabelText("主 Commander"), "0");
  await userEvent.type(
    screen.getByLabelText("允许读取的路径（每行一条）"),
    "src\ntests",
  );
  await userEvent.type(
    screen.getByLabelText("允许修改的路径（每行一条）"),
    "src",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存需求" }));
  await screen.findByRole("alert");
  await userEvent.type(screen.getByLabelText("希望完成什么"), "另一个需求");
  expect(
    (screen.getByLabelText("希望完成什么") as HTMLTextAreaElement).value,
  ).toBe("增加问候语");
  await userEvent.click(screen.getByRole("button", { name: "核对保存结果" }));
  expect(saved).toHaveBeenCalledWith("run-1");
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toBe(writes[0].body);
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[1].headers).get("Idempotency-Key"),
  );
  const body = JSON.parse(writes[0].body as string);
  expect(body.configuration_digest).toBe("c".repeat(64));
  expect(body.requirement).toEqual({
    goal: "增加问候语",
    acceptance: ["页面显示问候语"],
  });
  expect(body.authorization.write_paths).toEqual(["src"]);
  expect(body.authorization.profile_refs).toEqual([
    { id: "lead-profile", revision: 1 },
    { id: "worker-profile", revision: 2 },
  ]);
  expect(body.authorization.checks).toContain("independent_review");
});
