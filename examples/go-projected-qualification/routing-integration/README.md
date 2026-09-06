# Independent approved Run integration

Base `6133e942`; maintained public tests are `tests/runs/test_projected_go_routing.py`. All 10 cases pass. No routing product changes were made by this reviewer and there are no product findings in this bounded integration review.

The tests use real ProjectRegistry, versioned ExecutionPolicy, approved Run, ProfileQualificationStore, owner estimates, CapacityStore and ApprovedTaskAdmission. SyntheticSuite is an explicitly labeled producer substitute; planning receipts and catalog/capacity declarations are fixtures. This is not actual model qualification.

The positive case persists narrower I=6000/O=4096/C=12000/F=2300/bps=2200 limits and their policy/qualification references, then reserves once without dispatching. Fixture/v1 suites, v1 policy, source or margin mismatch, unsupported output allowance and T3 create zero reservations. Requalification or revocation blocks the original reserved execution while preserving its one existing hold.

`boundaries.xml` is the initial 10-case success; the later combined Windows result after formatting is `../store-author/windows-final.xml`. `history/fixture-configuration-failure.xml` is a test preparation correction: catalog declarations retained an old Profile digest after the fixture changed to Go. The registry correctly refused configuration readiness. Public fixture declarations were corrected, and actual routing continued to consume Store facts. No product changed for this correction. `history/first-positive.xml` retains the first successful end-to-end case.

Run from the repository root:

```text
python -m pytest tests/runs/test_projected_go_routing.py -o "pythonpath=backend tests/projects tests/runs tests/routing tests/capacity" -q
```

Source hashes are in `review.json`. Historical XML bytes are preserved by the leaf `.gitattributes`.
