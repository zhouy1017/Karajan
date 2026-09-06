# Independent Spec preparation

Scope: root-authored `backend/karajan/projects/qualification.py`, based on the working diff from `9bcf9815cde6c6feb65c236bb7762cbac2573c34`. Credentials and suite implementations are separate authors' work; only read their necessary public contracts here. No product changes, actual provider calls or real credential access.

Specs: `docs/planning/go-runtime-qualification-next.md` and `docs/implementation/m3-go-profile-qualification.md`.

Read-only review found the intended durable sequence and refusal boundaries in place: owner/catalog transaction commits immutable start and scenario identities before credential resolution; command replay reads history or returns unknown; latest selection uses project, exact Profile, scope and suite; source/credential/profile are rechecked before completion and facts projection; fixed observations cannot satisfy `runtime_tools`.

Execute after the dependent public `FixedGoSuite.observe` and credential implementation are ready:

1. Public Registry + credential registration + real Linux native, UDS relay and HTTP fixture: inspect start through `get_command_start` during first upstream request; final history and journal identities must match. Reopen and replay same command with no new requests.
2. Interrupt after durable start and before result; retry the same key must remain unknown and never replenish grants. Concurrent same-key command must also never start another observation.
3. Later unknown or failed observation in the same exact scope/suite must block the earlier passed observation. Keep local fixture and HTTP-fixture scope from covering official scope.
4. During actual fixture execution change current Profile, revoke/rotate credential generation, or change the runtime source. Completion must retain failed evidence; historical replay stays historical and current facts fail closed.
5. Public fixed-scope facts remain explicit and narrow: fixture provenance, no generic read/edit capability, null context, unknown budget, no Reviewer/Commander qualification, no dispatch. Default routing guard must reject a fixed file observation.
6. Read-only current facts must reject expired/revoked/source-mismatched observations; reopening must retain durable history and revocations.

Preparation note to suite author/root: validate the fixed official service against actual account provider identity and allowed billing mode, not merely internal account/channel/profile equality. This is a WIP concern, not a confirmed finding against frozen code.

All executable inputs will use synthetic credentials and a code-injected `httpx.MockTransport`; no `official_go` model request is authorized for this review. Do not simulate official provenance by relabeling an HTTP fixture.
