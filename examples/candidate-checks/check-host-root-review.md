# Check Host independent review

Reviewer: root, independent of the Host implementation author (`capacity_facts`).
Issue: https://github.com/zhouy1017/Karajan/issues/94
Review date: 2026-09-06, Asia/Hong_Kong.

Read the complete new manifest and changes in Host, supervisor and exports against
624ad8b8490003f155baf7842ba91b9975b9526a. Checked the repository issue-tracking
requirements and the issue's deterministic Check process scope. No actionable
Standards or Spec finding in this bounded change.

The discriminator preserves the original Commander/Worker/Reviewer manifest and
requires explicit Check schema, environment revision/source and execution digest.
Host preparation validates serialized bytes again before persistence. All prior
Host/supervisor manifest parsing sites accept the union. Current Check fences carry
the approved environment and execution identity; existing model fences retain their
Profile. The original activation, PID/birth, start identity, cancellation and late
result rules still apply. This does not by itself attest Candidate evidence or a
successful quality gate.

Independently executed on Windows:

```text
python -m pytest tests/execution/test_check_manifest.py -q -o "pythonpath=backend tests/projects tests/runs tests/web tests/isolation tests/adapters/opencode tests/candidates tests/execution" --junitxml=.cache/check-host-root-review.xml
```

Result: 24 passed, CLI elapsed 4.12 seconds. Actual local supervisor/child tests
include original launch replay, controller identity denial, current control
withdrawal and exact historical cancellation. The author separately ran the full
121-test execution group on Windows and WSL; that is author evidence, not an
independent full-group execution by this reviewer.

Reviewed raw source SHA-256:

- `backend/karajan/execution/manifest.py`: `50f8b73246a22a72709ea0e7c26fe285dad8d17d32d96c60d06dd0e4ba56979a`
- `backend/karajan/execution/host.py`: `5e3d0f8d8f84649690854ddd672a6a2643d4ad923f79b2eff584c4cdf73164f1`
- `backend/karajan/execution/_supervisor.py`: `a9753aa1607a3785e5fa7bcab704cbfa93400e660f4f6ab55f02a7e140575b0f`
- `backend/karajan/execution/__init__.py`: `7efb1589d70d5b71867c793b34939c25fb58fc11ee76c89c99689231a85fbec3`
- `tests/execution/test_check_manifest.py`: `51ad23293b23cdfbe84093ddd691be324694e9278888250809e471dfb0976a3e`

Evidence level C/P only. No provider or account access. Full Check facade,
environment execution, shared Run budget and delivery are outside this review.
