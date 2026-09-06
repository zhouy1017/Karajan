# Final composed Task execution — author native evidence

Issue: https://github.com/zhouy1017/Karajan/issues/90

Candidate commit: `c2c4b145473a259582d0d37a01e423ca03f03b65`. This includes the normal-mode SQLite Path compatibility fix, full transactional Host cancellation binding, and the separately reviewed Spark Relay patch. The product sources stayed frozen throughout this run.

The three public actual Linux Host/native cases passed in **161.88 seconds**. Each case saved its complete Task runner source manifest before effects. Every controller source hash matched the corresponding current file after execution. This directory preserves the e-round evidence separately; the earlier d-round evidence under `../native/` remains unchanged.

| Case | Result | Time |
| --- | --- | --- |
| Normal execution | Actual Host direct child and native read/edit produced a stopped immutable projection and a complete baseline Candidate; three requests were counted before HTTP, zero unknown calls, grant revoked. Candidate validation and independent Review remain missing. | 69.735 s |
| Grant create reply lost | The original persistent effect claim remained consumed; the committed grant was revoked with zero requests and no native start. Repeated advance did not issue another grant. | 33.834 s |
| Cancellation after send | Exactly one committed request remained `send_unknown`; the original grant was revoked and the Host child stopped. No Candidate, replacement grant or further HTTP request was created. | 54.765 s |

The normal capture report confirms local native stop. The generic Host/Journal observation retains native/provider stop as `unknown` because that observation port does not turn Host exit into native or remote evidence. The cancellation case has no captured native stop proof and retains that uncertainty.

These are author C/P observations using the fixed Linux OpenCode ELF and pinned local tokenizer, real persistent stores, and a fixed controller-owned **test entry**. External qualification/planning/quota facts are explicitly synthetic. The test transport is restricted to loopback HTTP and never forwards a provider credential. This is not the production bootstrap or official provider/S qualification. Candidate checks, independent Review, PR delivery and account-window settlement are outside this run.

The original private fixture state is retained under `/tmp/kt90b`, outside pytest retention. Only public source/operation JSON and JUnit were copied here; there are no private databases, credentials, capabilities, raw HTTP bodies or native workspaces in this evidence directory.

Reproduce from the candidate worktree, using a new short private temporary directory so the Unix socket path fits the OS limit:

```text
PYTHONPATH=backend:tests/runs:tests/projects:tests/web:tests/isolation:tests/adapters/opencode
KARAJAN_REQUIRE_OPENCODE_ISOLATION=1
KARAJAN_OPENCODE_LINUX_BINARY=<fixed Linux ELF>
KARAJAN_GO_TOKENIZER_DIRECTORY=<pinned local tokenizer directory>
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python -m pytest tests/runs/test_go_task_execution_native.py -p no:cacheprovider --basetemp=/tmp/k90e -q
```

`freeze.json` binds the exact executed test/harness inputs and published evidence bytes. `source-manifest.json` in each scenario directory binds the complete production and test runner sources used by that case. No production or test source was changed to collect this final evidence.
