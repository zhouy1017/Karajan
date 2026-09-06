# Fixed OpenCode namespace author evidence

Scope: the three new isolation modules and their public runtime tests. Base: `15084c2`.
This is author evidence, not an independent review or a provider qualification receipt.

The real fixed Linux x64 OpenCode 1.18.29 binary runs inside new user, mount, PID,
and network namespaces plus a chroot. Its SHA256 is
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
The tests use synthetic HTTP/SSE responses; no provider or user credential is used.
The existing `_namespace.py` mount/capability helpers are reused unchanged.
The artifact is rechecked at construction, at start, and on the actual read-only mounted
ELF before execution. A path replacement after the start check is rejected at that mount.

## Public interface

```python
runtime = IsolatedOpenCode(binary, new_controller_directory, relay_socket, local_capability)
(runtime.workspace / "fixture.py").write_text(initial_fixture)
runtime.start()
session = runtime.request("POST", "/session", {"title": "Fixed tools", "agent": "probe"})
runtime.request("POST", f"/session/{session['id']}/prompt_async", {
    "agent": "probe",
    "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
    "parts": [{"type": "text", "text": "Read and edit only fixture.py."}],
})
messages = runtime.request("GET", f"/session/{session['id']}/message")
probe = runtime.probe_lifecycle()  # optional fixed diagnostic; no arguments/commands
closed = runtime.close()
```

The controller owns the fresh directory and fixture materialization. `start()` rejects
a linked `fixture.py`. Concurrent mutation of these paths by a trusted host controller
is outside this diagnostic's authority boundary. No arbitrary argv, configuration,
permission, file attachment, or unissued session ID is accepted by the management API.
Only the individual `fixture.py` inode is projected into `/workspace`; the directory is
read-only while that file bind is writable. If the controller supplied no fixture, start
creates an empty private regular file for health probes. Other host workspace files and
links are absent from the namespace, as observed through the actual init process root.
This is a fixed single-file diagnostic projection, not a general repository projection API.

Only the outside socket inode is bind-mounted at `/bridge/inference.sock`; its parent
directory is absent. Inside, a fixed loopback bridge at `127.0.0.1:5001` forwards only
`POST /v1/chat/completions`, preserving the local Bearer capability and native session
header. Management uses the separate inherited socketpair and native server port 5002.
The native process does not inherit the socketpair; its password is outside `/workspace`.
Do not publish raw `GET /config`: that trusted response contains the local relay capability.

