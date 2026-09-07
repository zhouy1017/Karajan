# Official Go tokenizer provisioning

The CI preparation script downloads the three fixed public tokenizer files at
the pinned Hugging Face revision. It never reads credentials, enables a proxy,
or accepts a source override. Existing files are reused only after their fixed
size and SHA-256 have been verified.

For a missing or invalid file, connection and read timeouts, connection errors,
and HTTP 429 or 5xx responses may be attempted at most three times. Attempts
share the existing 180-second per-artifact budget. Length, digest, redirect,
HTTP 4xx, and filesystem failures are deterministic failures and are not
retried. Every attempt uses its own temporary file; only a verified file is
atomically renamed into place, and failed temporary files are removed.

The command line reports only stable, redacted categories. HTTP failures may
include the numeric status (`TOKENIZER_HTTP_STATUS_429`); transport failures
use `TOKENIZER_NETWORK_ERROR`; exception text, response bodies, signed URLs,
and local paths do not cross the boundary.

Offline retry behavior is covered by:

```text
python -m pytest tests/tools/test_go_tokenizer_provision_retry.py -q
```

The tests replace the artifact table with a small in-memory fixture and never
contact the public source. The existing artifact-backed tests continue to
verify fixed sizes, digests, atomic publication, reuse without network access,
and the required missing-artifact CI gate.
