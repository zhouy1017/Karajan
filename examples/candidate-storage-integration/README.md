# Combined Candidate storage regression

Date: 2026-09-07, Asia/Hong_Kong. Executor: qualification_integration.
Combined HEAD: `3d47194147edf153a0a48a183b34ff7222d674d4`.
This combines the #94 Check branch at `45ff065` with the #96 Candidate rebind
code from `58eafc3`. No product or formal test was edited during this verification.

The three public CandidateStore methods are present. `ports.json` independently
compares their method text and AST against the respective original commit:

- `lookup_evidence`: exact match with `45ff065`;
- `rebind_reviewers`, `lookup_review_rebind`: exact matches with `58eafc3`.

The rebind helper, binding models and formal rebind test SHA-256 match the #96
author freeze. All 12 recorded product/support/test source hashes and HEAD are
unchanged before and after these runs. The full combined Store has its own hash
in `source-before.json` and `source-after.json`; it is not mislabeled as the old
#96-only Store.

Only the requested two test files were executed, once per platform:

```text
python -m pytest tests/candidates/test_review_rebind.py tests/candidates/test_evidence_recovery.py -q
```

Windows: **53 passed**, 49.09 seconds. WSL/Linux: **53 passed**, 7.50 seconds.
The 53 cases are 42 rebind cases plus 11 existing Evidence recovery cases, not
new tests authored for this integration. They use real temporary Git/CAS/SQLite
and explicitly synthetic authorization/qualification declarations.

Windows used the root `.venv/Scripts/python.exe`, `PYTHONPATH=backend`, and
`.cache/integration-rebind/windows-tmp` for fixtures. WSL used
`/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`, `PYTHONPATH=backend`,
`-p no:cacheprovider`, and the fresh native POSIX fixture root
`/tmp/kr96-integration-a` so executable-mode assertions exercised Linux modes.
Both XML and console results are in this directory.

Ruff passed for the four rebind changes plus the Evidence recovery test. Strict
mypy passed for all six Candidate modules with Windows and Linux platform
settings. `static.json` records each exit code.

This is new combined-source **C/P Candidate storage regression only**. It does
not rerun or upgrade #94's actual Host child, Python sandbox, lifecycle/fencing,
checks orchestration or source-qualified execution evidence. No Host, native
model, provider, credential or key was used; no Git state was mutated.

Publication-safe artifacts are `README.md`, `source-before.json`,
`source-after.json`, `ports.json`, `windows.xml`, `windows.txt`, `wsl.xml`,
`wsl.txt`, `ruff.txt`, `mypy-windows.txt`, `mypy-linux.txt`, `static.json`, and
`report.json`. Temporary fixture directories are not evidence to publish.
