# VALID / PR97: common repair integration review

Pinned implementation: 552dd6b5c78279efc398126e417969c41688232d. Base: 45ff0651517333225666353076a23c9cad85833c. Common repair: ef440f4e7902bc1d3a262afa9112154cd9690a4a.

Windows C: 117 passed, 1 Linux-only skipped. Linux C: 118 passed, 0 skipped, including that exact native-failure/consume/replay case. Linux P: 3 passed, 0 skipped. Ruff check . and mypy backend/karajan passed (136 backend source files). These are independently executed existing cases; repeated platforms and shared inputs are not added up as new unique tests.

Standards: 0 confirmed documented-rule breaches, 0 actionable judgment findings. Spec: 0 confirmed integration defects. The same independent reviewer assessed both axes; this does not represent two separate reviewers.

The actual merge only adds the stable native-failure persistence method and consumer diagnostic call. The old claim_process/current_process, new-effect deadline guards and independent stopped-capture guard remain. The diagnostic retains the original intent/runner claim and fixed enum facts, and is saved before Collector rejects missing capture. The affected public tests include lost reply/replay, new-effect deadline refusal and late capture retention.

Actual Linux production Check factory, Host child and fixed namespace Checks; planning, Writer and model-role qualification remain explicit fixtures. Valid and defective candidates both run all required Checks; an actually running check is cancelled while two advances race. The selected real Check processes produced five output logs. The archive excludes databases, bootstrap files, environment images and entire temporary workspaces. Recorded controller source digests match the pinned Git blobs and source before/after snapshots. Actual Check receipts verify the formal controller source; SUBJECT additionally verifies its explicitly synthetic fixed child inputs.

The real factory still blocks missing Reviewer qualification. Review evidence is still absent, so delivery remains blocked. No provider calls or keys were used. S and current-head G were not run here, and no merge or Issue closure is implied. Original CI and earlier official qualification do not upgrade this source.

Each test command, XML, stdout and full source before/after map is retained. Original timing fields remain unchanged; Windows stdout rounds differently from JUnit. The original independent budget input is copied byte-for-byte from examples/candidate-checks/shared-budget-independent/test_effect_budget.py.txt and run from a separate owned cache file. Its qualification and child-identity boundaries are explicit doubles, with actual SQLite/CAS/Capacity/Host/Journal persistence.

VALID also preserves two pre-pytest launch failures in launch-history.json. The Windows Git worktree pointer needed explicit --git-dir/--work-tree under Linux. No behavioral failure was discarded or retried. Later evidence-inspection shape corrections did not execute tests or change products.
