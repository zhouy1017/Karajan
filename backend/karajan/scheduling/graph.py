"""Versioned task graphs: immutable revisions, frozen tasks, surviving obligations.

The graph is the durable answer to "what work exists in this run". It is built
from accepted decisions, and it never rewrites a task that already exists. Three
rules are decided here rather than in the store, because they are properties of
the graph itself:

**A revision is immutable.** ``TaskGraphRevision`` N+1 records its parent, the
decision that produced it, the nodes it added or replaced and the resulting
digest of the whole effective task set. Nothing in revision N changes; a
consumer holding revision N keeps holding exactly what it was given.

**A claimed task is frozen.** Once a task is claimed, its inputs, role, paths,
model and dependencies are what the execution consumer was handed. A later
revision can supersede it — producing a *new* node with its own identity — but
it can never edit the old one.

**An obligation is not a suggestion.** Every task carries the outcomes it
promised. A revision is refused unless every promised outcome of every task that
is no longer live is still covered by a live task, so "delete it", "rename it",
"make it optional" and "replace it with something smaller" are all refused when
they would erase a promised result. A failed, cancelled or unknown task keeps its
own record and its own history; only the *coverage* may move.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import SchedulingError, located
from .grants import Grant
from .values import (
    bounded_identifier,
    content_digest,
    non_negative_count,
    normalized_path,
    normalized_scopes,
)

GRAPH_SCHEMA_VERSION = "karajan.task-graph-revision.v1"
TASK_SCHEMA_VERSION = "karajan.scheduled-task.v1"
EXPANSION_SCHEMA_VERSION = "karajan.expansion-set.v1"

#: The control-plane state of one task. ``superseded`` and ``cancelled`` are the
#: two states in which a task is no longer live; both keep their record.
TASK_STATES = (
    "queued",
    "blocked",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "superseded",
    "unknown",
)

#: States in which the task no longer contributes coverage.
RETIRED_STATES = frozenset({"superseded", "cancelled"})

#: States that keep their occupancy: an execution that was started and whose
#: outcome is unknown still holds whatever it holds until it is reconciled.
OCCUPYING_STATES = frozenset({"claimed", "unknown"})

#: Declared terminal states an execution consumer may report. They are reports
#: about an attempt, never a physical verification performed by this engine.
REPORTED_OUTCOMES = ("completed", "failed", "unknown")

TASK_FIELDS = frozenset(
    {
        "task_id",
        "item_key",
        "role_alias",
        "execution_kind_ref",
        "model_ref",
        "tool_refs",
        "output_contract_ref",
        "inputs",
        "depends_on",
        "required",
        "priority",
        "write_zone",
        "path_scope",
        "required_outcomes",
        "expansion_id",
        "joins",
        "supersedes",
        "cancels",
        "obligation_coverage",
    }
)

DECISION_FIELDS = frozenset(
    {
        "decision_id",
        "grant_id",
        "term",
        "expected_graph_revision",
        "graph_digest",
        "inputs_digest",
        "trigger",
        "reason",
        "actions",
    }
)

ACTION_FIELDS = {
    "expand_graph": frozenset({"action", "expansion_id", "tasks"}),
    "bind_role": frozenset({"action", "task_id", "role_alias"}),
    "set_dependencies": frozenset({"action", "task_id", "depends_on"}),
    "set_priority": frozenset({"action", "task_id", "priority"}),
    "request_dispatch": frozenset({"action", "task_id", "dispatch"}),
    "seal": frozenset({"action", "expansion_id", "members", "obligations"}),
}

_ITEM_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def task_identity(decision_id: str, item_key: str) -> str:
    """The stable task identity one decision item produces.

    It is derived from the decision and the item key rather than from a
    submission counter, so a replay of the same decision produces the same task
    identities instead of a second batch of tasks under new names.
    """
    if _ITEM_KEY.fullmatch(item_key) is None:
        raise SchedulingError(
            "SCHEDULING_TASK_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_TASK_INVALID",
                    "scheduling#/actions/tasks/item_key",
                    "an item key is 1..64 letters, digits, dot, dash or underscore",
                )
            ],
        )
    candidate = f"{decision_id}.{item_key}"
    if len(candidate) > 128:
        raise SchedulingError(
            "SCHEDULING_TASK_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_TASK_INVALID",
                    "scheduling#/actions/tasks/item_key",
                    "the derived task identity exceeds 128 characters",
                )
            ],
        )
    return candidate


# --------------------------------------------------------------------- tasks


@dataclass(frozen=True, slots=True)
class Task:
    """One frozen task, as stored and as read back."""

    task_id: str
    run_id: str
    graph_revision: int
    origin: str
    decision_id: str | None
    parent_task_id: str | None
    expansion_id: str | None
    role_alias: str | None
    role_ref: str | None
    execution_kind_ref: str
    model_ref: str | None
    tool_refs: tuple[str, ...]
    output_contract_ref: str
    inputs: Mapping[str, Any]
    depends_on: tuple[str, ...]
    joins: tuple[str, ...]
    required: bool
    priority: int
    write_zone: str
    path_scope: tuple[str, ...]
    required_outcomes: tuple[str, ...]
    obligation_coverage: Mapping[str, Any]
    state: str
    supersedes: str | None
    superseded_by: str | None
    dispatch: str
    claim_id: str | None
    claimed_by: str | None
    claimed_at: float | None
    attempt_ref: str | None
    reports: tuple[Mapping[str, Any], ...]
    history: tuple[Mapping[str, Any], ...]
    digest: str
    document: Mapping[str, Any]

    @property
    def live(self) -> bool:
        return self.state not in RETIRED_STATES

    @property
    def obligations(self) -> tuple[str, ...]:
        """Every outcome this task promised, in a stable order."""
        return self.required_outcomes

    def as_document(self) -> dict[str, Any]:
        return dict(self.document)


def read_task(document: Mapping[str, Any]) -> Task:
    return Task(
        task_id=str(document["task_id"]),
        run_id=str(document["run_id"]),
        graph_revision=int(document["graph_revision"]),
        origin=str(document["origin"]),
        decision_id=document.get("decision_id"),
        parent_task_id=document.get("parent_task_id"),
        expansion_id=document.get("expansion_id"),
        role_alias=document.get("role_alias"),
        role_ref=document.get("role_ref"),
        execution_kind_ref=str(document["execution_kind_ref"]),
        model_ref=document.get("model_ref"),
        tool_refs=tuple(str(item) for item in document.get("tool_refs") or ()),
        output_contract_ref=str(document["output_contract_ref"]),
        inputs=dict(document.get("inputs") or {}),
        depends_on=tuple(str(item) for item in document.get("depends_on") or ()),
        joins=tuple(str(item) for item in document.get("joins") or ()),
        required=bool(document.get("required", True)),
        priority=int(document.get("priority", 0)),
        write_zone=str(document["write_zone"]),
        path_scope=tuple(str(item) for item in document.get("path_scope") or ()),
        required_outcomes=tuple(str(item) for item in document.get("required_outcomes") or ()),
        obligation_coverage=dict(document.get("obligation_coverage") or {}),
        state=str(document["state"]),
        supersedes=document.get("supersedes"),
        superseded_by=document.get("superseded_by"),
        dispatch=str(document.get("dispatch", "dispatched")),
        claim_id=document.get("claim_id"),
        claimed_by=document.get("claimed_by"),
        claimed_at=document.get("claimed_at"),
        attempt_ref=document.get("attempt_ref"),
        reports=tuple(document.get("reports") or ()),
        history=tuple(document.get("history") or ()),
        digest=str(document["digest"]),
        document=dict(document),
    )


def task_document(
    *,
    spec: Mapping[str, Any],
    task_id: str,
    run_id: str,
    graph_revision: int,
    origin: str,
    decision_id: str | None,
    role_ref: str | None,
    parent_task_id: str | None,
    supersedes: str | None,
) -> dict[str, Any]:
    """Build one immutable task document, with the digest that identifies it."""
    document: dict[str, Any] = {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "revision": 1,
        "graph_revision": graph_revision,
        "origin": origin,
        "decision_id": decision_id,
        "parent_task_id": parent_task_id,
        "expansion_id": spec.get("expansion_id"),
        "role_alias": spec.get("role_alias"),
        "role_ref": role_ref,
        "execution_kind_ref": spec["execution_kind_ref"],
        "model_ref": spec.get("model_ref"),
        "tool_refs": list(spec.get("tool_refs") or ()),
        "output_contract_ref": spec["output_contract_ref"],
        "inputs": dict(spec.get("inputs") or {}),
        "depends_on": list(spec.get("depends_on") or ()),
        "declared_depends_on": list(spec.get("declared_depends_on") or ()),
        "inferred_depends_on": list(spec.get("inferred_depends_on") or ()),
        "joins": list(spec.get("joins") or ()),
        "required": bool(spec.get("required", True)),
        "priority": int(spec.get("priority", 0)),
        "write_zone": spec["write_zone"],
        "path_scope": list(spec.get("path_scope") or ()),
        "required_outcomes": list(spec.get("required_outcomes") or ()),
        "obligation_coverage": dict(spec.get("obligation_coverage") or {}),
        "state": "queued",
        "supersedes": supersedes,
        "superseded_by": None,
        "dispatch": "dispatched",
        "claim_id": None,
        "claimed_by": None,
        "claimed_at": None,
        "attempt_ref": None,
        "reports": [],
        "history": [
            {
                "state": "queued",
                "at": None,
                "by": origin,
                "graph_revision": graph_revision,
            }
        ],
        # Two facts that are deliberately separate from every state above: a
        # claim is not a physical start and a report is not a physical
        # completion, so neither is ever written as one.
        "physical_execution": "not_started",
        "model_calls": 0,
    }
    document["digest"] = content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )
    return document


def reseal_task(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of one task document with its digest re-derived.

    Every mutation goes through here, so a stored task can never have a digest
    that describes a different task.
    """
    record = dict(document)
    record["digest"] = content_digest(
        {key: value for key, value in record.items() if key != "digest"}
    )
    return record


