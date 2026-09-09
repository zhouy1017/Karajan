import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { CommanderWorkbench } from "./CommanderWorkbench";

const project = {
  id: "project-one",
  name: "项目一",
  repository: { root: "C:/repo/one", base_ref: "main" },
  target_branch: "main",
};
const conversation = {
  id: "conversation-one",
  project_id: project.id,
  title: "Commander",
  revision: 1,
  commander_profile_ref: "commander",
  commander_source_ref: "go",
};

class EventSourceFixture {
  static instances: EventSourceFixture[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  constructor(public url: string) {
    EventSourceFixture.instances.push(this);
  }
  addEventListener(name: string, handler: EventListenerOrEventListenerObject) {
    this.listeners.set(name, handler as (event: MessageEvent<string>) => void);
  }
  close() {}
  emit(name: string, value: unknown) {
    this.listeners.get(name)?.({
      data: JSON.stringify(value),
    } as MessageEvent<string>);
  }
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  EventSourceFixture.instances = [];
  vi.unstubAllGlobals();
});

function installFetch(
  overrides: Record<
    string,
    | Response
    | ((
        input: RequestInfo | URL,
        init?: RequestInit,
      ) => Response | Promise<Response>)
  > = {},
) {
  vi.stubGlobal("EventSource", EventSourceFixture);
  vi.stubGlobal("crypto", { randomUUID: () => "stable-command-id" });
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const match = Object.entries(overrides).find(
        ([path]) => url === path || url.startsWith(path),
      );
      if (match)
        return typeof match[1] === "function"
          ? match[1](input, init)
          : match[1];
      if (url.endsWith("/conversations")) return Response.json({ items: [] });
      throw new Error(`Unexpected request ${url} ${init?.method ?? "GET"}`);
    },
  );
}

it("loads only configured Commander choices and sends a non executing string task draft", async () => {
  const taskRequest = vi.fn();
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({
      items: [
        { profile_ref: "commander", profile_revision: 1, source_ref: "go" },
      ],
    }),
    "/v1/projects/project-one/conversations": Response.json({
      items: [conversation],
    }),
    "/v1/conversations/conversation-one/snapshot": Response.json({
      conversation,
      messages: [],
      draft: { content: "", revision: 0 },
      task_drafts: [],
      runs: [],
      snapshot_event_seq: 0,
    }),
    "/v1/conversations/conversation-one/task-drafts": () => {
      taskRequest();
      return Response.json({ id: "task-one", state: "draft" }, { status: 201 });
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await screen.findByRole("heading", { name: "Commander" });
  expect(screen.getByRole("option", { name: "commander" })).toBeTruthy();
  expect(screen.queryByRole("option", { name: "gpt-5.6-luna" })).toBeNull();
  const input = screen.getByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "Add a button");
  await userEvent.click(screen.getByRole("button", { name: "＋ 新任务草稿" }));
  expect(taskRequest).toHaveBeenCalled();
});

it("keeps a failed draft save visible and reports the server error", async () => {
  let draftSaves = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": Response.json({
      items: [conversation],
    }),
    "/v1/conversations/conversation-one/snapshot": Response.json({
      conversation,
      messages: [],
      draft: { content: "", revision: 0 },
      task_drafts: [],
      runs: [],
      snapshot_event_seq: 0,
    }),
    "/v1/conversations/conversation-one/draft": () => {
      draftSaves += 1;
      return new Response(
        JSON.stringify({ reason_code: "DRAFT_TEMPORARY_FAILURE" }),
        { status: 503 },
      );
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "keep this text");
  input.blur();
  await screen.findByRole("alert");
  expect(draftSaves).toBe(1);
  expect((input as HTMLTextAreaElement).value).toBe("keep this text");
  expect(screen.getByRole("alert").textContent).toContain(
    "DRAFT_TEMPORARY_FAILURE",
  );
});

const projectTwo = {
  id: "project-two",
  name: "项目二",
  repository: { root: "C:/repo/two", base_ref: "main" },
  target_branch: "main",
};
const conversationTwo = {
  ...conversation,
  id: "conversation-two",
  project_id: projectTwo.id,
  title: "Commander two",
};

function snapshotFor(
  item: typeof conversation,
  extra: Record<string, unknown> = {},
) {
  return {
    conversation: item,
    messages: [],
    draft: { content: "", revision: 0 },
    task_drafts: [],
    runs: [],
    snapshot_event_seq: 4,
    ...extra,
  };
}

it("keeps an unresolved A draft out of B and restores it after failed navigation save", async () => {
  const draftUrls: string[] = [];
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-two/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/projects/project-two/conversations": () =>
      Response.json({ items: [conversationTwo] }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-two/snapshot": () =>
      Response.json(snapshotFor(conversationTwo)),
    "/v1/conversations/conversation-one/draft": () => {
      draftUrls.push("A");
      return new Response(JSON.stringify({ reason_code: "TEMPORARY" }), {
        status: 503,
      });
    },
    "/v1/conversations/conversation-two/draft": () => {
      draftUrls.push("B");
      return Response.json({ revision: 1 });
    },
  });
  render(<CommanderWorkbench projects={[project, projectTwo]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "draft belonging to A");
  await userEvent.click(screen.getByRole("button", { name: "›项目二" }));
  await screen.findByRole("heading", { name: "Commander two" });
  expect(draftUrls).not.toContain("B");
  await userEvent.click(screen.getByRole("button", { name: "›项目一" }));
  const restored = await screen.findByRole("textbox", { name: "消息草稿" });
  expect((restored as HTMLTextAreaElement).value).toBe("draft belonging to A");
  expect(screen.getByRole("alert").textContent).toContain("TEMPORARY");
});

