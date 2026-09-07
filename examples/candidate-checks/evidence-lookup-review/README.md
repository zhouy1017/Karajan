# Independent Candidate evidence recovery review

Scope is root's new `CandidateStore.lookup_evidence` and its contract, not facade authorization or the reviewer's earlier unrelated code. No product or author test was changed. **No findings in this bounded scope.**

The method validates the complete CheckResult/ReviewResult request and exact log hash/size, reads the uniquely keyed evidence row through an existing-only read connection, and compares the redundant database identity before returning detached historical JSON. No Candidate gate, artifact reader/writer, timestamp or execution function is invoked. A missing row is distinct from a mismatched existing key. The returned historical status is not current permission, current log availability or a refreshed gate.

Five independent public cases passed on Windows in 2.95s, using actual SQLite and temporary Git/CAS fixtures. Check and Review failures remain recoverable after all artifact, Git-object and original repository directories are moved away; returned nested data can be mutated without changing the next read or database bytes. Rebinding provenance or outcome under the same key is rejected. An explicitly corrupted redundant subject is rejected without repair or database writes. Fixture tests create no processes except ordinary Git setup, no model effects, and use no credentials.

The reviewed product/source hashes remain unchanged across the run and are recorded in `review.json`. This narrow check does not establish public ownership checks, actual check execution, Review qualification, whole Candidate delivery eligibility or remote CI success. Those belong to the consuming facade and separate tests.

```text
python -m pytest .cache/evidence-lookup-review/test_independent_lookup.py -o "pythonpath=backend tests/candidates" -q --junitxml=.cache/evidence-lookup-review/windows.xml
```

Ruff passed for the independent test; final formatting changed only import/line layout after execution. The author test matrix was read for contract context, not counted as independent execution.
