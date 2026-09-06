# Independent Spec result

PASS — no confirmed findings in the root-authored persistent qualification entry and facts projection. Baseline `9bcf9815cde6c6feb65c236bb7762cbac2573c34`; reviewed working sources are bound in `freeze.json`.

Five independent public-boundary tests passed again in 189.06 seconds on actual WSL Linux OpenCode, namespaces, UDS relay and SQLite, using synthetic credentials and a code-injected HTTP fixture. Author fixtures supply setup only; the independent test defines its own assertions and fault triggers. Six complete fixed suites produced thirty fixture HTTP requests in total. No provider call was made.

Verified durable exact replay without new requests; changed parameters and unsupported suite rejection; source, scope, expiry and revocation checks; narrow fixture facts and blocked generic routing. A public clock fault after the second suite completed but before record persistence left its start unknown. Reopening neither resent its calls nor fell back to the earlier passed observation; both original grants remained revoked with request counts three and two.

While actual tools ran, independent HTTP boundary hooks changed the approved Profile, revoked its credential generation, and rotated actual synthetic credential material into a new controller generation. Each command retained failed history with `QUALIFICATION_SOURCE_CHANGED`; replay remained historical and sent nothing further. Cleanup and current-facts rejection were verified.

The official controller was constructed only for read-only scope selection. No fixture record was relabeled official, and no uploaded report, enabled Profile or generic Task capability was fabricated. Context remains null, budget unknown, fixture roles empty, and dispatch false. Arbitrary Task permissions, Commander/Reviewer, Collector, context capacity and cash bounds remain outside this slice.

The final dependency hashes match the bytes used by these cases: root qualification `a726c01b…`, suite `942b23a8…`, credentials `2a5f7e84…`. The unchanged five tests were rerun because a separate suite review required a grant-ownership cleanup fix. All original `c7e7dedc…` results and source bindings remain byte-preserved under `history/before-suite-grant-ownership-fix`; they are not evidence for the final suite. No product or author-test edits, commits, pushes, merges, CI reruns or real key access were performed in this review. Independent test Ruff and formatting checks passed. No red artifact is claimed by this root-entry Spec review because its own tests observed no product failure; the suite's separate finding and red history remain separate evidence.

Command:

```text
wsl.exe -d Ubuntu -- /bin/sh -c 'cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-profile-qualification && PYTHONPATH=backend:tests/projects:tests/isolation KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest .cache/go-profile-qualification-spec/test_public_persistence.py -vv --tb=short -p no:cacheprovider --junitxml=.cache/go-profile-qualification-spec/final.junit.xml'
```