def task_changed(document: Mapping[str, Any]) -> bool:
    """Whether a stored task row no longer matches its own digest."""
    declared = document.get("digest")
    return not isinstance(declared, str) or declared != content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )


# ---------------------------------------------------------------- validation


def validate_task_spec(
    value: object,
    *,
    grant: Grant,
    pointer: str,
    definitions: "_Definitions",
    known_tasks: Mapping[str, Task],
    producers: Collection[str] = (),
) -> dict[str, Any]:
    """Validate one task a decision wants to create, against the grant and the graph.

    Nothing here consults the body for authority: the permitted role revisions,
    paths, tools and outputs all come from the *grant*, and a value the grant does
    not permit is refused rather than narrowed silently.
    """
    if not isinstance(value, Mapping):
        raise SchedulingError(
            "SCHEDULING_TASK_INVALID",
            diagnostics=[located("SCHEDULING_TASK_INVALID", pointer, "expected an object")],
        )
    unknown = sorted(set(value) - TASK_FIELDS)
    if unknown:
        raise SchedulingError(
            "SCHEDULING_TASK_INVALID",
            diagnostics=[
                located("SCHEDULING_TASK_INVALID", pointer, "unknown field") for _ in unknown[:8]
            ],
        )
    task_id: str | None = None
    if value.get("task_id") is not None:
        task_id = bounded_identifier(
            value.get("task_id"), "SCHEDULING_TASK_INVALID", f"{pointer}/task_id"
        )
    elif value.get("item_key") is not None:
        item_key = bounded_identifier(
            value.get("item_key"), "SCHEDULING_TASK_INVALID", f"{pointer}/item_key"
        )
        task_id = item_key  # derived by the caller with its decision identity
    role_alias = bounded_identifier(
        value.get("role_alias"), "SCHEDULING_TASK_INVALID", f"{pointer}/role_alias"
    )
    # The grant fixes the role revisions a decision may bind, and the compiled
    # workflow fixes the execution kinds. A body cannot widen either.
    if role_alias not in grant.role_refs:
        raise SchedulingError(
            "SCHEDULING_GRANT_ROLE_NOT_PERMITTED",
            fields={"role_alias": role_alias},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_ROLE_NOT_PERMITTED",
                    f"{pointer}/role_alias",
                    "the grant does not permit binding that role",
                )
            ],
        )
    role_ref = grant.role_refs[role_alias]
    kind_ref = bounded_identifier(
        value.get("execution_kind_ref"),
        "SCHEDULING_TASK_INVALID",
        f"{pointer}/execution_kind_ref",
    )
    definitions.require_kind(kind_ref, pointer)
    if grant.execution_kind_refs and kind_ref not in grant.execution_kind_refs:
        raise SchedulingError(
            "SCHEDULING_GRANT_KIND_NOT_PERMITTED",
            fields={"execution_kind_ref": kind_ref},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_KIND_NOT_PERMITTED",
                    f"{pointer}/execution_kind_ref",
                    "the grant does not permit that execution kind revision",
                )
            ],
        )
    model_ref = value.get("model_ref")
    if model_ref is not None:
        model_ref = bounded_identifier(model_ref, "SCHEDULING_TASK_INVALID", f"{pointer}/model_ref")
        if model_ref not in grant.model_refs:
            raise SchedulingError(
                "SCHEDULING_GRANT_MODEL_NOT_PERMITTED",
                fields={"model_ref": model_ref},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_MODEL_NOT_PERMITTED",
                        f"{pointer}/model_ref",
                        "the grant does not permit that model source",
                    )
                ],
            )
    tool_refs = sorted(
        bounded_identifier(item, "SCHEDULING_TASK_INVALID", f"{pointer}/tool_refs")
        for item in _list(
            value.get("tool_refs", []), "SCHEDULING_TASK_INVALID", f"{pointer}/tool_refs"
        )
    )
    extra_tools = sorted(set(tool_refs) - set(grant.tool_refs))
    if extra_tools:
        raise SchedulingError(
            "SCHEDULING_GRANT_TOOL_NOT_PERMITTED",
            fields={"tool_refs": extra_tools[:8]},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_TOOL_NOT_PERMITTED",
                    f"{pointer}/tool_refs",
                    "the grant does not permit that tool",
                )
            ],
        )
    output_contract_ref = bounded_identifier(
        value.get("output_contract_ref"),
        "SCHEDULING_TASK_INVALID",
        f"{pointer}/output_contract_ref",
    )
    definitions.require_contract(output_contract_ref, pointer)
    inputs = value.get("inputs", {})
    if not isinstance(inputs, Mapping):
        raise SchedulingError(
            "SCHEDULING_TASK_INVALID",
            diagnostics=[
                located("SCHEDULING_TASK_INVALID", f"{pointer}/inputs", "expected an object")
            ],
        )
    definitions.validate_inputs(
        kind_ref, dict(inputs), pointer, grant=grant, producers=producers
    )
    # ``grant.inputs`` is the set of *references* the grant permits reading, not a
    # list of argument names. The argument names are the execution kind's, and
    # they are already checked above; authorising by argument name would let a
    # grant that names ``sources`` accept any reference whatsoever under it.
    declared_depends_on = [
        bounded_identifier(item, "SCHEDULING_TASK_INVALID", f"{pointer}/depends_on")
        for item in _list(
            value.get("depends_on", []),
            "SCHEDULING_TASK_INVALID",
            f"{pointer}/depends_on",
        )
    ]
    # A binding that consumes ``producer.output`` is only satisfiable once that
    # producer has produced it, so the edge is derived from the binding rather
    # than left to an author to restate. The inferred edges join the declared
    # ones before cycle detection and before readiness is computed, so a consumer
    # can never be reported ready ahead of the producer it reads.
    inferred = sorted(
        {
            reference[: -len(".output")]
            for value_bound in inputs.values()
            for reference in _references_of(value_bound)
            if reference.endswith(".output")
            and reference[: -len(".output")] in producers
        }
    )
    depends_on = sorted(set(declared_depends_on) | set(inferred))
    if task_id is not None and task_id in depends_on:
        raise SchedulingError(
            "SCHEDULING_TASK_SELF_DEPENDENCY",
            diagnostics=[
                located(
                    "SCHEDULING_TASK_SELF_DEPENDENCY",
                    f"{pointer}/depends_on",
                    "a task depends on itself",
                )
            ],
        )
    joins = sorted(
        bounded_identifier(item, "SCHEDULING_TASK_INVALID", f"{pointer}/joins")
        for item in _list(value.get("joins", []), "SCHEDULING_TASK_INVALID", f"{pointer}/joins")
    )
    write_zone = normalized_path(
        value.get("write_zone"), "SCHEDULING_TASK_INVALID", f"{pointer}/write_zone"
    )
    path_scope = normalized_scopes(
        value.get("path_scope") or list(grant.scope),
        "SCHEDULING_TASK_INVALID",
        f"{pointer}/path_scope",
    )
    from .values import contains_scope

    if not contains_scope(grant.scope, path_scope):
        raise SchedulingError(
            "SCHEDULING_GRANT_SCOPE_EXCEEDED",
            fields={"scope": list(path_scope)[:8]},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_SCOPE_EXCEEDED",
                    f"{pointer}/path_scope",
                    "the read scope is not contained in the grant's paths",
                )
            ],
        )
    if not contains_scope(grant.write_scope, [write_zone]):
        raise SchedulingError(
            "SCHEDULING_GRANT_SCOPE_EXCEEDED",
            fields={"write_zone": write_zone},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_SCOPE_EXCEEDED",
                    f"{pointer}/write_zone",
                    "the write zone is not contained in the grant's write paths",
                )
            ],
        )
    required_outcomes = sorted(
        bounded_identifier(item, "SCHEDULING_TASK_INVALID", f"{pointer}/required_outcomes")
        for item in _list(
            value.get("required_outcomes", []),
            "SCHEDULING_TASK_INVALID",
            f"{pointer}/required_outcomes",
        )
    )
    outside = sorted(set(required_outcomes) - set(grant.required_outcomes))
    if outside:
        raise SchedulingError(
            "SCHEDULING_GRANT_OUTCOME_NOT_PERMITTED",
            fields={"required_outcomes": outside[:8]},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_OUTCOME_NOT_PERMITTED",
                    f"{pointer}/required_outcomes",
                    "the grant does not carry that outcome obligation",
                )
            ],
        )
    expansion_id = value.get("expansion_id")
    if expansion_id is not None:
        expansion_id = bounded_identifier(
            expansion_id, "SCHEDULING_TASK_INVALID", f"{pointer}/expansion_id"
        )
    required = value.get("required", True)
    if required is not True and required is not False:
        raise SchedulingError(
            "SCHEDULING_TASK_INVALID",
            diagnostics=[
                located("SCHEDULING_TASK_INVALID", f"{pointer}/required", "expected a boolean")
            ],
        )
    priority = non_negative_count(
        value.get("priority", 0), "SCHEDULING_TASK_INVALID", f"{pointer}/priority", maximum=1000
    )
    supersedes = value.get("supersedes")
    if supersedes is not None:
        supersedes = bounded_identifier(
            supersedes, "SCHEDULING_TASK_INVALID", f"{pointer}/supersedes"
        )
        if supersedes not in known_tasks:
            raise SchedulingError(
                "SCHEDULING_TASK_NOT_FOUND",
                diagnostics=[
                    located("SCHEDULING_TASK_NOT_FOUND", f"{pointer}/supersedes", "no such task")
                ],
            )
        if not known_tasks[supersedes].live:
            raise SchedulingError(
                "SCHEDULING_TASK_FROZEN",
                diagnostics=[
                    located(
                        "SCHEDULING_TASK_FROZEN",
                        f"{pointer}/supersedes",
                        "that task is already retired",
                    )
                ],
            )
        if known_tasks[supersedes].state == "claimed":
            # A claimed task is frozen: its successor is a new node, and the old
            # node keeps its claim, its inputs and its history.
            pass
    coverage = value.get("obligation_coverage", {})
    if not isinstance(coverage, Mapping):
        raise SchedulingError("SCHEDULING_TASK_INVALID")
    return {
        "task_id": task_id,
        "item_key": value.get("item_key"),
        "role_alias": role_alias,
        "role_ref": role_ref,
        "execution_kind_ref": kind_ref,
        "model_ref": model_ref,
        "tool_refs": tool_refs,
        "output_contract_ref": output_contract_ref,
        "inputs": dict(inputs),
        "depends_on": depends_on,
        # Kept separately from the effective set so a reader can tell an edge the
        # author wrote from one the binding implies.
        "declared_depends_on": sorted(declared_depends_on),
        "inferred_depends_on": inferred,
        "joins": joins,
        "required": required,
        "priority": priority,
        "write_zone": write_zone,
        "path_scope": list(path_scope),
        "required_outcomes": required_outcomes,
        "expansion_id": expansion_id,
        "supersedes": supersedes,
        "cancels": None,
        "obligation_coverage": {str(key): value for key, value in coverage.items()},
    }


