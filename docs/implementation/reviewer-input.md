# Reviewer input compiler

`compile_reviewer_input(admissions, candidates, *, run_id, operation_id,
principal, final_check_evidence_ids)` is the C-only handoff from current
controller records to a future read-only Reviewer consumer. It reads the Run
through `RunPlanner`, the original operation through the read-only operation
reader, and the current `karajan.candidate-validation-subject.v1` through the
existing Candidate CAS binding. Caller mappings cannot substitute an older
subject, approval, requirement, candidate, input, or policy. The compiler checks
the current workspace input digest and derives the current validation policy
from the approved execution policy, including an installed Reviewer binding,
before it asks CandidateStore for Evidence.

The pure internal assembler accepts the already-read subject and requirement
only as a seam for deterministic content tests; the public compiler always
obtains them from those trusted readers first. The byte budget is a compiler
limit and does not claim Go token, source, or execution qualification.

The compiler reloads the exact Candidate and baseline from CAS, verifies their
identity, and materializes both into a temporary sibling directory. It reads
only those CAS copies, then removes the directory before returning. The
candidate must have the same ordinary files and modes as its baseline. Added or
deleted files, mode changes, non-UTF-8/NUL content, missing or failed current
Check Evidence, and content larger than the 256 KiB compiler limit are explicit
unsupported or invalid results; no input is silently truncated. Returned files
and diff are restricted to approved workspace `read_paths`; an out-of-scope
changed file is an explicit scope rejection.

Check IDs are set identity at the interface and are emitted in the approved
policy order. Each required check must be the exact current Evidence record,
with matching Candidate ID, candidate input/policy, approved `argv` and
environment, check revision, effective `passed` status, and an available log.
The compiler does not require the overall gate to pass because a missing
Reviewer result keeps that gate pending.

The returned immutable `ReviewerInput` contains canonical UTF-8 JSON bytes,
their SHA-256 and size, the candidate ID/revision, the exact Check Evidence IDs,
and the file allowlist. The bytes carry the approved requirement, candidate
identity, unified diff, existing source files, and redacted Check result/log
summaries. No Actor, Attempt, qualification, authorization, Review Evidence,
model request, or delivery permission is created. Future consumer code must
perform current Run/approval/qualification checks and compile its own trusted
Review result fields.
