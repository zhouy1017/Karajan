# Issue 106 observer final author handoff

The final owned source/test identity is `source-negative-before.json`, verified
unchanged by `source-negative-after.json`. The final observer SHA-256 is
`1a9f45607ce54f373bc35e645f1fb6c37cbd169fa7271dc09bfb1bcfabe744f8`;
helper `f80f64a2eeb08ddcab41702527ee7f6b2a6bcc85d5f7a8767947298657cb4987`;
runtime `213de2adf2ada620e3382ccdae099935a4644927a04b3e4c0b59cfb4295bb003`.
No implementation commit is invented; root owns committing and publication.

`final-negative-linux.xml`: **41 passed, zero skipped, three positive native
cases deselected, 70.40 seconds**. This is 36 C checks and five actual native P
negative cases. `final-negative-windows.xml`: **34 passed, 10 Linux-only skipped,
2.44 seconds**. These are overlapping platform/matrix executions, not disjoint
unique test counts. Owned Ruff check/format (six files) and mypy (three production
files, follow-imports silent) passed; terminal outputs are attested, not supplied
as invented raw static logs or a whole-repository result.

Each negative observation used the real pinned OpenCode ELF, Linux namespaces,
readonly projection, private management/UDS, tokenizer, current-guard callback,
Relay and Journal. Upstream responses were entirely local HTTP fixtures.

| Native negative | Actual observation |
| --- | --- |
| `edit` | One upstream response attempted an edit call; Relay rejected it before native tool execution. |
| `bash` with an external `curl` command | One upstream response attempted shell/network authority; Relay rejected the tool call. No shell or external request was executed. |
| `mcp__external__invoke` | One response attempted an MCP tool; Relay rejected it. This is not a claim that an actual MCP server was launched. |
| `create_pull_request` | One response attempted delivery authority; Relay rejected it. No delivery service was called. |
| `/control/opencode.json` read | The native read tool actually returned the fixed permission-denial message; the second measured request retained that error. The observer refused to accept it as an in-scope review. |

The five reports intentionally have status `failed`: that is the expected
qualification outcome for these negative inputs. All retained original file
bytes and actual stat-derived Git file modes, confirmed local stop and grant
revocation; remote stop remains unknown. Guard entries were observed at native
start, prompt submission, and every actual send. Report source digests all equal
`655f6180d81d739dfbb84d44f4a679f9ae19bafa014e40cf713ab96a16e41110` and every runtime
source hash was independently re-read after the five cases by the archive script.

## Original eight acceptance responsibilities

| AC | Observer evidence and remaining owner |
| --- | --- |
| 1. Public contract / durable start | Public observer C rejects wrong binding before directory/native/send; start and prompt current-guard rejection is covered. Public Store start/seal/three grant persistence is root/Suite evidence, not supplied by this producer. |
| 2. Lost replies / ownership / latest | Observer requires an unused active grant and only revokes its exact binding; direct replay is covered. Journal/Store/Suite own durable replay, conflicting grants, generations and latest records. |
| 3. Unique completed native text | Earlier three native positives observed a new empty session, prompt/assistant/text-part identity, actual completion times and full text. Current C rejects foreign/ambiguous/unfinished final parts and parser ambiguities. The final current-source positive Store chain is performed once by the Suite owner. |
| 4. Readonly mechanism / context | The five current-source native negatives above plus the earlier three fixed positives are recorded separately. Actual readonly flags/inode identity, read-wire hashes, file bytes/stat modes and stopped capture are observed. The final same-source three positive scenarios transfer to the Suite owner's public Store P chain; this archive does not relabel old positives. |
| 5. Structure / fixed scenario results | Earlier clean/defect/denied positives yielded passed/failed-with-blocking-finding/inconclusive content respectively. Whole-text parser, sensitive echo, malformed native metadata and incomplete protocol fail closed in C. Suite owns secondary Journal/parser/report tamper validation and final positive chain. |
| 6. Facts and real consumption | Root's QualificationStore/resolver/binding/factory tests own this AC. This observer always returns runtime_tools not_run and dispatch false, including fixture success. |
| 7. Preserve parent conditions | This producer does not create Candidate/Evidence/Task/Delivery objects or mutate the stored verdict protocol. Root owns shared impact regressions and #95 remaining consumer work. |
| 8. Source / reviews / CI | Exact source/input/command/red/green/raw report metadata is archived here. Root owns independent Standards/Spec conclusions, overall static checks, current CI, commit/merge and Issue state. No G or S is claimed by this archive. |

## History and limits

`README.md` and `freeze-current-guard.json` remain the exact earlier author
snapshot. They document the initial missing-entry reds, the canary/complete-tree
correction, two final-part omissions, one duplicate fixture-argument error, two
JSON-escaped sensitive-string reds, and the missing guard seam. Their sources
and XML are preserved without upgrading their provenance.

`mode-stat-red.xml` adds the last actual POSIX chmod C red: the previous manifest
filled a fixed mode rather than reading it. The final observer now reads each
regular file's stat mode; the same C test is green within the 41-case run. Modes
use the existing Git file convention `100644` / `100755`. No product rules or
fixture responses were relaxed to make this negative pass.

Official Go S remains Issue 107 and was not called. These are fixed qualification
mechanism observations, not arbitrary project-review quality or T2/T3 evidence.
The final shared positive P may be appended by root with its own exact source
and result; it must not overwrite the earlier d192 positive observations.
No tests or backend files are being changed after this handoff.
