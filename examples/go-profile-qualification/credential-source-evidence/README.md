# Credential generation author evidence

This slice changes only `backend/karajan/projects/credential_sources.py` and
`tests/projects/test_credential_sources.py`. Baseline: PR #53 head `8a3d4ea`.
These are author tests and evidence, not an independent review.

## Interface

`CredentialSourceStore(projects, sources=..., private_directory=..., clock=...)`
uses the existing ProjectRegistry database and owner transaction. Controller-only
configuration maps `(project_id, auth_ref)` to `LocalKeyFile(source_id, path)`.
No operation accepts a request-supplied path or credential value.

- `register(project_id, auth_ref, principal=..., command_key=...,
  expected_generation=None|previous_uuid)` performs initial registration or a
  compare-and-swap rotation. The server chooses the new immutable generation UUID.
- `current(...)` and `current_locked(db, ..., principal=...)` return the same
  immutable public generation record. The locked port reuses the caller's existing
  ProjectRegistry `BEGIN IMMEDIATE`; it does not start a nested transaction.
- `resolve_exact(project_id, auth_ref, generation, principal=...)` verifies the
  current source and exact material and returns a frozen `ResolvedCredential`.
  Its public fields are project_id/auth_ref/generation/source_id, while `reveal()`
  explicitly returns the provider key. Repr/str are redacted and pickling is denied.
- `get(...)` returns `{record, revoked, revocation}`. `revoke(..., command_key=...)`
  appends a separate revocation fact and returns the same view. History is retained.

The public generation record is exactly:

```json
{
  "schema_version": "karajan.credential-generation.v1",
  "project_id": "controller-project-id",
  "auth_ref": "secret:go",
  "generation": "server-generated-uuid",
  "source": {"kind": "controller_local_key_file", "id": "configured-source-id"},
  "registered_at": 1000.0,
  "previous_generation": null
}
```

It contains no mutable checked_at/state wrapper, file path, key, or material digest.
The public database contains public-record and command digests only. Material
seals are HMACs in private SQLite state and include the original file bytes,
source path and immutable public record. Changing only a BOM/newline also changes
the material identity even when the normalized credential string is unchanged.

## Persistence and private storage

The private directory contains a random 32-byte HMAC key and material seals; it
does not contain a copied provider key. POSIX checks actual current ownership and
0700/0600-style permissions. Windows reads the actual owner/DACL through Win32
APIs and permits only that owner, owner rights, SYSTEM and administrators. The
existing directory is validated on reopen; weakened permissions are rejected and
not silently repaired. Directory creation uses Python's owner/admin/SYSTEM-only
Windows mode 0700 behavior, which was observed on the provided Python 3.12.14.

WSL private state must be on native Linux storage. Windows ACLs cannot be inferred
from the mode bits of a DrvFS mount. The directory and its parents are controller-
managed; this is not a defense against a hostile controller account or host admin.
Private state inside a registered repository is rejected before creating files.

The private intent is committed before the public generation. A failed public
commit leaves a private orphan: repeating that command rejects with
`CREDENTIAL_REGISTRATION_INCOMPLETE`, never promotes it, never silently rotates,
and never invents a replacement result. A separately authorized new command may
create a new generation. Loss of private state fails closed instead of creating
a replacement HMAC key from public metadata.

Public transaction stability does not mean file-lock atomicity. Actual material
is read and compared at every current/resolve check. A returned Python string
cannot be revoked or reliably erased; qualification must recheck the source at
completion and during later fact consumption. Registration-command replays return
immutable history even after material changes/revocation, and are not current
authorization. Explicit resolution rejects stale or revoked generations.

## Validation

Final Windows and WSL2 author suites each contain 22 passing cases. Windows
mypy was run with both `--platform win32` and `--platform linux`; both pass.
Ruff passes. The WSL Python environment has no mypy installed, so no native-WSL
mypy execution is claimed.

Coverage includes real project creation/owner persistence, private-state reopen,
material changes with identical mtime, source-path and source-id changes, complete
history/rotation/revocation, concurrent idempotent registration, command conflicts,
locked reads blocking revocation, public-commit failure with a retained private
orphan, missing private files, actual weakened Windows/POSIX permissions, malformed
key bytes, source hard links, and scanning exported public SQLite data for key,
path and material-digest absence. Test keys and fixture repositories are synthetic.
No actual provider key was read, no model/provider was called, and no product Git
commit was created. Synthetic test repositories use their own fixture Git history.

TDD history is retained: `red.xml` records the missing public module; the first
public persistent chain then passed. `expanded-before.xml` records the test that
caught private files being created before an unsafe repository-location rejection;
`expanded-after.xml` records its correction. `linux.xml` includes one fixture-only
failure because DrvFS rounded restored nanosecond timestamps to whole seconds.
The final test uses a whole-second timestamp before registration, then preserves
that exact timestamp while changing the actual contents on both platforms.

`freeze.json` binds the final source, test and evidence bytes. The final behavior
runs are `windows-frozen.xml` and `linux-frozen.xml`. Temporary private state is
removed by the test fixture. On WSL only private state uses native `/tmp`; project
databases and retained evidence remain in the worktree cache.

## Reproduction

From this worktree on Windows:

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'backend')
& C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe -m pytest tests/projects/test_credential_sources.py -q -p no:cacheprovider --basetemp=.cache/credential-source-evidence/replay-tmp
```

From this worktree in WSL:

```sh
PYTHONPATH="$PWD/backend" /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/projects/test_credential_sources.py -q -p no:cacheprovider --basetemp=.cache/credential-source-evidence/linux-replay-tmp
```
