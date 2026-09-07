# Reviewer binding and Candidate validation subjects

Implementation: `1fc97849697cfe89a79595cba07e9ec028c6d0b2`.
Base: `3d47194147edf153a0a48a183b34ff7222d674d4`.
Scope: [#100](https://github.com/zhouy1017/Karajan/issues/100) trusted ID-only binding
(parent #95) and [#101](https://github.com/zhouy1017/Karajan/issues/101) subject
consumption (parent #94), using the #96 Candidate CAS primitive.

Approved Reviewer membership is compiled from current controller facts, persisted
before a one-use CAS claim, and consumed through exact operation receipts. New
subjects rerun every approved Check with fresh identities and the existing Run
budget. The original Worker capture and old cycle evidence remain historical.

This index binds the final implementation; copied author/reviewer records retain
their original bytes and as-of status. In particular the producer freeze's null
implementation commit and older pending/not_run statements are not rewritten.
`publication-map.json` maps each old `.cache/...` path to its published path and
SHA-256. Historical source references under `backend/` and `tests/` refer to this
implementation commit; original red source snapshots remain separate text files.

## Evidence scopes and counts

| Evidence | Result | Boundary |
| --- | --- | --- |
| [Producer author](reviewer-binding-author/README.md) | 18 C passed on Windows; 18 C passed on WSL | Real stores/Git/CAS; explicit planning and qualification doubles |
| [Consumer author](subject-author/README.md) | 16 C passed on Windows; original WSL group 16 C + 2 P passed in 107.30s | Real persistence; the two P cases use real Host and Check namespace processes |
| [Consumer independent review](subject-independent-review/README.md) | Standards/Spec: no confirmed findings; Windows 47 passed = 5 independent + 16 author + 26 existing; WSL independent 5 passed | Five independent C designs, not 47 new tests |
| [Producer independent review](reviewer-binding-root-review/README.md) | No actionable finding; existing 18 author C cases independently rerun on Linux | Independent execution and review, not 18 additional designs |
| [Production factory](check-factory-subject-review/README.md) | 19 Windows regression cases passed, including one new refusal case | Real qualification Store rejects synthetic ready B before Host.prepare |

The consumer's earlier 40-case report is 14 then-existing new cases plus the 26
old Checks cases; its results overlap the later reports. Do not add platform runs,
independent reruns or first/final reports into a count of unique test designs.
Producer and consumer use distinct author/reviewer ownership; reports state when
one independent reviewer covered both Standards and Spec.

## Actual Check P

The [first report](subject-author/native-passed.json) contains A and B, each with
two actual approved Checks and four distinct Attempt/Evidence identities. All four
passed. Complete Candidate manifest, authors, baseline, user tree and original
capture remain unchanged. The same Run budget keeps its original start and counts
one fixture Writer plus four actual Check process claims. Review remains not_run.

The [second report](subject-author/active-cancel-passed.json) first observes the
old namespace's live PID/birth and a command-created candidate-copy marker. A
premature ready receipt from the explicit producer double must be refused by the
consumer. Two advance calls and cancel then leave A current, confirm stop and do
not claim the second Check. Actual native observation was completed / exit -9 /
local_stop confirmed; business state was cancelled, so this is not a passed Check.
The trusted final output log independently contains the printed marker.

The separate fixed `tests/runs/subject_check_fixture.py` is a test deployment;
its exact bytes are included in every persisted execution controller source.
It is not copied as a deployment helper and is not a production fixture flag.
Reviewer qualification is explicitly synthetic. All five actual executions'
backend source descriptors match the same 139-entry before/after set. Those two
JSON files preserve different original formatting; their parsed entries match.
The five selected logs match their original receipt hashes and lengths.

## Recovery and retained failures

[Consumer history](subject-author/README.md) retains the missing interface, terminal
A not installing B, and stopped old observation not submitting late Evidence,
with their green follow-ups. Its cancellation error-priority mismatch is described
separately. [Producer history](reviewer-binding-author/README.md) retains the exact
authentication-source guard red input/source and green results. The independent
consumer's first failure was an invalid review fixture Digest, correctly rejected
by the product; it is not a product finding. No original record is relabelled.

Author/public test inputs are committed under `tests/runs/`; independent Python
inputs are published as `.py.txt`. To replay the independent cases, copy the exact
executed text to a controlled temporary `.py` path and use the original report's
pytest/PYTHONPATH command from the repository root. The publication operation did
not rerun behavior tests or providers. The map plus leaf `.gitattributes` preserves
raw report and failure-history bytes through Git line-ending normalization.

## Remaining work

The positive tests do not establish a real Reviewer role qualification or S.
The normal factory wires the actual qualification Store and `current_locked`, but
currently has no acceptable Reviewer suite/credential runtime configuration.
It rejects unsupported role authority with REVIEWER_QUALIFICATION_REQUIRED and
has no caller-supplied validator or fixture switch. Actual read-only Reviewer
admission/execution, Review Evidence and final new-Evidence-set consumption remain
#95 work. The real planning bridge remains #93; production GitHub delivery remains
#14. This local publication does not claim current PR CI, merge, deployment or G.

Only explicit file selections are published: no database, bootstrap contents,
temporary/runtime directory, bytecode or private helper. The only `.log` files are
the five fixed Check output logs listed in the map. `publication-check.json` records
copy/hash, reference, whitespace and protected-source checks for this publication.

## Publication whitespace check scope correction

The earlier successful `git diff --check` covered the pre-staging tracked
implementation-document diff. It did not check the then-untracked archive, so
it was not a successful full-publication whitespace check.

The current `git diff --cached --check` exits **2** and reports
**937 whitespace diagnostics in 10 files** across the staged index
(10 files in this publication, 0 elsewhere in the staged evidence).
Original CRLF reports, XML and historical Python text retain their existing
whitespace byte-for-byte. No original evidence or Git whitespace rule is changed
to hide these diagnostics. This is an archive whitespace result, not a product
behavior-test or test-CI failure; no product tests were rerun.

A separate check covers only the three newly edited implementation documents and
four generated publication metadata files: LF and no trailing whitespace pass.
Copied originals are explicitly excluded from that new-text check. The detailed
scope, file counts and reason summary are in `publication-check.json`; long raw
XML diagnostic lines are not copied into it.

The publication map preserves all raw-copy hashes and binds the corrected
generated README. The check report also retains the earlier README digest so
the scope correction remains explicit.
