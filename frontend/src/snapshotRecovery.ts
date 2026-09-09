/**
 * SnapshotRecovery owns retry and cancellation for one active conversation.
 * Navigation creates a new lifecycle; ordinary recovery never changes that
 * navigation identity and always re-snapshots before a stream is reopened.
 */
export type RecoveryContext = {
  navigation: number;
  projectId: string;
  conversationId: string;
};

type Timer = ReturnType<typeof setTimeout>;

function sameContext(left: RecoveryContext, right: RecoveryContext): boolean {
  return (
    left.navigation === right.navigation &&
    left.projectId === right.projectId &&
    left.conversationId === right.conversationId
  );
}

export class SnapshotRecovery<T> {
  private active?: RecoveryContext;
  private timer?: Timer;
  private request?: AbortController;
  private attempts = 0;
  private running = false;
  private runId = 0;

  constructor(
    private readonly read: (
      context: RecoveryContext,
      signal: AbortSignal,
    ) => Promise<T>,
    private readonly onSnapshot: (
      context: RecoveryContext,
      snapshot: T,
    ) => void,
    private readonly onError: (context: RecoveryContext, error: Error) => void,
    private readonly schedule: (run: () => void, delayMs: number) => Timer = (
      run,
      delayMs,
    ) => setTimeout(run, delayMs),
    private readonly cancelTimer: (timer: Timer) => void = clearTimeout,
  ) {}

  navigate(context: RecoveryContext): void {
    this.cancel();
    this.active = { ...context };
    this.run(context);
  }

  recover(context: RecoveryContext): void {
    if (!this.active || !sameContext(this.active, context)) return;
    this.cancelTimerIfNeeded();
    if (!this.running) this.run(context);
  }

  cancel(): void {
    this.cancelTimerIfNeeded();
    this.request?.abort();
    this.request = undefined;
    this.runId += 1;
    this.running = false;
    this.active = undefined;
    this.attempts = 0;
  }

  private run(context: RecoveryContext): void {
    if (!this.active || !sameContext(this.active, context) || this.running)
      return;
    this.running = true;
    const runId = ++this.runId;
    const request = new AbortController();
    this.request = request;
    void this.read(context, request.signal)
      .then((snapshot) => {
        if (!this.active || !sameContext(this.active, context)) return;
        this.attempts = 0;
        this.onSnapshot(context, snapshot);
      })
      .catch((cause) => {
        if (!this.active || !sameContext(this.active, context)) return;
        const error =
          cause instanceof Error ? cause : new Error("无法读取会话快照。");
        this.onError(context, error);
        const delay = Math.min(1_000 * 2 ** this.attempts, 8_000);
        this.attempts += 1;
        this.timer = this.schedule(() => {
          this.timer = undefined;
          this.run(context);
        }, delay);
      })
      .finally(() => {
        if (this.request === request) this.request = undefined;
        if (this.runId === runId) this.running = false;
      });
  }

  private cancelTimerIfNeeded(): void {
    if (this.timer !== undefined) this.cancelTimer(this.timer);
    this.timer = undefined;
  }
}
