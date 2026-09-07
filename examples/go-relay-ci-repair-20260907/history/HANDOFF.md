# Frozen review handoff — correction not applied

Current product: `go_relay.py` SHA256 `c42862bd38638629f4670c2437273d42282d9890c842ea3153861c14eb81f8de`, on base `7c85669a3d2a5b717bbcaea7f8dbbf2c5ea937a0`. The requested Spark model hit its usage limit before producing a third repair. Root is awaiting user direction about the repair model. This reviewer has not applied a fix, edited the product/tests, or rerun CI.

`minimal-correction.patch.txt` is an **unapplied, untested patch proposal**, limited to the two open findings:

1. Derive the pre-rejection unread-byte budget from a single valid Content-Length, capped at the existing request limit; ambiguous Transfer-Encoding/length framing gets zero speculative drain budget. This does not authorize the request or change the later authentication/validation error precedence. Consumed-body tracking and the existing total deadline remain intact.
2. Restore the required `call_id` argument to generic-exception recovery and remove its optional/no-op default. The identifier is already allocated before `begin_call`. This restores read-only recovery of a committed call, allowing the existing withdrawal path to revoke the original grant and retain its unknown call; it does not issue another begin or send.

The original four independent cases are unchanged (`78792f8d...`) and pass on the current patch. The two follow-up cases fail: incorrect declared-body drain extent, and a lost committed metered begin that leaves the grant active and permits a second request. Evidence and exact source snapshots are retained. The one scheduled old-source control independently reproduced WinError 10053; its intentionally failing XML is historical and must not be included as an expected-green test run.

After an authorized author applies a correction, run only the six bounded cases first:

```powershell
$env:PYTHONPATH='backend'
$env:KARAJAN_GO_TOKENIZER_DIRECTORY='C:/Users/Chooo/Playground/Karajan/.cache/go-task-execution/.cache/go-context-artifacts'
$env:KARAJAN_REQUIRE_GO_TOKENIZER='1'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
& 'C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe' -m pytest .cache/spark-ci-10053-independent/test_rejection_boundary.py .cache/spark-ci-10053-independent/test_followup_boundaries.py -o 'pythonpath=backend tests/adapters/opencode' -q --junitxml=.cache/spark-ci-10053-independent/final-six.xml
```

Keep the real HTTPX wrong-capability path as well as the scheduled socket controls. Check the full final diff for unrelated changes, then run relevant existing relay/context/Journal tests and static checks. Do not overwrite original red evidence or treat an author's run of the independent files as independent reviewer acceptance. All these tests use synthetic local HTTP and offline tokenizer artifacts; no provider or real credential is needed.
