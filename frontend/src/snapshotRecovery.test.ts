import { expect, it, vi } from "vitest";
import { SnapshotRecovery, type RecoveryContext } from "./snapshotRecovery";

const context: RecoveryContext = {
  navigation: 1,
  projectId: "project-a",
  conversationId: "conversation-a",
};

it("retries a failed recovery without changing navigation and resumes later snapshots", async () => {
  vi.useFakeTimers();
  let reads = 0;
  const snapshots: string[] = [];
  const errors: string[] = [];
  const recovery = new SnapshotRecovery(
    async () => {
      reads += 1;
      if (reads === 1) throw new Error("temporary");
      return "watermark-9";
    },
    (_context, snapshot) => snapshots.push(snapshot),
    (_context, error) => errors.push(error.message),
  );

  recovery.navigate(context);
  await vi.runAllTimersAsync();
  expect(errors).toEqual(["temporary"]);
  expect(snapshots).toEqual(["watermark-9"]);
  vi.useRealTimers();
});

it("cancels an obsolete navigation without delivering its snapshot", async () => {
  let resolveFirst!: (snapshot: string) => void;
  let reads = 0;
  const snapshots: string[] = [];
  const recovery = new SnapshotRecovery(
    (_context, signal) => {
      reads += 1;
      if (reads > 1) return new Promise<string>(() => undefined);
      return new Promise<string>((resolve, reject) => {
        signal.addEventListener("abort", () =>
          reject(new DOMException("", "AbortError")),
        );
        resolveFirst = resolve;
      });
    },
    (_context, snapshot) => snapshots.push(snapshot),
    () => undefined,
  );

  recovery.navigate(context);
  recovery.navigate({
    ...context,
    navigation: 2,
    conversationId: "conversation-b",
  });
  resolveFirst("obsolete");
  await Promise.resolve();
  expect(snapshots).toEqual([]);
});
