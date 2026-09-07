# Candidate cancellation receipt race — author history

## Scope

PR103 (`codex/m3-reviewer-validation-subject`) started from source `37daff5fa16322c4453bde40d342776f8a1b4fa7`. The reported Linux CI failures were runs `34080490250` and `34080491334`, archived in the shared failure text at `C:\Users\Chooo\Playground\Karajan\.cache\ci-resume-20260907\subject-dev97\failed-linux.txt`; the failure was `tests/runs/test_candidate_subjects_native.py::test_active_old_namespace_blocks_ready_subject_and_concurrent_cancel`, with `first_final["observation"] is None`.

## Red / green evidence

The two deterministic public boundary tests are in `tests/runs/test_candidate_checks.py`:

- On a temporary `37daff5` source snapshot (`/tmp/karajan-subject-old`, exported from `C:\Users\Chooo\AppData\Local\Temp\karajan-subject-old.tar`), with the current test file loaded, `test_public_cancel_retains_host_until_native_receipt_arrives` was red: `1 failed in 1.26s`. The old production behavior called Host cancel before the late native receipt.
- On the repaired worktree, the same two tests were green: `2 passed in 5.80s`.

The red and green outputs were emitted by the WSL pytest commands in the agent tool output and were not saved as standalone log files. No original CI log was recreated or altered.

Green command (WSL, complete path set): `cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject && env PYTHONPATH='/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/backend:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/projects:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/runs:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/web:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/isolation:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/adapters/opencode:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/candidates:/mnt/c/Users/Chooo/Playground/Karajan/.cache/reviewer-validation-subject/tests/execution' /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest -q -o pythonpath='...' tests/runs/test_candidate_checks.py::test_public_cancel_retains_host_until_native_receipt_arrives tests/runs/test_candidate_checks.py::test_public_history_cancel_stops_host_from_persisted_observation --basetemp=/tmp/karajan-subject-boundary3`.

The earlier real native P command used the same environment and test module path, targeting `tests/runs/test_candidate_subjects_native.py::test_active_old_namespace_blocks_ready_subject_and_concurrent_cancel --basetemp=/tmp/karajan-subject-current`; its only output was tool output, not a saved log: `1 passed in 24.68s` (the initial repair). After the deadlock-safe correction, root independently reran the original native P and the two C tests successfully in its integrated tree.

## Final source

Author worktree commit: `9d2ee17551361f0b451f36a0ab188a76c03681f5`.

Raw source blob hashes at that commit:

- `backend/karajan/orchestration/candidate_checks.py`: `f219a30dfe0bc9fd3f6edf1f944e885dc32a2af7`
- `backend/karajan/isolation/check_runner.py`: `ed44580e2d3dacf581183bc6b19c7dd7d14b195a`
- `tests/runs/test_candidate_checks.py`: `d9c8763a2852c2752e1ec640c0c1ff3252d4f0de`

The final repair restores immediate runner cancellation, defers Host cancellation while a native publisher may still be writing its receipt, and permits Host cleanup once a trusted observation is already persisted. No provider, paid API, reset, push, or merge was performed in this worktree.
