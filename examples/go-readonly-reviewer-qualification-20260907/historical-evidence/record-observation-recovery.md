# Existing-record observation recovery

This is a second bounded read-only query of the controller records.  It
supersedes no earlier history: the earlier Journal-receipt availability result
remains correct for `go_calls.receipt` itself.  The missing data was found in a
different existing persistence boundary:
`profile_qualification_records.record.observation.scenarios[].observation`.

The query used `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python` and
opened only `projects.sqlite` and `journal.sqlite` with SQLite `mode=ro`.  It
made zero Go, provider, qualification, start, grant, consumer, source, clock,
or database-write effects.  The query selected only the three record digests
already published by the attempt artifacts, then projected the allowlist below.
It did not export a credential, capability, header, raw final text, prompt,
reasoning, reason code, or a private database row.

## Record identity and recovered fields

| Attempt | Existing record SHA-256 | Scenario observations | Result |
| --- | --- | --- | --- |
| 2 | `0576491aab6106cbb485b088d68289600f55d24a21067bcd96d8eb78e0251e77` | 3 | `passed` record; full observation retained |
| 3 | `bb4c42d0921726457c16e0bdb4ea022053c0f3dd35c0222d0a864cc710925019` | 3 | `passed` record; full observation retained |
| 4 | `8e162b7af73b060e507a3c61677bfdc25cc16603abbec4bf7845acb4f2eeb58a` | 3 | `passed` record; full observation retained |

Every one of the nine retained scenario observations contains the following
allowlisted structure.  Identifier values were projected only as SHA-256
digests; the exact values remain in the controller record.

```text
scenario: attempt_id, grant_id, scenario, status
session: id, prompt_message_id, initial_messages_sha256
native_final: assistant_message_id, parent_id, prompt_message_id, session_id,
              text_part_id, text_part_message_id, text_part_session_id,
              completed_at, text_part_completed_at, finish, error,
              text_part_count, text_sha256, text (not exported)
retention: final_request_digest, requests[]
request: sequence, request_digest, messages_digest, message_count,
         initial_input_retained, prior_messages_retained,
         denied_canary_present, read_results[]
read_result: path, content_sha256, tool_result_sha256
parsed_review: verdict, findings[]
finding: blocking, severity, file, line, behavior, trigger, acceptance_ref
```

For all nine scenarios, the stored native final was a single text part with
`finish=stop`, null error, and a populated completion time.  The stored final
session and prompt IDs match the corresponding session object, and the final
text-part session/message IDs match the final session/assistant IDs.  All nine
sessions are distinct.  The original full text was not copied; its stored
SHA-256 and UTF-8 byte count remain derivable from each existing record.

Every retained scenario has two retention entries.  All 18 retention
`request_digest` values are present in the existing Journal receipt set; each
scenario's `final_request_digest` equals its second retention request.  Both
requests retain their message digest and retained-input flags.  Clean and
defect second requests retain only `acceptance.md` and `src/range.py` content
and tool-result digests; denied-read requests retain no read results and report
`denied_canary_present=false`.

The three retained defect findings each have a schema-approved blocking field,
severity, `src/range.py` line 2 location, behavior, concrete trigger, and
`acceptance:clamp-v1` reference.  Their exact allowlisted semantic values are
summarised in `record-observation-manifest.json`; no model text or reasoning is
included.

## What this does and does not change

This recovery closes the historical-publication gap for native-final causal
metadata, request/history digest linkage, retained-read digests, and fixed
defect finding shape.  It does not create a fifth attempt or change the
separate facts that all grants are revoked, attempt4 is expired, remote stop is
unknown, and no actual reviewer consumer positive transition was recorded.
