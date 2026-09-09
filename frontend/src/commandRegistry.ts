/**
 * Keeps an idempotency key alive until the caller has completely consumed and
 * validated the response.  Callers only need prepare/complete/reject; retry
 * identity and payload equality are implementation details.
 */
export type PreparedCommand<T> = {
  kind: string;
  target: string;
  payload: T;
  revision?: number;
  id: string;
};

export type CommandStorage = Pick<
  Storage,
  "getItem" | "setItem" | "removeItem"
>;

const storageKey = "karajan:commander-unresolved-commands";

function identity(
  kind: string,
  target: string,
  payload: unknown,
  revision?: number,
) {
  return `${kind}:${target}:${revision ?? ""}:${JSON.stringify(payload)}`;
}

export class CommandRegistry {
  private unresolved = new Map<string, PreparedCommand<unknown>>();

  constructor(
    private readonly newId: () => string,
    private readonly storage?: CommandStorage,
  ) {
    this.restore();
  }

  prepare<T>(
    kind: string,
    target: string,
    payload: T,
    revision?: number,
  ): PreparedCommand<T> {
    const key = identity(kind, target, payload, revision);
    const existing = this.unresolved.get(key) as PreparedCommand<T> | undefined;
    if (existing) return existing;
    const command: PreparedCommand<T> = {
      kind,
      target,
      payload,
      revision,
      id: this.newId(),
    };
    this.unresolved.set(key, command);
    this.persist();
    return command;
  }

  complete(command: PreparedCommand<unknown>): void {
    const key = identity(
      command.kind,
      command.target,
      command.payload,
      command.revision,
    );
    if (this.unresolved.get(key)?.id === command.id) {
      this.unresolved.delete(key);
      this.persist();
    }
  }

  reject(_command: PreparedCommand<unknown>): void {
    // A rejected or body-incomplete operation deliberately remains unresolved
    // so the next explicit retry is byte-for-byte the same command.
    this.persist();
  }

  private restore(): void {
    if (!this.storage) return;
    try {
      const values = JSON.parse(this.storage.getItem(storageKey) ?? "[]");
      if (!Array.isArray(values)) return;
      for (const value of values) {
        if (!this.validCommand(value)) continue;
        this.unresolved.set(
          identity(value.kind, value.target, value.payload, value.revision),
          value,
        );
      }
    } catch {
      // A malformed local retry record must not be sent or replaced silently.
    }
  }

  private persist(): void {
    if (!this.storage) return;
    if (!this.unresolved.size) {
      this.storage.removeItem(storageKey);
      return;
    }
    this.storage.setItem(
      storageKey,
      JSON.stringify(Array.from(this.unresolved.values())),
    );
  }

  private validCommand(value: unknown): value is PreparedCommand<unknown> {
    if (typeof value !== "object" || value === null) return false;
    const command = value as Partial<PreparedCommand<unknown>>;
    return (
      typeof command.kind === "string" &&
      typeof command.target === "string" &&
      typeof command.id === "string" &&
      (command.revision === undefined ||
        typeof command.revision === "number") &&
      "payload" in command
    );
  }
}
