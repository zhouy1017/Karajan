import { expect, it } from "vitest";
import {
  ConversationDraftLedger,
  type DraftStorage,
} from "./conversationDraft";

function storage(): DraftStorage {
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

const context = { projectId: "project-a", conversationId: "conversation-a" };

it("never lets an older save acknowledgement replace a newer draft edit", () => {
  const ledger = new ConversationDraftLedger(storage(), () => "save-x");
  ledger.open(context, { content: "server", revision: 3, selection: null });
  ledger.edit(context, "X", { kind: "task", id: "task-x" });
  const command = ledger.prepareSave(context)!;
  ledger.edit(context, "Y", { kind: "attempt", id: "attempt-y" });

  const after = ledger.acknowledgeSave(command, 4)!;
  expect(after).toMatchObject({
    content: "Y",
    selection: { kind: "attempt", id: "attempt-y" },
    dirty: true,
    acknowledgedRevision: 4,
  });
});

it("keeps a newer edit through an older failed save and a recovered snapshot", () => {
  const persisted = storage();
  const ledger = new ConversationDraftLedger(persisted, () => "save-x");
  ledger.open(context, { content: "server", revision: 3, selection: null });
  ledger.edit(context, "X", { kind: "task", id: "task-x" });
  const command = ledger.prepareSave(context)!;
  ledger.edit(context, "Y", { kind: "attempt", id: "attempt-y" });
  ledger.rejectSave(command, "TEMPORARY");

  const refreshed = new ConversationDraftLedger(persisted, () => "save-y");
  expect(
    refreshed.open(context, {
      content: "old server",
      revision: 3,
      selection: null,
    }),
  ).toMatchObject({
    content: "Y",
    selection: { kind: "attempt", id: "attempt-y" },
    dirty: true,
  });
});

it("persists a dirty edit and selection through refresh after a failed save", () => {
  const persisted = storage();
  const first = new ConversationDraftLedger(persisted, () => "save-x");
  first.open(context, { content: "old", revision: 1, selection: null });
  first.edit(context, "pending local", { kind: "task", id: "task-a" });
  const command = first.prepareSave(context)!;
  first.rejectSave(command, "TEMPORARY");

  const refreshed = new ConversationDraftLedger(persisted, () => "save-y");
  expect(
    refreshed.open(context, {
      content: "old server",
      revision: 1,
      selection: null,
    }),
  ).toMatchObject({
    content: "pending local",
    selection: { kind: "task", id: "task-a" },
    dirty: true,
    error: "TEMPORARY",
  });
});

it("retries a failed save after refresh with its original identity and revision", () => {
  const persisted = storage();
  const first = new ConversationDraftLedger(persisted, () => "save-x");
  first.open(context, { content: "old", revision: 7, selection: null });
  first.edit(context, "pending", { kind: "task", id: "task-a" });
  const command = first.prepareSave(context)!;
  first.rejectSave(command, "TEMPORARY");

  const refreshed = new ConversationDraftLedger(persisted, () => "save-y");
  refreshed.open(context, {
    content: "old server",
    revision: 7,
    selection: null,
  });
  expect(refreshed.prepareSave(context)).toEqual(command);
});

it("uses a new key and acknowledged revision for an A to B to A save sequence", () => {
  let id = 0;
  const ledger = new ConversationDraftLedger(storage(), () => `save-${++id}`);
  ledger.open(context, { content: "", revision: 0, selection: null });
  ledger.edit(context, "A", null);
  const firstA = ledger.prepareSave(context)!;
  ledger.acknowledgeSave(firstA, 1);
  ledger.edit(context, "B", null);
  const middleB = ledger.prepareSave(context)!;
  ledger.acknowledgeSave(middleB, 2);
  ledger.edit(context, "A", null);
  const secondA = ledger.prepareSave(context)!;

  expect([firstA.id, middleB.id, secondA.id]).toEqual([
    "save-1",
    "save-2",
    "save-3",
  ]);
  expect(secondA).toMatchObject({
    content: "A",
    baseRevision: 2,
    body: { content: "A" },
  });
});

it("clears only the exact submitted edit version", () => {
  const ledger = new ConversationDraftLedger(storage(), () => "save-x");
  ledger.open(context, { content: "", revision: 0, selection: null });
  const submitted = ledger.edit(context, "send X", null);
  ledger.edit(context, "send X and later Y", null);

  expect(ledger.clearSubmitted(context, submitted.editVersion)).toMatchObject({
    content: "send X and later Y",
    dirty: true,
  });
});
