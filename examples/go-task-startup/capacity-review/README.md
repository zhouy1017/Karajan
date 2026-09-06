# Independent Capacity pre-effect review

**31 passed, 0 failed, 0 skipped / 1.83 s** from this published path. No new findings in the reviewed Capacity slice. Base `405ce11`; implementation author `capacity_facts`, independent reviewer `ui_spec_finalize`.

```text
python -m pytest examples/go-task-startup/capacity-review/test_pre_effect_contract.py -o "pythonpath=backend tests/capacity tests/routing tests/runs tests/projects tests/web" -q --junitxml=examples/go-task-startup/capacity-review/final.xml
```

The test has no `.cache` dependency. It uses public fixture/helpers in `tests/capacity/test_admission_bindings.py`, real temporary SQLite stores, and explicitly synthetic quota observations. No private database mutation, Host startup, credential read or provider request is substituted for a passing test.

Coverage includes own reservation counted once, other Run reserved/active/unknown holds retained, read-only activation receipt recovery, current policy/window/observation/cooldown rejection, exact request/state/expiry binding, actual transaction serialization with concurrent public writers, and preserving durable activation after an effect-body exception. The body marker proves this Capacity boundary only; it does not prove model execution authorization or zero native startup in a future integrated consumer.

`review.json` records the actual published-path result, current source hashes and all evidence hashes. `history/` is a byte-for-byte archive of the original independent report, initial test input, initial XML and pre-publication final XML. Paths inside archived report JSON retain their original development-directory meaning; they are provenance, not instructions to load `.cache`. The current manifest enumerates the published locations.

Historical results: initial development run 4 passed / 27 missing-guard failures; pre-publication frozen run 31 passed / 1.54 s. The implementation author had already added the activation receipt branch before the first run. These missing-interface failures are not presented as defects in a completed implementation. The initial test differs from the current test only in import ordering.

New publication text uses LF. Historical XML and original test bytes are preserved by the leaf `.gitattributes`; those attributes are part of the evidence publication and must remain in place. No SQLite files, temporary repositories or bytecode are included.
