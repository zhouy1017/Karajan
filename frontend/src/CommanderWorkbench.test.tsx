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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
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
        run_summaries: [
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

it("keeps a newer edit dirty when an older draft acknowledgement arrives", async () => {
  const first = deferred<Response>();
  const second = deferred<Response>();
  let saves = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-one/draft": () => {
      saves += 1;
      return saves === 1 ? first.promise : second.promise;
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "X");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await userEvent.type(input, "Y");
  await act(async () => first.resolve(Response.json({ revision: 1 })));
  await vi.waitFor(() => expect(saves).toBe(2));
  expect((input as HTMLTextAreaElement).value).toBe("XY");
  expect(
    sessionStorage.getItem(
      `karajan:commander-draft:${project.id}:${conversation.id}`,
    ),
  ).toContain("XY");
  expect(screen.getByText("未发送的本地草稿待保存")).toBeTruthy();
  await act(async () => second.resolve(Response.json({ revision: 2 })));
});

it("preserves text typed while send and task commands await their responses", async () => {
  const message = deferred<Response>();
  const task = deferred<Response>();
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-one/messages": () => message.promise,
    "/v1/conversations/conversation-one/task-drafts": () => task.promise,
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "send X");
  await userEvent.click(
    screen.getByRole("button", { name: "发送给 Commander" }),
  );
  await userEvent.type(input, " plus Y");
  await act(async () => message.resolve(Response.json({ id: "message-a" })));
  expect((input as HTMLTextAreaElement).value).toBe("send X plus Y");

  await userEvent.click(screen.getByRole("button", { name: "＋ 新任务草稿" }));
  await userEvent.type(input, " task later text");
  await act(async () => task.resolve(Response.json({ id: "task-a" })));
  expect((input as HTMLTextAreaElement).value).toBe(
    "send X plus Y task later text",
  );
});

it("does not let a late send response alter the newly selected conversation", async () => {
  const message = deferred<Response>();
  const conversationB = {
    ...conversation,
    id: "conversation-b",
    title: "Conversation B",
  };
  sessionStorage.setItem("karajan:commander-project", project.id);
  sessionStorage.setItem(
    `karajan:commander-conversation:${project.id}`,
    conversation.id,
  );
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation, conversationB] }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-one/messages": () => message.promise,
    "/v1/conversations/conversation-one/draft": () =>
      Response.json({ revision: 1 }),
    "/v1/conversations/conversation-b/snapshot": () =>
      Response.json(snapshotFor(conversationB)),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "message from A");
  await userEvent.click(
    screen.getByRole("button", { name: "发送给 Commander" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "›项目一" }));
  await userEvent.click(
    await screen.findByRole("button", { name: /Conversation B/ }),
  );
  const bInput = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(bInput, "draft for B");
  await act(async () => message.resolve(Response.json({ id: "message-a" })));
  expect((bInput as HTMLTextAreaElement).value).toBe("draft for B");
});

it("retries a body-incomplete creation with the same key and opens its intended new conversation", async () => {
  const created = {
    ...conversation,
    id: "conversation-new",
    title: "New blank",
  };
  const keys: string[] = [];
  let attempts = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": (_input, init) => {
      if (init?.method !== "POST")
        return Response.json({
          items: attempts ? [conversation, created] : [conversation],
        });
      keys.push(
        String((init.headers as Record<string, string>)["Idempotency-Key"]),
      );
      attempts += 1;
      return attempts === 1
        ? new Response("{", {
            status: 201,
            headers: { "Content-Type": "application/json" },
          })
        : Response.json(created, { status: 201 });
    },
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-new/snapshot": () =>
      Response.json(snapshotFor(created)),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await screen.findByRole("heading", { name: "Commander" });
  await userEvent.click(screen.getByRole("button", { name: "新对话" }));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "新对话" }));
  await screen.findByRole("heading", { name: "New blank" });
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});

