# Reviewer execution intent boundary

`ReviewerExecutionIntents` persists its records in a dedicated SQLite database.
It consumes only a Reviewer operation ID, its trusted Worker lineage, and the
current `reviewer_reserved_effect_guard`.  The compiler is read before that
guard because its existing Run/CAS reads use independent store transactions;
the guard is then reacquired before an inert intent is committed or a Host
preparation is used.

The record seals the full `reviewer-input.v2` identity, Candidate identity,
Checks, current admission/profile facts, planned attempt/context and fixed
deployment source.  A later Host preparation recompiles and compares those
facts.  The Host manifest is always a reviewer with `read` permission and the
fixed selected Profile binding.

This leaf never invokes `Host.start`, creates a native session, grants a Relay
capability, sends HTTP, parses output, writes Evidence, or changes delivery
eligibility.  A future fixed observer may claim its effect only after Host has
already started and registered its runner.  The claim is one-shot and records
the Host-provided PID/birth identity; it does not authorize Host start.
