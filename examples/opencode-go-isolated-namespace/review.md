# Independent fixed OpenCode namespace review

Scope: `opencode_runtime.py`, `_opencode_namespace.py`, `_opencode_inner.py`
and the author's `tests/isolation/test_opencode_runtime.py`, against baseline
`15084c2d07f36165c1e00901d6689c3fd105b749`.

This is a Standards and security-behavior review of the fixed Linux runtime
boundary. The reviewer did not modify product source. The reviewer's own
`go_journal.py` is explicitly excluded from the independent-review claim.
Root-owned qualification composition and observation authority have a separate
reviewer; they are only consumption context here.

Final result: **0 unresolved findings in this fixed-runtime slice. Five findings
were independently reproduced and corrected during review.** Final WSL2
verification: **7 passed in 67.33 seconds**. Windows: **7 Linux skips**, counted
only as successful collection. Ruff passed for the independent test.

The review applies the documented separation between runtime permissions and OS
isolation, exact runtime/OS capability evidence, exclusive start ownership,
unknown outcomes, and broker-only credential handling in
`docs/architecture/03-execution-and-delivery.md` (lines 3, 15-17, 27, 37, 64,
68-86 and 118). No additional actionable documented-standards or module-design
finding remained within the reviewed fixed-runtime scope.

## Findings and evidence

1. **GO-NAMESPACE-001 / P2 — close crashed on a real procfs exit race.**
   `Path('/proc/<pid>/stat').read_text()` raised `ProcessLookupError` after the
   process exited, but `_birth` caught only `FileNotFoundError`. The initial
   public close call failed instead of returning a conservative stop receipt
   (`initial.xml`). The author now handles ESRCH. A real namespace test also
   reproduces the observed error at the public filesystem-read boundary and
   confirms close finishes, with no remaining observed process.
2. **GO-NAMESPACE-002 / P2 — concurrent starts launched two namespace wrappers.**
   Two calls on one instance passed the non-atomic start guard. The independent
   test observed two actual `subprocess.Popen` results (`initial.xml`), risking
   overwritten process/control ownership. The author now atomically claims
   `starting` and rechecks it immediately before process creation. The original
   simultaneous-start test now observes exactly one actual process launch.
3. **GO-NAMESPACE-003 / P2 — callers could change retained close evidence.**
   A shallow result copy shared `observed_processes` with the retained receipt.
   Clearing a returned list changed a later public `close()` result (`copy.xml`).
   Deeply detached receipts now preserve the original process evidence.
4. **GO-NAMESPACE-004 / P2 — changed runtime bytes could keep a pinned identity.**
   The ELF digest was checked only during construction. Appending bytes to a
   private copied ELF afterward still allowed it to start and report the pinned
   runtime identity (`artifact.xml`). The author added a start-time verification
   and verification of the actual mounted ELF. The original public test now
   rejects changed bytes before starting the native runtime. The shared installed
   artifact was never modified.
5. **GO-NAMESPACE-005 / P2 — denied native edits exposed a host-content oracle.**
   With the entire host workspace mounted, an edit of the prohibited
   `/workspace/blocked.txt` produced a permission denial when a guessed `oldString`
   existed, and `Could not find oldString...` when it did not. Both errors reached
   a subsequent native inference request. The file was unchanged, but that does
   not remove the information leak. `edit-oracle.xml` records the original two
   guesses; `oracle-property-before.xml` separately records the precise equality
   property failing before the mount change. The bounded correction exposes only
   the fixed `fixture.py` inode, leaving other host files outside the namespace.
   Independent `/proc/<namespace-init-pid>/root/workspace` inspection now sees
   exactly that file, with the same inode as the intended host fixture. The
   original two guesses now produce identical missing-file errors, with no
   content-matching distinction. This closes the host-file leak for the narrowed
   projection; it does not fix or generalize the native SDK's permission ordering.

The first four corrections passed the corresponding independent checks in
`repaired-lifecycle.xml` (five passing cases). Its sixth failure concerns the
separately investigated native edit behavior, not a lifecycle regression.
All seven final cases pass in `final.xml` against the exact source hashes in
`review.json`; final source files and the independent test use LF bytes.

## Coverage and limits

The tests launch the actual pinned OpenCode 1.18.29 Linux ELF in fresh user,
mount, PID and network namespaces. The expected ELF SHA256 is
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
Native file calls use a real local Unix-socket relay and synthetic upstream SSE.
There are no provider requests or real credential reads.

Independent checks cover retained evidence, process creation races, procfs exit
races, fixed artifact identity, the eight-session limit, rejection of auth/config/
shell/permission and unknown-session RPC expansion, actual inherited environment
and file-descriptor exclusion, native read/edit attempts, and host-file projection.
Only the socket-pair/process and filesystem-read OS ports are instrumented for
deterministic races; namespace creation, native execution and stop operations
remain real. Synthetic provider response bytes are explicit test fixtures.

In the final eight-operation native file test, the permitted fixture read
completes; six other operations return explicit native permission denials, and
the non-fixture host-file edit returns a missing-file error because that file is
absent from the namespace. The different mechanisms are asserted separately.

The controller's parent environment includes a synthetic credential marker and
an inheritable synthetic file descriptor; neither reaches the actual native
process. Its observed network interfaces contain only loopback, there are no
IPv4 routes, `/mnt/c` and WSL interop are absent, and the control socket is not
inherited by the native process. Source inspection confirms the inference bridge
uses one mounted Unix pathname and the fixed inference route, rather than a
caller-selectable outbound destination.

These results do not qualify any production Profile or general repository
permissions. The fixed fixture boundary cannot be generalized to arbitrary task
paths. Native `/tmp` configuration/state and namespace-internal process data have
their own authority boundaries; this review does not claim they are inaccessible
to every conceivable native tool operation. A local stop receipt remains separate
from remote stop, which remains unknown. Runtime snapshots and close receipts
continue to report `runtime_tools_status=not_run` and `dispatch_eligible=false`.

## Test history

`history/initial-test.py` preserves the initial three-test source. The concurrent
socket-pair barrier was subsequently adjusted so a correctly serialized start
can proceed after its second caller is denied, without manufacturing a timeout
failure. `history/oracle-reproduction-test.py` preserves the original double-guess
assertion requiring native permission denials. The newer oracle property permits
identical missing-file errors when the actual host file is not mounted; it was
independently red on the original mount implementation before the product change.

`native.xml` also contains a test-fixture mistake: the synthetic forbidden-content
marker was itself supplied as an edit argument, then incorrectly asserted absent
from the complete call state. It is not a product leak finding. The argument was
changed to a separate known seed. The subsequent diagnostic runs retained in
`native-*.xml` expose the content-oracle behavior, later reduced to its own test.
No historical failing evidence has been overwritten with a passing result.

## Reproduction

Run from this worktree on Linux/WSL2 with its backend explicitly selected and the
pinned Linux ELF available. Unix socket directories are created under native
Linux `/tmp` and automatically removed; DrvFS `/mnt/c` cannot host these sockets.
No live model is required.

```sh
PYTHONPATH="$PWD/backend" \
KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode \
/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest \
  .cache/go-namespace-review/test_namespace_review.py -q -p no:cacheprovider \
  -o 'pythonpath=backend tests/adapters/opencode' \
  --basetemp=.cache/go-namespace-review/replay-tmp
```

Windows collection skips the native Linux cases; those skips are not positive
behavior evidence. `review.json` records exact final source, helper, test and
JUnit digests, with initial and intermediate source hashes retained separately.
