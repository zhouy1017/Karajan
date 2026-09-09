import type { DraftSelection } from "./conversationDraft";
import { adaptSnapshot } from "./conversationSnapshot";
import {
  candidateSelections as flatCandidateSelections,
  currentAttemptId as flatCurrentAttemptId,
  feedbackFromSnapshot as flatFeedbackFromSnapshot,
  relatedEvidence as flatRelatedEvidence,
  type FeedbackFact,
} from "./snapshotFacts";

export type { FeedbackFact };

/** @deprecated Import from snapshotFacts after adapting at the HTTP seam. */
export function candidateSelections(snapshot: unknown) {
  return flatCandidateSelections(adaptSnapshot(snapshot));
}

/** @deprecated Import from snapshotFacts after adapting at the HTTP seam. */
export function currentAttemptId(snapshot: unknown, selection: DraftSelection) {
  return flatCurrentAttemptId(adaptSnapshot(snapshot), selection);
}

/** @deprecated Import from snapshotFacts after adapting at the HTTP seam. */
export function feedbackFromSnapshot(snapshot: unknown): FeedbackFact {
  return flatFeedbackFromSnapshot(adaptSnapshot(snapshot));
}

/** @deprecated The snapshot argument is intentionally no longer traversed. */
export function relatedEvidence(
  _snapshot: unknown,
  fact: unknown,
  selection: DraftSelection,
): unknown {
  return flatRelatedEvidence(adaptSnapshot(_snapshot), fact, selection);
}
