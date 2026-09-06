# Go output capture evidence

Implementation: [stopped projection capture](../../docs/implementation/m3-go-task-capture.md).
The final `freeze.json` binds source files and copied reports. All requests in
this evidence use local HTTP fixtures; no official provider credential was read
by these tests and no production Profile was qualified.

- `candidate-author`: complete baseline restoration, exact writable overlay,
  immutable candidate/replay and missing-validation gates on Windows and WSL.
- `qualification-author`: strictly separate v2 metered qualification grants,
  local HTTP behavior and legacy/Task compatibility.
- `host-author`: real Python process launches and current fence transactions.
- `native-author`: actual pinned Linux OpenCode read/edit, namespace stop, pinned
  file collection and complete candidate reconstruction. Failed development
  tests, when retained, are identified in that directory's own history.
- `candidate-review` and `host-review`: independent public cases and reports.
  The latter retains the original Host implementation and missing-ledger failure
  before the guard was changed to open only an existing database.
- `clock-diagnostic`: original journal timestamps show WSL wall time regressed
  by at least 1.4843492 seconds. In the final legacy-suite group, 25 cases passed
  and one failed its success precondition because the second start validation
  correctly rejected the now-future start time. Both grants had zero calls and
  no native directory was created. A single-case rerun passed. The original
  failure is retained; production time checks and the existing test are unchanged.

The root review also checked the qualification relay subject/spec/limit matching,
durable-before-send ordering and stop/capture implementation. No additional
product issue was found there. This is a code review, not provider qualification.

Final local checks include 16 new real-native capture cases; 22 existing native
cases plus 8 subtests; 231 adapter cases on Windows and 63 targeted Linux cases;
103 candidate cases on Windows (3 POSIX-only skips) and 106 on WSL; 65 Host cases
after the existing-ledger fix; and 28 independent cases from the published paths
on each platform. These groups overlap and are not summed. Ruff and both platform
mypy checks cover the backend. The old-suite clock-related failure above remains
explicitly separate from those successful checks.

The source checkout is a trusted controller source. These internal capture ports
do not replace a current approved Run, the owning execution intent or credential
and capacity revalidation. The subsequent fixed suite v2 and approved Task runner
must consume them before any `candidate_capture=passed` fact can be produced.

Independent cases are run in CI with:

```text
pytest -o "pythonpath=backend tests/candidates tests/execution" examples/go-task-capture
```

Native public tests additionally require Linux namespaces and the pinned ELF;
they run in the ordinary required Linux `pytest tests` job. Tests import the
existing candidate fixtures, so a targeted native invocation includes
`tests/isolation` and `tests/candidates` in its Python path. Tokenizer tests use
the verified local artifacts provisioned by CI before offline execution.
