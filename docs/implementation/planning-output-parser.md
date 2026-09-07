# Strict planning output parser

`karajan.runs.planning_output.parse_planning_output` is the boundary between a
native model response and Karajan's existing plan contracts. It accepts one
complete JSON value and returns `Plan` for `v1` or `PlanV2` for `v2`.

The caller supplies the version from the trusted execution binding. The model
content itself contains only `summary`, `authorization`, and `tasks`. It cannot
carry a `run_id`, intent, term, principal, receipt, admission state, source,
digest, or approval. Unknown fields are rejected by the existing strict
Pydantic contracts.

Before validation, the parser requires an exact `str` or `bytes` input, valid
UTF-8, unique JSON object keys, finite numbers, valid Unicode strings, one
object, and a complete value. The operational limits are 262,144 encoded bytes
and 16 levels of JSON nesting. The parser never truncates input. Rejections
raise `PlanningOutputError` with a stable, content-free `code`.

Parsing is not authorization. A successful result does not prove model
identity, source qualification, capacity or budget admission, artifact
integrity, a Run intent, an owner decision, or full plan validity against an
authorization ceiling. The execution consumer must bind the immutable model
output artifact and invoke `RunPlanner.submit_plan`; RunPlanner remains
responsible for term, commander, admission, path, graph, authorization and
revision checks.
