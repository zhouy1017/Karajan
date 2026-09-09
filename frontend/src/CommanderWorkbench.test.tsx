import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { CommanderWorkbench, stableKey } from "./CommanderWorkbench";

const backendCommandKey = /^[a-zA-Z0-9_-]{1,128}$/;

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
  vi.useRealTimers();
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
      if (init?.method && init.method !== "GET") {
        const key = new Headers(init.headers).get("Idempotency-Key");
        if (!key || !backendCommandKey.test(key))
          throw new Error(
            `Invalid backend command key for ${init.method} ${url}`,
          );
      }
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

it("encodes every Commander write identity as a distinct backend-valid command key", () => {
  const identity = "01234567-89ab-4cde-8fab-0123456789ab";
  const keys = [
    "conversation",
    "task-draft",
    "draft",
    "message",
    "settings",
  ].map((kind) => stableKey(kind, identity));

  expect(keys).toHaveLength(5);
  expect(new Set(keys)).toHaveLength(5);
  for (const key of keys) expect(key).toMatch(backendCommandKey);
});

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
      run_summaries: [],
      tasks: [],
      attempts: [],
      agents: [],
      blockers: [],
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
      run_summaries: [],
      tasks: [],
      attempts: [],
      agents: [],
      blockers: [],
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
    run_summaries: [],
    tasks: [],
    attempts: [],
    agents: [],
    blockers: [],
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
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(2));
  await vi.waitFor(() =>
    expect(EventSourceFixture.instances.at(-1)!.url).toContain("after_seq=30"),
  );
});

it("renders flat backend Tasks and Planning Attempts, then scopes details to the selected identity", async () => {
  const saves: unknown[] = [];
  installFetch({
    "/v1/projects/project-one/commander-options": Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": Response.json({
      items: [conversation],
    }),
    "/v1/conversations/conversation-one/snapshot": Response.json(
      snapshotFor(conversation, {
        runs: ["run-a"],
        run_summaries: [
          {
            id: "run-a",
            project_id: project.id,
            conversation_id: conversation.id,
            state: "running",
          },
        ],
        tasks: [
          {
            id: "task-a",
            run_id: "run-a",
            role: "Task A",
            state: "ready",
            readiness: "ready",
            depends_on: [],
            checks: [],
          },
        ],
        attempts: [
          {
            id: "attempt-a",
            run_id: "run-a",
            kind: "planning",
            state: "running",
            principal: "commander",
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
  await userEvent.click(await screen.findByRole("button", { name: /task-a/ }));
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

it("keeps an Attempt state honest across a finite feed, then re-snapshots a named state event", async () => {
  let snapshots = 0;
  let completed = false;
  // A fixed future observation stays non-stale without manufacturing a UI
  // timestamp from the client clock.
  const observed = 1_790_000_000;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          runs: ["run-a"],
          run_summaries: [
            {
              id: "run-a",
              project_id: project.id,
              conversation_id: conversation.id,
              state: completed ? "completed" : "running",
            },
          ],
          tasks: [
            {
              id: "task-a",
              run_id: "run-a",
              role: "Task A",
              state: "ready",
              readiness: "ready",
              depends_on: [],
              checks: [],
            },
          ],
          attempts: [
            {
              id: "attempt-a",
              run_id: "run-a",
              kind: "planning",
              state: completed ? "completed" : "running",
              observed_at: observed,
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
  expect(snapshots).toBe(1);
  expect(screen.getByRole("status").textContent).toContain(
    "反馈中断，等待核对",
  );
  await act(async () => {
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1_100));
  });
  completed = true;
  const current = EventSourceFixture.instances.at(-1)!;
  await act(async () =>
    current.emit("attempt_updated", {
      sequence: 5,
      attempt_id: "attempt-a",
      state: "completed",
    }),
  );
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(2));
  expect(
    screen.getByRole("button", { name: /attempt-a/ }).textContent,
  ).toContain("completed");
});

it("backs off finite event feeds without snapshot polling and cancels an old reconnect after navigation", async () => {
  let firstProjectSnapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-two/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/projects/project-two/conversations": () =>
      Response.json({ items: [conversationTwo] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      firstProjectSnapshots += 1;
      return Response.json(snapshotFor(conversation));
    },
    "/v1/conversations/conversation-two/snapshot": () =>
      Response.json(snapshotFor(conversationTwo)),
  });
  render(<CommanderWorkbench projects={[project, projectTwo]} csrf="csrf" />);
  await screen.findByRole("heading", { name: "Commander" });
  vi.useFakeTimers();
  const finite = () => EventSourceFixture.instances.at(-1)!;

  await act(async () => finite().onerror?.());
  expect(screen.getByRole("status").textContent).not.toContain("模型运行中");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(999);
  });
  expect(EventSourceFixture.instances).toHaveLength(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(EventSourceFixture.instances).toHaveLength(2);

  await act(async () => finite().onerror?.());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2_000);
  });
  expect(EventSourceFixture.instances).toHaveLength(3);

  await act(async () => finite().onerror?.());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4_000);
  });
  expect(EventSourceFixture.instances).toHaveLength(4);
  expect(firstProjectSnapshots).toBe(1);

  await act(async () => finite().onerror?.());
  await act(async () => {
    screen.getByRole("button", { name: "›项目二" }).click();
    await Promise.resolve();
  });
  expect(sessionStorage.getItem("karajan:commander-project")).toBe(
    projectTwo.id,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  const firstProjectStreams = EventSourceFixture.instances.filter((item) =>
    item.url.includes("conversation-one/events"),
  );
  expect(firstProjectStreams).toHaveLength(4);
  expect(firstProjectSnapshots).toBe(1);
});

