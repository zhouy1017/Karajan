# Independent Task binding compiler review

The source envelope embeds the actual qualified native mechanism and separately
hashes the Task producer/controller sources. Historical binding compilation is
explicitly separate from current authority. The design correctly requires
operation/Run/Project/Capacity/Host guards at effects, one committed claim, and
no replacement grant after a lost response. No Facade or complete Task execution
is claimed by this review.

## GO-TASK-BINDING-001 (P2, closed)

The `_parts` compiler checks several frozen identities but omits operation id,
planned context id and the execution schema version. Starting with a real public
Run/Workspace/Capacity operation, changing any one of these outer fields still
allows task_grant_binding to compile it as the original operation. This creates
inconsistent compiled records and silently interprets an unknown schema. It is
a compiler consistency problem, not a claim that compilation confers execution
authority or that an untrusted caller can write the controller database.

Independent public test `test_binding_contract.py` preserves these three inputs
and an unchanged positive control. The actual first result was **3 failed,
1 passed**, 5.07 seconds, in `before.xml`. The before source is preserved as
`go_task_binding.before.py.txt`, with a hash in `before-source.json`.

Minimal fix: reject a mismatch between intent.operation_id and operation.id,
intent.context_id and operation.planned_context_id, and reject any execution
schema other than the explicitly implemented v1. Existing schema checks for
operation and Workspace can be made explicit in the same validator. No new
authority model or native test is necessary.

Root added the missing three checks and explicit known Workspace schema and
Workspace/operation identity matching. The untouched original four tests passed
in 4.53 seconds (`after.xml`). The byte-identical formal copy at
`tests/runs/test_go_task_binding_independent.py` also passed, four tests in 4.50
seconds (`published.xml`). `final.json` binds the source and original/formal test
hashes; `go_task_binding.after.py.txt` preserves the reviewed source. No findings
remain in this bounded compiler review.

All data is synthetic fixture material. No product source was changed by this
reviewer, and no model calls or real credential reads were made.

```text
python -m pytest .cache/go-task-binding-review/test_binding_contract.py -o "pythonpath=backend tests/runs tests/projects" -q
```
