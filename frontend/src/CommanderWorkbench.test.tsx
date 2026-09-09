import { cleanup, render, screen } from "@testing-library/react";
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
    Response | (() => Response | Promise<Response>)
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
      if (match) return typeof match[1] === "function" ? match[1]() : match[1];
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
