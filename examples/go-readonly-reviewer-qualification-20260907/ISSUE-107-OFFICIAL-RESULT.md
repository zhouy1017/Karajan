# Issue 107 official fixed Reviewer result

Date: 2026-09-07. Candidate: `dev` merge `6cf89abc1a6ca8cb99d76f82191dc7d82efeb05b`; implementation source is bound by the preflight and the existing #106 source map. PR #108 is merged and its eight required CI jobs succeeded.

The first controller setup check failed before a persistent start because the DrvFS checkout could not be used as a Linux Project repository. That record is preserved in `issue107-first-pre-effect-failure.json`. The first persistent start then failed before every `journal.create_grant`: all three preallocated IDs return `GRANT_NOT_FOUND`. The fixed observer creates those grants before it can construct `GoRelayAuthorization`, and the Relay requires that authorization to send, so the complete Journal observation establishes zero authorized Relay sends for that start. It does not infer a remote refund or remote stop; that remains unknown. The initial missing `reviewer_work_root` parent was a controller setup omission, corrected by creating the existing private root at mode 0700. `issue107-first-start-no-grant-failure.json` preserves this second failure.

The replacement command `issue107-official-go-reviewer-20260907-attempt2` used the same frozen `official_go` source, OpenCode 1.18.29 ELF, pinned tokenizer, `glm-5.3-flash` profile, and fixed suite ref. It made six sends: two in each scenario. All were within the per-scenario maximum of six and total maximum of eighteen; the persisted start had a 600-second limit.

| Scenario | Result | Parsed result | Sends | Read-only / stop |
| --- | --- | --- | --- | --- |
| `clean_review` | passed | pass, 0 findings | 2 | unchanged / confirmed |
| `defect_review` | passed | changes requested, 1 blocking finding | 2 | unchanged / confirmed |
| `denied_read` | passed | inconclusive, 0 findings | 2 | unchanged / confirmed |

`issue107-official-evidence.json` is a desensitized projection of the complete private Store/Journal record. It retains source hashes, hashed attempt/grant identities, call states and usage, final-text digests, parser outcomes, canary-retention result, replay counts, facts, and revoke outcome. It contains no key, capability, raw header, private database, raw model output, or reasoning. The complete final output was retained and parsed at the private parser boundary.

The same command replay returned the identical record and each hashed grant count stayed at two: no new grant, session, call, or official request was created. Before revocation, the real Store exported only the constrained `reviewer` / `read` / T1 existing-file facts, with max six requests and dispatch false. `store.revoke` then made the current Store consumer fail with `QUALIFICATION_REVOKED` while historical readback remained available; provider remote stop is explicitly `unknown`.

The fixed Plan path through `ApprovedReviewerBindings.current_locked` was not consumed before the deliberately irreversible revoke. No Reviewer Task, Candidate Review, Review Evidence, capacity reservation, or quality gate ran. Therefore the direct Store-facts consumption is passed, but the separate `ApprovedReviewerBindings.current_locked` acceptance item is **not_run** and Issue 107 as a whole must remain open until an expressly authorized new start can establish current facts for that fixed-plan consumer test.
