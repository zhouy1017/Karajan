# Independent Capacity Spec result

Reviewed source: `backend/karajan/capacity/store.py`, SHA-256 `2825bc579a5f7406f180176bf118c44b1b854511b79dc32232c18d540153b143`, base `405ce11`. Implementation author `capacity_facts`; independent reviewer/test author `ui_spec_finalize`. The previously authored ExpectedCapacity binding interface is not counted as independently reviewed new work here.

**31 passed, 0 failed, 0 skipped / 1.54 seconds. No new findings in this bounded slice.** Source hash after the run matches the author's announced freeze. `final.xml` retains the actual Windows run. Public command:

```text
python -m pytest .cache/capacity-pre-effect-draft/test_pre_effect_contract.py -o "pythonpath=backend tests/capacity" -q --junitxml=.cache/capacity-pre-effect-draft/final.xml
```

The same 31 cases as the draft were used; the only normalization after `initial.xml` was import ordering. The historical 27 missing-method failures were development red checks, not defects in a declared complete implementation. The initial 4 passes included the implementation author's already-added activation receipt branch.

Verified through actual public SQLite Store calls: own reservation excluded once, other Run reserved/active/unknown holds retained, activation receipt recovered without clock/action replay, changed capacity facts rejected before body entry, original request/state/expiry checked, policy/observation writers serialized behind the held real transaction, and a body failure cannot roll back the previously committed activation. Code inspection confirms `PRAGMA query_only=ON` applies after acquiring the real `BEGIN IMMEDIATE`; expired-unsent filtering does not call mutating `_held`.

The test fixture's numeric observations and approved-scope labels are synthetic, clearly marked `fixture`. A body marker is not a real Host or Go effect. This report covers Capacity's gate and recovery interface only; Run authority, credential/qualification currency, Task projection, actual model startup, remote-stop guarantees and cash enforcement remain separate integration responsibilities. No provider request, credential read, product edit or Git write occurred during this review.
