# Relay rejection and lost-call recovery evidence — 2026-09-07

Implementation: `03984aa08aecf79c33c44a9a054a707ae04748e0`.
Base: `7c85669a3d2a5b717bbcaea7f8dbbf2c5ea937a0`.
Reviewed production relay SHA-256:
`deb0239590d67ddfb077f025773bb9c7c2947a7f3379c693f7ee469d0aab2453`.

This repair bounds rejected HTTP request-body draining by both the actual unread
body extent and a total deadline. Ambiguous framing receives no speculative
drain budget. It also restores the required call identity when a real Journal
begin commits but its reply is lost: recover that exact unknown call, revoke
the original grant, and block another send. It does not manufacture a refund,
an unused request slot, provider completion, or a new execution permission.

The implementation commit contains the three reviewed source/test blobs.
[publication-map.json](publication-map.json) binds their Git blob identities and
SHA-256 values, and maps every selected original file to its published copy.
The author's and independent review's original pre-commit freeze language is
retained; this newer manifest supplies the commit association without rewriting
their historical statements.

## Results and scope

| Executor and evidence | Recorded result | What it proves |
|---|---|---|
| Luna author: [formal.xml](author/formal.xml), [formal.log](author/formal.log) | 196 Windows formal cases passed, 0 failed, 0 skipped | Existing relay, context, Journal, and nullable-name regression coverage on this implementation |
| Luna author: [independent.xml](author/independent.xml) | 6 copied regression cases passed | Author reran the original independent inputs; this is not a second independent review |
| Independent reviewer: [six.xml](independent/six.xml) | Original 6 cases passed | Real TCP/HTTPX rejection boundaries and real SQLite lost-begin recovery |
| Independent reviewer: [framing.xml](independent/framing.xml) | 4 new TCP cases passed | Duplicate/invalid length and Transfer-Encoding avoid speculative drain; oversized declared bodies retain the existing byte cap |
| Independent Linux follow-up: [linux.xml](linux-followup/linux.xml), [result.json](linux-followup/result.json) | 199 passed, 0 failed, 0 skipped, 17.04s | The same 196 formal case identities plus 3 Unix transport cases, independently executed in WSL; 125 input hashes unchanged |
| Author static checks: [ruff.log](author/ruff.log), [mypy-source.log](author/mypy-source.log) | Affected Ruff passed; production relay mypy passed | Static results for the recorded source scope |
| Root's actual tool-output report | `ruff check .`: exit 0, `All checks passed!`; `mypy backend/karajan`: exit 0, `Success: no issues found in 118 source files` | Current full-repository lint and backend typing; no raw log file was supplied, and this archive does not fabricate one |
| New PR/current-head CI and merge | G pending; not merged | Local passing evidence does not replace the new remote checks or owner merge decision |

The independent boundary review executed 10 cases (6 original + 4 new), not 10
newly designed cases. Its later Linux follow-up independently executed 199
formal/Unix cases; that execution is a separate layer, not 199 new test designs.
Repeated executions by the author are reported separately. The
author's formal terminal time is 16.08s; its JUnit time is 16.034s. Other XML
and report times are likewise retained as their original measurements.

[Independent Standards/Spec review](independent/README.md) records zero blocking
Standards findings, one non-blocking judgment observation about redundant helper
configuration/assignments, and zero new confirmed Spec defects. One independent
reviewer assessed both axes separately; these were not two separately staffed
reviews. The outstanding P2 findings `10053-REVIEW-003` and `10053-REVIEW-004`
are closed for the exact `deb02395…` production bytes. Earlier deadline and
already-consumed-body regression cases also passed. The optional helper cleanup
is not hidden or promoted into a new blocking requirement.

All requests used a synthetic upstream and synthetic credentials. TCP/HTTPX,
buffered request handling, the offline pinned tokenizer, and SQLite Journal
behavior were real local mechanisms. There was no provider call, real credential
use, or S qualification. The controlled historical reproduction does not recover
the exact packet timing of the original Windows CI failure.

## Retained static-check limitation

[author/mypy.log](author/mypy.log) contains **20 errors in the optional test-module
typing run**. They are not all characterized as preexisting: the report includes
untyped fixtures/calls and return/dictionary typing errors, and this archive does
not claim a per-error historical attribution that was not established. The
actual CI command types `backend/karajan`, not the test modules. Production relay
mypy passed, and root separately confirmed all 118 backend sources passed.
The optional failing output is retained without weakening the configured gate.