def _list(value: object, code: str, pointer: str, *, limit: int = 512) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SchedulingError(
            code, diagnostics=[located(code, pointer, "a list of identifiers is required")]
        )
    if len(value) > limit:
        raise SchedulingError(
            code, diagnostics=[located(code, pointer, f"at most {limit} entries are accepted")]
        )
    if len(set(value)) != len(value):
        raise SchedulingError(code, diagnostics=[located(code, pointer, "each entry appears once")])
    return list(value)


def validate_decision_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchedulingError("SCHEDULING_INPUT_INVALID")
    unknown = sorted(set(value) - DECISION_FIELDS)
    if unknown:
        raise SchedulingError(
            "SCHEDULING_INPUT_INVALID",
            diagnostics=[
                located("SCHEDULING_INPUT_INVALID", "scheduling#/decision", "unknown field")
                for _ in unknown[:8]
            ],
        )
    decision_id = bounded_identifier(
        value.get("decision_id"), "SCHEDULING_DECISION_INVALID", "scheduling#/decision_id"
    )
    grant_id = bounded_identifier(
        value.get("grant_id"), "SCHEDULING_GRANT_INVALID", "scheduling#/grant_id"
    )
    term = non_negative_count(value.get("term"), "SCHEDULING_INPUT_INVALID", "scheduling#/term")
    expected = non_negative_count(
        value.get("expected_graph_revision"),
        "SCHEDULING_INPUT_INVALID",
        "scheduling#/expected_graph_revision",
    )
    graph_digest = value.get("graph_digest")
    if graph_digest is not None and (
        not isinstance(graph_digest, str) or len(graph_digest) != 64
    ):
        raise SchedulingError("SCHEDULING_INPUT_INVALID")
    inputs_digest = value.get("inputs_digest")
    if not isinstance(inputs_digest, str) or len(inputs_digest) != 64:
        raise SchedulingError(
            "SCHEDULING_INPUT_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_INPUT_INVALID",
                    "scheduling#/inputs_digest",
                    "a decision states the input digest it was made against",
                )
            ],
        )
    trigger = value.get("trigger", "approved_input")
    if not isinstance(trigger, str) or not trigger:
        raise SchedulingError("SCHEDULING_INPUT_INVALID")
    reason = value.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 8_000:
        raise SchedulingError("SCHEDULING_INPUT_INVALID")
    actions = value.get("actions")
    if not isinstance(actions, list) or not actions:
        raise SchedulingError(
            "SCHEDULING_INPUT_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_INPUT_INVALID",
                    "scheduling#/actions",
                    "a decision declares at least one action",
                )
            ],
        )
    if len(actions) > 4_000:
        # An engineering bound on one request body, not a cap on how many tasks a
        # run may contain: the same decisions can be submitted in more batches.
        raise SchedulingError(
            "SCHEDULING_INPUT_INVALID",
            fields={"maximum_actions": 4_000},
            diagnostics=[
                located(
                    "SCHEDULING_INPUT_INVALID",
                    "scheduling#/actions",
                    "submit further batches to add more work",
                )
            ],
        )
    parsed: list[dict[str, Any]] = []
    for index, item in enumerate(actions):
        parsed.append(_action(item, f"scheduling#/actions/{index}"))
    return {
        "decision_id": decision_id,
        "grant_id": grant_id,
        "term": term,
        "expected_graph_revision": expected,
        "graph_digest": graph_digest,
        "inputs_digest": inputs_digest,
        "trigger": trigger,
        "reason": reason,
        "actions": parsed,
    }


