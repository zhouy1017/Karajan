# Running Check cancellation race — actual Linux P evidence

One test passed in 22.70 seconds on the current backend:
`tests/runs/test_candidate_checks_concurrency_native.py`.
Test SHA256: `b4f8204619dc3fce4e762f8e59fb6fb67166cc185eb4fda051cfe5fe3b00282c`.
The 136 source entries in the actual execution's persisted controller descriptor
match the final filesystem hashes in `report.json`. Backend code was not changed
by this task. Planning, qualification and the Worker author remain explicit
fixtures; the Check factory, direct Host child, namespace, command, cancellation,
CAS and Evidence are actual. Provider calls: zero.

The owner-approved first check has a 60-second bound. Its fixed command prints a
known marker with flush, writes the same marker inside its private candidate
copy, then sleeps. The external test verifies the namespace-init execution
digest, PID/birth is still running, and the marker through that process's proc
root before releasing a three-thread barrier: two public `advance` calls and
one public `cancel` call.

The candidate-copy marker is only a synchronization signal proving this fixed
test command actually ran. It is not trusted product Evidence. The runner has
no live persisted stdout port: its controller log is written at completion.
After cancellation, this test separately verifies the same printed marker in
the final trusted log (`trusted-output.log`, exact observed SHA/size).

Results:

- Exactly one actual native `started.json`; the first check is cancelled with
  confirmed native and Host stop, a complete trusted log and inconclusive
  Evidence. The logged exit is not treated as a pass.
- The second check never obtains `claimed_at`, a native claim or a process.
  The shared Run budget has only its original Writer plus the first Check.
- Both advancing calls finish without deadlock. Historical reopening twice
  retains the original Check/Attempt/Evidence keys and committed Evidence ID,
  with no second start. Original CAS/user-tree scope is unchanged.
- `local_gate_passed=false`, `delivery_eligible=false`; Host remote stop remains
  `unknown`. No Reviewer, provider or full Run completion is inferred.

Reproduction from the worktree in Linux/WSL:

```bash
python -m pytest -q -p no:cacheprovider -o 'pythonpath=backend tests/capacity tests/routing tests/runs tests/projects tests/web' tests/runs/test_candidate_checks_concurrency_native.py --basetemp=/tmp/karajan-check-race-reproduce
```

The original result is retained as `../check-concurrency-first.xml`; `final.xml`
is a byte-for-byte copy. Ruff and format checks are recorded beside this report.
An initial evidence-assembly comparison used the earlier root composition map
and found the known CandidateStore formatting change. No test failed. Assembly
was corrected to compare this run's own persisted source descriptor, which is
the relevant identity; all 136 entries match. No extra native run was performed.

Publish only this README, report JSON, original XML, trusted synthetic log and
static-check text, alongside the public test. Do not publish `write_evidence.py`,
SQLite databases, private runtime directories, bootstrap or bytecode.
