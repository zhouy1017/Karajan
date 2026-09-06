# Stopped native projection capture — author evidence

Base: `1534825`, worktree `go-task-capture`. Owned product files are
`isolation/opencode_runtime.py` and the new private `_opencode_capture.py`; the
public tests are `tests/isolation/test_opencode_projection_capture.py`.

`IsolatedOpenCode.capture_projection()` accepts no caller stop receipt or path.
Only a successful start with an explicit controller projection can produce a
`StoppedProjection`. Capture actually closes the owned runtime, requires confirmed
local stop, then checks the original init/process identities and PID namespace
again. It does not claim that remote inference stopped.

The frozen result contains `runtime_sha256`, a tuple of frozen `ProjectionEntry`
descriptors (`path`, initial `sha256`, `writable`), and a tuple of `(path, bytes)`
file pairs. `stop_evidence` is a fresh decoded view of internally retained JSON.
The first successful result is cached and returned unchanged on repeated capture;
later host writes cannot replace its bytes. A failed capture remains rejected.
The result makes no claim about Run, Task, Attempt, fence, approval or qualification;
the consuming controller must independently establish those bindings.

Start pins the workspace root, required parent directories and original file
inodes using non-following Linux FDs with close-on-exec. Pins prevent reuse of the
original inode and are not passed to the native process. Failed start releases
them; completed capture releases them; an unused stopped runtime retains them
until the runtime is disposed. Start remains compatible with existing diagnostics
that place unprojected files in the host workspace. Capture requires an exact tree
of projected files and necessary parent directories, with no extra empty directory.

Capture rejects missing or replaced paths, symlinks, shared hard links, changed
read-only files (including restored bytes and mtime), unstable reads, files larger
than 8 MiB, and snapshots larger than 64 MiB. Initial pinning is also bounded to
4,096 files plus required directories. Both identity and tree checks are repeated
around bounded reads. This is an owned-workspace capture boundary after stopping
the namespace, not a general host filesystem transaction against a hostile owner.

The public suite has 16 Linux cases. Its end-to-end case uses the prepared native
OpenCode ELF, actual namespace/management/Unix HTTP transport, and an HTTP fixture
instead of a real provider. The native read/edit tools modify `src/task.py`; the
observed file inode is preserved. The stopped bytes are frozen through the public
CandidateStore and materialized as the complete registered Git baseline, retaining
the unprojected binary, empty and executable files. Only the target changes, and
the check/review gates still report their missing evidence. Test Actor/Writer
bindings are explicit synthetic fixtures; this does not prove approved Run capture.

The stop-unknown case runs real startup and pidfd kill, but injects unavailable OS
wait/poll receipts through the subprocess boundary. Capture refuses the retained
unknown receipt. No actual stuck kernel process is claimed by that case.

Evidence:

- `tracer-red.xml`: public type missing before implementation (expected import error).
- `tracer-green.xml`: real native startup, stop and immutable capture passed.
- `readonly-red.xml`: actual rewrite/restoration escaped the initial implementation;
  retaining the initial file version closes that gap.
- `native-edit.xml`: early end-to-end assertions passed, followed by a misplaced
  test assertion raising NameError. This test-editing error was corrected before
  the final suite; it is not classified as a runtime failure.
- `final.xml`: all 16 public capture cases passed in 153.68 seconds.
- `windows.xml`: all 16 cases correctly skip on Windows; imports succeed.
- `regression.xml`: existing native lifecycle and projection compatibility run.
- `freeze.json`: exact product/test/evidence digests and final local check outcomes.

Reproduction from the worktree in prepared Linux CI/WSL:

```sh
PYTHONPATH=backend:tests/isolation:tests/candidates \
KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 \
KARAJAN_OPENCODE_LINUX_BINARY=/path/to/prepared/opencode \
python -m pytest tests/isolation/test_opencode_projection_capture.py -q \
  -o cache_dir=/tmp/karajan-capture-pytest-cache \
  --basetemp=/tmp/karajan-capture-replay
```

The artifact used locally is the existing fixed Linux ELF under
`Karajan/.cache/go-linux-runtime/package/bin/opencode`; its expected SHA is
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
No real key or provider was accessed. Only synthetic test repositories were created;
there was no project Git mutation, product edit outside the assigned scope, or CI
failure repair.