it("retires a definitive draft conflict, reconciles, and waits for an explicit successor save", async () => {
  const commands: { key: string; revision: string; body: unknown }[] = [];
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          draft: {
            content: snapshots === 1 ? "" : "server draft",
            revision: snapshots === 1 ? 1 : 2,
          },
        }),
      );
    },
    "/v1/conversations/conversation-one/draft": (_input, init) => {
      commands.push({
        key: String(
          (init?.headers as Record<string, string>)["Idempotency-Key"],
        ),
        revision: String((init?.headers as Record<string, string>)["If-Match"]),
        body: JSON.parse(String(init?.body)),
      });
      return commands.length === 1
        ? new Response(
            JSON.stringify({
              reason_code: "DRAFT_REVISION_CONFLICT",
              current_revision: 2,
            }),
            { status: 409 },
          )
        : Response.json({ revision: 3 });
    },
  });
  let uuid = 0;
  vi.stubGlobal("crypto", { randomUUID: () => `draft-command-${++uuid}` });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "local X");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await vi.waitFor(() => expect(commands).toHaveLength(1));
  expect(snapshots).toBeGreaterThanOrEqual(2);
  expect((input as HTMLTextAreaElement).value).toBe("local X");

  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await vi.waitFor(() => expect(commands).toHaveLength(2));
  expect(commands.map((command) => command.revision)).toEqual(['"1"', '"2"']);
  expect(commands.map((command) => command.body)).toEqual([
    {
      content: "local X",
      selected_task_id: null,
      base_plan_revision: null,
    },
    {
      content: "local X",
      selected_task_id: null,
      base_plan_revision: null,
    },
  ]);
  expect(commands[0].key).not.toBe(commands[1].key);
});

