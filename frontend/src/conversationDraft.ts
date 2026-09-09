/**
 * The draft ledger is the single owner of local, not-yet-acknowledged
 * conversation input.  Its interface deliberately distinguishes an edit from
 * a server acknowledgement so a late command can never replace a newer edit.
 */
export type DraftContext = { projectId: string; conversationId: string };
export type DraftSelection = {
  kind: "task" | "attempt" | "candidate";
  id: string;
  version?: number;
  head?: string;
} | null;
export type DraftState = {
  content: string;
  selection: DraftSelection;
  /** True only for unacknowledged content. */
  dirty: boolean;
  /** Monotonic version of content edits. */
  editVersion: number;
  /** Monotonic version of selection changes. */
  selectionVersion: number;
  /** A supported selection still needs a server acknowledgement. */
  selectionDirty: boolean;
  acknowledgedRevision: number;
  /** Candidate and execution-Attempt IDs are not accepted by the endpoint. */
  selectionLocalOnly?: boolean;
  error?: string;
};
export type DraftCommand = {
  context: DraftContext;
  content: string;
  selection: DraftSelection;
  baseRevision: number;
  editVersion: number;
  selectionVersion: number;
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
  selectionVersion?: number;
  selectionDirty?: boolean;
  pending?: DraftCommand;
};

function key(context: DraftContext): string {
  return `karajan:commander-draft:${context.projectId}:${context.conversationId}`;
}

function sameSelection(left: DraftSelection, right: DraftSelection): boolean {
  return (
    left?.kind === right?.kind &&
    left?.id === right?.id &&
    left?.version === right?.version &&
    left?.head === right?.head
  );
}

function requiresSave(state: DraftState): boolean {
  return state.dirty || state.selectionDirty;
}

