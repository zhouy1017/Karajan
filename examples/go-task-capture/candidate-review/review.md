# Independent Candidate projection review

Base: `1534825`. Scope: the new `CandidateStore.get_baseline` / `freeze_projection` integration and `candidates/_projection.py`, against `docs/implementation/m3-go-task-capture.md` and the task-runner design. No product findings remain in this bounded review.

Independent public tests use a real temporary Git baseline, the actual CandidateStore, CAS artifacts and materialized files. They confirm complete baseline retention when the original checkout is unavailable; exact writable overlays; read-only, missing, extra, traversal, case-alias and broad-directory rejection; validation of even unprojected CAS bytes and hardlinks; detached baseline reads; replay and immutable candidate revisions. WSL additionally observes executable modes on disk. The existing baseline fixture is reused; the author's projected-capture fixture is not.

One test runs an actual local Python check against the materialized candidate. Its evidence is explicitly recorded as fixture provenance. Even after that check passes, an empty approved-reviewer set leaves `REVIEW_EVIDENCE_MISSING` and the local gate false. Controller author, environment and stopped-writer identities in these tests are synthetic inputs to this low-level port. These tests do not establish native termination, current Run authorization, production environment qualification, model capability or a completed Task execution.

Results: Windows 13 passed, 0 skipped (13.499 seconds); WSL 13 passed, 0 skipped (2.257 seconds). These are the same 13 cases on two platforms, not 26 distinct cases. Ruff and formatting checks pass. Product hashes and exact commands are in `review.json`.

The initial run was 12 passed / 1 failed solely because the test expected LF stdout while the real Windows Python process emitted CRLF; its exit code was already zero. The fixture now compares `splitlines()`. Original test bytes are retained in `history/initial-test.py.txt` and original output in `initial.xml`. There was no product change or product finding from that correction. A long adjacent string literal was also wrapped without changing the executed argv.

The earlier baseline-materialization implementation was authored by this reviewer in a previous slice. This review independently exercises the new projection integration through that public API; it is not presented as an independent re-review of the older helper implementation. Host guard authorship and its separate independent review are outside this Candidate review.
