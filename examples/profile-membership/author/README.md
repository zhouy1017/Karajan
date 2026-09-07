# #99 Profile membership — bounded author evidence

Base: `3d47194147edf153a0a48a183b34ff7222d674d4`.
Issue: https://github.com/zhouy1017/Karajan/issues/99 (parent #95).
Implementation commit, independent review and current-head CI remain pending.
This is C evidence over explicit supplied fixtures, not S qualification.

The public interface is:

```python
from karajan.routing import evaluate_profile_membership

result = evaluate_profile_membership(task_snapshot, policy_snapshot, as_of=1000.0)
```

It accepts the existing complete TaskSnapshot/PolicySnapshot and a finite native
int/float timestamp. Booleans, strings, NaN/Infinity, Decimal and unrepresentably
large integers raise `MEMBERSHIP_AS_OF_INVALID`. No clock, store, credential,
network, CapacitySnapshot, quota estimate, cash price or resource admission is
read or constructed.

The result schema is `karajan.routing.profile-membership.v1`. It contains the
normalized task/policy snapshots and their hashes, supplied as_of, Rulebook
identity/hash/compiler diagnostics, selected rule/effective class/group facts,
candidate rejection reasons and `eligible_profiles` in canonical identity order.
This order is not a ranking. Static candidate rows contain profile, eligible,
reason_codes, qualification=`simulation_only`, and, when available,
profile_sha256, required_capabilities and qualification_evidence. There are no
pool evaluations, cash estimates, ranking or capacity snapshots.

`selected_profile=None`, `activation_allowed=False`, `dispatch_enabled=False`,
`live_qualification=not_run` and `scope=supplied_profile_facts_membership` remain
explicit even for a nonempty membership set. Supplying a fixture or imported
observation does not establish that it came from a current trusted producer.

## Shared implementation and boundary

The existing rule/classification, normal/quality-stage and exact approved-group
logic now lives once in `_membership`; public membership, evaluate_route and
evaluate_reserved_profile all consume it. `_profile_checks` receives only as_of
instead of a capacity document. No second policy engine or brand selector is
introduced. Existing routing retains its original quota/cash/ranking execution,
output schema, reasons and validation ordering.

Both existing stages are supported. Quality still needs the original approved
index, prior Profile, QUALITY_FAILED and reached repair round. This interface
does not manufacture failure history or stage permission. The first Reviewer
binding consumer intends to use only normal-stage membership.

The shared static boundary includes approved Profile/revision and group sets,
channel/account/destination/tools, role/class/isolation/capabilities, context
including output reservation, observation identity/validity and all authors'
Attempt/context/family independence. Reviewer risk/complexity inherits the
strictest recorded author via the existing classifier.

Existing duration admission is in the quota layer, combining Run permission and
the current Capacity account policy; cash enforcement/price/amount checks stay
in the cash layer. Those are not evaluated by this membership-only operation.
The trusted compiler must derive exact approved task requirements, and eventual
routing/admission must still evaluate the complete resource gates. This avoids
presenting static membership as full authorization to run.

## Verification

- `first-red.xml`: actual public import failed because the requested interface
  did not exist. `test-first.py.txt` and `evaluator-before.py.txt` retain the input
  and prior implementation bytes. `first-green.xml`: 1 passed.
- `boundaries.xml`: 32 passed after adding finite-time, complete-author,
  qualification/permission, stage, exact-group and cash-separation cases.
- `final-complete-windows.xml`: 156 passed, 1.64 seconds, including 33 new cases
  and all 123 existing routing cases. The additional case verifies credential
  fields are rejected before returning snapshots.
- `final-linux.xml`: the same 156 passed, 5.52 seconds, no provider calls.
- `legacy-results-before.json` captures seven complete route/reserved public
  scenarios before the implementation change. `legacy-comparison.json` records
  14 exact complete-result comparisons with zero differences.
- Ruff check/format and mypy on all nine routing source files passed. Exact
  owned/source hashes and commands are recorded in `freeze.json`.

`first-routing-regression.xml` (124) and `final-windows.xml` (155) are earlier
successful test-set sizes retained as history, not the final complete matrix.
A baseline-generation helper initially omitted PYTHONPATH and therefore imported
the unrelated root checkout; it failed before writing any baseline. Reissuing
with explicit `PYTHONPATH=backend` produced the retained pre-change results.

## Reproduce

From this worktree, with repository development dependencies installed:

```text
PYTHONPATH=backend python -m pytest tests/routing -q
python -m ruff check backend/karajan/routing/evaluator.py backend/karajan/routing/__init__.py tests/routing/test_profile_membership.py
python -m ruff format --check backend/karajan/routing/evaluator.py backend/karajan/routing/__init__.py tests/routing/test_profile_membership.py
PYTHONPATH=backend python -m mypy backend/karajan/routing
```

No databases, account keys, real provider requests, model execution or Git
mutations were used. The implementation does not complete the #95 binding
compiler, role qualification, Capacity path, read-only Reviewer or subject
handoff. The evidence directory is not a new runtime dependency.
