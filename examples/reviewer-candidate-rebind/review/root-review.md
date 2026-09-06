# Independent Candidate review-rebind review

Reviewer: root. Author: qualification_integration. Date: 2026-09-07 Asia/Hong_Kong.
Base: 624ad8b8490003f155baf7842ba91b9975b9526a. Scope: the full new helper,
strict binding models and two thin CandidateStore ports; not the #95 runtime.

Standards and Spec: no actionable finding. Existing freeze behavior and schema
are unchanged. New effects and exact historical replay have distinct resource
requirements. The transaction protects current-source and command-key checks,
and all Candidate/baseline CAS bytes are verified before inserting a new row.
The request digest binds author/Writer/path/class metadata as well as content.
Only the Reviewer allowed set and derived policy digest change. Model authority
remains the trusted controller's responsibility; stored declarations cannot
qualify a Profile or authorize execution.

Independently ran seven of the author's public Linux cases, covering full content
and executable modes, chained/asset-free lost-reply history and full CAS rejection:

```text
python -m pytest tests/candidates/test_review_rebind.py -q -p no:cacheprovider -k "preserves_complete_candidate or exact_lost_reply_recovery or new_effect_verifies_full" --junitxml=.cache/root-review.xml
```

Result: 7 passed, 35 deselected, 1.42 seconds. These are independent executions of
existing tests, not seven newly authored cases. Full author regressions are
recorded separately. No provider, credential, Git mutation or product edit.

Reviewed source SHA-256:

- `_review_binding.py`: ea6da0ecbf0d3683dd033a5991c740d79138d16e1723b5752bed88c339bf1283
- `models.py`: e5a94a8042d33c46fdd3af58c3bbd8e616895e2b35ee127e19c2757e64c43fb9
- `store.py`: e2833cf7d039dc36c4a03467d66b4ac6eed21886a406b38402c707db26a38eeb
- `test_review_rebind.py`: ae76b86abb4e7eac5c086a560b59a6c4b7096f06218dcd3f4eb2c8e3311d7693

The separate Check implementation's `lookup_evidence` is not in this worktree;
integration must preserve that addition and verify the combined source.
