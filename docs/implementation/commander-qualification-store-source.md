# Commander qualification store/source slice

This slice adds the independent `qualify_commander_planning` producer path.
Its public input is a registered project/Profile identity, authenticated
principal, command key, and bounded validity only.  It commits an original,
sealed `profile_qualification_starts` row before exact credential resolution or
observation, using the existing record/revocation/idempotency ledger.

The sealed v2 source includes the exact Profile binding, credential generation
and material-sealed source, fixed runtime/tokenizer/controller source, probe
digest, and the two fixed no-tools inline scenes: `legal_plan` and
`denied_tool`.  Each scene is limited to six requests and 150 seconds; a start
is limited to twelve requests and 420 seconds.  Context limits are I12288,
O4096, C16384, margin 2048 and ratio 2000bps.

`commander-qualification-source.v2.json` is a distinct protected descriptor;
the legacy Go qualification settings and the task-runtime deployment source
are not Commander sources.  The persistent current reader reconstructs this
same canonical source, including the dynamic Profile/authentication generation.

There is no native Commander probe in this slice.  Production observation fails
closed as `COMMANDER_NATIVE_PROBE_UNAVAILABLE`, creates no official Commander
fact, and therefore makes no Planning admission reservation.  The dedicated
C-only test double persists `fixture` provenance and has no route to production
composition or official current facts.

Not run here: native parser/retention/isolation behavior, real official Go
observation, and the full P/S qualification suite.  Those remain work for the
next controller-owned probe slice.
