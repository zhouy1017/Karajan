# CONSUMER-001 author repair

The independent reviewer demonstrated that an original Task cancel could affect a Host row sharing only Attempt/start IDs while fence, budget or profile revision differed. The frozen original negative tests remain in `.cache/task-consumer-independent/`; the author did not edit them.

`RunnerHost.cancel(..., expected_binding=None)` adds an internal optional full frozen binding. The supplied prepared ID, complete manifest and complete ProcessSpec JSON are compared in the same Host transaction before cancellation/control writes. Consumer cleanup always supplies the original persisted launch binding. Default legacy callers remain compatible. Historical cleanup compares the stored original, without requiring current authorization/fence or an existing old working directory. No new control, process or grant is issued.

Author tests: 8 new cases red→green (prepared ID, fence, budget, Profile revision, argv, cwd, timeout; exact replay after control withdrawal and cwd removal). Combined new8 + facade11 + original reviewer10 executed by author: 29 passed22.49s. Legacy Host/once-control plus new8:61passed11.09s. Ruff and Windows/Linux platform mypy passed. This author run is not independent review.

The independent reviewer subsequently reran the unchanged original 10 cases against the frozen repair on Windows and WSL: 10 passed11.72s and 10 passed8.57s. Host and consumer hashes remained unchanged. CONSUMER-001 is closed; the independent report and original failure evidence are retained separately in `.cache/task-consumer-independent/`.

Evidence: `.cache/exact-host-cancel-red.xml`, `.cache/exact-host-cancel-green.xml`, `.cache/exact-host-regression.xml`. This repair follows the d-round native source; final combined native execution is intentionally deferred until root announces source freeze.
