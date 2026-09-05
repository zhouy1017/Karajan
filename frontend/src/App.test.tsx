import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("revokes the session with CSRF before returning to the login page", async () => {
  const writes: RequestInit[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path === "/v1/session")
      return Response.json({ csrf_token: "csrf-fixture" });
    if (path === "/v1/projects") return Response.json({ items: [] });
    if (path === "/v1/session/logout") {
      writes.push(options!);
      return new Response(null, { status: 204 });
    }
    throw new Error("Unexpected request");
  });
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: "退出登录" }),
  );
  await screen.findByLabelText("本机访问码");
  expect(writes).toHaveLength(1);
  expect(writes[0].method).toBe("POST");
  expect(new Headers(writes[0].headers).get("X-CSRF-Token")).toBe(
    "csrf-fixture",
  );
  expect(screen.queryByRole("heading", { name: "你的项目" })).toBeNull();
});

it("exchanges the local code and lists authenticated projects without preserving the code", async () => {
  const received: { path: string; body?: string }[] = [];
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    received.push({ path, body: options?.body as string | undefined });
    if (path === "/v1/session") return new Response("{}", { status: 401 });
    if (path === "/v1/session/bootstrap")
      return Response.json({ csrf_token: "csrf-fixture" });
    if (path === "/v1/projects") return Response.json({ items: [] });
    throw new Error("Unexpected request");
  });
  render(<App />);
  await userEvent.type(
    await screen.findByLabelText("本机访问码"),
    "private-code",
  );
  await userEvent.click(screen.getByRole("button", { name: "进入工作台" }));
  await screen.findByRole("heading", { name: "你的项目" });
  expect(screen.queryByDisplayValue("private-code")).toBeNull();
  expect(localStorage.length).toBe(0);
  expect(
    received.find((row) => row.path === "/v1/session/bootstrap")?.body,
  ).toBe('{"token":"private-code"}');
});

it("registers a repository with CSRF and reuses the command identity after an unknown response", async () => {
  const writes: RequestInit[] = [];
  let created = false;
  const project = {
    id: "project-1",
    name: "我的仓库",
    revision: 1,
    repository: {
      root: "/allowed/repo",
      base_ref: "main",
      base_sha: "a".repeat(40),
    },
    target_branch: "main",
    configuration: {
      status: "unconfigured",
      revision: 0,
      dispatch_eligible: false,
    },
  };
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path === "/v1/session")
      return Response.json({ csrf_token: "csrf-fixture" });
    if (path === "/v1/projects" && options?.method === "POST") {
      writes.push(options);
      if (writes.length === 1) throw new TypeError("Disconnected");
      created = true;
      return Response.json(project, { status: 201 });
    }
    if (path === "/v1/projects")
      return Response.json({ items: created ? [project] : [] });
    if (path.startsWith("/v1/runs?")) return Response.json({ items: [] });
    throw new Error("Unexpected request");
  });
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: "登记项目" }),
  );
  await userEvent.type(screen.getByLabelText("项目名称"), "我的仓库");
  await userEvent.type(screen.getByLabelText("本机仓库路径"), "/allowed/repo");
  await userEvent.click(screen.getByRole("button", { name: "保存项目" }));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "保存项目" }));
  await screen.findByRole("heading", { name: "我的仓库" });
  await userEvent.click(screen.getByRole("button", { name: "需求与计划" }));
  await screen.findByRole("heading", { name: "我的仓库 · 需求与计划" });
  expect(writes).toHaveLength(2);
  expect(new Headers(writes[0].headers).get("X-CSRF-Token")).toBe(
    "csrf-fixture",
  );
  expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBe(
    new Headers(writes[1].headers).get("Idempotency-Key"),
  );
  expect(JSON.parse(writes[0].body as string)).toEqual({
    name: "我的仓库",
    repository_path: "/allowed/repo",
    base_ref: "main",
    target_branch: "main",
    allowed_target_branches: ["main"],
  });
});

it("requires a new preview after editing configuration and applies only the saved preview identity", async () => {
  const writes: { path: string; options?: RequestInit }[] = [];
  const project = {
    id: "project-1",
    name: "Sample",
    revision: 1,
    repository: {
      root: "/allowed/repo",
      base_ref: "main",
      base_sha: "a".repeat(40),
    },
    target_branch: "main",
    configuration: {
      status: "unconfigured",
      revision: 0,
      dispatch_eligible: false,
    },
  };
  vi.stubGlobal("fetch", async (path: string, options?: RequestInit) => {
    if (path === "/v1/session")
      return Response.json({ csrf_token: "csrf-fixture" });
    if (path === "/v1/projects") return Response.json({ items: [project] });
    if (path.endsWith("/configuration"))
      return Response.json({ configuration: { source: "saved" } });
    writes.push({ path, options });
    if (path.endsWith("/preview"))
      return Response.json({
        preview_id: "preview-1",
        project_revision: 1,
        status: "draft",
        can_apply: true,
        issues: [{ code: "CONFIGURATION_INCOMPLETE", path: "resources" }],
        dispatch_eligible: false,
      });
    if (path.endsWith("/apply"))
      return Response.json({
        ...project,
        revision: 2,
        configuration: {
          status: "draft",
          revision: 1,
          dispatch_eligible: false,
        },
      });
    throw new Error("Unexpected request");
  });
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: "检查配置" }),
  );
  expect(
    (screen.getByLabelText("配置内容") as HTMLTextAreaElement).value,
  ).toContain("saved");
  await userEvent.click(screen.getByRole("button", { name: "预览配置" }));
  await screen.findByRole("button", { name: "保存这份配置" });
  await userEvent.type(screen.getByLabelText("配置内容"), " ");
  expect(screen.queryByRole("button", { name: "保存这份配置" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "预览配置" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "保存这份配置" }),
  );
  await screen.findByText("配置已保存。执行资格仍需单独验证。");
  const applied = writes.find((row) => row.path.endsWith("/apply"))!;
  expect(JSON.parse(applied.options?.body as string)).toEqual({
    preview_id: "preview-1",
  });
  expect(new Headers(applied.options?.headers).get("If-Match")).toBe('"1"');
});
