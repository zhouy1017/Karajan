# Fixed factory Reviewer binding guard

Author: root. Independent read-through: capacity_facts. Date: 2026-09-07.
The factory gives routing and ApprovedReviewerBindings the same real Project
and qualification Store, then injects `current_locked` into the Check consumer.
There is no production fixture flag or request-supplied validator.

The new public factory regression deploys real stored A history and an explicitly
synthetic ready B. The normal factory rejects B with the specific
`REVIEWER_QUALIFICATION_REQUIRED` code before Host.prepare; the complete Check
history and subject remain unchanged. Historical reads still work with missing
runtime/image assets. First case: 1 passed / 2.67s; complete factory regression:
19 passed / 9.04s, `first.xml` and `windows.xml`. Ruff and format passed.

Independent read-through found no blocking Standards/Spec issue. The current
factory deliberately has no configured Reviewer suite/credential runtime;
qualification rejects it before any real Reviewer source can be accepted.
This test proves default rejection and wiring, not positive real-role authority,
current credential-generation consumption or a native Reviewer execution.

Reviewed source SHA-256:

- check_services_factory.py: `e58c8b5b5771b80b11323831cbe785622d97d009add5386f581d777322489868`
- test_check_services_factory.py: `019122b5c8f12d70cb5281fb0dd93fd10f54dee4ca83d81916099fea555a86d0`

Root recorded this read-through from capacity_facts' final response. That reviewer
did not edit root's files or repeat these tests. No provider or key was used.
