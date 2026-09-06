# Reserved-routing independent review: preparation

Base `405ce11`, worktree `go-task-startup`. Read-only review of root's new `ApprovedRunRouting.reserved_execution_guard` and `_build` integration. The pure `evaluate_reserved_profile` implementation is independently authored and initially still WIP; no final test verdict is claimed before that dependency is ready.

The current guard takes only Run/assessment identities and principal. It loads the immutable assessment through `get`, then enters the Run guard and shared Project qualification guard without nested public Run reads. It reuses the original planned Attempt/Context and selected Profile, compares complete approval/execution-policy/routing bindings plus approved TaskSnapshot and selected qualification/estimate source rows. That is the intended current-authority boundary. It neither proves a reservation exists nor authorizes a Host start; the operation/Workspace and matching Capacity request belong to the next consumer.

The public draft `test_reserved_routing_public.py` adds 10 parameterized cases using real Registry/Run/Estimate/Capacity stores (an initial manual estimate of 11 was corrected after collection). Positive qualification is explicitly a source double, including one independently declared source-replacement test. Draft cases cover: no reservation side effect; returned-value tampering cannot change stored identity; capacity-only constraints stay with the Capacity gate; current Profile restriction; replacement qualification source; pending versus approved Task changes; estimate expiry; rejection of a blocked historical assessment. Existing root tests additionally cover wrong owner, missing assessment, estimate revision/revocation and held Run/Project transactions. Counts are not combined or double-credited.

## Actual Task qualification dependency cycle

The existing chain is:

`ApprovedTaskWorkspace.prepare` requires reserved operation → `ApprovedTaskAdmission.advance` needs selected current route → `ApprovedRunRouting._build` asks for `scope="runtime_tools"` → fixed Go qualification `_go_facts` rejects `TASK_PERMISSION_SCOPE_NOT_QUALIFIED`.

If new runtime qualification itself requires this already-reserved Workspace, there is no entry into the cycle. Do not fix it with fake passed facts or by weakening the current fixed-fixture restriction.

Minimal direction: qualify the controller/runtime's bounded capability to enforce existing-file projection and the configured local context measurement, using a separately controlled qualification workspace and frozen implementation/source identity. This observation grants no Task or project path access. Then, after an actual Task is approved and reserved, its immutable Workspace supplies the concrete readable/writable paths, input hashes and Task binding; the actual launch checks that those demands are within the qualified mechanism's capability and approved policy. Fixed `fixture.py` read/edit alone remains insufficient proof of this more general enforcement capability.

This is a necessary producer/consumer distinction, not permission to mark capability passed now. The runtime producer must test projection escape denial, allowed existing-file edits, immutable input/source identity, meter configuration and bounded lifecycle with an actual isolated runtime; Task authorization remains derived from the approved Run. No user preference is missing: this follows the existing owner-approved scope and fail-closed requirements. The final capability schema and observation suite remain implementation work outside this review slice.
