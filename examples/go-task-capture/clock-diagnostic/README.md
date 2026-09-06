# Retained suite failure: conservative rejection after wall-clock rollback

The original final local run recorded **25 passed, 1 failed**. The `call_id` case
returned `['not_run', 'not_run']` instead of the tamper test's expected
`['failed', 'not_run']`. A later single-case run passed, but that does not explain
or replace the original failure.

Read-only inspection of the retained original directory establishes:

- Both grants existed and were revoked, with **zero calls** each.
- The suite directory was empty. There was no scenario/native directory or
  namespace log. The observer did not run; its wrapped `report.status` assertion
  was not the cause.
- Both persisted runtime source digests equal the current frozen runtime source.
- The unchanged test helper sets `expires_at = started_at + 420`. The persisted
  expiry therefore reconstructs `started_at = 1788693180.3810837`.
- The subsequent first grant creation recorded `1788693178.8967345`, **1.48434925
  seconds earlier than that start**. The second creation and both revocations also
  precede the derived start. These are actual persisted wall-clock rollback facts.

The suite checks `started_at <= now` again after creating the grants, before it
marks a scenario as entered or calls the observer. The retained state is the
correct conservative rejection when the clock falls behind the approved start.
The exact second clock sample was not persisted; this report does not invent it.
Production source and the original tests remain unchanged. No change to the
production time rule is needed.

`test_clock_boundary.py` makes one deterministic public-suite replay and one
control. It uses the real fixed source descriptor and SQLite journal, and an
explicit no-effects observer sentinel. With a replay clock between the recorded
grant creation and the derived start, the public suite returns the original two
`not_run` statuses, never enters the observer, and revokes both unused grants.
With a stable clock, it reaches the sentinel and marks the first scenario `failed`.
This distinguishes a time guard rejection from an observer/assertion failure.
Both cases passed in **8.88 seconds**. No native process or provider was started.

Files intended for publication are this README, `report.json`,
`test_clock_boundary.py` and `final.xml`. The report contains only allowlisted
grant IDs, timestamps, zero counts, empty-directory facts, source comparison and
the original Journal file SHA-256. It contains no original SQLite bytes, capability,
credential, auth header or native message. The retained-inspection script is a
local forensic helper; it depends on the original temporary directory and need
not be published.

Reproduction from the repository in the prepared Linux environment:

```sh
PYTHONPATH=backend:tests/projects python -m pytest \
  path/to/test_clock_boundary.py -q \
  -o cache_dir=/tmp/karajan-clock-diagnostic-cache \
  --basetemp=/tmp/karajan-clock-diagnostic-replay
```

The test reads `report.json` beside itself. Set `KARAJAN_OPENCODE_LINUX_BINARY` to
the environment's prepared fixed artifact; otherwise it uses the retained local
artifact path. It hashes that artifact for source verification but never executes
it. Windows skips this Linux source boundary. The retained original timestamps
and facts stay unchanged.