[failed-harness-static.txt](author/failed-harness-static.txt) records a malformed
workdir rejected before a static-check process started. It is not a product
failure and is not counted as an executed static check.

## Archive layout and historical references

This directory contains exactly 37 byte-preserved originals:

- `author/`: the 11 explicitly selected Luna author files.
- `independent/`: the 13 files named by the independent freeze plus that
  [freeze.json](independent/freeze.json), 14 files total.
- `history/`: four explicitly selected records from the prior Spark review:
  [HANDOFF.md](history/HANDOFF.md), [README.md](history/README.md),
  [round-two-new-boundaries.xml](history/round-two-new-boundaries.xml) (2 failed),
  and [round-two-original-four.xml](history/round-two-original-four.xml) (4 passed).
- `linux-followup/`: 8 subsequently authorized files, including the independent
  [README](linux-followup/README.md), [freeze](linux-followup/freeze.json), source
  maps, results, and [run.sh.txt](linux-followup/run.sh.txt). The shell script is
  retained test input, not a production entry.

Every original `.py` input is published as `.py.txt`, and `run.sh` as `run.sh.txt`;
only their filenames changed.
The independent `*.before.py.txt` names describe the source snapshot at the start
of that review. Their bytes are the final reviewed source/test bytes, not the
older defective relay. The map records each exact identity.

Links and commands **inside the copied originals** retain their original
worktree meaning. The roots were `.cache/luna-relay-author`,
`.cache/relay-resume-independent`, `.cache/spark-ci-10053-independent`, and
`.cache/relay-linux-followup` in
the `ci-spark-relay` worktree. Use the manifest's `origin` → `destination`
mapping to find the selected published equivalents. For example, the resumed
review's `../spark-ci-10053-independent/HANDOFF.md` corresponds here to
`history/HANDOFF.md`; its local `test_followup_boundaries.py` corresponds to
`independent/test_followup_boundaries.py.txt`.

The historical files also mention material deliberately outside this whitelist:
`before.xml`, `old-source-control.xml`, old source/observation snapshots,
`minimal-correction.patch.txt`, `../ci-next-failure/`, temporary test directories,
and a proposed `final-six.xml`. Those references are not all resolvable in this
archive. No missing file, unexecuted correction, or future output was fabricated.
Historical “pending correction” and model-limit statements describe that earlier
stage; the current exact-source closure is recorded by the resumed independent
review and this publication, not by editing the old documents.

No temporary database, private library, bootstrap, full CLI event stream,
tokenizer asset directory, or credential file is included. The Linux follow-up
retains its own command and before/after source maps. Its original reference to
`../luna-relay-author/formal.xml` maps to `author/formal.xml` here. The pinned
tokenizer hashes are recorded, but the artifacts themselves are not copied.

## Reproduction inputs

At the implementation commit, the 196-case formal set recorded in JUnit consists
of these repository modules:

```text
python -m pytest tests/adapters/opencode/test_go_context.py tests/adapters/opencode/test_go_journal.py tests/adapters/opencode/test_go_relay.py tests/adapters/opencode/test_go_relay_context.py tests/adapters/opencode/test_go_relay_journal.py tests/adapters/opencode/test_go_relay_nullable_names.py
```

To replay the independent cases, copy the three `independent/test_*.py.txt`
inputs into a fresh disposable directory and remove only their final `.txt`
suffix. Run those copies together with
`-o "pythonpath=backend tests/adapters/opencode"`. The follow-up input writes a
content-free observation next to itself, so do not execute against the retained
originals. Use the pinned offline tokenizer required by the existing public
test helpers (`KARAJAN_GO_TOKENIZER_DIRECTORY`,
`KARAJAN_REQUIRE_GO_TOKENIZER=1`, `HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`). No provider credential is needed. These are replay
instructions; the publication task did not execute them.

## Publication checks

[publication-check.json](publication-check.json) records a separate final
byte comparison of the 37 copied originals against both their source files and
manifest, plus the three implementation Git blobs. Newly written README,
manifest, check metadata, and `.gitattributes` use LF and have no trailing spaces.

Original evidence preserves its existing CRLF, whitespace, and SHA-256. The leaf
`* -text` attribute prevents newline conversion; it does not change whitespace
error rules. No staged-diff whitespace check was run by this publication task,
and untracked archive files are not claimed to have passed one. Existing raw
evidence whitespace is a publication property, not a product or test CI failure.
Root will inspect the actual staged diff separately. No product tests were
rerun and no Git or remote state was changed while preparing this archive.