def _action(value: object, pointer: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchedulingError(
            "SCHEDULING_ACTION_INVALID",
            diagnostics=[located("SCHEDULING_ACTION_INVALID", pointer, "expected an object")],
        )
    name = value.get("action")
    allowed = ACTION_FIELDS.get(str(name))
    if allowed is None:
        raise SchedulingError(
            "SCHEDULING_ACTION_UNSUPPORTED",
            fields={"action": str(name)[:64]},
            diagnostics=[
                located(
                    "SCHEDULING_ACTION_UNSUPPORTED",
                    pointer,
                    "an action is one of the declared scheduling actions",
                )
            ],
        )
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SchedulingError(
            "SCHEDULING_ACTION_INVALID",
            diagnostics=[
                located("SCHEDULING_ACTION_INVALID", pointer, "unknown field") for _ in unknown[:8]
            ],
        )
    return {str(key): item for key, item in value.items()}


def _references_of(value: Any) -> list[str]:
    """Every symbolic reference one bound value names, in a stable order.

    A ``literal:``-marked value is data and names nothing; a bare string, and each
    element of a list, is a reference that has to resolve.
    """
    if isinstance(value, str):
        return [] if value.startswith("literal:") else [value]
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_references_of(item))
        return found
    return []


# ------------------------------------------------------------- graph helpers


