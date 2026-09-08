# Commander #147 producer hardening evidence

This document records only local C/P evidence. It does not claim an official
provider observation, Commander admission, business Run/Plan/Attempt/Capacity
identity, account use, or S evidence.

## Implemented bounded slice

- `commander-qualification-settings.v3` requires exactly the v3 keys; v2
  remains decodeable as history-only and cannot assemble Journal/work-root
  authority.
- The protected descriptor is re-read before current facts/effect guards.
  Controller paths reject leaf and parent symlinks, Windows reparse points, and
  linked files; descriptor publication flushes the file and, on POSIX, its
  directory. Reads do not fsync a directory.
- The source seal includes descriptor, Project DB, Journal and work-root
  identities. Runtime/tokenizer/probe accounting remains part of the source
  descriptor. Credential material continues to be checked through the original
  `CredentialSourceStore`.
- Each Journal grant creation is now under the same current guard as native and
  relay effects. The guard samples the clock again after source/material reads.
- Probe spec v3 gives requirements, facts, constraints and a PlanV2 shape, but
  does not put its expected plan in the native prompt. The verifier parses the
  original PlanV2 parser output and checks ordered reasoning tasks, dependencies
  and the no-tools permission ceiling.

## Commands and outcomes

- WSL, 2026-09-08: `pytest -q --basetemp /tmp/karajan147-source-verify-4
  tests/projects/test_commander_qualification_store.py
  tests/projects/test_go_commander_producer_source.py` — `8 passed`.
- WSL, 2026-09-08, with only pinned local runtime/tokenizer and a local HTTP
  fixture: `pytest -q --basetemp /tmp/karajan147-native-verify-2
  tests/isolation/test_go_commander_probe.py` — `2 passed`. This uses the
  Linux native child, Relay and Journal; upstream is fixture-only.
- WSL, 2026-09-08: Commander Relay/Journal negative matrix — `22 passed,
  2 skipped` (the skips were native composition tests before the binary env was
  supplied, not S evidence).

## Remaining original acceptance work

The full eight-condition leaf is not accepted. Still required: public
factory-to-producer-to-current-reader fixture integration with an actual
temporary Registry/Credential/Journal, full concurrent/reopen and all
start/grant/begin/complete/record reply-loss recovery matrix, complete native
malformed/multifinal/log/usage/time negative matrix, Windows protected-descriptor
fixture with a provisioned DACL, independent reviews and CI. Official S and
owner-approved business Planning remain explicitly unrun.
