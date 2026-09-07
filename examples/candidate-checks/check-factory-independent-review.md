# Fixed Check factory / entry independent read-through

Scope: `orchestration/check_services_factory.py`, `_candidate_check_runner.py`, and `tests/runs/test_check_services_factory.py`. The reviewer authored `isolation/check_runner.py` and does not count it as independently reviewed. This is a code-only review, with no extra execution of the author's existing tests and no provider or credential access.

No blocking finding in the reviewed boundaries:

- The fixed entry accepts exactly four IDs, selects the bootstrap only from its controlled cwd, and loads the backend from its own `__file__` under `-I`. It accepts no executable, workspace, URL, image, result, or PID argument. Its error output contains stable fixed codes.
- `open_check_services(..., for_execution=False)` reopens required authority databases with existing-only semantics and creates a runner with an empty environment map. It does not call `check_controller_source`, instantiate an image, provision output directories, or resolve the configured Python executable. Existing-only qualification construction validates its schema and does not create qualification records. Deferred Host/Candidate handles postpone their resource validation to actual use.
- Execution construction explicitly validates the private deployment paths, separation from managed repositories and task work root, and existing Host database before constructing the controller-owned image mapping. No request supplies those paths.
- An already recorded native claim, observation, or Evidence takes the entry's historical reconcile branch. For new work, the entry observes its real current process identity; `consume_check` waits for Host direct-child registration and compares that identity before its current-runner guards. It does not trust an argv PID or treat parent Host startup as the candidate-command gate.
- The compiler fixes Python `-I`, exact entry, four original IDs, control cwd and bounded Host timeout. The actual Check timeout remains the separately bound effective timeout in the consumer/runner contract.
- The checked-in tests distinguish explicit bootstrap provisioning from opening resources, exercise strict fields/duplicates/paths, actual bad-ID subprocess entry, and history reopening without images/runtime. Their imported Go deployment fixture is synthetic prerequisite data, not proof of an official Commander or Go execution.

The legitimate fixed Host child → current guards → actual sandbox → Evidence composition remains the root/UI integration's responsibility. This read-through does not replace that C/P test or attest production end-to-end service qualification. Source hashes are recorded in `check-factory-independent-source.json`.
