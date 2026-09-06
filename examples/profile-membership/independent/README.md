# Independent profile membership review

Reviewer: root. Date: 2026-09-07 Asia/Hong_Kong.
Implementation: `3a8cc5875b075285ab18796d1ab4bc36303192a1`.
Independent branch base: latest verified dev `2e587d1773c514361689e13ebbd16ba62f1cd219`.

Standards and original #99 Spec: no actionable finding. The public interface
shares classification, stage, approved-group and Profile checks with both legacy
routing paths. Quota, cash and preference ranking remain solely in execution
routing. A nonempty membership set cannot select, reserve or activate a model.
Supplied facts and timestamps are not evidence of current real qualification.

The author developed against integration base 3d47194. Git comparison confirmed
the routing package/tests, contracts, Profile models and resource broker did not
change between dev 2e587d1 and that base. Root reviewed and independently tested
the final LF source in this separate dev worktree:

- Windows: 156 passed / 3.32 seconds, `windows.xml`.
- Linux/WSL: 156 passed / 5.40 seconds, `linux.xml`.
- Fourteen complete pre-change route/reserved results match exactly, using the
  original seven fixed inputs: `legacy-comparison.json`.
- Ruff passed; strict mypy passed for all nine routing source files.

These are independent executions of the author's 33 new and original 123 cases,
not 156 new test designs. No provider, credential, Capacity store or model ran.
Current PR CI and merge are separate, pending GitHub evidence.

Original CRLF source bytes and author freeze/XML are retained. Only two source
files needed LF conversion; all three owned files have identical Python ASTs.
The final formal test was already LF. No failure was hidden or overwritten.

Final SHA-256:

- evaluator.py: `4a7b320ee0a8e9c8a40ab4dffaa524ac9130b2665a0e532065679b59353f1b59`
- routing/__init__.py: `1a11ca32e11f0a542337b6facf58473644626199549a9465975aea7d3297650b`
- test_profile_membership.py: `28d1477a758eb7302c624d44af3eed09ff50edc114cfc6e81ca6e58ae5dd6a0f`
