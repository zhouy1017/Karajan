# Independent Go relay integration review

Reviewed `backend/karajan/adapters/opencode/go_relay.py` against
`15084c2d07f36165c1e00901d6689c3fd105b749`. The author's `go_journal.py`
implementation is a dependency of these integration tests, explicitly excluded
from this review's independent-review claim. No product source was changed.

Current result: **0 unresolved findings**. One state-reporting defect was independently
reproduced and fixed by the relay author during review.

## Resolved finding

An HTTP 200 status was sufficient for `_complete_journal` to write
`response_received`, even when reading the SSE body subsequently raised `ReadError`.
The public HTTP test observed a native-facing 502 alongside that incorrect durable
state. This was inconsistent with retaining unknown outcomes after transport loss
and with the execution architecture's conservative treatment of incomplete evidence
(`docs/architecture/03-execution-and-delivery.md`, lines 27, 60 and 118).

The author added an explicit observation that the response body was fully read.
The same test now passes: status 200 remains an observed fact while the incomplete
response stays `send_unknown`. This distinction does not prove remote stopping.
Initial source SHA256: `15b74fd27f70e310215d113c3b56fd070390b6bfd75893418832bf798f81084d`.
The initial failing run is retained in `windows-initial.xml`.

## Independent checks

Final test collection: **11 cases**. Windows: **8 passed, 3 Linux skips**.
WSL2: **11 passed**. Ruff passed for the independent test.

- Exact grant binding is checked before upstream sends, and relay construction
  detaches the binding from later controller object mutation.
- The journal capability is not accepted as the native relay capability. Repeated
  native session/call headers cannot select or merge trusted logical call IDs.
- Lost `begin_call` commit returns stop the relay before upstream sending while
  preserving the committed unknown count. A historical `send_allowed=False`
  receipt also prevents an upstream send.
- Completion-persistence failure stays visible in relay receipts and leaves the
  journal unknown. Native HTTP delivery may already have succeeded; this is not
  a qualification result and the producer must inspect durable and local evidence.
- Cancellation revokes the grant before closing transport. An in-flight handler
  stays unknown while blocked; a restarted relay using the revoked grant emits
  no new upstream request. Subsequent transport closure does not erase the count.
- Real Unix path tests preserve occupied sockets and dangling symlinks, and closing
  the relay preserves a different socket that replaced its old pathname.

The review also inspected the fixed upstream URL, copy of authorization binding,
credential redaction, completed-record timing, condition-lock/handler cleanup, and
the absence of automatic grant revocation in `close`. No actionable documented
standards violation or Fowler-baseline smell remained in this bounded diff.

## Evidence limits and environment

The relay, HTTP parser, SQLite journal, TCP transport and Unix socket paths are real.
Upstream HTTP uses explicit `httpx.MockTransport` responses and synthetic credentials.
Two journal subclasses inject failures at their **public** boundary; they do not
fabricate persisted receipts. This review makes no provider, OS isolation, model
qualification, budget guarantee or remote-cancellation claim.

WSL DrvFS (`/mnt/c`) rejected Unix pathname sockets with `EOPNOTSUPP` in an initial
environment attempt (`wsl.xml`: 8 passed, 2 environment failures). The final Unix
tests use short, automatically removed native Linux `/tmp` directories. Test code,
SQLite temporary data and retained evidence remain in the worktree review directory.
The producer likewise needs native Linux storage for its actual Unix socket paths.

`review.json` records exact final source, helper, test and JUnit hashes. The initial
test file itself was not separately frozen before expansion; the original JUnit
and its failing assertion are retained without claiming a nonexistent source hash.

## Reproduction

From this worktree, with its backend explicitly selected:

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'backend')
& C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe -m pytest .cache/go-relay-review/test_relay_review.py -q -p no:cacheprovider -o 'pythonpath=backend tests/adapters/opencode' --basetemp=.cache/go-relay-review/replay-tmp
```

From the same worktree in WSL, use the available Linux Python environment. Unix
tests require a native Linux `/tmp`, not DrvFS. They remove their own socket directories:

```sh
PYTHONPATH="$PWD/backend" /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest .cache/go-relay-review/test_relay_review.py -q -p no:cacheprovider -o 'pythonpath=backend tests/adapters/opencode' --basetemp=.cache/go-relay-review/linux-replay-tmp
```
