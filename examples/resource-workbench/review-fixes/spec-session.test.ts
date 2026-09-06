// Public App regression, isolated HTTP fixture; no real session or provider credentials.
import {
  cleanup,
  render,
  screen,
  act,
} from "../../../frontend/node_modules/@testing-library/react/dist/index.js";
import userEvent from "../../../frontend/node_modules/@testing-library/user-event/dist/esm/index.js";
import {
  afterEach,
  expect,
  it,
  vi,
} from "../../../frontend/node_modules/vitest/dist/index.js";
import { createElement } from "../../../frontend/node_modules/react/index.js";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { createHash } from "node:crypto";
import { App } from "../../../frontend/src/App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("a late expired resource response must not log out the replacement owner session", async () => {
  const bytes = readFileSync(
    "examples/resource-workbench/review-fixes/spec-session.input.json",
  );
  const input = JSON.parse(bytes.toString());
  let oldResponse!: (response: Response) => void;
  const requests: string[] = [];
  vi.stubGlobal("fetch", async (path: string) => {
    requests.push(path);
    if (path === "/v1/session")
      return Response.json({ csrf_token: input.initial_csrf });
    if (path === "/v1/projects") return Response.json({ items: [] });
    if (path === "/v1/resources")
      return new Promise<Response>((resolve) => {
        oldResponse = resolve;
      });
    if (path === "/v1/session/logout")
      return new Response(null, { status: 204 });
    if (path === "/v1/session/bootstrap")
      return Response.json({ csrf_token: input.replacement_csrf });
    throw new Error("Unexpected fixture request");
  });
  render(createElement(App));
  await screen.findByRole("heading", { name: "你的项目" });
  await userEvent.click(screen.getByRole("button", { name: "资源与配额" }));
  await screen.findByText("正在读取额度…");
  await userEvent.click(screen.getByRole("button", { name: "退出登录" }));
  await userEvent.type(
    await screen.findByLabelText("本机访问码"),
    input.replacement_code,
  );
  await userEvent.click(screen.getByRole("button", { name: "进入工作台" }));
  await screen.findByRole("heading", { name: "你的项目" });
  await act(async () => {
    oldResponse(
      new Response("{}", { status: input.old_resource_response_status }),
    );
  });
  const sources = ["frontend/src/App.tsx", "frontend/src/ResourcePanel.tsx"];
  const evidence = {
    input,
    input_sha256: createHash("sha256").update(bytes).digest("hex"),
    requests,
    old_resource_response_status: input.old_resource_response_status,
    replacement_session_was_logged_in: true,
    project_visible: !!screen.queryByRole("heading", { name: "你的项目" }),
    login_visible: !!screen.queryByLabelText("本机访问码"),
    source_sha256: Object.fromEntries(
      sources.map((path) => [
        path,
        createHash("sha256").update(readFileSync(path)).digest("hex"),
      ]),
    ),
    provider_calls: 0,
    cash_calls: 0,
    source: "public App UI with explicitly deferred local HTTP fixture",
  };
  const output =
    process.env.KARAJAN_RESOURCE_REVIEW_OUTPUT ??
    ".cache/resource-review/session-race.replay.json";
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, JSON.stringify(evidence, null, 2) + "\n");
  expect(evidence.project_visible).toBe(true);
  expect(evidence.login_visible).toBe(false);
});
