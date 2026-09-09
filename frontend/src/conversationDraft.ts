/**
 * The draft ledger is the single owner of local, not-yet-acknowledged
 * conversation input.  Its interface deliberately distinguishes an edit from
 * a server acknowledgement so a late command can never replace a newer edit.
 */
export type DraftContext = { projectId: string; conversationId: string };
export type DraftSelection = {
  kind: "task" | "attempt" | "candidate";
  id: string;
} | null;
export type DraftState = {
  content: string;
  selection: DraftSelection;
  dirty: boolean;
  editVersion: number;
  acknowledgedRevision: number;
  error?: string;
};
export type DraftCommand = {
  context: DraftContext;
  content: string;
  selection: DraftSelection;
  baseRevision: number;
  editVersion: number;
  id: string;
  body: {
    content: string;
    selected_task_id: string | null;
    base_plan_revision: null;
  };
};

export type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

type StoredDraft = Omit<DraftState, "acknowledgedRevision"> & {
  acknowledgedRevision?: number;
  pending?: DraftCommand;
};

function key(context: DraftContext): string {
  return `karajan:commander-draft:${context.projectId}:${context.conversationId}`;
}

export class ConversationDraftLedger {
  private states = new Map<string, DraftState>();
  private pending = new Map<string, DraftCommand>();

  constructor(
    private readonly storage: DraftStorage,
    private readonly newId: () => string,
  ) {}

  open(
    context: DraftContext,
    server: { content: string; revision: number; selection: DraftSelection },
  ): DraftState {
    const current = this.states.get(key(context));
    const stored = this.readStored(context);
    const storedState = stored
      ? {
          content: stored.content,
          selection: stored.selection,
          dirty: stored.dirty,
          editVersion: stored.editVersion,
          acknowledgedRevision: stored.acknowledgedRevision ?? 0,
          error: stored.error,
        }
      : undefined;
    const pending = current?.dirty
      ? current
      : storedState?.dirty
        ? storedState
        : undefined;
    const state: DraftState = pending
      ? {
          ...pending,
          acknowledgedRevision: server.revision,
        }
      : {
          content: server.content,
          selection: server.selection,
          dirty: false,
          editVersion: current?.editVersion ?? stored?.editVersion ?? 0,
          acknowledgedRevision: server.revision,
        };
    this.states.set(key(context), state);
    const pendingCommand =
      this.pending.get(key(context)) ??
      (this.validCommand(stored?.pending, context)
        ? stored?.pending
        : undefined);
    if (state.dirty && pendingCommand)
      this.pending.set(key(context), pendingCommand);
    else if (!state.dirty) this.pending.delete(key(context));
    if (state.dirty) this.persist(context, state);
    return state;
  }

  current(context: DraftContext): DraftState | undefined {
    return this.states.get(key(context));
  }

  edit(
    context: DraftContext,
    content: string,
    selection: DraftSelection,
  ): DraftState {
    const prior = this.states.get(key(context)) ?? {
      content: "",
      selection: null,
      dirty: false,
      editVersion: 0,
      acknowledgedRevision: 0,
    };
    const next: DraftState = {
      ...prior,
      content,
      selection,
      dirty: true,
      editVersion: prior.editVersion + 1,
      error: undefined,
    };
    this.states.set(key(context), next);
    this.persist(context, next);
    return next;
  }

  prepareSave(context: DraftContext): DraftCommand | undefined {
    const state = this.states.get(key(context));
    if (!state?.dirty) return undefined;
    const existing = this.pending.get(key(context));
    // An unresolved command must be retried before a later edit can use its
    // successor revision.  The caller may then prepare the newer edit.
    if (existing) return existing;
    const command: DraftCommand = {
      context: { ...context },
      content: state.content,
      selection: state.selection,
      baseRevision: state.acknowledgedRevision,
      editVersion: state.editVersion,
      id: this.newId(),
      body: {
        content: state.content,
        selected_task_id: state.selection?.id ?? null,
        base_plan_revision: null,
      },
    };
    this.pending.set(key(context), command);
    this.persist(context, state);
    return command;
  }

  acknowledgeSave(
    command: DraftCommand,
    revision: number,
  ): DraftState | undefined {
    const state = this.states.get(key(command.context));
    if (!state) return state;
    if (this.pending.get(key(command.context))?.id === command.id)
      this.pending.delete(key(command.context));
    const next: DraftState = {
      ...state,
      acknowledgedRevision: revision,
      ...(state.editVersion === command.editVersion
        ? { dirty: false, error: undefined }
        : {}),
    };
    this.states.set(key(command.context), next);
    if (next.dirty) this.persist(command.context, next);
    else this.storage.removeItem(key(command.context));
    return next;
  }

  rejectSave(command: DraftCommand, error: string): DraftState | undefined {
    const state = this.states.get(key(command.context));
    if (!state) return undefined;
    // Keep the exact unresolved command for a retry, but never replace a later
    // edit or its persisted metadata with the failed command's payload.
    const next: DraftState =
      state.editVersion === command.editVersion
        ? { ...state, dirty: true, error }
        : state;
    this.states.set(key(command.context), next);
    if (next.dirty) this.persist(command.context, next);
    return next;
  }

  clearSubmitted(
    context: DraftContext,
    editVersion: number,
  ): DraftState | undefined {
    const state = this.states.get(key(context));
    if (!state || state.editVersion !== editVersion) return state;
    const next: DraftState = {
      ...state,
      content: "",
      selection: null,
      dirty: false,
      error: undefined,
    };
    this.states.set(key(context), next);
    this.pending.delete(key(context));
    this.storage.removeItem(key(context));
    return next;
  }

  private readStored(context: DraftContext): StoredDraft | undefined {
    const raw = this.storage.getItem(key(context));
    if (!raw) return undefined;
    try {
      const value = JSON.parse(raw) as StoredDraft;
      if (
        typeof value.content !== "string" ||
        typeof value.editVersion !== "number" ||
        value.dirty !== true
      )
        return undefined;
      return value;
    } catch {
      return undefined;
    }
  }

  private persist(context: DraftContext, state: DraftState): void {
    this.storage.setItem(
      key(context),
      JSON.stringify({
        content: state.content,
        selection: state.selection,
        dirty: true,
        editVersion: state.editVersion,
        acknowledgedRevision: state.acknowledgedRevision,
        error: state.error,
        pending: this.pending.get(key(context)),
      }),
    );
  }

  private validCommand(
    value: unknown,
    context: DraftContext,
  ): value is DraftCommand {
    if (typeof value !== "object" || value === null) return false;
    const command = value as Partial<DraftCommand>;
    return (
      command.context?.projectId === context.projectId &&
      command.context?.conversationId === context.conversationId &&
      typeof command.content === "string" &&
      typeof command.baseRevision === "number" &&
      typeof command.editVersion === "number" &&
      typeof command.id === "string" &&
      command.body?.content === command.content &&
      (typeof command.body.selected_task_id === "string" ||
        command.body.selected_task_id === null) &&
      command.body.base_plan_revision === null
    );
  }
}
