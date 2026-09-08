# DG01 OpenCode timeout fixture repair

Scope: GitHub #151, original AC1--AC5. This repair executes pinned OpenCode
1.18.29 only against the fixture's two Python loopback peers. It does not read a
provider credential or call a provider, subscription, billing, or cash service.

## Preserved original red

The original failing `dev` tree was
`bdd6830a80110a04e70ed000ce3ec92dc654d9b9`: CI run 34219059454's Windows job
expected `runtime_error` from `timeout_once`, but got `completed` (1 failed,
2888 passed, 169 skipped). The first repair candidate,
`0e53567cd7d1930fd0166eae945299134dba9800`, was independently reproduced as
the same failure in a clean Windows worktree:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest \
  tests/adapters/opencode/test_opencode_probe.py -q \
  --basetemp .cache/root151-final-windows-001
1 failed, 12 passed in 54.34s
```

That exact run's test was then named
`test_header_timeout_is_reported_as_error_without_inventing_a_retry`. Its
actual evidence was three provider requests and three original broker receipts,
a nonempty final, no `session.error`, and an actual retry status event
(`attempt: 1`, `Provider response headers timed out after 500ms`). The first
receipt eventually recorded `RemoteDisconnected`; the retry-path requests
completed the tool loop. This red remains evidence against `0e53567`; it is
not replaced by old green CI or static review.

## Source-backed distinction and repaired fault matrix

Pinned 1.18.29 installs independent timers around the same fetch: the provider
option `headerTimeout` raises a header-specific timeout, while `timeout` is the
general request deadline. The session retry policy can retry the header-timeout
message even though the AI SDK entry's `maxRetries` is zero. The earlier
`timeout=501` / `headerTimeout=500` diagnostic established this distinction,
but its 1 ms separation is scheduling-sensitive and is not a regression test.

`OpenCodeProbe.SCENARIOS` now has two intentionally separate first-request
faults. Both keep the provider waiting before response headers; neither sends
an early EOF.

| Scenario | Native configuration | Required observation |
| --- | --- | --- |
| `timeout_once` | `timeout=400`, `headerTimeout=500` | The general request deadline wins. It is terminal: one provider request and original receipt, empty final, native `session.error` with `UnknownError: The operation timed out.`, and no retry status. Its public CLI exit remains 2. |
| `header_timeout_once` | `timeout=2000`, `headerTimeout=500` | The materially earlier header deadline wins. The provider is released only after the real native `session.status` retry event reports `Provider response headers timed out after 500ms`; it then completes the real tool loop. The run preserves all three admitted receipts, including the first request's late `RemoteDisconnected` broker receipt, rather than merging or hiding it. |

The header scenario's five-second provider wait is only bounded cleanup. Its
normal release is causally gated by the actual native retry event; a
`safety_deadline` or `cleanup_without_native_terminal` lifecycle cannot pass
either timeout test. `provider_release=after_native_retry` is therefore not a
synthetic retry status or a fabricated transport response.

The Linux configuration-isolation repair from `7d255c5` remains: both uppercase
and lowercase proxy variables are fixed to loopback values and both `NO_PROXY`
casings exempt loopback. No inherited proxy or credential can redirect this
fixture outside its peers.

## Current local evidence

At current worktree head `7d255c5ae03286bb446674f349074a40e63d9432` plus this
uncommitted AC1/AC2 completion, the first fresh Windows focused execution was:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest \
  tests/adapters/opencode/test_opencode_probe.py \
  -k 'general_request_deadline or native_header_timeout' -q \
  --basetemp .cache/root151-header-and-request-first
2 passed, 12 deselected in 10.49s
```

The generic-deadline report held headers for 0.516 seconds and recorded one
receipt, empty final, and the terminal event. The actual header-deadline report
held for 0.578 seconds, recorded exactly one retry event and three unique
receipts with the same Attempt/fence; its first receipt had
`transport_error=RemoteDisconnected`, and the latter two had HTTP 200. It had
no `session.error`, completed with the fixture's real read result, and released
only after the observed retry event. Fixture secrets and request bodies are not
published in this document.

Because the original fault was a timer race, the stable, 1.5-second-gap header
case also ran three additional fresh Windows repetitions; all passed in
6.80--7.09 seconds. The complete matrix contains the original 13 probe paths,
this separate header case, and two management tests (16 tests total):

```text
# Windows, pinned official executable
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest \
  tests/adapters/opencode/test_opencode_probe.py \
  tests/adapters/opencode/test_management.py -q \
  --basetemp .cache/root151-ac12-recorded-windows \
  --junitxml .cache/root151-ac12-recorded-windows.xml
16 passed in 58.521s (JUnit: 0 failures, 0 errors)

# Linux / WSL, a temporary test layout with a copied ELF named opencode.exe
PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates \
  /tmp/karajan151-linux-venv/bin/python -m pytest \
  tests/adapters/opencode/test_opencode_probe.py \
  tests/adapters/opencode/test_management.py -q \
  --basetemp .cache/root151-ac12-recorded-linux \
  --junitxml /tmp/root151-ac12-recorded-linux.xml
16 passed in 122.032s (JUnit: 0 failures, 0 errors)
```

The copied Linux binary reported `1.18.29` and SHA-256
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
The temporary venv and layout are test-only; no Windows executable was used
through WSL. On this candidate, `ruff check .` passed, and
`mypy backend/karajan --platform win32` plus `--platform linux` each reported
150 source files with no issues. These local facts do not satisfy AC5's
independent review, CI, merge/readback, or issue-state requirements.

## Acceptance mapping and limits

1. **AC1:** The original Windows red and the race's two native paths are kept
   distinct. The new stable header case exposes the actual retry event and every
   receipt; the renamed general-deadline test covers the terminal event.
2. **AC2:** Headers are held without an EOF until a native event controls the
   release. `timeout_once` asserts the terminal general deadline; the separate
   `header_timeout_once` asserts the real 500 ms header-timeout retry policy.
3. **AC3:** Receipt construction, `receipt_id`, Attempt, fence, provider
   request trace, bounded wait, owned server/socket/thread cleanup, and rejected
   qualification are unchanged. Remote stop remains unknown.
4. **AC4:** The pre-existing tool, 429 retry, disconnect retry, cancellation,
   configuration tampering, admission, cleanup, and proxy-poison tests remain
   in the owned suite. Current-platform results must be bound to this candidate.
5. **AC5:** Not locally closable: independent Standards and Spec review of the
   final commit, required CI, merge into `dev`, final-tree/CI readback, and issue
   state evidence remain Commander-owned gates. No push, PR, merge, or issue
   closure is claimed here.