The observed no-Git native worktree is `/`. OpenCode 1.18.29 evaluates tool permission
patterns relative to that worktree, so the exact permitted pattern is
`workspace/fixture.py`, representing `/workspace/fixture.py`. This was verified through
actual read/edit and the fixed upstream [read implementation](https://raw.githubusercontent.com/anomalyco/opencode/v1.18.29/packages/opencode/src/tool/read.ts).

## Evidence coverage

- Actual native version, four different namespace identities, chroot host path absence.
- Actual native configuration and exact read/edit fixture permissions; disabled other tools,
  plugins, MCP, LSP, formatting, updates, snapshots, and compaction.
- Actual native read then edit through the bound Unix socket, with session/capability headers.
- Actual native read denial for `/proc/self/environ`, `/proc/1/fd`, `/control/inner.py`,
  `/tmp/config`, `/tmp/data`, and a synthetic protected workspace file; its content does not
  appear in management messages or upstream requests.
- For a host-only protected file, two native edit guesses produce the same missing-file
  error because the file is not projected. This is OS exclusion, not a claim that the SDK
  checks edit permissions before every content precondition. No blanket claim is made that
  all native internal `/tmp`, `/proc`, or management state is unreadable in every operation.
- Both management boundaries reject authority-changing bodies and unissued IDs.
- Native process FD inspection shows no control socket inheritance; network inspection
  observes only loopback and no IPv4 route; capabilities are zero and no-new-privileges set.
- The optional fixed lifecycle probe creates a detached `setsid` leader and another
  `setsid` child. PID/birth observations confirm the known subtree stops when namespace init
  is killed through a pidfd. This proves lifecycle containment, not shell-tool qualification.
- Startup response loss uses the real process and a fault injected only at the socket
  receive boundary. Cleanup still runs; without observed init identity, stop remains unknown.
- Start claims the instance once under a short lifecycle lock. Close can cancel before
  process creation or while the native welcome is pending. Process creation and final
  startup publication recheck that state; no lock is held while waiting for the welcome.
- A process that disappears during `/proc` read is handled whether the kernel reports
  ENOENT or ESRCH. Returned lifecycle/stop evidence is detached from retained observations.
- `runtime_tools_status` stays `not_run`, `dispatch_eligible` stays false, and `remote_stop`
  stays unknown. The separate GoRelay/GoJournal composition belongs to the root author.

## Re-run

From the repository root on the qualified Linux/WSL environment:

```sh
KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 \
KARAJAN_OPENCODE_LINUX_BINARY=/absolute/path/to/the/pinned/opencode \
python -m pytest -p no:cacheprovider --basetemp /tmp/karajan-go-namespace-rerun \
  tests/isolation/test_opencode_runtime.py -v --junitxml=/tmp/karajan-go-namespace.xml
```

Without the binary environment override, tests look in the repository's locked runtime
`runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode`. With the require flag,
a missing artifact fails. Non-Linux platforms explicitly skip; their result is not evidence
of isolation success. Type-check these Linux-only modules with `mypy --platform linux`.

`final.xml` and `source-final.json` identify the final run and exact source bytes.
`pre-public-annotations.xml` and `pre-platform-guards.xml` are earlier complete runs before
explicit public Path types and cross-platform static guards. The final run is repeated
after those changes; repeated runs are not counted as additional cases.
`pre-independent-review-source-final.json` and `pre-independent-review-final.xml` retain
the earlier nine-case freeze. `pre-projection-*` preserves the thirteen-case correction
before the single-file projection. The current final has fourteen distinct public tests and
eight additional passing subtests; pytest JUnit includes those subtests in its numeric total.

## TDD observations

The chronological red observations were: absent module; absent management method; no
inference bridge; an exact relative-path permission mismatch; accepted extra management
authority; absent lifecycle probe; absent FD/network observations; accepted linked fixture;
and startup-response-loss cleanup overstating unobserved stop. Each was corrected before
the final run. The permission mismatch was implementation behavior under the synthetic
fixture, not an independent-review finding. Startup cleanup was raised by root review.
No tests call a real model or treat synthetic responses as qualification facts.

## Independent review corrections

The independent reviewer `qualification_integration` found four issues in the initial
freeze: an actual ESRCH read race escaping close; two simultaneous starts spawning two
namespaces; a caller mutating retained nested stop evidence through a returned result; and
an artifact changed after construction still executing under the original reported hash.
These findings belong to that reviewer. Their original independent reports remain in
`.cache/go-namespace-review`; they are not recounted as author discoveries here.
The same reviewer then found a fifth issue: the two native edit errors for a protected
workspace file exposed whether a guessed string matched its content. There was no observed
plaintext disclosure or unauthorized write. The approved fix removed that host file from
the namespace rather than changing the SDK: only the allowed fixture is projected.

The author's unchanged replay of the first three public cases produced two failures and
one pass (`review-red.xml`); the initial ESRCH observation belongs to the reviewer's run.
The artifact original case separately failed (`artifact-red.xml`). The focused correction
reports and final full run record the fixes. Four permanent author regression cases also
cover pre-spawn cancellation, cancellation during pending welcome, ESRCH after observed
process exit plus immutable retained evidence, and atomic replacement after the start hash
check. Independent final review is recorded separately by the reviewer.
`projection-red.xml` records the old whole-directory exposure; `projection-correction.xml`
records actual OS projection and successful native read/edit on the file bind.
`oracle-correction.xml` replays the reviewer's updated exact equality property and passes.