it("reuses an unresolved message command exactly and retires completed draft keys", async () => {
  const messageKeys: string[] = [];
  const draftKeys: string[] = [];
  let messageAttempts = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": Response.json({
      items: [conversation],
    }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-one/messages": (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      messageKeys.push(
        String((init?.headers as Record<string, string>)["Idempotency-Key"]),
      );
      messageAttempts += 1;
      if (messageAttempts === 1) throw new Error("lost response");
      return Response.json({ id: "message-one" });
    },
    "/v1/conversations/conversation-one/draft": (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      draftKeys.push(
        String((init?.headers as Record<string, string>)["Idempotency-Key"]),
      );
      return Response.json({ revision: draftKeys.length });
    },
  });
  let uuid = 0;
  vi.stubGlobal("crypto", { randomUUID: () => `unique-command-${++uuid}` });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "same command");
  await userEvent.click(
    screen.getByRole("button", { name: "发送给 Commander" }),
  );
  await screen.findByRole("alert");
  await userEvent.click(
    screen.getByRole("button", { name: "发送给 Commander" }),
  );
  expect(messageKeys).toHaveLength(2);
  expect(messageKeys[0]).toBe(messageKeys[1]);
  await userEvent.type(input, " one");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await userEvent.type(input, " two");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  expect(new Set(draftKeys).size).toBeGreaterThanOrEqual(2);
});

it("recovers from an SSE gap at the snapshot watermark and refreshes named event facts", async () => {
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": Response.json({
      items: [conversation],
    }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, { snapshot_event_seq: snapshots * 10 }),
      );
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await screen.findByRole("textbox", { name: "消息草稿" });
  const first = EventSourceFixture.instances.at(-1)!;
  expect(first.url).toContain("after_seq=10");
  await act(async () => first.emit("event_gap", { sequence: 11 }));
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(2));
  await vi.waitFor(() =>
    expect(EventSourceFixture.instances.at(-1)!.url).toContain("after_seq=20"),
  );
  const recovered = EventSourceFixture.instances.at(-1)!;
  await act(async () => recovered.emit("message_created", { sequence: 21 }));
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(3));
  await vi.waitFor(() =>
    expect(EventSourceFixture.instances.at(-1)!.url).toContain("after_seq=30"),
  );
});

it("persists Task and Attempt selection and scopes detail facts to the selected identity", async () => {
  const saves: unknown[] = [];
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": Response.json({
      items: [conversation],
    }),
    "/v1/conversations/conversation-one/snapshot": Response.json(
      snapshotFor(conversation, {
        proposed_plan: { tasks: [{ id: "task-a", title: "Task A" }] },
        runs: [
          {
            id: "run-a",
            tasks: [{ id: "task-a", title: "Task A" }],
            attempts: [{ id: "attempt-a", task_id: "task-a" }],
          },
        ],
        checks: {
          items: [
            { task_id: "task-a", result: "only A" },
            { task_id: "task-b", result: "only B" },
          ],
        },
        logs: {
          items: [
            { attempt_id: "attempt-a", result: "attempt A" },
            { attempt_id: "attempt-b", result: "attempt B" },
          ],
        },
      }),
    ),
    "/v1/conversations/conversation-one/draft": (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      saves.push(JSON.parse(String(init?.body)));
      return Response.json({ revision: saves.length });
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await userEvent.click(await screen.findByRole("button", { name: /Task A/ }));
  await userEvent.click(screen.getByRole("button", { name: "Checks" }));
  await vi.waitFor(() =>
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "PRE" &&
          element.textContent?.includes("only A") === true,
      ),
    ).toBeTruthy(),
  );
  expect(
    screen.queryByText(
      (_, element) =>
        element?.tagName === "PRE" &&
        element.textContent?.includes("only B") === true,
    ),
  ).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Hub" }));
  await userEvent.click(screen.getByRole("button", { name: /attempt-a/ }));
  await userEvent.click(screen.getByRole("button", { name: "Logs" }));
  await vi.waitFor(() =>
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "PRE" &&
          element.textContent?.includes("attempt A") === true,
      ),
    ).toBeTruthy(),
  );
  expect(
    screen.queryByText(
      (_, element) =>
        element?.tagName === "PRE" &&
        element.textContent?.includes("attempt B") === true,
    ),
  ).toBeNull();
  expect(
    saves.map(
      (body) => (body as { selected_task_id: string }).selected_task_id,
    ),
  ).toEqual(["task-a", "attempt-a"]);
});

it("allows configured selections before first conversation and never invents feedback time", async () => {
  const created: unknown[] = [];
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({
      items: [{ profile_ref: "commander", source_ref: "go" }],
    }),
    "/v1/projects/project-one/conversations": (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      if (init?.method === "POST") {
        created.push(JSON.parse(String(init.body)));
        return Response.json(conversation, { status: 201 });
      }
      return Response.json({ items: [] });
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await screen.findByRole("option", { name: "commander" });
  const profile = screen.getByRole("combobox", {
    name: "已配置 Commander 模型",
  });
  await userEvent.selectOptions(profile, "commander");
  expect(
    (
      screen.getByRole("combobox", {
        name: "已配置 Commander 来源",
      }) as HTMLSelectElement
    ).value,
  ).toBe("go");
  await userEvent.click(
    screen.getByRole("button", { name: "与 Commander 开始" }),
  );
  await vi.waitFor(() =>
    expect(created).toEqual([
      {
        title: "新的 Commander 会话",
        commander_profile_ref: "commander",
        commander_source_ref: "go",
      },
    ]),
  );
});
