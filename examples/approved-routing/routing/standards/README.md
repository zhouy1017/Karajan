# Approved routing assessment: independent Standards evidence

This is the independent Standards review of the approved routing assessment slice against
`35c00f7cbc04704c805c36e0b00ff1b743f2143b`. The Spec review is separate. `source.json` records the
reviewed product and author-test hashes, including the final Commander membership and exact
channel-to-destination fixes. `review.json` additionally binds the published independent test and
its repository helper dependencies. `history/` retains the initial hashes and later LF-only change.

Run from the repository root with Python 3.12 and the project's development dependencies installed:

```powershell
$env:PYTHONPATH = "$PWD/backend;$PWD/tests/web;$PWD/tests/runs"
python -m pytest examples/approved-routing/routing/standards/test_routing_standards.py -q -p no:cacheprovider --basetemp=.cache/reproduce-routing-standards --junitxml=.cache/reproduce-routing-standards.junit.xml
```

The explicit helper paths are required: the independent tests reuse `run_client`, `v2_plan`,
`policy_request`, and `request_v2` to build real persisted Project, configuration, execution policy,
Run, plan and owner-approval records. Their planning receipt port is an explicitly scripted fixture;
it does not claim real model qualification or production planning admission. The router itself uses
the real qualification store and therefore correctly reports unqualified runtime tools as unavailable.

`junit.xml` is the fresh run of the published independent test. Reproduction writes fresh SQLite,
temporary test repositories and other state only under `.cache/`, not into this evidence directory.
There are no model calls, credential reads or changes to the product repository. Temporary test
repositories are initialized by existing project fixtures.

The 21 independent cases cover authenticated empty-body assessment, receipt digests and durable
replay, owner/session isolation, command conflicts, forbidden caller-supplied routing material,
source database identity, changed current configuration, absent qualification/cash/estimates, unknown
observation confidence, and Commander membership through normal eligible groups only. A permitted
participant still produces a planning intent awaiting its own admission receipt.

`author-scope.junit.xml` separately records 22 passing author tests, including the explicit
test-only qualification double used to exercise selected assessment and exact destination binding.
That double is not installed in product wiring. Assessment never reserves capacity, dispatches an
Attempt, invokes Host, or proves a production model can execute a task.