def dependency_cycle(tasks: Mapping[str, Task]) -> list[str] | None:
    """The first dependency cycle in the effective task set, or ``None``.

    Cycles are detected over the *whole* effective set, not only over the tasks a
    single decision adds, because a new edge can close a cycle between two older
    nodes.
    """
    colour: dict[str, int] = {}
    for start in sorted(tasks):
        if colour.get(start):
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                if colour.get(node) == 2:
                    continue
                colour[node] = 1
                path.append(node)
            neighbours = [
                name for name in tasks[node].depends_on if name in tasks
            ]
            if index < len(neighbours):
                stack.append((node, index + 1))
                neighbour = neighbours[index]
                if colour.get(neighbour) == 1:
                    return [*path[path.index(neighbour) :], neighbour]
                if colour.get(neighbour) != 2:
                    stack.append((neighbour, 0))
            else:
                colour[node] = 2
                path.pop()
    return None


def uncovered_obligations(
    effective: Mapping[str, Task],
    all_tasks: Mapping[str, Task],
) -> list[str]:
    """Every outcome a live task promised that the graph no longer carries.

    Two distinctions decide this, and conflating them is how an obligation gets
    silently dropped:

    * A **task's** ``required_outcomes`` are promises that task made, so retiring
      or superseding a task must leave each of its promises on some live task.
      Deleting, renaming, making optional and replacing-with-something-smaller
      are all refusals, because each one would leave a promise with no carrier.
    * The **Run's** ``required_outcomes`` are what the user authorisation asked
      the run to produce overall. They are deliberately *not* checked here. A
      decision that expands part of the work is not required to deliver the whole
      run in one batch; the outcomes still outstanding are reported to the caller
      rather than being refused, and the reconciliation of the Run itself stays
      an explicit, separately-decided act.

    The comparison is over every task the run has ever created, not only the ones
    in the current revision, so a task retired in an earlier revision stays
    covered only as long as something live still carries its promise.
    """
    live: set[str] = set()
    for task in effective.values():
        if task.live:
            live.update(task.obligations)
    missing: set[str] = set()
    for task in all_tasks.values():
        if task.live and task.task_id in effective:
            continue
        for outcome in task.obligations:
            if outcome not in live:
                missing.add(outcome)
    return sorted(missing)


