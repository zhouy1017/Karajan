# DG01 OpenCode timeout fixture repair

Scope: GitHub #151. This is an offline fixture repair: the pinned OpenCode
1.18.29 server talks only to two loopback Python peers. It neither reads a
provider credential nor performs a provider, subscription, or cash call.

## Preserved current-candidate failure

The first repair candidate was `0e53567cd7d1930fd0166eae945299134dba9800`.
It was **not** fully validated. Commander reproduced the original Windows
failure on that exact commit in a clean review worktree with:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_opencode_probe.py -q --basetemp .cache/root151-final-windows-001
1 failed, 12 passed in 54.34s
```

`test_header_timeout_is_reported_as_error_without_inventing_a_retry` observed
`report.status == "completed"`, not `"runtime_error"`. Its preserved,
read-only report has three provider requests and three broker receipts, a
nonempty final response, no `session.error`, and this lifecycle:

```text
provider_header_wait=started
native_terminal=not_observed
provider_release=safety_deadline
```

The discriminating native status event was a real retry: `attempt: 1`,
`Provider response headers timed out after 500ms`. The first receipt later
recorded `RemoteDisconnected`; the two retry-path calls completed the tool
loop. This failure remains evidence against `0e53567`; old isolated green
runs, static reviews, and the baseline's Linux CI do not replace it.

## Diagnosis and repair

The pinned 1.18.29 source installs two independent abort paths around the same
fetch: `headerTimeout` produces `ProviderHeaderTimeoutError`, while `timeout`
uses `AbortSignal.timeout`. The runtime's `SessionRetry.policy` retries timeout
messages, including the header-specific error. The previous fixture assigned
both deadlines to 500 ms. Which native timer won was therefore a scheduler
race:

- with `timeout=501` and `headerTimeout=500`, the real header timer won; the
  fixture recorded the native retry and three real receipts, then completed;
- with `timeout=499` and `headerTimeout=500`, the request deadline won; the
  real terminal event was `session.error: The operation timed out.`

The repair makes that precedence intentional rather than timing-dependent:
`timeout_once` retains an explicit 500 ms header deadline but sets its terminal
request deadline to 400 ms while the provider is withholding headers. This is
a real native request timeout during the header wait, not a fabricated event or
a synthetic disconnect. The regression requires exactly one request and
receipt, empty final text, one native `session.error`, and zero actual retry
status events.

The investigation also exposed a Linux-only configuration-isolation defect.
The server environment filter admitted inherited proxy variables
case-insensitively, then replaced only uppercase proxy names. A poisoned
lowercase `http_proxy` or `https_proxy` remained effective on Linux and drove
the normal local tool loop to its real six-call admission limit. The server now
sets both casings to fixed local values and both `NO_PROXY` casings to loopback.
No proxy, configuration, or credential is forwarded to an external endpoint.

The timeout fixture's lifecycle wording is now also exact: a release during
cleanup without a native terminal is `cleanup_without_native_terminal`; only an
unreleased five-second wait is `safety_deadline`.

## Local evidence for this candidate

The one-variable 501 ms probe was intentionally red-capable and failed with
the original symptom:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_opencode_probe.py -k header_timeout -q --basetemp .cache/root151-header-first-501
1 failed, 12 deselected in 6.50s
```

After the repair, five fresh focused Windows repetitions passed in 3.83--4.42
seconds. A representative report had `runtime_error`, one request/receipt,
empty final text, a 0.468-second header hold,
`native_terminal=session.error`,
`provider_release=after_native_error_cleanup`, and zero retry events.

The final complete owned suites used fresh bases:

```text
# Windows, pinned official executable
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_opencode_probe.py tests/adapters/opencode/test_management.py -q --basetemp .cache/root151-final-windows-fixed-002
15 passed in 50.70s

# Linux, separate temporary copy with the ELF named only inside that copy
PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates \
  /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest \
  tests/adapters/opencode/test_opencode_probe.py tests/adapters/opencode/test_management.py -q \
  --basetemp .cache/root151-final-linux-fixed-003
15 passed in 101.47s
```

The Linux executable was
`/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode`,
version `1.18.29`, SHA-256
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
It was copied into a temporary Linux test layout; the Windows executable was
not replaced or run through WSL.

Static gates on this candidate also passed:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .
All checks passed!

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend/karajan --platform win32
Success: no issues found in 150 source files

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend/karajan --platform linux
Success: no issues found in 150 source files
```

## Acceptance mapping and limits

1. The original current-candidate red is preserved above; the 501 ms
   one-variable invocation is a repeatable red-capable loop for the native
   header-timeout/retry path.
2. `timeout_once` now deterministically reaches a native terminal timeout
   while headers are withheld, without a synthetic EOF or retry completion.
   The distinct native header-timeout behavior and its retry policy are
   documented rather than mislabeled as terminal.
3. Every actual receipt remains unique and is persisted with its original
   attempt/fence data. Cleanup is bounded to the owned server, loopback peers,
   and their threads; `live_qualified` and `profile_enabled` remain false.
4. Normal tool, 429 retry, disconnect retry, cancellation, configuration
   tampering, admission/cleanup negatives, and both proxy-poison cases ran in
   the final Windows and Linux owned suites. Full Ruff and both platform mypy
   gates passed.
5. This local candidate has not been independently reviewed after this repair,
   pushed, submitted for PR, merged into `dev`, or used to update/close #151.
   Fresh independent Standards and Spec reviews, required CI, merge/readback,
   and issue-state evidence remain root-owned work. Local fake-provider
   evidence does not qualify real authentication, billing, OS egress
   containment, or remote cancellation state.