it("opens a blank B from active A while retaining A's unresolved local draft", async () => {
  const created = { ...conversation, id: "conversation-b", title: "Blank B" };
  let createdOnce = false;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": (_input, init) => {
      if (init?.method === "POST") {
        createdOnce = true;
        return Response.json(created, { status: 201 });
      }
      return Response.json({
        items: createdOnce ? [conversation, created] : [conversation],
      });
    },
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(snapshotFor(conversation)),
    "/v1/conversations/conversation-b/snapshot": () =>
      Response.json(snapshotFor(created)),
    "/v1/conversations/conversation-one/draft": () =>
      new Response(JSON.stringify({ reason_code: "A_DRAFT_TEMPORARY" }), {
        status: 503,
      }),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "A local draft");
  await userEvent.click(screen.getByRole("button", { name: "新对话" }));
  await screen.findByRole("heading", { name: "Blank B" });
  expect(
    (screen.getByRole("textbox", { name: "消息草稿" }) as HTMLTextAreaElement)
      .value,
  ).toBe("");
  expect(
    sessionStorage.getItem(
      `karajan:commander-draft:${project.id}:${conversation.id}`,
    ),
  ).toContain("A local draft");
  expect(screen.getByRole("alert").textContent).toContain("尚未保存到服务器");
});

it("recovers a failed gap snapshot before accepting a later state event", async () => {
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      if (snapshots === 2)
        return new Response(
          JSON.stringify({ reason_code: "SNAPSHOT_TEMPORARY" }),
          {
            status: 503,
          },
        );
      return Response.json(
        snapshotFor(conversation, { snapshot_event_seq: snapshots * 10 }),
      );
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await screen.findByRole("textbox", { name: "消息草稿" });
  await vi.waitFor(() =>
    expect(EventSourceFixture.instances.at(-1)!.url).toContain("after_seq=10"),
  );
  await act(async () =>
    EventSourceFixture.instances.at(-1)!.emit("event_gap", { sequence: 5 }),
  );
  await act(async () => {
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1_100));
  });
  expect(snapshots).toBeGreaterThanOrEqual(3);
  await act(async () => {
    await Promise.resolve();
  });
  expect(EventSourceFixture.instances.at(-1)!.url).toContain("after_seq=30");
  await act(async () => {
    EventSourceFixture.instances.at(-1)!.emit("attempt_updated", {
      sequence: 31,
    });
    await Promise.resolve();
  });
  expect(snapshots).toBeGreaterThanOrEqual(4);
  await act(async () => {
    await Promise.resolve();
  });
  expect(EventSourceFixture.instances.at(-1)!.url).toContain("after_seq=40");
});

it("re-snapshots state events and exposes a disconnected current Attempt honestly", async () => {
  let snapshots = 0;
  let completed = false;
  const observed = Date.now();
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          runs: [
            {
              id: "run-a",
              tasks: [{ id: "task-a", current_attempt_id: "attempt-a" }],
              attempts: [
                {
                  id: "attempt-a",
                  task_id: "task-a",
                  status: completed ? "completed" : "running",
                  observed_at: observed,
                },
              ],
            },
          ],
        }),
      );
    },
    "/v1/conversations/conversation-one/draft": () =>
      Response.json({ revision: 1 }),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const agent = await screen.findByRole("button", { name: /attempt-a/ });
  await userEvent.click(agent);
  const source = EventSourceFixture.instances.at(-1)!;
  await act(async () => source.onopen?.());
  expect(screen.getByRole("status").textContent).toContain("模型运行中");
  await act(async () => source.onerror?.());
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(2));
  expect(screen.getByRole("status").textContent).toContain(
    "反馈中断，等待核对",
  );
  completed = true;
  const current = EventSourceFixture.instances.at(-1)!;
  await act(async () =>
    current.emit("attempt_updated", {
      sequence: 5,
      attempt_id: "attempt-a",
      state: "completed",
    }),
  );
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(3));
  expect(
    screen.getByRole("button", { name: /attempt-a/ }).textContent,
  ).toContain("completed");
});