def ready_ordering(tasks: Iterable[Task]) -> list[Task]:
    """Ready tasks in the order the engine would hand them out.

    Higher priority first, then insertion order: a role's priority decision is a
    real ordering, and a priority the engine silently re-sorted would not be one.
    Nothing here adds a task: the list is exactly the ready set.
    """
    return sorted(tasks, key=lambda task: (-task.priority, task.task_id))


# --------------------------------------------------------------- expansions


def expansion_document(
    *,
    expansion_id: str,
    run_id: str,
    grant_id: str,
    parent_task_id: str | None,
    goal: str,
    created_revision: int,
    required_outcomes: Sequence[str],
    member_policy: int | None,
) -> dict[str, Any]:
    """One expansion set: an open batch of members with a declared ceiling.

    ``member_policy`` is the workflow author's declared maximum, preserved as
    declared; ``engine_member_cap`` stays ``None`` because this engine imposes no
    member count of its own.
    """
    document: dict[str, Any] = {
        "schema_version": EXPANSION_SCHEMA_VERSION,
        "expansion_id": expansion_id,
        "run_id": run_id,
        "grant_id": grant_id,
        "parent_task_id": parent_task_id,
        "goal": goal,
        "state": "open",
        "member_policy": member_policy,
        "engine_member_cap": None,
        "required_outcomes": sorted(required_outcomes),
        "members": {},
        "coverage": {},
        "sealed_at": None,
        "sealed_by_decision": None,
        "sealed_revision": None,
        "created_revision": created_revision,
        "batches": 1,
    }
    document["digest"] = content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )
    return document


