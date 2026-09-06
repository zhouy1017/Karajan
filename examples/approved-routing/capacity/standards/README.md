# Capacity admission: independent Standards evidence

This is the independent Standards review of the capacity admission bindings slice against
`35c00f7cbc04704c805c36e0b00ff1b743f2143b`. The Spec review is separate. `review.json` and
`source.json` bind this published run to exact test, supporting fixture and product bytes.
`history/` preserves the original private-workspace review, before publication and test formatting.

Run from the repository root with Python 3.12 and the project's development dependencies installed:

```powershell
$env:PYTHONPATH = "$PWD/backend;$PWD/examples/approved-routing/capacity/standards"
python -m pytest examples/approved-routing/capacity/standards/test_capacity_standards.py -q -p no:cacheprovider --basetemp=.cache/reproduce-capacity-standards --junitxml=.cache/reproduce-capacity-standards.junit.xml
```

The checked-in `junit.xml` was produced by this published test file. Reproduction writes fresh state
under `.cache/`; it does not overwrite the checked-in evidence. The tests create temporary SQLite
databases. They do not call a provider, read credentials or change a repository checkout.

`legacy_capacity/` contains the complete `__init__.py`, `models.py`, `store.py`, and `facts.py` from
`backend/karajan/capacity/` at the fixed baseline commit. These are unmodified legacy test dependencies,
not a second supported product implementation. Their hashes are recorded in `review.json`. The test
creates actual old-format reservations and command receipts with this old package before reopening
the same database with the current implementation; compatibility is not inferred from fabricated rows.

The 15 independent cases cover omitted/explicit-null binding replay, old activation receipt replay,
current policy revalidation after an old admission receipt is replayed, idempotency payload conflicts,
and preserving lead reserve restrictions for worker/reviewer/check/adviser and unknown quota.
Capacity admission receipts do not authorize model execution or establish cross-database atomicity.
