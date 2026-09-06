# Nullable SSE tool-name diagnostic

The official first projected qualification returned HTTP 200, then failed relay
validation with `INVALID_TOOL_NAME` before executing tools. Its raw provider body
was not retained. This diagnosis does **not** establish which name fragment that
response contained, nor that nullable names caused that particular failure.

SSE parsing lives in `go_relay._stream_facts`; `go_evidence.py` checks observed
fixture behavior and contains no SSE parser. Before the compatibility fix,
`INVALID_TOOL_NAME` had three triggers: a non-string name fragment (including
explicit JSON null), a fragment longer than 32 characters, or concatenated names
for the same tool index longer than 32 characters. A completed disallowed name
instead produced `UNAPPROVED_TOOL`.

Eight synthetic public-relay HTTP cases passed as a diagnostic matrix:
split `re`/`ad`, omitted continuation name and independent parallel `read`
indices were accepted; null, oversized and non-string fragments reproduced
`INVALID_TOOL_NAME`; repeated `read` twice instead produced `UNAPPROVED_TOOL`,
while nine repetitions reached the 32-character accumulated-name limit.
These are deliberately synthetic streams, not reconstructed official bytes.

The parent verified the primary generated OpenAI response schema:
`ChoiceDeltaToolCallFunction.name: Optional[str] = None`, at
https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/chat/chat_completion_chunk.py.
This establishes a narrow compatibility requirement for nullable name deltas.
The formal public tests first produced seven failures and four passes. Three
failures were valid nullable read/edit streams rejected with HTTP 502; four
remaining failures concerned the final-name error category after null handling.

The authorized fix treats only explicit null as no new name fragment. It retains
the existing string length checks, concatenation and final read/edit allowlist.
Null-only, incomplete, repeated, other-tool and non-string names stay rejected.
Only successful streams containing null names receive the additional numeric
`tool_name_null_fragments` diagnostic. Responses without null keep their existing
receipt shape. No raw names, arguments or provider text are added to evidence.

No real key, provider call, system change or blind retry was performed here.
The original official failure remains independently recorded; any later official
qualification must use a new command and the newly bound runtime source.

Final validation: eleven nullable public cases plus the existing relay and
Journal public tests passed (84 total, 11.17 seconds). The initial combined run
also skipped 35 qualification accounting tests because the artifact environment
variable was omitted; those same 35 were then run with the pinned tokenizer
directory and all passed in 3.50 seconds. `after.xml` retains the original skips;
`accounted.xml` records their completed verification. Ruff/format and Linux
mypy passed. `freeze.json` records the exact final source/test/evidence bytes and
the pre-change relay hash. The other agent owns the subsequent actual native
Store fixture using null continuation frames; it is not counted as this author's
test evidence.
