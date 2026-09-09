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

The v3 descriptor additionally binds the pre-existing Go Journal and a private
controller work root.  Only that descriptor can compose the native producer;
legacy v2 descriptors are readable history and deliberately have no execution
right.  Every sealed scene context contains the exact `GoRequestAccounting`
source digest required by the original Journal grant.

`isolation/go_commander_probe.py` runs the pinned Linux OpenCode binary in a
fresh namespace with a readonly anchor projection and a native configuration
that denies every tool.  It creates a new empty session, sends the fixed inline
prompt through the original Relay/Journal and accepts only one causally linked
final PlanV2 matching the fixed scene semantics.  It retains the actual native
session/final, bounded output, Journal calls, Relay receipts and local stop
facts.  Local HTTP peers are explicitly `http_fixture`; production facts are
created only after both scenes have complete `official_go` observations.

The native client omits `tools` for an empty configured set.  Relay accepts
only that canonical omission (or explicit `tools: []`), while rejecting any
tool declaration, choice, history, or observed tool call.  This does not claim
read, edit, shell, MCP, delegation, maximum-context, billing, or official S
qualification.  Fixture provenance remains unable to satisfy Planning
admission.

The dedicated Linux C/P test uses the real pinned ELF, namespace, empty-tools
native wire, original SQLite Journal and a temporary local HTTP peer for both
scenes.  It is not an official provider request.  Real official Go observation
and the #113 business Plan/owner-approval flow remain not run.
