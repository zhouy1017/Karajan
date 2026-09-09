import { expect, it } from "vitest";
import { CommandRegistry, type CommandStorage } from "./commandRegistry";

function storage(): CommandStorage {
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

it("keeps a lost response command stable until its body is validated", () => {
  let id = 0;
  const registry = new CommandRegistry(() => `command-${++id}`);
  const first = registry.prepare("conversation", "project-a", { title: "new" });
  // A response body failure calls reject: the exact key and payload retry.
  registry.reject(first);
  expect(registry.prepare("conversation", "project-a", { title: "new" })).toBe(
    first,
  );

  registry.complete(first);
  expect(
    registry.prepare("conversation", "project-a", { title: "new" }).id,
  ).not.toBe(first.id);
});

it("restores an unresolved command after refresh without treating it as complete", () => {
  const persisted = storage();
  const first = new CommandRegistry(() => "command-x", persisted);
  const command = first.prepare("message", "conversation-a", {
    content: "retry",
  });
  first.reject(command);

  const refreshed = new CommandRegistry(() => "command-y", persisted);
  expect(
    refreshed.prepare("message", "conversation-a", { content: "retry" }),
  ).toEqual(command);
});