function requiresPersistence(state: DraftState): boolean {
  return requiresSave(state) || state.selectionLocalOnly === true;
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
    const stateKey = key(context);
    const current = this.states.get(stateKey);
    const stored = this.readStored(context);
    const storedState = stored
      ? {
          content: stored.content,
          selection: stored.selection,
          dirty: stored.dirty,
          editVersion: stored.editVersion,
          selectionVersion: stored.selectionVersion ?? 0,
          selectionDirty: stored.selectionDirty ?? false,
          acknowledgedRevision: stored.acknowledgedRevision ?? 0,
          selectionLocalOnly: stored.selectionLocalOnly,
          error: stored.error,
        }
      : undefined;
    const contentSource = current?.dirty
      ? current
      : storedState?.dirty
        ? storedState
        : undefined;
    const localSelectionSource = current?.selectionLocalOnly
      ? current
      : storedState?.selectionLocalOnly
        ? storedState
        : undefined;
    const selectionSource = current?.selectionDirty
      ? current
      : storedState?.selectionDirty
        ? storedState
        : undefined;
    const snapshotIsOlder =
      !contentSource &&
      !selectionSource &&
      current !== undefined &&
      current.acknowledgedRevision > server.revision;
    const selectionOwner = localSelectionSource ?? selectionSource;
    const state: DraftState = {
      content: contentSource
        ? contentSource.content
        : snapshotIsOlder
          ? current.content
          : server.content,
      selection: selectionOwner
        ? selectionOwner.selection
        : snapshotIsOlder
          ? current.selection
          : server.selection,
      dirty: contentSource?.dirty ?? false,
      editVersion:
        contentSource?.editVersion ??
        current?.editVersion ??
        storedState?.editVersion ??
        0,
      selectionVersion:
        selectionOwner?.selectionVersion ??
        current?.selectionVersion ??
        storedState?.selectionVersion ??
        0,
      selectionDirty: selectionSource?.selectionDirty ?? false,
      selectionLocalOnly: localSelectionSource ? true : undefined,
      acknowledgedRevision: Math.max(
        current?.acknowledgedRevision ?? 0,
        storedState?.acknowledgedRevision ?? 0,
        server.revision,
      ),
      error: contentSource?.error ?? selectionOwner?.error,
    };
    this.states.set(stateKey, state);
    const pendingCommand =
      this.pending.get(stateKey) ??
      (this.validCommand(stored?.pending, context)
        ? stored?.pending
        : undefined);
    if (requiresSave(state) && pendingCommand)
      this.pending.set(stateKey, pendingCommand);
    else if (!requiresSave(state)) this.pending.delete(stateKey);
    if (requiresPersistence(state)) this.persist(context, state);
    else this.storage.removeItem(stateKey);
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
      selectionVersion: 0,
      selectionDirty: false,
      acknowledgedRevision: 0,
    };
    const contentChanged = content !== prior.content;
    const selectionChanged = !sameSelection(selection, prior.selection);
    // Typing while a local-only selection is active must not teach that
    // identity to the server. Choosing another identity does.
    const selectionLocalOnly =
      prior.selectionLocalOnly && !selectionChanged ? true : undefined;
    const next: DraftState = {
      ...prior,
      content,
      selection,
      dirty: prior.dirty || contentChanged,
      editVersion: contentChanged ? prior.editVersion + 1 : prior.editVersion,
      selectionVersion: selectionChanged
        ? prior.selectionVersion + 1
        : prior.selectionVersion,
      selectionDirty: selectionLocalOnly
        ? false
        : prior.selectionDirty || selectionChanged,
      selectionLocalOnly,
      error: undefined,
    };
    this.states.set(key(context), next);
    this.persist(context, next);
    return next;
  }

  /** Keep an unsupported selection locally without issuing a bad API write. */
  selectLocalSelection(
    context: DraftContext,
    selection: Exclude<DraftSelection, null>,
  ): DraftState {
    const prior = this.states.get(key(context)) ?? {
      content: "",
      selection: null,
      dirty: false,
      editVersion: 0,
      selectionVersion: 0,
      selectionDirty: false,
      acknowledgedRevision: 0,
    };
    const selectionChanged = !sameSelection(selection, prior.selection);
    const next: DraftState = {
      ...prior,
      selection,
      selectionLocalOnly: true,
      selectionVersion: selectionChanged
        ? prior.selectionVersion + 1
        : prior.selectionVersion,
      selectionDirty: false,
      error: undefined,
    };
    this.states.set(key(context), next);
    this.persist(context, next);
    return next;
  }

  /** Candidate IDs are not accepted by the current backend draft endpoint. */
  selectCandidate(
    context: DraftContext,
    id: string,
    version?: number,
    head?: string,
  ): DraftState {
    return this.selectLocalSelection(context, {
      kind: "candidate",
      id,
      version,
      head,
    });
  }

  prepareSave(context: DraftContext): DraftCommand | undefined {
    const state = this.states.get(key(context));
    if (!state || !requiresSave(state)) return undefined;
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
      selectionVersion: state.selectionVersion,
      id: this.newId(),
      body: {
        content: state.content,
        selected_task_id: state.selectionLocalOnly
          ? null
          : (state.selection?.id ?? null),
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
    const stateKey = key(command.context);
    const state = this.states.get(stateKey);
    if (!state) return state;
    if (this.pending.get(stateKey)?.id === command.id)
      this.pending.delete(stateKey);
    const contentAcknowledged = state.editVersion === command.editVersion;
    const selectionAcknowledged =
      !state.selectionLocalOnly &&
      state.selectionVersion === command.selectionVersion;
    const next: DraftState = {
      ...state,
      acknowledgedRevision: revision,
      dirty: contentAcknowledged ? false : state.dirty,
      selectionDirty: selectionAcknowledged ? false : state.selectionDirty,
      error:
        contentAcknowledged || selectionAcknowledged ? undefined : state.error,
    };
    this.states.set(stateKey, next);
    if (requiresPersistence(next)) this.persist(command.context, next);
    else this.storage.removeItem(stateKey);
    return next;
  }

  rejectSave(command: DraftCommand, error: string): DraftState | undefined {
    const state = this.states.get(key(command.context));
    if (!state) return undefined;
    // Keep the exact unresolved command for a retry, but never replace a
    // later edit or its persisted metadata with the failed command's payload.
    const next: DraftState =
      state.editVersion === command.editVersion ||
      state.selectionVersion === command.selectionVersion
        ? { ...state, error }
        : state;
    this.states.set(key(command.context), next);
    if (requiresPersistence(next)) this.persist(command.context, next);
    return next;
  }

  /** A durable revision conflict is not an uncertain delivery: retire its key. */
  reconcileConflict(
    command: DraftCommand,
    revision: number | undefined,
  ): DraftState | undefined {
    const state = this.states.get(key(command.context));
    if (!state) return undefined;
    if (this.pending.get(key(command.context))?.id === command.id)
      this.pending.delete(key(command.context));
    const next: DraftState = {
      ...state,
      acknowledgedRevision:
        typeof revision === "number"
          ? Math.max(state.acknowledgedRevision, revision)
          : state.acknowledgedRevision,
      error: "DRAFT_REVISION_CONFLICT",
    };
    this.states.set(key(command.context), next);
    this.persist(command.context, next);
    return next;
  }

  /** Retire a known bad selection so a corrected successor gets a new key. */
  rejectDefinitive(
    command: DraftCommand,
    error: string,
    revision?: number,
  ): DraftState | undefined {
    const stateKey = key(command.context);
    const state = this.states.get(stateKey);
    if (!state) return undefined;
    if (this.pending.get(stateKey)?.id === command.id)
      this.pending.delete(stateKey);
    const rejectedSelection =
      !state.selectionLocalOnly &&
      state.selectionVersion === command.selectionVersion;
    const next: DraftState = {
      ...state,
      selection: rejectedSelection ? null : state.selection,
      selectionDirty: rejectedSelection ? false : state.selectionDirty,
      selectionLocalOnly: rejectedSelection
        ? undefined
        : state.selectionLocalOnly,
      acknowledgedRevision:
        typeof revision === "number"
          ? Math.max(state.acknowledgedRevision, revision)
          : state.acknowledgedRevision,
      error,
    };
    this.states.set(stateKey, next);
    if (requiresPersistence(next)) this.persist(command.context, next);
    else this.storage.removeItem(stateKey);
    return next;
  }

  clearSubmitted(
    context: DraftContext,
    editVersion: number,
  ): DraftState | undefined {
    const state = this.states.get(key(context));
    // Selection changes do not protect submitted text. Only a newer content
    // edit is allowed to keep it after a successful message/task command.
    if (!state || state.editVersion !== editVersion) return state;
    const next: DraftState = {
      ...state,
      content: "",
      // The server's message/task write does not mutate its draft resource.
      // Make an explicit successor draft command instead of claiming it cleared.
      dirty: true,
      editVersion: state.editVersion + 1,
      error: undefined,
    };
    this.states.set(key(context), next);
    this.persist(context, next);
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
        (value.dirty !== true &&
          value.selectionDirty !== true &&
          value.selectionLocalOnly !== true)
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
        dirty: state.dirty,
        editVersion: state.editVersion,
        selectionVersion: state.selectionVersion,
        selectionDirty: state.selectionDirty,
        acknowledgedRevision: state.acknowledgedRevision,
        selectionLocalOnly: state.selectionLocalOnly,
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
      typeof command.selectionVersion === "number" &&
      typeof command.id === "string" &&
      command.body?.content === command.content &&
      (typeof command.body.selected_task_id === "string" ||
        command.body.selected_task_id === null) &&
      command.body.base_plan_revision === null
    );
  }
}
