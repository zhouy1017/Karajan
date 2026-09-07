# Issue 107 ordered execution handoff

This was the reviewed execution handoff, not authorization and not new S
evidence.  The command below was consumed as immutable attempt4.  Its suite
completed with six official requests, but the process did not persist a binding
positive control or subsequent revoke before the start expired.  It must not be
replayed or substituted with `run_official_issue107.run`; see
`ISSUE-107-ATTEMPT4-INTERRUPTED.md` for its retained outcome.

```text
wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/dispatch-reviewer-official && /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python examples/go-readonly-reviewer-qualification-20260907/run_issue107_ordered_consumer.py execute --private-root /home/chow/karajan-issue107-controller --report examples/go-readonly-reviewer-qualification-20260907/issue107-ordered-attempt4-evidence.json'
```

The scope is one new immutable command start with the existing private root,
fixed `readonly-reviewer-107@1`, official Go suite, three scenarios, six
requests per scenario, eighteen requests total, 150 seconds per scenario, and a
600-second start. The driver rejects an already-existing ordered command and
never makes another command key or automatic retry.

Its enforced function order is:

1. `ProfileQualificationStore.qualify_runtime_tools`, then bound-count checks.
2. Same-key `qualify_runtime_tools` replay, with unchanged Journal counts.
3. `prepare_issue107_consumer.positive_result`, which calls the real
   `ApprovedReviewerBindings.advance` twice and requires `prepared -> ready`.
   It is membership-only on the explicit controller fixed Plan/Candidate fixture;
   it creates no Reviewer Task, Candidate Review, Review Evidence, or model attempt.
4. `ProfileQualificationStore.revoke` only after that positive assertion.
5. The same consumer's revoked negative control, historical record readback, and
   same-key replay with unchanged counts.

The qualification suite still owns its own Journal-grant cleanup and local-stop
observation while it runs. That cleanup is separate from the later qualification
record revoke. Provider remote stop remains `unknown` where the source reports it.

`run_issue107_ordered_consumer.py dry-run` performs no effect and statically
asserts this source order. It was used only to produce
`issue107-ordered-driver-dry-run.json`; its output cannot qualify a profile.