def reseal_expansion(document: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(document)
    record["digest"] = content_digest(
        {key: value for key, value in record.items() if key != "digest"}
    )
    return record


def expansion_changed(document: Mapping[str, Any]) -> bool:
    declared = document.get("digest")
    return not isinstance(declared, str) or declared != content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )


def graph_document(
    *,
    run_id: str,
    revision: int,
    parent_revision: int,
    parent_digest: str,
    decision_id: str,
    grant_id: str,
    grant_revision: int,
    accepted_under: str,
    user_authorization_id: str,
    inputs_digest: str,
    nodes_added: Sequence[str],
    nodes_superseded: Sequence[str],
    nodes_retired: Sequence[str],
    expansion_states: Mapping[str, str],
    tasks: Mapping[str, Task],
    created_at: float,
) -> dict[str, Any]:
    """The immutable revision record, with a digest over the effective task set."""
    effective = [
        {
            "task_id": task.task_id,
            "digest": task.digest,
            "state": task.state,
            "required": task.required,
            "depends_on": list(task.depends_on),
            "required_outcomes": list(task.required_outcomes),
        }
        for task in sorted(tasks.values(), key=lambda item: item.task_id)
    ]
    document: dict[str, Any] = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "run_id": run_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "parent_digest": parent_digest,
        "decision_id": decision_id,
        "grant_id": grant_id,
        "grant_revision": grant_revision,
        # How this revision came to exist: under the original user
        # authorization, or under a grant that authorization enabled. The
        # initial approval digest is never recomputed into a "new approval".
        "accepted_under": accepted_under,
        "user_authorization_id": user_authorization_id,
        "inputs_digest": inputs_digest,
        "nodes_added": list(nodes_added),
        "nodes_superseded": list(nodes_superseded),
        "nodes_retired": list(nodes_retired),
        "expansions": dict(sorted(expansion_states.items())),
        "tasks": effective,
        "task_count": len(effective),
        "created_at": created_at,
        "model_calls": 0,
        "physical_execution": "not_started",
    }
    document["digest"] = content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )
    return document


def graph_changed(document: Mapping[str, Any]) -> bool:
    declared = document.get("digest")
    return not isinstance(declared, str) or declared != content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )


