# Task admission: independent Standards evidence

This review covers another author's `orchestration/admission.py`, the routing admission guard,
`web/admission.py` and application wiring against baseline `5818835`. It **excludes** the reviewer's
own `capacity/store.py` receipt/cancellation changes and their tests. Those require separate review.
The independent Spec report is a separate review axis.

From the repository root, using Python 3.12 with the project's development dependencies installed:

```powershell
$env:PYTHONPATH = "$PWD/backend"
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest examples/task-admission/standards/test_admission_standards.py -q -p no:cacheprovider -o 'pythonpath=backend tests/runs tests/web' --basetemp=.cache/reproduce-admission-standards --junitxml=.cache/reproduce-admission-standards.junit.xml
```

The explicit pytest pythonpath includes the existing Run and Web setup helpers; there is no generic
`fixture.py` module. Tests use actual persisted Project/Run/approved plan/owner estimate/capacity
records. Positive routing cases use the clearly labelled test-only qualification source from
`test_approved_routing_capacity.py`; planning admission setup also uses a scripted fixture receipt.
Neither source is installed into product wiring or claims real model qualification.

Lost-response cases wrap a public Capacity port, invoke its real transaction, then raise before
returning. Recovery is checked after actual estimate revocation and, separately, after expiration;
the port's action count and public snapshots prove no duplicate reservation/cancellation occurs.
No command receipt is fabricated. These checks review the coordinator's use of the port, not an
independent review of the port implementation.

The 39 cases cover owner-only operations, three strict HTTP mutation bodies, session/CSRF checks,
durable recovery, immutable enqueue replay, changed operation identities, separate journal files,
actual estimate revocation blocked by the project guard, and current expiration/release/unknown
state projection. Quota reservations, routing results and fixture qualification do not activate a
Host or model. The production source still reports missing runtime qualification as blocked.

`junit.xml` is the fresh run from this published path. `source.json` and `review.json` bind exact
product, test, helper and JUnit hashes. The initial source manifest remains in `history/`.
Reproduction creates all temporary SQLite/test repositories under `.cache/`; this directory contains
no live state or credentials. No provider calls are made.
