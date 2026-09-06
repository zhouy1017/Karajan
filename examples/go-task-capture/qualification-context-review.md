# Independent review: qualification v2 Journal and relay context

Verdict: no blocking or non-blocking findings in the bounded change. Standards: 0;
Spec: 0. This is **code-only independent review**, not an independent test run or
official qualification result. I did not repeat the author suites, start a native
runtime, access a provider/key, change product code, or repair CI failures.

Compared the working-tree changes against `1534825` using
`git diff 1534825 -- backend/karajan/adapters/opencode/go_journal.py backend/karajan/adapters/opencode/go_relay.py backend/karajan/isolation/go_probe.py`.
The spec is `docs/implementation/m3-go-task-capture.md`, especially “Metered
qualification identity”; the author contract/evidence inventory is
`.cache/qualification-context-evidence/README.md`. I also read both new public
adapter test files, the shared ContextMeasurement/accounting implementation,
Contract strictness and existing completion/recovery/revocation paths. The author
and root reported test totals are supporting context, not my independent results.

## Standards

No findings. The new grant variant reuses the same immutable SQLite grant/call
ledger, canonical authentication, expiry tombstone, transaction, replay and
completion mechanisms. Qualification and Task limits stay distinct; the shared
accounting implementation still owns full request/tool-history rendering and
arithmetic. The small limits Contract centralizes the six v2 fields for both
Journal and relay, rather than introducing another ledger or tokenizer pipeline.

The context carries only a source digest and bounded numeric limits. Strict
Contracts reject extra keys; measurements retain digests/counts, not raw requests
or tool results. The accounting object and capabilities are excluded from repr;
completion still uses the existing numeric usage and stable-reason allowlist.
No added code claims subscription exactness, cash pricing or Profile capability.

## Spec

No findings. Checked these concrete paths:

- `_binding` distinguishes Task by `subject`, v2 qualification by explicit schema,
  and legacy by the original shape. Strict models reject unknown/mixed versions.
  Legacy key ordering/default shape is unchanged; the original Task model is
  unchanged. The v2 grant retains the common fixed model, Attempt/fence, runtime,
  Profile, channel, generation, expiry and 1–6 request limit.
- The v2 grant immutably binds spec digest, `edit`/`denied_read`, tokenizer source,
  input/output/window limits and both margins. Relay matches spec/scenario and all
  six context fields, then checks the accounting source and performs the original
  measurement. Journal authenticates the complete persisted binding/capability
  and requires a validated matching measurement before a new intent is inserted.
- The original `BEGIN IMMEDIATE` transaction commits numeric measurement and
  `send_unknown` before `begin_call` grants its one send return. `client.stream`
  follows that return. Native IDs never replace the controller-generated UUID.
  Replay returns history without send permission; context changes conflict;
  expiry/revocation and completion cannot refund a slot.
- Missing usage, exceeded observed usage or transport/unknown failures reuse the
  existing withdrawal path. Pre-authentication failures lack a durable call ID
  and cannot revoke a foreign grant. Lost-begin recovery requires the exact stored
  binding, controller UUID and measurement before it associates a durable call;
  a failed lookup only closes local transport. Completion failure preserves the
  already persisted unknown and withdraws future sends.
- `observe_go_tools` rejects a stored `schema_version` before creating a directory,
  resolving runtime source or constructing a relay. It checks the persisted
  snapshot rather than a caller's fabricated legacy binding. The new capture
  helper is included in the runtime source descriptor.

Limits: the forthcoming v2 producer must still derive the spec, scenario and
limits from a trusted persisted qualification start, connect the exact credential
generation and Collector, and verify the actual native behavior. This slice
explicitly does not produce those facts. Journal/controller ports accept trusted
code inputs; numeric self-consistency alone does not prove a real measurement.

## Source binding

The three product hashes were checked before and after this review and unchanged.

| File | SHA-256 |
| --- | --- |
| `adapters/opencode/go_journal.py` | `006ea225cd1ddbb5373a806a8286f11a7e58062f130612cc797fdb6155772eeb` |
| `adapters/opencode/go_relay.py` | `2f8f32de73d95d0c2298f1e10f7cb8bee667cd6e4c394cabaa3e81a881d7ac16` |
| `isolation/go_probe.py` | `f9fe86a085db0e9d863a79087f67356c3715048735eddde5917cbf4b141d4743` |
| `test_go_qualification_grants.py` | `84be018469e6362467586ac21a525e2995e0849bb4ba58514751a12e08def444` |
| `test_go_qualification_relay.py` | `adfec038df367eb16e14085debe934f4771ac90921e092a4b34fa7b4fd6734a0` |
| implementation spec | `506520f077cb434050438c8d42f5ec2fdbac6a393768512d0ddba6e1959f65d3` |
| author README | `23c90f60b07db346153d84d6a88e6a858bfd7aee26b98fdce95a84728173c2a5` |