class _Definitions:
    """The trusted definitions one run froze, consulted for reference checks.

    It is a thin adapter over the compiled workflow and the module-private
    execution-kind registry, so a decision can only name roles, kinds and
    contracts that the frozen deployment really contains.
    """

    def __init__(
        self,
        *,
        role_refs: Mapping[str, str],
        execution_kinds: Mapping[str, Mapping[str, Any]],
        contracts: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.role_refs = dict(role_refs)
        self.execution_kinds = {key: dict(value) for key, value in execution_kinds.items()}
        self.contracts = {key: dict(value) for key, value in contracts.items()}

    def as_document(self) -> dict[str, Any]:
        """The frozen definitions, as stored on the Run that froze them.

        Only the surfaces a decision may name are recorded: the role aliases
        with the exact role revisions they bind, and the execution kinds and
        contracts the loaded deployment really contains. A decision is checked
        against this copy rather than against the live registry, so a later
        deployment cannot widen what an existing Run may reference.
        """
        return {
            "role_refs": dict(sorted(self.role_refs.items())),
            "execution_kinds": {
                key: dict(value) for key, value in sorted(self.execution_kinds.items())
            },
            "contracts": {key: dict(value) for key, value in sorted(self.contracts.items())},
        }

    def require_role(self, alias: str, pointer: str) -> str:
        reference = self.role_refs.get(alias)
        if reference is None:
            raise SchedulingError(
                "SCHEDULING_ROLE_UNRESOLVED",
                fields={"role_alias": alias},
                diagnostics=[
                    located(
                        "SCHEDULING_ROLE_UNRESOLVED",
                        f"{pointer}/role_alias",
                        "the frozen deployment declares no such role alias",
                    )
                ],
            )
        return reference

    def require_kind(self, reference: str, pointer: str) -> None:
        if reference not in self.execution_kinds:
            raise SchedulingError(
                "SCHEDULING_KIND_UNRESOLVED",
                fields={"execution_kind_ref": reference},
                diagnostics=[
                    located(
                        "SCHEDULING_KIND_UNRESOLVED",
                        f"{pointer}/execution_kind_ref",
                        "the frozen deployment does not reference that registered kind",
                    )
                ],
            )

    def require_contract(self, reference: str, pointer: str) -> None:
        if reference not in self.contracts:
            raise SchedulingError(
                "SCHEDULING_CONTRACT_UNRESOLVED",
                fields={"contract_ref": reference},
                diagnostics=[
                    located(
                        "SCHEDULING_CONTRACT_UNRESOLVED",
                        f"{pointer}/output_contract_ref",
                        "the frozen deployment does not declare that contract",
                    )
                ],
            )

    def validate_inputs(
        self,
        kind_ref: str,
        inputs: Mapping[str, Any],
        pointer: str,
        *,
        grant: Grant,
        producers: Collection[str] = (),
    ) -> None:
        """Check one task's bindings against the kind's declared arguments *and* the references.

        Two different questions are decided, and an argument *name* is not an
        authority:

        * the **shape**: the input names are the execution kind's declared
          argument names, and a required argument is bound; and
        * the **reference**: every value a binding resolves names something the
          Run actually froze - one of its declared inputs, or an output of a task
          it already has - and the reference is inside the *grant's* permitted
          set. A grant that names an argument therefore does not authorise an
          arbitrary reference passed as that argument.

        ``literal:``-marked text is data and is not resolved as a reference.
        """
        kind = self.execution_kinds[kind_ref]
        required = [str(name) for name in kind.get("required_inputs") or ()]
        missing = sorted(name for name in required if name not in inputs)
        if missing:
            raise SchedulingError(
                "SCHEDULING_INPUT_MISSING",
                fields={"inputs": missing[:8]},
                diagnostics=[
                    located(
                        "SCHEDULING_INPUT_MISSING",
                        f"{pointer}/inputs",
                        "a required input of the execution kind is not bound",
                    )
                ],
            )
        unknown = sorted(name for name in inputs if name not in set(kind.get("inputs") or ()))
        if unknown:
            raise SchedulingError(
                "SCHEDULING_INPUT_UNKNOWN",
                fields={"inputs": unknown[:8]},
                diagnostics=[
                    located(
                        "SCHEDULING_INPUT_UNKNOWN",
                        f"{pointer}/inputs",
                        "the execution kind declares no such input",
                    )
                ],
            )
        for name, value in sorted(inputs.items()):
            for reference in _references_of(value):
                if reference in grant.inputs:
                    continue
                if reference.endswith(".output"):
                    # A binding may consume the output of a real producer. The
                    # producer must be a declared step of the frozen deployment
                    # or a task this run already has, so a binding cannot name an
                    # output nothing produces.
                    producer = reference[: -len(".output")]
                    if producer in producers:
                        continue
                    raise SchedulingError(
                        "SCHEDULING_INPUT_REFERENCE_UNRESOLVED",
                        fields={"input": name, "reference": reference[:96]},
                        diagnostics=[
                            located(
                                "SCHEDULING_INPUT_REFERENCE_UNRESOLVED",
                                f"{pointer}/inputs/id={name}",
                                "no step or task of this run produces that output",
                            )
                        ],
                    )
                raise SchedulingError(
                    "SCHEDULING_GRANT_INPUT_NOT_PERMITTED",
                    fields={"input": name, "reference": reference[:96]},
                    diagnostics=[
                        located(
                            "SCHEDULING_GRANT_INPUT_NOT_PERMITTED",
                            f"{pointer}/inputs/id={name}",
                            "the grant does not permit reading that reference",
                        )
                    ],
                )
