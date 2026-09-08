# DG01 OpenCode timeout fixture repair

Scope: GitHub #151. This is an offline fixture repair only. It runs the pinned
OpenCode 1.18.29 server with two loopback Python peers; it neither reads provider
credentials nor makes a provider, subscription, or cash call.

The implementation fix is `1ddc1a58e08f2ac359042c3228b534efa0c02c95`
(`fix: make OpenCode timeout fixture wait for native error`).

## Fixed failure and cause

Baseline `bdd6830a80110a04e70ed000ce3ec92dc654d9b9` failed the post-merge Windows
[CI 34219059454](https://github.com/zhouy1017/Karajan/actions/runs/34219059454)
at `test_header_timeout_is_reported_as_error_without_inventing_a_retry`:
`report.status` was `completed`, not `runtime_error`. That remains a failure on
that baseline; earlier candidate and Linux CI passes are not evidence for it.

The fixture called `sleep(1.5)` and then closed the provider connection. That
made the claimed header timeout depend on a race between the native timeout and a
synthetic EOF. The broker retained that EOF as `RemoteDisconnected`, and a slower
or differently scheduled native timeout could therefore observe a completion path
instead of the intended timeout.

`timeout_once` now holds the first provider request before sending any headers.
The hold is released only by local cleanup after `_observe` has received the
actual native `session.error`; a five-second safety deadline bounds a broken
observation rather than manufacturing an early disconnect. The report preserves
`provider_header_wait`, `native_terminal`, `provider_release`, the provider wait
duration, all actual receipts, and the later broker transport error. It does not
synthesize a report, suppress a receipt, or add a request. The regression asserts
the native error message, one receipt/request, empty final text, zero retry events,
and the `after_native_error_cleanup` lifecycle.

## Red and green evidence

The regression was added at the actual `OpenCodeProbe.run("timeout_once")` call
site before the lifecycle repair. Against the original sleep/disconnect behavior:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_opencode_probe.py -k header_timeout -q --basetemp .cache/dg01-timeout-red-regression-001
1 failed in 5.10s
provider_release: elapsed_disconnect
```

After the repair, the same command passed. The recorded Windows event was
`session.error` with `The operation timed out.`, with one provider request, one
broker receipt, no retry event, empty final text, and a 0.563-second header hold.
Five additional fresh Windows repetitions passed (4.12–4.78 seconds each).

The Linux verification used the actual ELF selected by CI's locked runtime layout,
not the Windows executable:

```text
/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode
SHA256 ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650
version 1.18.29
```

Three fresh Linux timeout runs produced `runtime_error`, one receipt/request, zero
retries, and header holds of 0.608, 0.646, and 0.649 seconds. The same actual
runtime also completed the normal tool and 429 paths, observed the real disconnect
retry and bounded cancel behavior, retained admission/cleanup negatives, and
rejected model, permission, and endpoint configuration tampering.

Windows focused probe/management tests passed in three groups: 5 core, 6
admission/config/retry/disconnect, and 2 timeout/cancel; management tests added 2
more. Ruff passed for the changed adapter/test files. Strict `mypy backend/karajan`
passed for both `--platform win32` and `--platform linux` (150 source files).

## Limits

This proves only the local fake-provider timing contract. `live_qualified` and
`profile_enabled` remain false, and no cancellation result claims remote stop.
The previous Windows CI failure belongs to the baseline and has not been erased by
a remote rerun. Final candidate CI, independent review, integration into `dev`,
and Issue state changes remain outside this repair.