it("keeps an execution Attempt local and only sends its corrected Task identity", async () => {
  const draftBodies: { selected_task_id: string | null }[] = [];
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(
        snapshotFor(conversation, {
          draft: { content: "server text", revision: 2 },
          tasks: [{ id: "task-a", role: "Task A" }],
          attempts: [
            {
              id: "execution-attempt-a",
              task_id: "task-a",
              kind: "execution",
              state: "running",
            },
          ],
        }),
      ),
    "/v1/conversations/conversation-one/draft": (_input, init) => {
      draftBodies.push(
        JSON.parse(String(init?.body)) as { selected_task_id: string | null },
      );
      return Response.json({ revision: 3 });
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);

  await userEvent.click(
    await screen.findByRole("button", { name: /execution-attempt-a/ }),
  );
  expect(draftBodies).toEqual([]);

  await userEvent.click(screen.getByRole("button", { name: /task-a/ }));
  await vi.waitFor(() => expect(draftBodies).toHaveLength(1));
  expect(draftBodies[0].selected_task_id).toBe("task-a");
});

it("persists the post-send clear and restores a later local Y after refresh", async () => {
  let serverDraft = "";
  let serverRevision = 0;
  const draftBodies: string[] = [];
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () =>
      Response.json(
        snapshotFor(conversation, {
          draft: { content: serverDraft, revision: serverRevision },
        }),
      ),
    "/v1/conversations/conversation-one/draft": (_input, init) => {
      const body = JSON.parse(String(init?.body)) as { content: string };
      draftBodies.push(body.content);
      serverDraft = body.content;
      serverRevision += 1;
      return Response.json({ revision: serverRevision });
    },
    "/v1/conversations/conversation-one/messages": () =>
      Response.json({
        client_message_id: "stable-command-id",
        conversation_id: conversation.id,
      }),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  await userEvent.type(input, "X");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await vi.waitFor(() => expect(draftBodies).toEqual(["X"]));
  await userEvent.click(
    screen.getByRole("button", { name: "发送给 Commander" }),
  );
  await vi.waitFor(() => expect(draftBodies).toEqual(["X", ""]));
  await vi.waitFor(() => expect((input as HTMLTextAreaElement).value).toBe(""));

  await userEvent.type(input, "Y");
  cleanup();
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const restored = await screen.findByRole("textbox", { name: "消息草稿" });
  expect((restored as HTMLTextAreaElement).value).toBe("Y");
  expect(screen.getByText("未发送的本地草稿待保存")).toBeTruthy();
});

it("accepts consecutive feedback sequences once without a false snapshot recovery", async () => {
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          runs: ["run-a"],
          run_summaries: [{ id: "run-a", state: "running" }],
          tasks: [],
          attempts: [
            {
              id: "attempt-a",
              run_id: "run-a",
              kind: "planning",
              state: "unknown",
            },
          ],
          snapshot_event_seq: 10,
        }),
      );
    },
    "/v1/conversations/conversation-one/draft": () =>
      Response.json({ revision: 1 }),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: /attempt-a/ }),
  );
  const source = EventSourceFixture.instances.at(-1)!;
  await act(async () => {
    source.emit("feedback", {
      sequence: 11,
      attempt_id: "attempt-a",
      state: "waiting_input",
      observed_at: 1_790_000_000,
    });
    source.emit("feedback", {
      sequence: 12,
      attempt_id: "attempt-a",
      state: "completed",
      observed_at: 1_790_000_001,
    });
    source.emit("feedback", {
      sequence: 11,
      attempt_id: "attempt-a",
      state: "running",
      observed_at: 1_790_000_002,
    });
  });

  expect(screen.getByRole("status").textContent).toContain("已完成");
  expect(snapshots).toBe(1);
  expect(EventSourceFixture.instances).toHaveLength(1);
});

