# Independent review of the resumed relay repair

Review base: `7c85669a3d2a5b717bbcaea7f8dbbf2c5ea937a0` (`HEAD`).
Scope is the uncommitted diff in `go_relay.py`, `test_go_relay.py`, and
`test_go_relay_context.py`. No later author's execution is counted as this
reviewer's result. Reviewed product SHA-256:
`deb0239590d67ddfb077f025773bb9c7c2947a7f3379c693f7ee469d0aab2453`.

The fixed repair brief and original independent cases come from
`../spark-ci-10053-independent/HANDOFF.md` and `README.md`. Repository standards
come from `AGENTS.md`, `docs/agents/issue-tracker.md`, the existing coordinator
ADR, and `docs/implementation/m3-go-task-context.md`'s durable-send recovery
contract. The `code-review` smell baseline was applied as a judgment heuristic.
All four collaboration slots were occupied; one reviewer assessed the two axes
separately, rather than claiming they were two independently staffed reviews.

## Standards

**No blocking documented-standard violation found.** The change keeps HTTP
rejection cleanup in the relay, reuses the existing byte cap and Journal
recovery/revocation paths, and does not add a scheduler or broaden caller
authority. Authentication errors retain their existing precedence. It preserves
the unknown committed call rather than inventing an unused slot or a refund.

One **non-blocking judgment observation** (possible speculative generality /
duplicated code): `_set_request_body_budget` still accepts an unused `None`
default which restores the maximum budget, and the parsed `size` is assigned
repeatedly at lines 635, 637, and 639. Every current call supplies an integer and
the duplicates do not change behavior. A future focused cleanup could remove
the unused branch and redundant assignments; this is not a functional finding
or a requirement to broaden the present CI repair.

## Spec

**No new confirmed defect found in the reviewed version.**

- A single valid Content-Length supplies the pre-rejection unread-byte budget,
  capped at 262,144 bytes. Duplicate/invalid length or Transfer-Encoding leaves
  a zero speculative drain budget. A declared body of 20 bytes is read exactly
  once to its end, and an oversized declaration stops at the existing cap.
- Actual buffered HTTP readers use `read1` with a monotonic total deadline and
  the remaining timeout. A trickling peer receives 403 within the independent
  1.2-second acceptance bound around the 0.5-second drain budget. This evidence
  does not claim that an arbitrary injected reader's fallback `read` has the
  same property; the production Handler uses the standard buffered reader.
- Fully consumed invalid JSON-model payloads are not drained a second time.
  The original HTTPX wrong-capability shape and the delayed split-body case
  remain covered with exact 403 responses, zero relay receipts, unchanged
  Journal bytes, and zero synthetic upstream calls.
- `_recover_context_call` again requires `call_id`; both exception call sites
  pass the ID generated before `begin_call`. The real SQLite public command is
  committed and its response is then deliberately lost. Read-only recovery
  matches the exact grant binding, call ID, and measured request context,
  revokes that original grant, preserves one `send_unknown` call, and blocks
  the second request with 503. No upstream send occurs.

The original P2 findings 10053-REVIEW-003 and 10053-REVIEW-004 are therefore
closed **for this exact product hash**. The earlier deadline and consumed-body
regressions also remain green. This does not determine the exact packet timing
of the original CI failure or qualify a model/provider.

## Independent executions

The original four-case and two-case files are copied here **byte for byte**.
Their SHA-256 values match the untouched originals:

- `test_rejection_boundary.py`: `78792f8df5a199d892c7217de54a2d31dae7f66c8b9c6ba46d9cb318854981a7`
- `test_followup_boundaries.py`: `7a8853d2002badaa4eb2a36ce0e54a9307c96dc04d572c5906afa443fd2dcab1`

Copying is necessary because the follow-up test writes a content-free
observation next to `__file__`; running it in the old directory would overwrite
the prior evidence. The copied file wrote `lost-begin-observation.json` here.

Windows / Python 3.12, independently executed by this reviewer:

1. Original four plus two: **6 passed, 2.32 seconds**, `six.xml`.
2. New actual TCP framing/cap tests: **4 passed, 0.50 seconds**, `framing.xml`.

```text
python -m pytest .cache/relay-resume-independent/test_rejection_boundary.py .cache/relay-resume-independent/test_followup_boundaries.py -o "pythonpath=backend tests/adapters/opencode" -q --basetemp=.cache/relay-resume-independent/tmp-six --junitxml=.cache/relay-resume-independent/six.xml
python -m pytest .cache/relay-resume-independent/test_framing_boundaries.py -o "pythonpath=backend tests/adapters/opencode" -q --basetemp=.cache/relay-resume-independent/tmp-framing --junitxml=.cache/relay-resume-independent/framing.xml
```

Executable: `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe`.
The metered case used the existing pinned tokenizer at
`C:/Users/Chooo/Playground/Karajan/.cache/go-task-execution/.cache/go-context-artifacts`;
`KARAJAN_REQUIRE_GO_TOKENIZER=1`, `HF_HUB_OFFLINE=1`, and
`TRANSFORMERS_OFFLINE=1` were set. Transport was real local TCP/HTTPX; the
upstream and credentials were synthetic. No network download or provider call
was performed.

`before-sources.json` and `after-sources.json` match all five recorded original
product/test/input paths. `before.patch.txt` and the three `.before.py.txt`
copies preserve the exact diff and source examined. `freeze.json` selects only
source, tests, XML, and content-free reports, excluding the temporary databases.
Any later product change needs separate assessment; no old passing result is
automatically transferred to it. Product files, formal tests, dependencies,
Git, GitHub, and the original independent evidence were not modified.

Standards: zero blocking findings, one non-blocking heuristic observation.
Spec: zero new confirmed findings; original two outstanding P2s pass on the
recorded hash. Author broad regressions and final CI remain root/author work.
