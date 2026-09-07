# Final independent Spec review: #120 / #122 / #123

Fixed candidate `205cd9063e14ad516c1a21cb1d62f674bda75885`, base `6cf89abc1a6ca8cb99d76f82191dc7d82efeb05b`. Reviewer GPT-6 high, 2026-09-07.

**Spec passed: zero unresolved findings in the current combination.** This is review approval, not a claim of final CI completion, merge, or issue closure.

## #120 final DNS finding closed

The exact production child program now catches only socket.gaierror and returns a restricted status/errno envelope. Parent maps EAI_AGAIN to the existing internal retryable ConnectionError; other resolver failures remain deterministic. The independent test injects an actual platform gaierror inside the child's getaddrinfo call, retaining the production serializer, real Popen/communicate, parser and provision retry loop. EAI_AGAIN now makes three attempts; EAI_NONAME makes exactly one. Both preserve old bytes and clean temporary files. The former test script and failing output remain unchanged; the new script adapts injection to the new protocol instead of bypassing its exception handler.

Ten affected independent checks passed: actual localhost child resolves IPv4/IPv6; blocked child killed/reaped (0.15s budget, 0.172s observed); transient and deterministic DNS classification; real multiple-address TCP budget; malformed JSON, unknown status, arbitrary/signed-URL error code and invalid address shape reject safely; slow EAI_AGAIN attempts share 0.30s budget (two children, 0.328s total). All children observed reaped; all failure temp files removed. CLI contains only stable error categories, no injected URL, exception text or path.

Affected regression: **31 passed in 4.67s** (28 author + three prior independent), zero skips. Prior independent **12/12 actual verified HTTPS cases on aee3** remain applicable: this delta changes only resolver result encoding/classification; TCP/TLS/select, deadline propagation, standard HTTPResponse/BufferedReader and body/file publication are unchanged. Current localhost success and DNS failure tests exercise the changed resolver protocol. No full suite or unchanged asset tests repeated.

## Original findings closure

- Limited transient retries and fixed shared deadline: closed, including reads, slow headers/chunk framing/trailers, redirect response headers, TCP/TLS handshake, blocking resolver and DNS retry phases.
- Permanent certificate failure wrongly retried: closed; deterministic cert, integrity, unsafe redirect and filesystem failures remain non-retryable.
- Content-Length waiting for connection close and chunk TCP fragmentation regression: closed; standard HTTPResponse framing preserved.
- Temporary DNS error classification: closed by this candidate with real-child evidence above.
- Length/hash validation, atomic replace, existing-file protection, temp cleanup, fixed source/revision/digest and no proxy/auth changes remain intact within the reviewed scope. Error output stays redacted.

## #122 / #123 exact inheritance

Workflow/budget/docs snapshots are byte-identical to the reviewed e0a3 scope; CheckRunner source/tests/evidence docs are byte-identical to reviewed 88dcd. Prior #122/#123 Spec passes therefore bind this combination. Exact hashes and changed-file audit are in `final-205-inheritance.json`. No unreviewed combination paths. Current CI/merge acceptance remains separate.

## Reproduction

Root cwd `C:/Users/Chooo/Playground/Karajan`:

```powershell
& .venv/Scripts/python.exe .cache/reviewer-high-120/review_205_dns.py > .cache/reviewer-high-120/final-205-dns.json 2> .cache/reviewer-high-120/final-205-dns.stderr.txt
& .venv/Scripts/python.exe .cache/reviewer-high-120/review_205_protocol.py > .cache/reviewer-high-120/final-205-protocol.json 2> .cache/reviewer-high-120/final-205-protocol.stderr.txt
```

Tokenizer worktree cwd `.cache/dispatch-tokenizer-ci`:

```text
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/tools/test_go_tokenizer_provision_retry.py C:/Users/Chooo/Playground/Karajan/.cache/reviewer-high-120/test_spec_boundaries.py -q -p no:cacheprovider --basetemp C:/Users/Chooo/Playground/Karajan/.cache/reviewer-high-120/pytest-205 --junitxml=C:/Users/Chooo/Playground/Karajan/.cache/reviewer-high-120/final-205-regression.xml
```

All new raw scripts/results and inherited actual HTTPS evidence are byte-copied into the final publication bundle, with candidate applicability and SHA-256 in its manifest. Earlier failure records remain in the reviewer directory and prior bundles. No external network/provider/model calls, product changes, or pushes occurred.
