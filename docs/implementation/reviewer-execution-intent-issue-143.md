# #143 Reviewer execution intent

Candidate base: `b1bd5e9e58eba9dd6d5d4e3d63b815edadd265d2`.

`open_reviewer_execution_intents(control_directory)` is the production-only
composition point.  It accepts no request identity, profile, source, process
arguments, or paths beyond the fixed private control directory.  It reopens
the two provisioned descriptors and existing Registry, Planner, Capacity,
Candidate, Host, routing, admission, qualification, and Reviewer-binding
stores.  It does not provision a database or start a Host.

The fixed `reviewer_execution_runner.py` is deliberately content-free: its
only action is to make the observer claim from the Host's direct child
identity.  It has no native, provider, HTTP, Journal, Evidence, parsing, or
delivery path.  Current source is recomputed at every prepare/claim guard;
changed deployment or child bytes reject the old intent.  `read` remains a
detached recovery operation, including after cancellation or source changes.

## Original AC evidence (2026-09-08)

| Original acceptance condition | Evidence | Result |
| --- | --- | --- |
| Real lineage/CAS/Checks/Capacity create one complete intent; forged or incomplete material creates none | `tests/runs/test_reviewer_execution_intent.py` together with existing `test_reviewer_binding.py` coverage; focused collection blocked below | P (prior local unit coverage; this candidate's full focused run not collected) |
| Replay, concurrency, reopen and tamper preserve/reject exact identity | intent ledger implementation and focused intent tests; collection blocked | P |
| Re-enter current admission/compiler before prepare and claim; changed input/source blocks effects | `ReviewerExecutionIntents._current_guard`; source recomputation added in this candidate | C |
| Durable Host prepare/control and read-only inspection; no facade start | `test_reviewer_execution_intent.py`; no `Host.start` call in facade | C |
| Only registered direct Host child gets first claim; replay/lost reply gets no second right | `RunnerHost.current_runner_guard` is used by fixed child claim; direct-child integration needs Linux fixture collection | P |
| Cancellation remains permanent and delivery stays false/not_run | `test_reviewer_execution_intent.py` cancellation test | P |
| Public state avoids sensitive contents; lint/type checks; no native/HTTP/model/Journal grant | static source review; commands below | C |

`C` means code/structural evidence in this candidate. `P` means local test
coverage exists but the requested full focused collection was not runnable in
this checkout.  No native/provider or real qualification claim is made.

## Raw command results

| Command | Result |
| --- | --- |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/runs/test_reviewer_execution_bootstrap.py -q --basetemp .../.cache/dg01-bootstrap-2` | `3 passed in 0.23s` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py -q --basetemp .../.cache/dg01-reviewer-focused` | not collected: `ModuleNotFoundError: test_projected_qualification_store` imported transitively by the runs fixtures |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend` | `Success: no issues found in 154 source files` |
| `wsl -d Ubuntu -- bash -lc 'cd /mnt/c/.../dg01-reviewer-20260908 && /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py -q --basetemp /tmp/karajan-dg01-bootstrap-20260908'` | `3 passed in 1.26s` |

The Linux direct-child test is not recorded as run: this Windows checkout's
focused suite fails collection before a test can be selected, and no test or
runtime dependency was altered to mask that failure.
