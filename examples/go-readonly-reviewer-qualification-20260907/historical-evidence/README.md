# Issue 107 historical-evidence supplement

This supplement is a bounded, read-only availability audit of the existing
Issue 107 attempt archive and the controller's existing Store / Journal
records.  It made **zero** provider, Go, qualification, start, grant, or
consumer calls, and did not alter a private database, source, clock,
generation, or qualification record.

The controller was accessed with its prescribed Python interpreter and SQLite
`mode=ro`; only the allowlisted field-presence and aggregate results in
`availability.json` were emitted.  Keys, capabilities, raw headers, private
database values, prompts, raw responses, and reasoning were neither copied nor
printed.

The audit confirms that the existing Journal can support the already-recorded
18-call accounting (nine grants with two calls each) and response-receipt
metadata.  It cannot recover the fields required to prove the full-final and
causal-history acceptance conditions: native session identity, prompt identity,
complete final/text digest, and read/tool-history digest are absent from every
stored call receipt.  `response_bytes` presence is not a replacement for any
of those fields.

The follow-up recovery in `record-observation-recovery.md` and
`causal-identity-digests.json` found the same historical native-final and
retention facts in the existing qualification records.  It corrects only the
earlier statement about what the Journal receipt table itself retained.

This archive still adds no passed Issue 107 S claim.  It preserves the earlier
failures, unknown remote stop, expired/revoked state, and missing real consumer
positive transition.  See `availability.json` for the fixed candidate, sources,
method, and per-acceptance result.