it("updates progress observation time without inventing an Attempt state", async () => {
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          attempts: [
            {
              id: "attempt-a",
              kind: "planning",
              state: "unknown",
            },
          ],
          snapshot_event_seq: 10,
        }),
      );
    },
    "/v1/conversations/conversation-one/draft": () =>
      Response.json({ revision: 1 }),
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await userEvent.click(
    await screen.findByRole("button", { name: /attempt-a/ }),
  );
  const source = EventSourceFixture.instances.at(-1)!;
  await act(async () => source.onopen?.());
  await act(async () =>
    source.emit("progress", {
      sequence: 11,
      attempt_id: "attempt-a",
      observed_at: 1_790_000_000,
    }),
  );

  const status = screen.getByRole("status");
  expect(status.textContent).toContain("状态未知，等待核对");
  expect(status.querySelector("time")?.dateTime).toBe(
    new Date(1_790_000_000_000).toISOString(),
  );
  expect(snapshots).toBe(1);

  await act(async () =>
    source.emit("progress", {
      sequence: 11,
      attempt_id: "attempt-a",
      observed_at: 1_790_000_001,
    }),
  );
  expect(status.querySelector("time")?.dateTime).toBe(
    new Date(1_790_000_000_000).toISOString(),
  );
  expect(snapshots).toBe(1);
});

it("retains a local-only candidate selection through recovery and excludes sibling and old-version checks", async () => {
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          runs: ["run-a"],
          run_summaries: [{ id: "run-a", state: "running" }],
          tasks: [
            {
              id: "task-a",
              run_id: "run-a",
              state: "ready",
              readiness: "ready",
              depends_on: [],
              checks: [],
            },
            {
              id: "task-b",
              run_id: "run-a",
              state: "ready",
              readiness: "ready",
              depends_on: [],
              checks: [],
            },
          ],
          attempts: [
            { id: "attempt-a", run_id: "run-a", state: "running" },
            { id: "attempt-b", run_id: "run-a", state: "running" },
          ],
          candidate: {
            items: [
              { id: "candidate-a", version: 2 },
              { id: "candidate-a", version: 1 },
            ],
          },
          checks: {
            items: [
              {
                candidate_id: "candidate-a",
                candidate_version: 2,
                result: "current candidate",
              },
              {
                candidate_id: "candidate-a",
                candidate_version: 1,
                result: "old candidate",
              },
              { task_id: "task-b", result: "sibling B" },
            ],
          },
          snapshot_event_seq: snapshots * 10,
        }),
      );
    },
  });
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const candidates = await screen.findAllByRole("button", {
    name: /candidate-a/,
  });
  await userEvent.click(candidates[0]);
  await vi.waitFor(() => expect(snapshots).toBeGreaterThanOrEqual(2));
  await userEvent.click(screen.getByRole("button", { name: "Checks" }));
  await vi.waitFor(() =>
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "PRE" &&
          element.textContent?.includes("current candidate") === true,
      ),
    ).toBeTruthy(),
  );
  expect(screen.queryByText(/old candidate/)).toBeNull();
  expect(screen.queryByText(/sibling B/)).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("仅保存在本地");
});

it("activates a project that arrives after the workbench, then loads options and creates its first conversation", async () => {
  const createRequests: Array<{ url: string; body: unknown }> = [];
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({
        items: [
          {
            profile_ref: "commander",
            source_ref: "opencode-go-channel",
          },
        ],
      }),
    "/v1/projects/project-one/conversations": (input, init) => {
      if (init?.method === "POST") {
        createRequests.push({
          url: String(input),
          body: JSON.parse(String(init.body)),
        });
        return Response.json({
          id: "conversation-created",
          project_id: project.id,
        });
      }
      return Response.json({ items: [] });
    },
    "/v1/conversations/conversation-created/snapshot": () =>
      Response.json(
        snapshotFor({
          ...conversation,
          id: "conversation-created",
          title: "Created conversation",
        }),
      ),
  });
  const view = render(<CommanderWorkbench projects={[]} csrf="csrf" />);

  view.rerender(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  await screen.findByRole("option", { name: "commander" });
  expect(screen.getByText(/项目一 \/ COMMANDER HUB/)).toBeTruthy();
  expect(sessionStorage.getItem("karajan:commander-project")).toBe(project.id);

  await userEvent.click(
    screen.getByRole("button", { name: "与 Commander 开始" }),
  );
  await vi.waitFor(() => expect(createRequests).toHaveLength(1));
  expect(createRequests[0]).toEqual({
    url: "/v1/projects/project-one/conversations",
    body: {
      title: "新的 Commander 会话",
      commander_profile_ref: null,
      commander_source_ref: null,
    },
  });
});

