# Reviewer Candidate rebind evidence

Issue [#96](https://github.com/zhouy1017/Karajan/issues/96) is a bounded C/P CAS
storage slice of [#95](https://github.com/zhouy1017/Karajan/issues/95). Base:
`624ad8b8490003f155baf7842ba91b9975b9526a`. Implementation commit: `58eafc337b7a2350c9e983d550e14ce50b2dc5e9`
(recorded in `publication-map.json`). This directory records local results, not
a merged implementation or a current CI result.

The primitive derives only a same-content Candidate's Reviewer policy set.
It retains full source/binding lineage, preserves original content and evidence,
and recovers an exact committed result without artifacts, Git or clock reads.
See the [implementation contract](../../docs/implementation/m3-reviewer-candidate-rebind.md).

## Published material

- `author/`: the unchanged author README/freeze, red and green results, boundary
  and commit-fault results, full Windows/WSL XML and static outputs.
- `inputs/test_review_rebind.py.txt`: exact final public test bytes, archived as
  text so evidence publication cannot add another pytest collection target.
- `review/root-review.md` and `.xml`: independent code review and independent
  Linux execution of seven existing author cases, with zero actionable findings.
- `publication/`: the requested publication-time Ruff and diff-check outputs.
- `publication-map.json`: source-to-published byte hashes, retained repository
  source dependencies, generated documentation and explicit omissions.

All 42 new tests passed. Full Candidate regressions: Windows **164 passed,
3 POSIX-only skips**; WSL **167 passed**. The new complete-content test checks
actual executable mode on Linux. Authorization/qualification declarations are
synthetic; Git/CAS/SQLite and file operations are real. No provider was called.

The original freeze retains references to its generating `freeze.py` and sibling
fixture helpers. Those helper files are not copied here. The publication map
identifies the omitted generator and records repository dependencies by hash.
Test state, databases, temporary repositories, keys and runtime directories are
excluded. Original failure XML and Markdown remain unchanged, including their
historical `.cache` paths and earlier #95 storage-slice wording.

## Reproduce

Run the formal test from the repository root; its sibling fixture dependencies
already live under `tests/candidates`. Use a fresh temporary root:

```bash
PYTHONPATH=backend python -m pytest tests/candidates/test_review_rebind.py -q \
  --basetemp=/tmp/reviewer-rebind-fresh
```

```powershell
$env:PYTHONPATH='backend'
python -m pytest tests/candidates/test_review_rebind.py -q --basetemp=.cache/rebind-fresh
```

Replace the test path with `tests/candidates` for the full regression. The
archive's input hash equals the formal test hash. It is not a standalone fixture
reader and should not be renamed or run without the repository dependencies.

#95 still owns qualified Reviewer binding compilation, runtime routing,
resource admission, actual read-only Review and service evidence. The #94/#95
subject handoff and complete Check rerun are not implemented by this storage
slice. New revisions still require their own Check/Review evidence, and delivery
eligibility remains false. No G or S completion is claimed here.
