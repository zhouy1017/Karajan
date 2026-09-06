# Fixed Linux Check runner author evidence

Issue #94 C/P runner slice. The final product is `isolation/check_runner.py`, `_check_environment.py`, and `_check_namespace.py`; none of the OpenCode, Go relay, CandidateStore, CI, or existing Host products were modified by this author. The root/UI integration supplies the fixed Host child, original operation, current authorization guards and Evidence lifecycle. The runner's direct tests use explicit test guards and synthetic Candidate prerequisites. They do not prove an official Commander/Worker run or model qualification.

## Final result

`final-linux.xml`: **19 passed in 48.88 seconds**, real WSL/Linux Python processes, temporary Git repositories and real CandidateStore. No provider or credential source was accessed. `results-summary.json` contains 13 persisted content-free observations; the other cases reject before such a result or exercise an unfinished claim. `environment-sources.json` contains the actual image/mechanism source descriptor. No SQLite database, private bootstrap, Python image, Candidate source tree or raw log is copied into this evidence directory.

Ruff passes. Strict mypy passes for the default Windows platform and `--platform linux`, using the existing Windows mypy installation. The WSL test venv lacks mypy; it was not installed or treated as a successful native mypy invocation. `windows-import.xml` records the initial 18-test Windows import check, with all 18 deliberately skipped because this is a Linux-only implementation. The subsequently added nineteenth deadline case was executed on Linux, not counted as a Windows test.

The independent reviewer owns a separate five-case report; this directory contains author evidence only. The code-only review of the root factory/entry is separately recorded in `.cache/check-factory-independent-review.md`, explicitly excluding this author's runner implementation.

## Public internal contract

`PythonCheckEnvironment.provision(directory)` is explicit local controller deployment. It copies the installed `/usr/bin/python3.12`, stdlib and ELF dependencies into a private, regular-file image; no downloads, shell, Git, pip or sitecustomize are included. `PythonCheckEnvironment(directory)` only reopens that configured location. `source()` reads every asset and verifies its immutable manifest, ownership and write permissions, then fingerprints the actual image, helper/controller files, launch executables and kernel release.

The sole environment kind is `python312-stdlib`, platform `linux_x64`, filesystem `candidate_copy`, network `none`. The actual `environment_sha256` must equal the owner-approved environment's `source_sha256`. Unknown mappings, unsupported executables or reserved environment overrides, oversized log policies and source mismatches reject before a claim. `FixedCheckRunner(directory, candidates, environments=...)` with an empty mapping remains available for history without checking or creating an image.

`run(execution, *, start_guard, cancelled)` receives a detached, already bound execution document from the trusted child. It checks the complete Candidate identity, exact approved check/argv/environment, original deadline and effective timeout. It materializes through CandidateStore, commits one private claim, and invokes the real unshare `Popen` while holding the supplied business guard. It waits and polls cancellation outside those business locks. A durable spawn intent precedes Popen, and both effective monotonic and original wall deadlines are checked again immediately after that durable write. The Host cleanup allowance is never added to the candidate command's budget.

The new PID, mount, network and user namespaces contain a read-only image, a 128 MiB tmpfs full Candidate copy, a 32 MiB temporary filesystem, namespace-local proc and minimal devices. The copied Candidate manifest is checked both before and after copying into the namespace. Capabilities are cleared and no-new-privileges is set. The fixed launcher retains its exact namespace-init process identity outside the candidate root. The candidate receives no controller directory, credential files, delivery program, host network or inherited control descriptor. Its modifications cannot alter the original CAS or user tree.

`CheckObservation` is a frozen DTO containing the execution digest, outcome/exit, local-stop fact, log completeness/hash/size, environment fingerprint, observation/executor refs and stable reasons. `inspect(execution)` reads only original results and never starts. `read_log(execution, observation)` verifies the exact retained log. Missing/corrupt/linked logs yield unavailable data and an unknown projection. `cancel(execution)` uses the original bound receipt and observed process identity; pidfd signaling cannot target a reused PID. A missing result after a claim remains unknown, and `run` cannot claim that same identity again.

Logs are collected through the external merged stdout/stderr pipe. The stored bytes begin with the fixed controller frame `karajan-check-log.v1\nmerged_stdout_stderr:\n`, followed by actual pipe bytes. This frame distinguishes a valid silent check from a missing log; it does not invent candidate output. The byte limit includes the frame. Overflow is retained as incomplete and never becomes passed merely because a prefix was saved. Exit facts, rather than any `passed` text in a log, decide Evidence eligibility.

## Covered boundaries

- Real stdout/stderr, unchanged binary baseline content, scratch-only writes, result replay with no second guard or process.
- Incomplete Candidate identity, wrong environment reference, actual asset mutation, unsupported environment/source, altered snapshot and inflated effective timeout reject.
- Real host credential/control/delivery/config canaries, host loopback listener, image writes, WSL host mount, shell/Git availability, process capabilities and symlink escape are denied by the actual namespace.
- Real nonzero exit, timeout, cancellation and oversized output remain non-passing; silent exit zero produces a complete real Candidate Check Evidence record.
- Actual Popen return loss remains unknown, never `not_started`, and cannot repeat. Result-save return loss is recovered from original durable bytes. Corrupt logs cannot recover as passed.
- Concurrent callers and cancellation share one claim; cancel before the actual effect prevents Popen. A committed claim with no result is read-only history.
- A delayed spawn-intent write cannot carry Popen past its time budget; arbitrary callback exception text is not persisted.

## Reproduction

From this worktree on WSL/Linux, use the existing Python 3.12 environment with project test dependencies:

```bash
PYTHONPATH=backend /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest \
  -c pyproject.toml -p no:cacheprovider tests/isolation/test_check_runner.py \
  --basetemp=/tmp/kcheck94-fresh -q --junitxml=.cache/check-runner-author/reproduction.xml
```

The fixture provisions its own temporary Python image. No Go binary, tokenizer, service key, external endpoint or system configuration change is needed. Pick a fresh temporary directory to retain earlier evidence.

## Retained red/green history

The first test failed on the absent module. Binding tests subsequently exposed incomplete Candidate identity and wrong environment-reference acceptance; the namespace test exposed a replaced snapshot reaching execution. The silent-check test reproduced CandidateStore's real unavailable status for an empty log. The lost-Popen test exposed an incorrect `not_started` fact, corrected by the durable send/start uncertainty boundary. Each has its original failing XML and subsequent green run.

The first full run had 16 passed and two test-input failures: a 1.5-second overall deadline was already consumed during source/setup work and correctly rejected before start; an unrelated unsupported-environment assertion had been misplaced into a completed execution's identity-conflict test. The timeout fixture now separately approves a 20-second deadline and a 0.5-second effective execution window, testing timeout after Popen without weakening production time checks. The misplaced assertion was restored to its intended preflight test. Two earlier fault-injection test imports were also missing; their raw XML remains visible and is not counted as product defects.

The first frozen 18-test source/input are retained as `check_runner.first-freeze.py.txt` and `test_check_runner.first18.py.txt`, with `first-freeze-linux18.xml` (18 passed in 46.17 seconds). A later author audit produced `deadline-write-red.xml`: a 100 ms durable-write delay consumed a 50 ms effect window but Popen still occurred. The final two-line recheck passes the same case, the related five-case subset, and all final 19 cases. Earlier passed sources are not substituted for the final source manifest.
