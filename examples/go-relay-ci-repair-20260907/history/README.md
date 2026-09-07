# Spark 10053 patch — independent boundary review

Scope: uncommitted `go_relay.py` and `test_go_relay.py` changes on base `7c85669a3d2a5b717bbcaea7f8dbbf2c5ea937a0`. The reviewer does not implement the repair. Original patch/test bytes and their SHA256 are preserved in `*-before.py.txt` and `before-sources.json`. The original CI failure is separately retained in `../ci-next-failure/`.

**Current result: 2 product P2 findings and one required coverage restoration; pending author correction.** Four independent real TCP/SQLite tests executed on Windows: **2 failed, 2 passed, 1.77s** (`before.xml`). The test's initial bytes remain in `test-before.py.txt`; only unused-import removal/formatting followed in the executable copy. The upstream is an explicit `httpx.MockTransport`; all credentials/capabilities are synthetic. The real Journal stays byte-identical, has zero calls, and relay receipts/upstream requests stay empty in every case.

## 10053-REVIEW-001: the drain has no total deadline (P2)

An invalid-capability client declares 4096 bytes and sends one byte every 100ms. `_drain_request_body` sets a 0.5s socket timeout, but buffered `read(4096)` performs repeated successful socket reads and does not return to the outer loop. The independent client still receives no 403 after 1.2s, over twice that timeout. Stopping the sender and closing its write side then permits cleanup. The sender itself is bounded to at most 25 bytes; the test does not leave a background client running.

The payload byte cap does not bound elapsed time under continuous small arrivals. Use a monotonic overall deadline and a read operation that returns after bounded available progress, applying only the remaining deadline budget. Keep all byte limits and rejection-before-upstream behavior. A test must exercise trickle arrivals across that deadline, not just a 20ms body split.

## 10053-REVIEW-002: consumed payloads are drained again (P2)

The `receipt is None` branch also covers validation failures *after* `_read_request` has consumed the entire body. A real request with a valid capability, a complete 125-byte body and invalid model performs reads `[(125, 125), (125, 0)]`. A transparent `rfile.read` observer records calls without changing their bytes or timing behavior. With a regular client that keeps its write side open, the unnecessary second read waits 0.5s. The author's green XML likewise shows each of the ten invalid-payload cases taking about 0.55s.

Track actual body-consumption state, or isolate the drain to rejections before the body read. Do not infer unread body merely from missing receipt. Never drain another request's bytes as though they were still part of the already-consumed body.

## Preserve the original client regression and qualify the diagnosis

The patch replaces the failing test's original `httpx` wrong-capability assertion with a raw-socket request. Restore the original assertion and add socket-specific controls separately. Our independent original-HTTPX control and split-body control both currently receive 403 and preserve zero side effects.

The author report labels unread-body TCP teardown as the root cause, but the reviewed evidence directory contains three passing 107-case XML files, not an old-source controlled failure proving that hypothesis. The original CI report proves a client-side `WinError 10053`; it does not itself prove the exact TCP cause. Until such evidence exists, describe the explanation as a hypothesis. Do not replace missing proof with retries, weakened assertions or acceptance of resets as HTTP 403.

After the initial review, root requested one independent old-source negative control. `test_old_source_control.py` loads the exact committed `7c85669` relay into a temporary module without editing the checkout, then applies one fixed 262144-byte body split with a 20ms interval to that source and to the saved first Spark patch. The old relay raises `ConnectionAbortedError / WinError 10053` at the second body send; the saved Spark patch returns 403. Both keep zero upstream requests and receipts and close cleanly. `old-source-control.xml` records **1 failed / 1 passed, 0.40s**; each module's observation JSON records the phase and fixed error category. No probabilistic retry was performed.

This controlled result establishes that the old early-close path can produce the same Windows error number under a delayed body. It supports repairing that concrete mechanism. It does not recover the CI request's exact packet timing: CI failed during HTTPX response-header reading with a smaller body, while this controlled case fails during the second send. The two total-deadline/consumed-body findings above remain applicable to the first Spark patch independently of this confirmed early-close mechanism.

```text
python -m pytest .cache/spark-ci-10053-independent/test_rejection_boundary.py -o "pythonpath=backend tests/adapters/opencode" -q --junitxml=.cache/spark-ci-10053-independent/before.xml
```

No product/author tests, authentication, GitHub state or Git state were changed by this reviewer. No real key or provider was accessed. Root retains repair ownership and will assign the corrections to the requested local Spark model.

## Second-patch independent verification

The original four-case test SHA256 `78792f8d...` and original failure/source bytes are unchanged. The author's statement about fixing an independent helper syntax issue is not reflected in these frozen files. Source `c42862bd...` and author-test `2bcb40c8...` are preserved in the round-two source files. The original four cases now pass independently (**4 passed, 2.03s**), closing REVIEW-001/002 for the actual buffered production reader. The HTTPX wrong-capability assertion is restored (with an added session header); the independent original header shape also remains covered.

The `read1` fallback to `read(n)` does not have the same total-deadline property, but the fixed production Handler uses the standard `rbufsize=-1` `StreamRequestHandler` setup, producing a buffered reader with `read1` on both TCP and the inherited Unix transport. No production path to a missing `read1` was found. This review does not manufacture a production failure from a custom fake reader.

Two new problems in the complete second-patch diff were then reproduced with two additional focused cases (**2 failed, 1.59s**, `round-two-new-boundaries.xml`):

- **10053-REVIEW-003, P2:** before checking capability, the code assumes `_REQUEST_LIMIT` unread bytes. A wrong-capability request declaring and sending exactly 20 bytes triggers observed reads `[(16384,20),(16384,0)]` instead of one bounded 20-byte read. Without client half-close this waits until the deadline after the declared body has already arrived. Derive the safe drain budget from a single valid Content-Length before early rejection; do not treat ambiguous framing as a 262144-byte body.
- **10053-REVIEW-004, P2:** unrelated changes drop `call_id` from the generic exception call to `_recover_context_call` and make it optional with an immediate no-op. A real metered Task Journal `begin_call` commits, then the test raises `OSError` to lose the response. The first relay request is 502, but the grant remains active. A subsequent request is accepted, creating a second durable call and one synthetic upstream send. The original contract is revoked grant, subsequent 503, one retained unknown call and zero sends. `lost-begin-before-observation.json` preserves the actual result. Restore the original required call-id signature and call site; this is behavioral recovery, not a typing-only change.

The follow-up tests use the real pinned offline tokenizer and public durable Journal. The lost-return injection lets the public commit finish before throwing; no report or database fact is fabricated. Original follow-up source is `test-followup-before.py.txt`; formatting afterward does not change its cases. Neither finding justifies changes outside the original relay repair scope.