it("rejects a stale saved project before it can open another project's saved conversation", async () => {
  const snapshots: string[] = [];
  sessionStorage.setItem("karajan:commander-project", "removed-project");
  sessionStorage.setItem(
    "karajan:commander-conversation:removed-project",
    "removed-conversation",
  );
  sessionStorage.setItem(
    `karajan:commander-conversation:${projectTwo.id}`,
    conversationTwo.id,
  );
  sessionStorage.setItem(
    `karajan:commander-draft:${project.id}:${conversation.id}`,
    JSON.stringify({
      content: "local first-project draft",
      selection: null,
      dirty: true,
      editVersion: 1,
      selectionVersion: 0,
      selectionDirty: false,
      acknowledgedRevision: 0,
    }),
  );
  installFetch({
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/projects/project-two/conversations": () =>
      Response.json({ items: [conversationTwo] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots.push("one");
      return Response.json(
        snapshotFor(conversation, {
          draft: { content: "server first-project draft", revision: 2 },
        }),
      );
    },
    "/v1/conversations/conversation-two/snapshot": () => {
      snapshots.push("two");
      return Response.json(snapshotFor(conversationTwo));
    },
  });
  const view = render(<CommanderWorkbench projects={[]} csrf="csrf" />);

  view.rerender(
    <CommanderWorkbench projects={[project, projectTwo]} csrf="csrf" />,
  );
  await screen.findByRole("heading", { name: "Commander" });
  const input = screen.getByRole("textbox", { name: "消息草稿" });
  await vi.waitFor(() =>
    expect((input as HTMLTextAreaElement).value).toBe(
      "local first-project draft",
    ),
  );
  expect(sessionStorage.getItem("karajan:commander-project")).toBe(project.id);
  expect(snapshots).toEqual(["one"]);
});

it("restores a delayed valid saved project and preserves its local draft without opening a sibling conversation", async () => {
  const snapshots: string[] = [];
  sessionStorage.setItem("karajan:commander-project", projectTwo.id);
  sessionStorage.setItem(
    `karajan:commander-conversation:${projectTwo.id}`,
    conversationTwo.id,
  );
  sessionStorage.setItem(
    `karajan:commander-draft:${projectTwo.id}:${conversationTwo.id}`,
    JSON.stringify({
      content: "local delayed-project draft",
      selection: null,
      dirty: true,
      editVersion: 1,
      selectionVersion: 0,
      selectionDirty: false,
      acknowledgedRevision: 1,
    }),
  );
  installFetch({
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation] }),
    "/v1/projects/project-two/conversations": () =>
      Response.json({ items: [conversationTwo] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots.push("one");
      return Response.json(snapshotFor(conversation));
    },
    "/v1/conversations/conversation-two/snapshot": () => {
      snapshots.push("two");
      return Response.json(
        snapshotFor(conversationTwo, {
          draft: { content: "server delayed-project draft", revision: 2 },
        }),
      );
    },
  });
  const view = render(<CommanderWorkbench projects={[]} csrf="csrf" />);

  view.rerender(
    <CommanderWorkbench projects={[project, projectTwo]} csrf="csrf" />,
  );
  await screen.findByRole("heading", { name: "Commander two" });
  const input = screen.getByRole("textbox", { name: "消息草稿" });
  await vi.waitFor(() =>
    expect((input as HTMLTextAreaElement).value).toBe(
      "local delayed-project draft",
    ),
  );
  expect(sessionStorage.getItem("karajan:commander-project")).toBe(
    projectTwo.id,
  );
  expect(snapshots).toEqual(["two"]);
});

async function assertOriginatingClearSurvivesNavigation(
  kind: "message" | "task",
  preservesNewerText: boolean,
) {
  const conversationB = {
    ...conversation,
    id: "conversation-b",
    title: "Conversation B",
  };
  const result = deferred<Response>();
  let serverDraft = "X";
  let serverRevision = 1;
  let taskCreated = false;
  let snapshots = 0;
  installFetch({
    "/v1/projects/project-one/commander-options": () =>
      Response.json({ items: [] }),
    "/v1/projects/project-one/conversations": () =>
      Response.json({ items: [conversation, conversationB] }),
    "/v1/conversations/conversation-one/snapshot": () => {
      snapshots += 1;
      return Response.json(
        snapshotFor(conversation, {
          draft: { content: serverDraft, revision: serverRevision },
          task_drafts: taskCreated
            ? [{ id: "task-created", requirement: "X", state: "draft" }]
            : [],
        }),
      );
    },
    "/v1/conversations/conversation-b/snapshot": () =>
      Response.json(snapshotFor(conversationB)),
    "/v1/conversations/conversation-one/draft": (_input, init) => {
      serverDraft = (JSON.parse(String(init?.body)) as { content: string })
        .content;
      serverRevision += 1;
      return Response.json({ revision: serverRevision });
    },
    "/v1/conversations/conversation-one/messages": () => result.promise,
    "/v1/conversations/conversation-one/task-drafts": () => result.promise,
  });
  sessionStorage.setItem("karajan:commander-project", project.id);
  sessionStorage.setItem(
    `karajan:commander-conversation:${project.id}`,
    conversation.id,
  );
  render(<CommanderWorkbench projects={[project]} csrf="csrf" />);
  const input = await screen.findByRole("textbox", { name: "消息草稿" });
  expect(snapshots).toBeGreaterThan(0);
  await vi.waitFor(() =>
    expect((input as HTMLTextAreaElement).value).toBe("X"),
  );
  await userEvent.click(
    screen.getByRole("button", {
      name: kind === "message" ? "发送给 Commander" : "＋ 新任务草稿",
    }),
  );
  if (preservesNewerText) await userEvent.type(input, "Y");

  await userEvent.click(screen.getByRole("button", { name: "›项目一" }));
  await userEvent.click(
    await screen.findByRole("button", { name: /Conversation B/ }),
  );
  await screen.findByRole("textbox", { name: "消息草稿" });
  taskCreated = kind === "task";
  await act(async () =>
    result.resolve(
      Response.json(
        kind === "message"
          ? { id: "message-created", conversation_id: conversation.id }
          : {
              id: "task-created",
              requirement: "X",
              state: "draft",
              conversation_id: conversation.id,
            },
      ),
    ),
  );
  await vi.waitFor(() =>
    expect(serverDraft).toBe(preservesNewerText ? "XY" : ""),
  );

  await userEvent.click(screen.getByRole("button", { name: /^◌ Commander$/ }));
  await screen.findByRole("heading", { name: "Commander" });
  const restored = screen.getByRole("textbox", { name: "消息草稿" });
  expect((restored as HTMLTextAreaElement).value).toBe(
    preservesNewerText ? "XY" : "",
  );
  if (kind === "task") expect(screen.getByText("X")).toBeTruthy();
}

it("clears the originating message draft after A to B to A navigation", async () => {
  await assertOriginatingClearSurvivesNavigation("message", false);
});

it("keeps newer message text after A to B to A navigation", async () => {
  await assertOriginatingClearSurvivesNavigation("message", true);
});

it("clears the originating task draft after A to B to A navigation and shows its saved goal", async () => {
  await assertOriginatingClearSurvivesNavigation("task", false);
});

it("keeps newer task-composer text after A to B to A navigation", async () => {
  await assertOriginatingClearSurvivesNavigation("task", true);
});
