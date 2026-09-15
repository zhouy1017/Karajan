"""The authenticated role-directed scheduling control plane.

This module owns the durable facts of one authorised workflow Run: the frozen
deployment and initial authorization it was created from, its scheduler grants
and delegated sub-grants, its issued credentials, its versioned task graph, its
expansion sets, its trusted resource observations and its write-zone claims.

Four boundaries are structural rather than documented.

**Authority comes from a credential the service issued.** Every entry point takes
a resolved :class:`~karajan.scheduling.credentials.Principal`. No request body is
consulted for a role name, an execution reference, a run identity or a
``user=true`` flag, and a body that carries one is refused by the route layer
before a store method is reached.

**A decision commits atomically or not at all.** Every action in a batch is
validated and the resulting graph is fully compiled *before* the first write
statement runs, and the writes then happen in one short transaction. A batch
whose second operation is invalid therefore leaves the graph, the receipt and the
queue exactly as they were.

**The graph pointer moves by compare-and-swap.** A revision is committed only if
the current pointer still names the revision the decision was made against, so
two grants modifying overlapping work produce one winner and one explicit
conflict rather than a silent overwrite.

**Occupancy is derived, never counted.** How much of a budget a run holds is read
from its live tasks at the moment it is asked, so a restart, a new grant, a new
graph revision or a timeout cannot reset it by forgetting a counter.
"""

import json
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from karajan.projects import ProjectError, ProjectRegistry
from karajan.workflows.deployments import DeploymentStore
from karajan.workflows.errors import WorkflowError

from .credentials import (
    ROUTES_BY_KIND,
    CredentialRecord,
    IssuedCredential,
    Principal,
    new_token,
    parse_issued,
    token_digest,
    validate_issue_request,
    validate_kind,
)
from .errors import CONFLICT_CODES, SchedulingError, located
from .grants import (
    DELIVERY_RANKS,
    Grant,
    grant_digest,
    grant_expired,
    require_delegation_depth,
    require_subset,
    validate_grant_payload,
)
from .graph import (
    _Definitions,
    dependency_cycle,
    expansion_changed,
    expansion_document,
    graph_changed,
    graph_document,
    read_task,
    ready_ordering,
    reseal_expansion,
    reseal_task,
    task_changed,
    task_document,
    task_identity,
    uncovered_obligations,
    validate_decision_payload,
    validate_task_spec,
)
from .values import (
    bounded_identifier,
    canonical_json,
    content_digest,
    non_negative_count,
    normalized_path,
    number,
    positive_count,
    zones_overlap,
)

RUN_SCHEMA_VERSION = "karajan.workflow-run.v1"
AUTHORIZATION_SCHEMA_VERSION = "karajan.scheduling-authorization.v1"
DECISION_SCHEMA_VERSION = "karajan.scheduling-decision.v1"
CLAIM_SCHEMA_VERSION = "karajan.task-claim.v1"
OBSERVATION_SCHEMA_VERSION = "karajan.resource-observation.v1"
RESOURCE_POLICY_SCHEMA_VERSION = "karajan.scheduling-resource-policy.v1"

#: The identifier of the user authorisation a Run is created with. It is a
#: property of the Run, frozen at creation, and it never changes when the graph
#: grows: a later revision is ``accepted_under_grant`` against this same id.
INITIAL_AUTHORIZATION_ID = "run-initial-authorization"

#: Engineering page bound for one queue or task read. It is a transport bound on
#: one response, not a cap on how many tasks a run may contain: the same set is
#: read completely across pages.
MAXIMUM_PAGE = 100
DEFAULT_PAGE = 50

#: The declared page bound for one decision submission. A larger legal task set
#: is submitted in further batches; nothing truncates it to the first K items.
MAXIMUM_ACTIONS_PER_DECISION = 4_000

#: Resource-observation sources this store accepts from a *trusted* caller. A
#: scheduler credential cannot write one of these; that separation is what keeps
#: a role from inventing the capacity it is admitted against.
TRUSTED_SOURCES = ("local_ledger", "official", "manual")

RUN_FIELDS = frozenset(
    {
        "conversation_id",
        "slot",
        "expected_active_revision",
        "deployment_id",
        "inputs",
        "requirement",
        "required_outcomes",
        "delivery_artifact",
        "title",
    }
)

CLAIM_FIELDS = frozenset({"claim_key", "task_id"})
REPORT_FIELDS = frozenset(
    {"outcome", "evidence_ref", "attempt_ref", "note", "usage", "claim_key"}
)
OBSERVATION_FIELDS = frozenset(
    {
        "pool_id",
        "window_id",
        "metric",
        "amount",
        "limit",
        "source",
        "source_ref",
        "reset_at",
        "adjustment_reason",
    }
)
RESOURCE_POLICY_FIELDS = frozenset(
    {"pool_id", "max_concurrent_claims", "safety_margin", "require_observation"}
)


@dataclass(frozen=True, slots=True)
class _Consumption:
    """What one run already holds of one shared pool.

    ``live_claims`` is the occupancy a new claim must fit beside, and
    ``consumed_cost`` is the budget it has already spent. They are separate
    because a released claim stops occupying a slot while the budget it consumed
    stays spent.
    """

    pool_id: str
    live_claims: int
    consumed_cost: int
    budget_refs: frozenset[str] | set[str]


@dataclass(frozen=True, slots=True)
class Admitted:
    """The outcome of one capacity check, with the reason it was made."""

    admitted: bool
    reason: str
    pool_id: str
    occupied: int
    capacity: int | None
    remaining: int | None
    source: str

    def as_document(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "pool_id": self.pool_id,
            "occupied": self.occupied,
            "capacity": self.capacity,
            "remaining": self.remaining,
            "observation_source": self.source,
        }


class _RejectedDecision(SchedulingError):
    """One already-recorded refusal, replayed with its original facts.

    It carries the whole stored receipt, so the caller receives the same reason,
    revision and command identity that the first attempt received. It is a
    separate type from a freshly adjudicated refusal so the caller does not
    record it a second time: the durable evidence is written once.
    """

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(
            str(record["reason_code"]),
            current_revision=(
                None
                if record.get("current_graph_revision") is None
                else int(record["current_graph_revision"])
            ),
        )
        self.record = record


class SchedulingStore:
    """Durable roles, grants, graph revisions, resource facts and claims."""

    def __init__(
        self,
        projects: ProjectRegistry,
        deployments: DeploymentStore,
        conversations: Any | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.deployments = deployments
        # The conversation store owns its own durable state, so the ownership
        # check on a Run's conversation goes through it rather than guessing at
        # another module's table.
        self.conversations = conversations
        self.clock = clock
        self.unavailable: str | None = None
        if projects.existing_only:
            self.unavailable = "SCHEDULING_STATE_UNAVAILABLE"
            return
        with self.projects._transaction() as db:
            self._create_tables(db)

    @staticmethod
    def _create_tables(db: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS workflow_runs ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "record TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(project_id, run_id))",
            "CREATE TABLE IF NOT EXISTS workflow_run_keys ("
            "principal TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, "
            "run_id TEXT NOT NULL, PRIMARY KEY(principal, key))",
            "CREATE TABLE IF NOT EXISTS scheduler_grants ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "grant_id TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT NOT NULL, "
            "digest TEXT NOT NULL, revoked_at REAL, "
            "PRIMARY KEY(project_id, run_id, grant_id, revision))",
            "CREATE TABLE IF NOT EXISTS scheduler_grant_current ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "grant_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "PRIMARY KEY(project_id, run_id, grant_id))",
            "CREATE TABLE IF NOT EXISTS scheduling_credentials ("
            "credential_id TEXT NOT NULL PRIMARY KEY, project_id TEXT NOT NULL, "
            "run_id TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS scheduling_decisions ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "key TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL, "
            "PRIMARY KEY(project_id, run_id, key))",
            # A refused decision keeps its own receipt. The graph is untouched,
            # but "this command was refused, here is why, and here is the
            # revision that was current" is durable evidence that the conflict
            # happened, rather than a fact only the losing client remembers.
            "CREATE TABLE IF NOT EXISTS scheduling_decision_rejections ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "key TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL, "
            "PRIMARY KEY(project_id, run_id, key))",
            "CREATE TABLE IF NOT EXISTS task_graph_revisions ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "revision INTEGER NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, "
            "PRIMARY KEY(project_id, run_id, revision))",
            "CREATE TABLE IF NOT EXISTS task_graph_current ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "revision INTEGER NOT NULL, PRIMARY KEY(project_id, run_id))",
            "CREATE TABLE IF NOT EXISTS scheduling_tasks ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "task_id TEXT NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, "
            "PRIMARY KEY(project_id, run_id, task_id))",
            "CREATE TABLE IF NOT EXISTS task_expansions ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "expansion_id TEXT NOT NULL, record TEXT NOT NULL, "
            "PRIMARY KEY(project_id, run_id, expansion_id))",
            # Every version a task has ever had, keyed by its own digest. A seal
            # pins a member by revision and digest, and a digest with no body
            # behind it cannot be read: this is the table that makes a pinned
            # member resolvable after the live task has moved on.
            "CREATE TABLE IF NOT EXISTS scheduling_task_versions ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "task_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, "
            "record TEXT NOT NULL, PRIMARY KEY(project_id, run_id, task_id, revision))",
            "CREATE TABLE IF NOT EXISTS task_claims ("
            "project_id TEXT NOT NULL REFERENCES projects(id), run_id TEXT NOT NULL, "
            "claim_key TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL, "
            "PRIMARY KEY(project_id, run_id, claim_key))",
            # Occupancy is recorded, not merely derived, so it survives a graph
            # revision that supersedes the task that was claimed, and so a
            # restart, a new grant and a new graph cannot reset it by replacing
            # the row that used to carry the fact.
            "CREATE TABLE IF NOT EXISTS budget_consumption ("
            "claim_id TEXT NOT NULL PRIMARY KEY, project_id TEXT NOT NULL, "
            "run_id TEXT NOT NULL, pool_id TEXT NOT NULL, budget_ref TEXT NOT NULL, "
            "root_grant_id TEXT NOT NULL, record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS resource_observations ("
            "pool_id TEXT NOT NULL, sequence INTEGER NOT NULL, record TEXT NOT NULL, "
            "trusted INTEGER NOT NULL, digest TEXT NOT NULL, "
            "PRIMARY KEY(pool_id, sequence))",
            "CREATE TABLE IF NOT EXISTS resource_policies ("
            "pool_id TEXT NOT NULL PRIMARY KEY, record TEXT NOT NULL, digest TEXT NOT NULL)",
            # One issuance command, so a lost response is answered with the same
            # credential identity rather than by minting a second live one.
            "CREATE TABLE IF NOT EXISTS credential_issuances ("
            "principal TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, "
            "credential_id TEXT NOT NULL, record TEXT NOT NULL, "
            "PRIMARY KEY(principal, key))",
        )
        for statement in statements:
            db.execute(statement)

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _owned(self, project_id: str, principal: str = "owner") -> Iterator[sqlite3.Connection]:
        if self.unavailable is not None:
            raise SchedulingError(self.unavailable)
        if (
            not isinstance(project_id, str)
            or not 1 <= len(project_id) <= 256
            or any(character.isspace() for character in project_id)
        ):
            raise SchedulingError("SCHEDULING_PROJECT_NOT_FOUND")
        try:
            with self.projects._transaction() as db:
                known = db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
                if known is None:
                    raise SchedulingError("SCHEDULING_PROJECT_NOT_FOUND")
                self.projects._require_owner(db, project_id, principal)
                yield db
        except ProjectError as error:
            code = (
                "SCHEDULING_PROJECT_NOT_FOUND"
                if error.code in {"USER_DECISION_REQUIRED", "PROJECT_OWNER_UNRESOLVED"}
                else "SCHEDULING_PROJECT_NOT_FOUND"
                if "NOT_FOUND" in error.code
                else "SCHEDULING_INPUT_INVALID"
            )
            raise SchedulingError(code) from None
        except (OSError, sqlite3.Error) as error:
            raise SchedulingError(
                "SCHEDULING_STATE_UNAVAILABLE", fields={"detail": type(error).__name__}
            ) from None

    @contextmanager
    def _read(self, project_id: str, principal: str = "owner") -> Iterator[sqlite3.Connection]:
        """A short read-only transaction over the same durable state."""
        with self._owned(project_id, principal) as db:
            yield db

    # -------------------------------------------------------- run creation (AC1)

    def create_run(
        self,
        project_id: str,
        payload: object,
        *,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create one Run from an active, freshly loaded deployment.

        The definition is acquired through ``DeploymentStore.accept``, which
        re-reads and re-verifies the real package in *this* process. The acquired
        handle is frozen first and the chosen slot revision and ownership are then
        rechecked inside the short transaction that inserts the Run, so a
        deployment that is replaced while the verification runs cannot silently
        authorise a Run whose frozen identity no longer describes the active one.

        The command is reconciled **before** the fresh source is consulted. A
        repeated key therefore returns the original Run even when the active
        deployment has since been replaced or made unreadable: the replay answers
        from the durable command, not from whatever is active now. Only a *new*
        command — a new key — requires a currently verified active definition.
        """
        fields = self._require_fields(payload, RUN_FIELDS, "run#/payload")
        project_id = bounded_identifier(project_id, "SCHEDULING_PROJECT_NOT_FOUND", "run#/project")
        conversation_id = bounded_identifier(
            fields.get("conversation_id"), "SCHEDULING_CONVERSATION_INVALID", "run#/conversation_id"
        )
        slot = normalized_path(fields.get("slot"), "SCHEDULING_SLOT_INVALID", "run#/slot")
        if "/" in slot:
            raise SchedulingError("SCHEDULING_SLOT_INVALID")
        expected_active = non_negative_count(
            fields.get("expected_active_revision"),
            "SCHEDULING_INPUT_INVALID",
            "run#/expected_active_revision",
        )
        expected_deployment = bounded_identifier(
            fields.get("deployment_id"), "SCHEDULING_DEPLOYMENT_INVALID", "run#/deployment_id"
        )
        inputs = fields.get("inputs", {})
        if not isinstance(inputs, Mapping) or not inputs:
            raise SchedulingError(
                "SCHEDULING_RUN_INPUT_REQUIRED",
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_INPUT_REQUIRED",
                        "run#/inputs",
                        "a Run freezes the concrete inputs it was created with",
                    )
                ],
            )
        requirement = fields.get("requirement")
        if not isinstance(requirement, Mapping):
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        required_outcomes = sorted(
            self._text_list(
                fields.get("required_outcomes"),
                "SCHEDULING_INPUT_INVALID",
                "run#/required_outcomes",
            )
        )
        if not required_outcomes:
            raise SchedulingError(
                "SCHEDULING_INPUT_INVALID",
                diagnostics=[
                    located(
                        "SCHEDULING_INPUT_INVALID",
                        "run#/required_outcomes",
                        "the initial authorization names the outcomes the run must produce",
                    )
                ],
            )
        title = fields.get("title", "")
        if not isinstance(title, str) or len(title) > 200:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        inputs_digest = content_digest(dict(inputs))
        digest = content_digest(
            [
                "scheduling.create_run",
                project_id,
                conversation_id,
                slot,
                expected_active,
                expected_deployment,
                inputs_digest,
                dict(requirement),
                required_outcomes,
            ]
        )
        # ------------------------------------------------------------------
        # Reconciliation comes first, and it is authoritative.
        #
        # A repeated command is answered from the durable command ledger: the
        # same key with the same payload returns the original Run even if the
        # active deployment has since been replaced, rolled back or corrupted.
        # Only then, for a *new* command, is the fresh active source consulted.
        # ------------------------------------------------------------------
        run_id = f"run-{content_digest(['run', project_id, conversation_id, command_key])[:32]}"
        with self._owned(project_id) as db:
            original = self._run_key(db, principal, command_key)
            if original is not None:
                if original["digest"] != digest:
                    raise SchedulingError("SCHEDULING_IDEMPOTENCY_CONFLICT")
                return self._run_document(db, project_id, str(original["run_id"])), False
            existing = db.execute(
                "SELECT 1 FROM workflow_runs WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchone()
            if existing is not None:
                # A different command already owns this identity: the identity is
                # derived from the command, so this is a genuine collision rather
                # than a replay, and it must not overwrite the original Run.
                raise SchedulingError("SCHEDULING_RUN_ALREADY_EXISTS")
        # The conversation is resolved through its own store, which owns that
        # durable state; a Run is bound to a conversation of *this* project, and
        # another project's conversation identity is not usable here.
        self._require_conversation(project_id, conversation_id)
        # Freeze the exact acquired snapshot and remember what was confirmed.
        acquired = self.deployments.accept(project_id, slot, principal="owner")
        handle = acquired.as_document()
        observed_deployment = str(handle["deployment"]["deployment_id"])
        observed_slot_revision = int(handle["slot"]["slot_revision"])
        if (
            observed_deployment != expected_deployment
            or observed_slot_revision != expected_active
        ):
            raise SchedulingError(
                "SCHEDULING_RUN_SOURCE_STALE",
                fields={
                    "active_deployment_id": observed_deployment,
                    "active_slot_revision": observed_slot_revision,
                },
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_SOURCE_STALE",
                        "run#/deployment_id",
                        "the confirmed active deployment is no longer the loaded one",
                    )
                ],
            )
        # The concrete inputs are checked against the *frozen deployment*: a
        # missing declared input, an input the template never declares, and a
        # value whose shape the declared contract refuses are all refusals here
        # rather than being accepted as an opaque non-empty mapping.
        self._validate_run_inputs(handle, dict(inputs))
        definitions = self._definitions(handle)
        frozen = self._frozen_source(handle, acquired)
        record = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "command_key": command_key,
            "slot": slot,
            "title": title,
            "created_by": principal,
            "created_at": self.clock(),
            "inputs": dict(inputs),
            "inputs_digest": inputs_digest,
            "requirement": dict(requirement),
            "required_outcomes": required_outcomes,
            "delivery_artifact": fields.get("delivery_artifact"),
            "source": frozen,
            "definitions": definitions.as_document(),
            "initial_authorization": {
                "schema_version": AUTHORIZATION_SCHEMA_VERSION,
                "authorization_id": INITIAL_AUTHORIZATION_ID,
                "kind": "user_approval",
                "approved_by": principal,
                "approved_at": self.clock(),
                "inputs_digest": inputs_digest,
                "required_outcomes": required_outcomes,
                "requirement": dict(requirement),
            },
            "graph_revision": 0,
            "graph_digest": "",
            "model_calls": 0,
            "business_steps_executed": 0,
            "physical_execution": "not_started",
        }
        record["digest"] = content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        )
        with self._owned(project_id) as db:
            again = self._run_key(db, principal, command_key)
            if again is not None:
                if again["digest"] != digest:
                    raise SchedulingError("SCHEDULING_IDEMPOTENCY_CONFLICT")
                return self._run_document(db, project_id, str(again["run_id"])), False
            # Recheck the confirmed source and ownership in *this* transaction.
            self._recheck_source(db, project_id, run_id, slot, expected_active, expected_deployment)
            db.execute(
                "INSERT INTO workflow_runs VALUES (?,?,?,?)",
                (project_id, run_id, canonical_json(record), record["digest"]),
            )
            db.execute(
                "INSERT INTO task_graph_current VALUES (?,?,?)", (project_id, run_id, 0)
            )
            db.execute(
                "INSERT INTO workflow_run_keys VALUES (?,?,?,?)",
                (principal, command_key, digest, run_id),
            )
            return self._run_document(db, project_id, run_id), True

    def _validate_run_inputs(self, handle: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
        """Check the concrete inputs against the frozen deployment's declaration.

        Three different things are refused, and they are different because they
        mean different things: an input the template declares but the caller did
        not supply, an input the template never declares, and a value whose shape
        contradicts the declared input contract. A non-empty mapping is not, by
        itself, a valid set of inputs for a particular workflow.
        """
        definition = dict(handle["definition"])
        declared = [str(name) for name in definition.get("declared_inputs") or ()]
        contract_ref = str(definition.get("workflow", {}).get("input_contract_ref") or "")
        if not declared:
            raise SchedulingError(
                "SCHEDULING_RUN_INPUT_UNEXPECTED",
                fields={"inputs": sorted(inputs)[:16]},
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_INPUT_UNEXPECTED",
                        "run#/inputs",
                        "the frozen deployment declares no inputs for a Run to bind",
                    )
                ],
            )
        missing = sorted(name for name in declared if name not in inputs)
        if missing:
            raise SchedulingError(
                "SCHEDULING_RUN_INPUT_MISSING",
                fields={"missing_inputs": missing[:16]},
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_INPUT_MISSING",
                        f"run#/inputs/id={name}",
                        "the frozen deployment declares this input and nothing binds it",
                    )
                    for name in missing[:16]
                ],
            )
        unknown = sorted(name for name in inputs if name not in set(declared))
        if unknown:
            raise SchedulingError(
                "SCHEDULING_RUN_INPUT_UNEXPECTED",
                fields={"unexpected_inputs": unknown[:16]},
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_INPUT_UNEXPECTED",
                        f"run#/inputs/id={name}",
                        "the frozen deployment declares no such input",
                    )
                    for name in unknown[:16]
                ],
            )
        # The declared input contract decides the value's shape. ``text@1``
        # therefore takes non-empty text, and a name such as ``requirement.x``
        # is a *reference the workflow declares*, not a symbolic value the caller
        # may replace with a different kind of object.
        for name, value in sorted(inputs.items()):
            self._validate_input_value(
                name, value, contract_ref=contract_ref, declared=declared
            )

    @staticmethod
    def _validate_input_value(
        name: str, value: Any, *, contract_ref: str, declared: list[str]
    ) -> None:
        del declared
        if contract_ref != "text@1":
            if not isinstance(value, (str, int, float, bool, list, dict)) or value is None:
                raise SchedulingError(
                    "SCHEDULING_RUN_INPUT_INVALID",
                    fields={"input": name},
                    diagnostics=[
                        located(
                            "SCHEDULING_RUN_INPUT_INVALID",
                            f"run#/inputs/id={name}",
                            "the value is not of a declared input type",
                        )
                    ],
                )
            return
        if isinstance(value, list):
            if not value or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise SchedulingError(
                    "SCHEDULING_RUN_INPUT_INVALID",
                    fields={"input": name},
                    diagnostics=[
                        located(
                            "SCHEDULING_RUN_INPUT_INVALID",
                            f"run#/inputs/id={name}",
                            "a text@1 input is text, or a non-empty list of text",
                        )
                    ],
                )
            return
        if not isinstance(value, str) or not value.strip():
            raise SchedulingError(
                "SCHEDULING_RUN_INPUT_INVALID",
                fields={"input": name, "declared_contract": contract_ref},
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_INPUT_INVALID",
                        f"run#/inputs/id={name}",
                        "a text@1 input is non-empty text",
                    )
                ],
            )

    def _require_conversation(self, project_id: str, conversation_id: str) -> None:
        """Require a conversation of *this* project, resolved by its own store."""
        if self.conversations is None:
            raise SchedulingError("SCHEDULING_CONVERSATION_NOT_FOUND")
        try:
            owner = self.conversations.conversation_project(conversation_id)
        except Exception:  # noqa: BLE001 - a missing conversation is a refusal
            raise SchedulingError(
                "SCHEDULING_CONVERSATION_NOT_FOUND",
                diagnostics=[
                    located(
                        "SCHEDULING_CONVERSATION_NOT_FOUND",
                        "run#/conversation_id",
                        "no such conversation in this project",
                    )
                ],
            ) from None
        if owner != project_id:
            raise SchedulingError(
                "SCHEDULING_CONVERSATION_NOT_FOUND",
                diagnostics=[
                    located(
                        "SCHEDULING_CONVERSATION_NOT_FOUND",
                        "run#/conversation_id",
                        "that conversation belongs to another project",
                    )
                ],
            )

    def _recheck_source(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        slot: str,
        expected_active: int,
        expected_deployment: str,
    ) -> None:
        """Confirm the confirmed deployment is still the active one, now.

        The definition was acquired outside this transaction, so the slot could
        have moved while it was verified. The confirmation is only honoured if
        the slot still names the same deployment at the same revision; otherwise
        the Run would freeze a definition that was already no longer active when
        it was authorised.
        """
        del run_id
        row = db.execute(
            "SELECT slot_revision, deployment_id FROM workflow_deployment_slots "
            "WHERE project_id=? AND slot=?",
            (project_id, slot),
        ).fetchone()
        current_revision = int(row["slot_revision"]) if row is not None else 0
        current_deployment = (
            str(row["deployment_id"]) if row is not None and row["deployment_id"] else None
        )
        if current_deployment != expected_deployment or current_revision != expected_active:
            raise SchedulingError(
                "SCHEDULING_RUN_SOURCE_STALE",
                fields={
                    "active_deployment_id": current_deployment,
                    "active_slot_revision": current_revision,
                },
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_SOURCE_STALE",
                        "run#/deployment_id",
                        "the active deployment changed while the Run was being created",
                    )
                ],
            )

    def _frozen_source(self, handle: Mapping[str, Any], acquired: Any) -> dict[str, Any]:
        """The stable deployment identity a Run freezes, kept across revisions.

        The acquisition digest covers the load timestamp and the process that
        performed it, so it identifies *this* acquisition and is recorded as
        such. The recheckable identities are the durable ones: the deployment,
        the bundle revision, the compiled and per-file digests.
        """
        del acquired
        deployment = dict(handle["deployment"])
        return {
            "deployment_id": deployment["deployment_id"],
            "slot": deployment["slot"],
            "slot_revision": int(handle["slot"]["slot_revision"]),
            "bundle_id": deployment["bundle_id"],
            "bundle_revision": int(deployment["bundle_revision"]),
            "bundle_digest": deployment["bundle_digest"],
            "manifest_file_digest": deployment["manifest_file_digest"],
            "compiled_digest": deployment["compiled_digest"],
            "compiler_identity": deployment["compiler_identity"],
            "compiler_revision": int(deployment["compiler_revision"]),
            "delivery_kind": deployment["delivery_kind"],
            "files": [dict(item) for item in deployment["files"]],
            "available_execution_kinds": list(deployment["available_execution_kinds"]),
            "acquisition_digest": handle["definition_digest"],
            "acquired_by_process": handle["acquired_load"]["loaded_by_process"],
            "acquired_at": handle["acquired_load"]["loaded_at"],
            "loader_identity": handle["acquired_load"]["loader_identity"],
        }

    def _definitions(self, handle: Mapping[str, Any]) -> "_Definitions":
        """The trusted definitions frozen from one acquired handle.

        Only roles, kinds and contracts the *loaded* deployment really declares
        are advertised here, so a decision can name nothing else. An execution
        kind the deployment references but whose adapter is absent is still
        listed: the loader already refused to call such a deployment ready, so a
        run can never be created from one.
        """
        definition = dict(handle["definition"])
        roles = {
            str(step["role_alias"]): str(step["role_ref"])
            for step in definition.get("steps") or []
            if step.get("role_alias") and step.get("role_ref")
        }
        for alias, reference in (definition.get("roles") or {}).items():
            roles.setdefault(str(alias), str(reference))
        scheduler = definition.get("scheduling") or {}
        if scheduler.get("role_alias") and scheduler.get("role_ref"):
            roles.setdefault(str(scheduler["role_alias"]), str(scheduler["role_ref"]))
        kinds: dict[str, dict[str, Any]] = {}
        for kind_ref in definition.get("execution_kinds", {}).get("available") or []:
            registered = _registered_kind(str(kind_ref))
            if registered is not None:
                kinds[str(kind_ref)] = registered
        contracts = _declared_contracts()
        return _Definitions(role_refs=roles, execution_kinds=kinds, contracts=contracts)

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        with self._read(project_id) as db:
            return self._run_document(db, project_id, run_id)

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        with self._read(project_id) as db:
            rows = db.execute(
                "SELECT record FROM workflow_runs WHERE project_id=? ORDER BY run_id",
                (project_id,),
            ).fetchall()
            return [self._validated(json.loads(row["record"]), "SCHEDULING_RUN_RECORD_CHANGED")
                    for row in rows]

    def _run_key(
        self, db: sqlite3.Connection, principal: str, command_key: str
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT digest, run_id FROM workflow_run_keys WHERE principal=? AND key=?",
            (principal, command_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def _run_row(self, db: sqlite3.Connection, project_id: str, run_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT record FROM workflow_runs WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_RUN_NOT_FOUND")
        return self._validated(json.loads(row["record"]), "SCHEDULING_RUN_RECORD_CHANGED")

    def _run_document(
        self, db: sqlite3.Connection, project_id: str, run_id: str
    ) -> dict[str, Any]:
        """One Run as stored, beside the graph pointer as it is right now."""
        record = self._run_row(db, project_id, run_id)
        pointer = db.execute(
            "SELECT revision FROM task_graph_current WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchone()
        revision = int(pointer["revision"]) if pointer is not None else 0
        record = {**record, "graph_revision": revision}
        if revision:
            head = db.execute(
                "SELECT record FROM task_graph_revisions "
                "WHERE project_id=? AND run_id=? AND revision=?",
                (project_id, run_id, revision),
            ).fetchone()
            record["graph_digest"] = (
                str(json.loads(head["record"])["digest"]) if head is not None else ""
            )
        else:
            record["graph_digest"] = ""
        return record

    # ------------------------------------------------------------------ grants

    def issue_grant(
        self,
        project_id: str,
        run_id: str,
        payload: object,
        *,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Issue one top-level grant under the Run's initial user authorization."""
        parsed = validate_grant_payload(payload)
        with self._owned(project_id) as db:
            record = self._issue_grant(db, project_id, run_id, parsed, parent=None)
            return record, True

    def delegate_grant(
        self,
        project_id: str,
        run_id: str,
        payload: object,
        *,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Derive one child grant, refusing anything that is not a strict subset."""
        del command_key
        if not isinstance(payload, Mapping) or "parent_grant_id" not in payload:
            raise SchedulingError(
                "SCHEDULING_GRANT_INVALID",
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_INVALID",
                        "grant#/parent_grant_id",
                        "a delegated grant names the grant it derives from",
                    )
                ],
            )
        parent_id = bounded_identifier(
            payload["parent_grant_id"], "SCHEDULING_GRANT_INVALID", "grant#/parent_grant_id"
        )
        body = {key: value for key, value in payload.items() if key != "parent_grant_id"}
        parsed = validate_grant_payload(body)
        with self._owned(project_id) as db:
            parent = self._grant_row(db, project_id, run_id, parent_id)
            self._require_grant_live(parent, self.clock())
            record = self._issue_grant(db, project_id, run_id, parsed, parent=parent)
            return record, True

    def _issue_grant(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        parsed: Mapping[str, Any],
        *,
        parent: Grant | None,
    ) -> dict[str, Any]:
        run = self._run_row(db, project_id, run_id)
        now = self.clock()
        self._require_frozen_definitions(run, parsed, pointer="grant#/role_refs")
        if parent is not None:
            require_subset(parent, parsed)
            root = self._grant_row(db, project_id, run_id, parent.root_grant_id)
            depth = parent.depth + 1
            require_delegation_depth(root, parent, depth)
            root_grant_id = parent.root_grant_id
            root_authorization_id = parent.root_authorization_id
            issuer_ref = parent.grant_id
            term = parent.term
        else:
            depth = 0
            root_grant_id = str(parsed["grant_id"])
            root_authorization_id = str(run["initial_authorization"]["authorization_id"])
            issuer_ref = str(run["initial_authorization"]["authorization_id"])
            term = 0
        document = {
            "schema_version": "karajan.scheduler-grant.v1",
            "grant_id": parsed["grant_id"],
            "run_id": run_id,
            "project_id": project_id,
            "revision": 1,
            "issuer_ref": issuer_ref,
            "subject_ref": parsed["subject_ref"],
            "role_instance": parsed["role_instance"],
            "parent_grant_id": None if parent is None else parent.grant_id,
            "root_grant_id": root_grant_id,
            "root_authorization_id": root_authorization_id,
            "depth": depth,
            "term": term,
            "valid_from": now,
            "expires_at": parsed["expires_at"],
            "allowed_actions": list(parsed["allowed_actions"]),
            "inputs": list(parsed["inputs"]),
            "scope": list(parsed["scope"]),
            "write_scope": list(parsed["write_scope"]),
            "required_outcomes": list(parsed["required_outcomes"]),
            "role_refs": dict(parsed["role_refs"]),
            "model_refs": list(parsed["model_refs"]),
            "execution_kind_refs": list(parsed["execution_kind_refs"]),
            "tool_refs": list(parsed["tool_refs"]),
            "data_destinations": list(parsed["data_destinations"]),
            "artifact": parsed["artifact"],
            "resource_policy": parsed["resource_policy"].as_document(),
            "delegation": parsed["delegation"].as_document(),
            "revoked_at": None,
            "revoked_reason": None,
            "revoked_by": None,
            "user_authorization_id": root_authorization_id,
            "accepted_under": "user_authorization" if parent is None else "parent_grant",
            "model_calls": 0,
        }
        document["digest"] = grant_digest(document)
        existing = db.execute(
            "SELECT record, digest FROM scheduler_grants "
            "WHERE project_id=? AND run_id=? AND grant_id=? AND revision=1",
            (project_id, run_id, parsed["grant_id"]),
        ).fetchone()
        if existing is not None:
            if existing["digest"] != document["digest"]:
                raise SchedulingError("SCHEDULING_GRANT_ALREADY_EXISTS")
            replayed: dict[str, Any] = json.loads(existing["record"])
            return replayed
        db.execute(
            "INSERT INTO scheduler_grants VALUES (?,?,?,?,?,?,?)",
            (
                project_id,
                run_id,
                parsed["grant_id"],
                1,
                canonical_json(document),
                document["digest"],
                None,
            ),
        )
        db.execute(
            "INSERT INTO scheduler_grant_current VALUES (?,?,?,?)",
            (project_id, run_id, parsed["grant_id"], 1),
        )
        return document

    @staticmethod
    def _require_frozen_definitions(
        run: Mapping[str, Any], parsed: Mapping[str, Any], *, pointer: str
    ) -> None:
        """Refuse a grant naming a role, kind, model or contract the Run did not freeze.

        A Run froze the exact definitions it was authorised under. A management
        grant cannot introduce a *new* role revision, execution kind or contract
        inside that Run: doing so would widen what the frozen deployment permits,
        and the grant would be describing work the authorised definition never
        contained. The permitted surface is read from the Run's own snapshot, so
        a later deployment cannot widen it either.
        """
        definitions = dict(run["definitions"])
        permitted_roles = set(str(value) for value in definitions["role_refs"].values())
        unknown_roles = sorted(
            reference
            for reference in parsed["role_refs"].values()
            if str(reference) not in permitted_roles
        )
        if unknown_roles:
            raise SchedulingError(
                "SCHEDULING_GRANT_ROLE_UNRESOLVED",
                fields={"role_refs": unknown_roles[:16]},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_ROLE_UNRESOLVED",
                        f"{pointer}/id={reference}",
                        "the frozen deployment declares no such role revision",
                    )
                    for reference in unknown_roles[:16]
                ],
            )
        permitted_kinds = set(str(name) for name in definitions["execution_kinds"])
        unknown_kinds = sorted(
            reference
            for reference in parsed["execution_kind_refs"]
            if str(reference) not in permitted_kinds
        )
        if unknown_kinds:
            raise SchedulingError(
                "SCHEDULING_GRANT_KIND_UNRESOLVED",
                fields={"execution_kind_refs": unknown_kinds[:16]},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_KIND_UNRESOLVED",
                        f"{pointer}/id={reference}",
                        "the frozen deployment references no such registered kind",
                    )
                    for reference in unknown_kinds[:16]
                ],
            )
        permitted_contracts = set(str(name) for name in definitions["contracts"])
        artifact = parsed.get("artifact")
        if artifact is not None and "aggregated-report@1" not in permitted_contracts:
            raise SchedulingError("SCHEDULING_GRANT_CONTRACT_UNRESOLVED")
        declared = str(run["source"]["delivery_kind"])
        if artifact is not None and DELIVERY_RANKS.get(str(artifact), 0) > DELIVERY_RANKS.get(
            declared, 0
        ):
            # The Run froze the delivery target its definition was authorised
            # for, so a grant cannot promise a wider one than the frozen
            # deployment can produce.
            raise SchedulingError(
                "SCHEDULING_GRANT_ARTIFACT_INVALID",
                fields={"artifact": str(artifact), "frozen_delivery_kind": declared},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_ARTIFACT_INVALID",
                        f"{pointer}/id=artifact",
                        "the frozen deployment is not authorised for that delivery target",
                    )
                ],
            )

    def _grant_row(
        self, db: sqlite3.Connection, project_id: str, run_id: str, grant_id: str
    ) -> Grant:
        row = db.execute(
            "SELECT g.record FROM scheduler_grants g "
            "JOIN scheduler_grant_current c "
            "ON c.project_id=g.project_id AND c.run_id=g.run_id "
            "AND c.grant_id=g.grant_id AND c.revision=g.revision "
            "WHERE g.project_id=? AND g.run_id=? AND g.grant_id=?",
            (project_id, run_id, grant_id),
        ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_GRANT_NOT_FOUND")
        return self._grant_from(json.loads(row["record"]))

    @staticmethod
    def _grant_from(document: Mapping[str, Any]) -> Grant:
        from .grants import DelegationPolicy, ResourcePolicy

        declared = document.get("digest")
        if declared != grant_digest(document):
            raise SchedulingError("SCHEDULING_GRANT_RECORD_CHANGED")
        policy = dict(document["resource_policy"])
        delegation = dict(document["delegation"])
        return Grant(
            grant_id=str(document["grant_id"]),
            revision=int(document["revision"]),
            run_id=str(document["run_id"]),
            project_id=str(document["project_id"]),
            issuer_ref=str(document["issuer_ref"]),
            subject_ref=str(document["subject_ref"]),
            role_instance=str(document["role_instance"]),
            parent_grant_id=document.get("parent_grant_id"),
            root_grant_id=str(document["root_grant_id"]),
            root_authorization_id=str(document["root_authorization_id"]),
            depth=int(document["depth"]),
            term=int(document["term"]),
            valid_from=float(document["valid_from"]),
            expires_at=(
                None if document.get("expires_at") is None else float(document["expires_at"])
            ),
            allowed_actions=tuple(str(item) for item in document["allowed_actions"]),
            inputs=tuple(str(item) for item in document["inputs"]),
            scope=tuple(str(item) for item in document["scope"]),
            write_scope=tuple(str(item) for item in document["write_scope"]),
            required_outcomes=tuple(str(item) for item in document["required_outcomes"]),
            role_refs={str(key): str(value) for key, value in document["role_refs"].items()},
            model_refs=tuple(str(item) for item in document["model_refs"]),
            execution_kind_refs=tuple(str(item) for item in document["execution_kind_refs"]),
            tool_refs=tuple(str(item) for item in document["tool_refs"]),
            data_destinations=tuple(str(item) for item in document["data_destinations"]),
            artifact=document.get("artifact"),
            resource_policy=ResourcePolicy(
                budget_ref=str(policy["budget_ref"]),
                pool_id=str(policy["pool_id"]),
                cost_per_claim=int(policy["cost_per_claim"]),
                max_active_claims=(
                    None
                    if policy.get("max_active_claims") is None
                    else int(policy["max_active_claims"])
                ),
            ),
            delegation=DelegationPolicy(
                allowed=bool(delegation["allowed"]),
                maximum_depth=int(delegation["maximum_depth"]),
                actions=tuple(str(item) for item in delegation["actions"]),
            ),
            revoked_at=(
                None if document.get("revoked_at") is None else float(document["revoked_at"])
            ),
            revoked_reason=document.get("revoked_reason"),
            digest=str(document["digest"]),
            document=dict(document),
        )

    def revoke_grant(
        self, project_id: str, run_id: str, grant_id: str, *, reason: str
    ) -> dict[str, Any]:
        """Revoke one grant and every grant derived from it, in one transaction.

        Descendants are revoked explicitly rather than being left to a later
        check: revocation propagates through the chain, so a decision taken by a
        sub-grant whose ancestor was revoked cannot be accepted even if the
        sub-grant's own row still looks live.
        """
        grant_id = bounded_identifier(grant_id, "SCHEDULING_GRANT_NOT_FOUND", "grant#/id")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 512:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        with self._owned(project_id) as db:
            now = self.clock()
            rows = db.execute(
                "SELECT grant_id, record FROM scheduler_grants WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchall()
            documents = {str(row["grant_id"]): json.loads(row["record"]) for row in rows}
            if grant_id not in documents:
                raise SchedulingError("SCHEDULING_GRANT_NOT_FOUND")
            affected = {grant_id}
            changed = True
            while changed:
                changed = False
                for name, document in documents.items():
                    if name in affected:
                        continue
                    if document.get("parent_grant_id") in affected:
                        affected.add(name)
                        changed = True
            for name in sorted(affected):
                document = documents[name]
                if document.get("revoked_at") is not None:
                    continue
                document["revoked_at"] = now
                document["revoked_reason"] = reason
                document["revoked_by"] = grant_id
                document["digest"] = grant_digest(document)
                db.execute(
                    "UPDATE scheduler_grants SET record=?, digest=?, revoked_at=? "
                    "WHERE project_id=? AND run_id=? AND grant_id=? AND revision=?",
                    (
                        canonical_json(document),
                        document["digest"],
                        now,
                        project_id,
                        run_id,
                        name,
                        int(document["revision"]),
                    ),
                )
                if name == grant_id:
                    # Every credential issued for a revoked grant is revoked with
                    # it, so a late request under the old token is refused at the
                    # credential boundary as well as at the grant check.
                    self._revoke_grant_credentials(db, project_id, run_id, name, now, reason)
            return {
                "grant_id": grant_id,
                "revoked": sorted(affected),
                "revoked_at": now,
                "reason": reason,
                "descendants_revoked": sorted(affected - {grant_id}),
            }

    def _revoke_grant_credentials(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        grant_id: str,
        now: float,
        reason: str,
    ) -> None:
        rows = db.execute(
            "SELECT credential_id, record FROM scheduling_credentials "
            "WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchall()
        for row in rows:
            document = json.loads(row["record"])
            if document.get("grant_id") != grant_id or document.get("revoked_at") is not None:
                continue
            document["revoked_at"] = now
            document["revoked_reason"] = f"grant-revoked:{reason}"[:512]
            document["digest"] = content_digest(
                {key: value for key, value in document.items() if key != "digest"}
            )
            db.execute(
                "UPDATE scheduling_credentials SET record=? WHERE credential_id=?",
                (canonical_json(document), row["credential_id"]),
            )

    def list_grants(self, project_id: str, run_id: str) -> list[dict[str, Any]]:
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            rows = db.execute(
                "SELECT grant_id, revision, record FROM scheduler_grants "
                "WHERE project_id=? AND run_id=? ORDER BY grant_id, revision",
                (project_id, run_id),
            ).fetchall()
        return [
            {**json.loads(row["record"]), "is_current": True} for row in rows
        ]

    def _require_grant_live(self, grant: Grant, now: float) -> None:
        if not grant.live:
            raise SchedulingError(
                "SCHEDULING_GRANT_REVOKED",
                fields={"grant_id": grant.grant_id},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_REVOKED",
                        "grant#/revoked_at",
                        "this grant was revoked",
                    )
                ],
            )
        if grant_expired(grant, now):
            raise SchedulingError(
                "SCHEDULING_GRANT_EXPIRED",
                fields={"grant_id": grant.grant_id},
                diagnostics=[
                    located("SCHEDULING_GRANT_EXPIRED", "grant#/expires_at", "this grant expired")
                ],
            )

    def _require_chain_live(
        self, db: sqlite3.Connection, project_id: str, run_id: str, grant: Grant
    ) -> None:
        """Refuse a grant whose ancestry is not entirely live, right now.

        Revocation already cascades when it happens; this check is what makes a
        race safe. A decision that reaches the commit boundary while an ancestor
        is being revoked reads the ancestor's current row inside the same
        transaction, so one of the two orderings wins and neither observes a
        half-applied state.
        """
        now = self.clock()
        self._require_grant_live(grant, now)
        seen = {grant.grant_id}
        parent_id = grant.parent_grant_id
        while parent_id is not None:
            if parent_id in seen:
                raise SchedulingError("SCHEDULING_GRANT_CHAIN_INVALID")
            seen.add(parent_id)
            parent = self._grant_row(db, project_id, run_id, parent_id)
            self._require_grant_live(parent, now)
            parent_id = parent.parent_grant_id

    # ------------------------------------------------------------- credentials

    def issue_credential(
        self, project_id: str, run_id: str, payload: object, *, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        """Issue one narrow credential, bound to a run, a grant and a role instance.

        The raw token is returned exactly once, in this command's response. Only
        its sha256 reaches the durable row, so a leaked database yields no usable
        capability, and no receipt anywhere carries the token.

        The issuing command is keyed like every other command in this control
        plane, which is what makes a lost response recoverable: repeating the
        same key and payload reports the *same* credential identity and mints no
        second live capability, while a changed payload under the same key is a
        conflict. A genuinely new credential needs a new key.
        """
        with self._owned(project_id) as db:
            run = self._run_row(db, project_id, run_id)
            request = validate_issue_request(payload, clock=self.clock)
            requested_seconds = request["requested_seconds"]
            subject_digest = content_digest(
                [
                    "scheduling.credential",
                    project_id,
                    run_id,
                    request["kind"],
                    request["grant_id"],
                    request["role_instance"],
                    # The requested lifetime is part of the command, so it is
                    # hashed as the caller stated it. Hashing the clock-derived
                    # ``expires_at`` instead would make the identity depend on
                    # when the command ran rather than on what was asked for.
                    requested_seconds,
                ]
            )
            replay = self._credential_replay(db, "owner", command_key, subject_digest)
            if replay is not None:
                return replay, False
            kind = str(request["kind"])
            grant: Grant | None = None
            if request["grant_id"] is not None:
                grant = self._grant_row(db, project_id, run_id, str(request["grant_id"]))
                self._require_chain_live(db, project_id, run_id, grant)
                if grant.run_id != run_id:
                    raise SchedulingError("SCHEDULING_GRANT_NOT_FOUND")
            if kind == "role_protocol":
                assert grant is not None
                if grant.role_instance != request["role_instance"]:
                    # The credential's subject must be the subject the grant
                    # binds; a credential cannot hand a role instance authority
                    # the grant named for a different one.
                    raise SchedulingError(
                        "SCHEDULING_GRANT_SUBJECT_MISMATCH",
                        diagnostics=[
                            located(
                                "SCHEDULING_GRANT_SUBJECT_MISMATCH",
                                "grant#/role_instance",
                                "the grant binds a different role instance",
                            )
                        ],
                    )
            term = 0 if grant is None else grant.term
            token = new_token()
            credential_id = f"cred-{content_digest([command_key, subject_digest])[:24]}"
            credential = IssuedCredential(
                credential_id=credential_id,
                kind=validate_kind(kind),
                run_id=run_id,
                grant_id=None if request["grant_id"] is None else str(request["grant_id"]),
                role_instance=(
                    None
                    if request["role_instance"] is None
                    else str(request["role_instance"])
                ),
                term=term,
                issued_at=self.clock(),
                expires_at=(
                    None
                    if request["expires_at"] is None
                    else float(str(request["expires_at"]))
                ),
                digest=token_digest(token),
            )
            document = {
                **credential.as_document(),
                "project_id": project_id,
                "issued_by": "owner",
                "issue_command": command_key,
                "revoked_at": None,
                "revoked_reason": None,
                "delegated_from": None,
                "subject_ref": None if grant is None else grant.subject_ref,
                "digest": "",
            }
            document["digest"] = content_digest(
                {key: value for key, value in document.items() if key != "digest"}
            )
            run_identity = str(run["run_id"])
            db.execute(
                "INSERT INTO scheduling_credentials VALUES (?,?,?,?,?)",
                (credential.credential_id, project_id, run_identity, credential.digest,
                 canonical_json(document)),
            )
            # The durable receipt records the identity, never the token: it is
            # read back by ordinary list and receipt paths, and a stored token
            # would be a credential at rest.
            receipt = {
                "credential_id": credential.credential_id,
                "kind": credential.kind,
                "run_id": run_id,
                "grant_id": credential.grant_id,
                "role_instance": credential.role_instance,
                "term": credential.term,
                "token_returned_once": True,
                "stores_raw_token": False,
                "routes": sorted(ROUTES_BY_KIND[credential.kind]),
            }
            db.execute(
                "INSERT INTO credential_issuances VALUES (?,?,?,?,?)",
                ("owner", command_key, subject_digest, credential.credential_id,
                 canonical_json(receipt)),
            )
            return {
                "credential": document,
                "token": token,
                "token_returned_once": True,
                "stores_raw_token": False,
                "replayed": False,
            }, True

    def _credential_replay(
        self,
        db: sqlite3.Connection,
        principal: str,
        command_key: str,
        digest: str,
    ) -> dict[str, Any] | None:
        """Resolve a repeated issuance without minting a second live credential."""
        row = db.execute(
            "SELECT digest, record FROM credential_issuances "
            "WHERE principal=? AND key=?",
            (principal, command_key),
        ).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise SchedulingError("SCHEDULING_IDEMPOTENCY_CONFLICT")
        receipt = json.loads(row["record"])
        # The credential row is read on the connection this transaction already
        # holds. Opening a second transaction here would re-enter the same
        # database's write lock from the same process, so the lookup is done
        # inline instead.
        credential = db.execute(
            "SELECT record FROM scheduling_credentials WHERE credential_id=?",
            (str(receipt["credential_id"]),),
        ).fetchone()
        if credential is None:
            raise SchedulingError("SCHEDULING_CREDENTIAL_INVALID")
        # The token is deliberately absent: a lost response cannot re-reveal a
        # secret, so the operator issues a *new* credential under a new key
        # instead of the service answering with a stored token.
        return {
            "credential": json.loads(credential["record"]),
            "replayed": True,
            "token_returned_once": True,
            "stores_raw_token": False,
            "note": (
                "the credential identity is unchanged; the raw token is only ever "
                "returned by the response that issued it"
            ),
        }

    def resolve_credential(self, token: str) -> Principal:
        """Resolve one presented token into the trusted identity it stands for.

        Nothing about the request is consulted: the kind, the run, the grant, the
        role instance and the term are read from the credential row this token
        hashes to. A token that names no row, a revoked credential and an expired
        one are all refused here, before any route logic runs.
        """
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise SchedulingError("SCHEDULING_CREDENTIAL_INVALID")
        digest = token_digest(token)
        with self.projects._transaction() as db:
            row = db.execute(
                "SELECT record FROM scheduling_credentials WHERE digest=?", (digest,)
            ).fetchone()
            if row is None:
                raise SchedulingError("SCHEDULING_CREDENTIAL_INVALID")
            record = self._credential_from(json.loads(row["record"]))
        return Principal(
            credential_id=record.credential_id,
            kind=record.kind,
            runs=frozenset({record.run_id}),
            grant_id=record.grant_id,
            role_instance=record.role_instance,
            term=record.term,
            routes=ROUTES_BY_KIND[record.kind],
            token=token,
        )

    def credential_record(self, credential_id: str) -> CredentialRecord:
        with self.projects._transaction() as db:
            row = db.execute(
                "SELECT record FROM scheduling_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_CREDENTIAL_INVALID")
        return self._credential_from(json.loads(row["record"]))

    def _credential_from(self, document: Mapping[str, Any]) -> CredentialRecord:
        declared = document.get("digest")
        if declared != content_digest(
            {key: value for key, value in document.items() if key != "digest"}
        ):
            raise SchedulingError("SCHEDULING_CREDENTIAL_RECORD_CHANGED")
        issued = parse_issued(dict(document))
        now = self.clock()
        if document.get("revoked_at") is not None:
            raise SchedulingError(
                "SCHEDULING_CREDENTIAL_REVOKED",
                diagnostics=[
                    located("SCHEDULING_CREDENTIAL_REVOKED", "grant#/credential", "revoked")
                ],
            )
        if issued.expires_at is not None and now >= issued.expires_at:
            raise SchedulingError(
                "SCHEDULING_CREDENTIAL_EXPIRED",
                diagnostics=[
                    located("SCHEDULING_CREDENTIAL_EXPIRED", "grant#/credential", "expired")
                ],
            )
        return CredentialRecord(
            credential_id=issued.credential_id,
            kind=issued.kind,
            run_id=issued.run_id,
            grant_id=issued.grant_id,
            role_instance=issued.role_instance,
            term=issued.term,
            digest=issued.digest,
            issued_at=issued.issued_at,
            expires_at=issued.expires_at,
            revoked_at=(
                None
                if document.get("revoked_at") is None
                else float(document["revoked_at"])
            ),
            revoked_reason=document.get("revoked_reason"),
            delegated_from=document.get("delegated_from"),
            document=dict(document),
        )

    def list_credentials(self, project_id: str, run_id: str) -> list[dict[str, Any]]:
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            rows = db.execute(
                "SELECT record FROM scheduling_credentials WHERE project_id=? AND run_id=? "
                "ORDER BY credential_id",
                (project_id, run_id),
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    # ------------------------------------------------------- decision submission

    def submit_decision(
        self,
        project_id: str,
        run_id: str,
        grant_id: str,
        payload: object,
        *,
        principal: Principal,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Accept one scheduling decision, or refuse it without changing anything.

        The whole decision is compiled before the first write: the actions are
        applied to an in-memory view, the resulting graph is checked for unknown
        references, dependency cycles and lost obligations, and only then does a
        single short transaction write the new revision, the receipt and the
        complete queue. A batch whose later operation is invalid therefore leaves
        the durable graph exactly as it was.
        """
        principal.require_route("decision")
        principal.require_run(run_id)
        principal.require_grant(grant_id)
        parsed = validate_decision_payload(payload)
        if str(parsed["grant_id"]) != grant_id:
            raise SchedulingError("SCHEDULING_GRANT_MISMATCH")
        digest = content_digest(
            ["scheduling.decision", project_id, run_id, grant_id, parsed]
        )
        if len(parsed["actions"]) > MAXIMUM_ACTIONS_PER_DECISION:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        try:
            return self._decision_transaction(
                project_id=project_id,
                run_id=run_id,
                grant_id=grant_id,
                command_key=command_key,
                digest=digest,
                parsed=parsed,
                principal=principal,
            )
        except _RejectedDecision:
            raise
        except SchedulingError as error:
            if error.code in self.RECORDABLE_REJECTIONS:
                recorded = self._record_rejection(
                    project_id=project_id,
                    run_id=run_id,
                    command_key=command_key,
                    digest=digest,
                    parsed=parsed,
                    principal=principal,
                    code=error.code,
                    current_revision=error.current_revision,
                )
                if recorded is not None:
                    # The response to the first refusal carries the same fields
                    # its stored record does, so a client that lost this response
                    # and a client that re-reads the record see one document.
                    raise _RejectedDecision(recorded) from None
            raise

    def _decision_transaction(
        self,
        *,
        project_id: str,
        run_id: str,
        grant_id: str,
        command_key: str,
        digest: str,
        parsed: Mapping[str, Any],
        principal: Principal,
    ) -> tuple[dict[str, Any], bool]:
        """One decision's durable work, in a single transaction that can roll back.

        Keeping this separate from the rejection recording is what lets a refused
        decision leave *evidence* without leaving a partial graph: the
        transaction below rolls back around the refusal, and the receipt is
        written afterwards by the caller.
        """
        with self._owned(project_id) as db:
            replay = self._decision_replay(db, project_id, run_id, command_key, digest)
            if replay is not None:
                return replay, False
            run = self._run_row(db, project_id, run_id)
            grant = self._grant_row(db, project_id, run_id, grant_id)
            self._require_chain_live(db, project_id, run_id, grant)
            if principal.grant_id is not None and principal.grant_id != grant.grant_id:
                raise SchedulingError("SCHEDULING_GRANT_NOT_FOUND")
            if grant.role_instance != (principal.role_instance or grant.role_instance):
                raise SchedulingError(
                    "SCHEDULING_CREDENTIAL_SUBJECT_MISMATCH",
                    diagnostics=[
                        located(
                            "SCHEDULING_CREDENTIAL_SUBJECT_MISMATCH",
                            "grant#/role_instance",
                            "this credential does not act for that role instance",
                        )
                    ],
                )
            if parsed["term"] != principal.term:
                # The credential's term is the term the service issued it for;
                # a decision claiming a different one is refused rather than
                # being accepted as an unrecorded extension of authority.
                raise SchedulingError(
                    "SCHEDULING_GRANT_TERM_STALE",
                    fields={"current_term": principal.term},
                    diagnostics=[
                        located(
                            "SCHEDULING_GRANT_TERM_STALE",
                            "grant#/term",
                            "the credential was issued for the grant's current term",
                        )
                    ],
                )
            current = db.execute(
                "SELECT revision FROM task_graph_current WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchone()
            current_revision = int(current["revision"]) if current is not None else 0
            if parsed["expected_graph_revision"] != current_revision:
                raise SchedulingError(
                    "SCHEDULING_GRAPH_REVISION_CONFLICT",
                    current_revision=current_revision,
                    diagnostics=[
                        located(
                            "SCHEDULING_GRAPH_REVISION_CONFLICT",
                            "scheduling#/expected_graph_revision",
                            "the decision was made against a different graph revision",
                        )
                    ],
                )
            if parsed["graph_digest"] is not None and current_revision:
                head = db.execute(
                    "SELECT digest FROM task_graph_revisions "
                    "WHERE project_id=? AND run_id=? AND revision=?",
                    (project_id, run_id, current_revision),
                ).fetchone()
                if head is None or str(head["digest"]) != parsed["graph_digest"]:
                    raise SchedulingError(
                        "SCHEDULING_GRAPH_REVISION_CONFLICT", current_revision=current_revision
                    )
            if parsed["inputs_digest"] != str(run["inputs_digest"]):
                raise SchedulingError(
                    "SCHEDULING_INPUT_DIGEST_MISMATCH",
                    diagnostics=[
                        located(
                            "SCHEDULING_INPUT_DIGEST_MISMATCH",
                            "scheduling#/inputs_digest",
                            "the decision was made against different Run inputs",
                        )
                    ],
                )
            tasks = self._tasks(db, project_id, run_id)
            expansions = self._expansions(db, project_id, run_id)
            # What a binding may consume: the declared steps of the frozen
            # deployment, plus every task the run already has.
            producers = frozenset(
                str(step.get("step_id"))
                for step in (run["definitions"].get("steps") or ())
                if step.get("step_id")
            ) | frozenset(tasks)
            definitions = _Definitions(
                role_refs=dict(run["definitions"]["role_refs"]),
                execution_kinds=dict(run["definitions"]["execution_kinds"]),
                contracts=dict(run["definitions"]["contracts"]),
            )
            plan = self._plan_decision(
                run=run,
                grant=grant,
                parsed=parsed,
                tasks=tasks,
                expansions=expansions,
                definitions=definitions,
                producers=producers,
                revision=current_revision + 1,
                now=self.clock(),
            )
            return self._commit_decision(
                db=db,
                project_id=project_id,
                run_id=run_id,
                principal=principal,
                command_key=command_key,
                digest=digest,
                parsed=parsed,
                grant=grant,
                plan=plan,
                current_revision=current_revision,
            ), True

    #: Refusals that are recorded as durable evidence. A malformed request, a
    #: missing run and an unavailable store are not decisions the engine
    #: considered and refused on their merits, so they are not recorded here.
    RECORDABLE_REJECTIONS = (
        CONFLICT_CODES
        | frozenset(
            {
                "SCHEDULING_DEPENDENCY_CYCLE",
                "SCHEDULING_OBLIGATION_UNCOVERED",
                "SCHEDULING_EXPANSION_OBLIGATIONS_UNCOVERED",
                "SCHEDULING_EXPANSION_MEMBER_STALE",
                "SCHEDULING_EXPANSION_MEMBERS_MISMATCH",
                "SCHEDULING_EXPANSION_MEMBER_POLICY_EXCEEDED",
                "SCHEDULING_INPUT_DIGEST_MISMATCH",
                "SCHEDULING_GRANT_ACTION_NOT_PERMITTED",
                "SCHEDULING_GRANT_INPUT_NOT_PERMITTED",
                "SCHEDULING_GRANT_ROLE_NOT_PERMITTED",
                "SCHEDULING_GRANT_KIND_NOT_PERMITTED",
                "SCHEDULING_GRANT_MODEL_NOT_PERMITTED",
                "SCHEDULING_GRANT_TOOL_NOT_PERMITTED",
                "SCHEDULING_GRANT_OUTCOME_NOT_PERMITTED",
                "SCHEDULING_GRANT_SCOPE_EXCEEDED",
                "SCHEDULING_TASK_FROZEN",
                "SCHEDULING_TASK_ALREADY_EXISTS",
                "SCHEDULING_TASK_NOT_FOUND",
                "SCHEDULING_EXPANSION_NOT_PERMITTED",
                "SCHEDULING_EXPANSION_NOT_FOUND",
                "SCHEDULING_CREDENTIAL_SUBJECT_MISMATCH",
                "SCHEDULING_GRANT_MISMATCH",
                "SCHEDULING_INPUT_REFERENCE_UNRESOLVED",
            }
        )
    )

    def _record_rejection(
        self,
        *,
        project_id: str,
        run_id: str,
        command_key: str,
        digest: str,
        parsed: Mapping[str, Any],
        principal: Principal,
        code: str,
        current_revision: int | None,
    ) -> dict[str, Any] | None:
        """Persist one refused decision, in its own committed transaction.

        The receipt names the immutable command identity - its key, its payload
        digest and its decision identity - with the reason and the revision that
        was current when it was refused, so a client that lost a race can read
        back the conflict it received instead of only remembering a 409.

        The write happens *after* the refusal, in a separate short transaction:
        the decision's own transaction has already rolled back, and a receipt
        written inside it would be rolled back with the graph it declined to
        change. Nothing here writes a graph, a task or a queue row.

        Raising is deliberately impossible: a failure to record a refusal must
        never replace the caller's own reason code with a second one.
        """
        outcome = "conflict" if code.endswith("CONFLICT") else "rejected"
        try:
            with self._owned(project_id) as db:
                existing = db.execute(
                    "SELECT digest, record FROM scheduling_decision_rejections "
                    "WHERE project_id=? AND run_id=? AND key=?",
                    (project_id, run_id, command_key),
                ).fetchone()
                if existing is not None and existing["digest"] == digest:
                    # This exact command already owns a refusal. That record is
                    # the durable evidence and is returned unchanged.
                    earlier: dict[str, Any] = json.loads(existing["record"])
                    return earlier
                if existing is not None:
                    # The key is held by a *different* command. That is the
                    # conflict the caller was just told about, and it must not be
                    # replaced by the original command's refusal.
                    return None
                record = {
                    "schema_version": DECISION_SCHEMA_VERSION,
                    "outcome": outcome,
                    "reason_code": code,
                    "decision_id": parsed["decision_id"],
                    "run_id": run_id,
                    "project_id": project_id,
                    "command_key": command_key,
                    "command_digest": digest,
                    "grant_id": parsed["grant_id"],
                    "term": parsed["term"],
                    "credential_id": principal.credential_id,
                    "credential_kind": principal.kind,
                    "expected_graph_revision": parsed["expected_graph_revision"],
                    "current_graph_revision": current_revision,
                    "current_revision": current_revision,
                    "inputs_digest": parsed["inputs_digest"],
                    "recorded_at": self.clock(),
                    "accepted": False,
                    "graph_mutated": False,
                    "model_calls": 0,
                }
                record["digest"] = content_digest(
                    {key: value for key, value in record.items() if key != "digest"}
                )
                # Write once. The first refusal of a command key is the durable
                # evidence of what that command was and why it was refused, so a
                # later attempt of the same key neither replaces its reason and
                # timestamp nor rewrites the digest the command was made with.
                db.execute(
                    "INSERT INTO scheduling_decision_rejections VALUES (?,?,?,?,?) "
                    "ON CONFLICT(project_id, run_id, key) DO NOTHING",
                    (project_id, run_id, command_key, digest, canonical_json(record)),
                )
                return record
        except (SchedulingError, sqlite3.Error, KeyError):
            return None

    def rejections(self, project_id: str, run_id: str) -> list[dict[str, Any]]:
        """Every refused decision of this run, read back by its command key."""
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            rows = db.execute(
                "SELECT record FROM scheduling_decision_rejections "
                "WHERE project_id=? AND run_id=? ORDER BY key",
                (project_id, run_id),
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    def decision_rejection(
        self, project_id: str, run_id: str, command_key: str
    ) -> dict[str, Any]:
        """One refused decision, answered from its durable receipt."""
        with self._read(project_id) as db:
            row = db.execute(
                "SELECT record FROM scheduling_decision_rejections "
                "WHERE project_id=? AND run_id=? AND key=?",
                (project_id, run_id, command_key),
            ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_DECISION_NOT_FOUND")
        record: dict[str, Any] = json.loads(row["record"])
        return record

    def _decision_replay(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        command_key: str,
        digest: str,
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT digest, record FROM scheduling_decisions "
            "WHERE project_id=? AND run_id=? AND key=?",
            (project_id, run_id, command_key),
        ).fetchone()
        if row is None:
            # A command that was already refused is answered with its own
            # refusal, so a replay is coherent rather than being re-adjudicated
            # against a graph that has moved since.
            refused = db.execute(
                "SELECT digest, record FROM scheduling_decision_rejections "
                "WHERE project_id=? AND run_id=? AND key=?",
                (project_id, run_id, command_key),
            ).fetchone()
            if refused is None:
                return None
            if refused["digest"] != digest:
                # The key is taken by a different payload: the original command
                # owns this key, and nothing overwrites what it recorded.
                raise SchedulingError("SCHEDULING_IDEMPOTENCY_CONFLICT")
            original: dict[str, Any] = json.loads(refused["record"])
            raise _RejectedDecision(original)
        if row["digest"] != digest:
            raise SchedulingError("SCHEDULING_IDEMPOTENCY_CONFLICT")
        receipt: dict[str, Any] = json.loads(row["record"])
        return receipt

    # -------------------------------------------------------------- planning

    def _plan_decision(
        self,
        *,
        run: Mapping[str, Any],
        grant: Grant,
        parsed: Mapping[str, Any],
        tasks: dict[str, Any],
        expansions: dict[str, dict[str, Any]],
        definitions: _Definitions,
        producers: Collection[str],
        revision: int,
        now: float,
    ) -> dict[str, Any]:
        """Compile one decision into the revision it would commit.

        Every check the acceptance contract requires happens here, on copies, so
        a failure leaves the durable state untouched and a success is a single
        write. Nothing in this method reads the clock a second time or consults
        the request for authority.
        """
        working: dict[str, dict[str, Any]] = {
            name: dict(task.document) for name, task in tasks.items()
        }
        working_expansions = {
            name: dict(expansion) for name, expansion in expansions.items()
        }
        allowed = set(grant.allowed_actions)
        run_id = str(run["run_id"])
        nodes_added: list[str] = []
        nodes_superseded: list[str] = []
        # Every version this decision produced, so the commit can archive all of
        # them rather than only the survivors.
        versions: dict[str, list[dict[str, Any]]] = {}
        nodes_retired: list[str] = []
        sealed: dict[str, list[str]] = {}
        for index, action in enumerate(parsed["actions"]):
            pointer = f"scheduling#/actions/{index}"
            name = str(action["action"])
            if name not in allowed:
                raise SchedulingError(
                    "SCHEDULING_GRANT_ACTION_NOT_PERMITTED",
                    fields={"action": name, "grant_id": grant.grant_id},
                    diagnostics=[
                        located(
                            "SCHEDULING_GRANT_ACTION_NOT_PERMITTED",
                            pointer,
                            "the grant does not authorise that action",
                        )
                    ],
                )
            if name == "expand_graph":
                expansion_id = bounded_identifier(
                    action.get("expansion_id"),
                    "SCHEDULING_ACTION_INVALID",
                    f"{pointer}/expansion_id",
                )
                expansion = working_expansions.get(expansion_id)
                if expansion is None:
                    raise SchedulingError(
                        "SCHEDULING_EXPANSION_NOT_FOUND",
                        diagnostics=[
                            located(
                                "SCHEDULING_EXPANSION_NOT_FOUND",
                                f"{pointer}/expansion_id",
                                "no such expansion set in this run",
                            )
                        ],
                    )
                self._require_expansion_open(expansion, pointer)
                if expansion["grant_id"] != grant.grant_id:
                    raise SchedulingError(
                        "SCHEDULING_EXPANSION_NOT_PERMITTED",
                        diagnostics=[
                            located(
                                "SCHEDULING_EXPANSION_NOT_PERMITTED",
                                f"{pointer}/expansion_id",
                                "the granted authority does not own that expansion",
                            )
                        ],
                    )
                declared = action.get("tasks")
                if not isinstance(declared, list) or not declared:
                    raise SchedulingError(
                        "SCHEDULING_ACTION_INVALID",
                        diagnostics=[
                            located(
                                "SCHEDULING_ACTION_INVALID",
                                f"{pointer}/tasks",
                                "an expansion batch carries at least one member",
                            )
                        ],
                    )
                members = dict(expansion["members"])
                for item_index, item in enumerate(declared):
                    spec = validate_task_spec(
                        item,
                        grant=grant,
                        pointer=f"{pointer}/tasks/{item_index}",
                        definitions=definitions,
                        known_tasks={name: tasks[name] for name in tasks},
                        producers=producers,
                    )
                    item_key = spec["item_key"]
                    if item_key is None:
                        raise SchedulingError(
                            "SCHEDULING_TASK_INVALID",
                            diagnostics=[
                                located(
                                    "SCHEDULING_TASK_INVALID",
                                    f"{pointer}/tasks/{item_index}/item_key",
                                    "a member states the item key it is derived from",
                                )
                            ],
                        )
                    task_id = task_identity(str(parsed["decision_id"]), str(item_key))
                    if task_id in working:
                        raise SchedulingError(
                            "SCHEDULING_TASK_ALREADY_EXISTS",
                            fields={"task_id": task_id},
                            diagnostics=[
                                located(
                                    "SCHEDULING_TASK_ALREADY_EXISTS",
                                    f"{pointer}/tasks/{item_index}/item_key",
                                    "that item key already exists in this run",
                                )
                            ],
                        )
                    if expansion["member_policy"] is not None and len(members) >= int(
                        expansion["member_policy"]
                    ):
                        raise SchedulingError(
                            "SCHEDULING_EXPANSION_MEMBER_POLICY_EXCEEDED",
                            fields={"member_policy": expansion["member_policy"]},
                            diagnostics=[
                                located(
                                    "SCHEDULING_EXPANSION_MEMBER_POLICY_EXCEEDED",
                                    f"{pointer}/tasks",
                                    "the workflow author declared a smaller member set",
                                )
                            ],
                        )
                    supersedes = spec.get("supersedes")
                    if supersedes is not None:
                        previous = working.get(str(supersedes))
                        if previous is None:
                            raise SchedulingError("SCHEDULING_TASK_NOT_FOUND")
                        if previous["state"] in {"superseded", "cancelled"}:
                            raise SchedulingError(
                                "SCHEDULING_TASK_FROZEN",
                                fields={"task_id": str(supersedes)},
                                diagnostics=[
                                    located(
                                        "SCHEDULING_TASK_FROZEN",
                                        f"{pointer}/tasks/{item_index}/supersedes",
                                        "that task is already retired",
                                    )
                                ],
                            )
                        # The replacement must carry every obligation the task it
                        # replaces still owed, so a successor cannot quietly
                        # narrow what the run promised.
                        owed = set(str(item) for item in previous["required_outcomes"])
                        carried = set(str(item) for item in spec["required_outcomes"])
                        uncovered = sorted(owed - carried)
                        if uncovered:
                            raise SchedulingError(
                                "SCHEDULING_OBLIGATION_UNCOVERED",
                                fields={
                                    "task_id": str(supersedes),
                                    "uncovered": uncovered[:16],
                                },
                                diagnostics=[
                                    located(
                                        "SCHEDULING_OBLIGATION_UNCOVERED",
                                        f"{pointer}/tasks/{item_index}/required_outcomes",
                                        "a replacement carries every obligation it replaces",
                                    )
                                ],
                            )
                        previous["state"] = "superseded"
                        previous["superseded_by"] = task_id
                        history = list(previous.get("history") or [])
                        history.append(
                            {
                                "state": "superseded",
                                "at": now,
                                "by": str(parsed["decision_id"]),
                                "graph_revision": revision,
                                "superseded_by": task_id,
                            }
                        )
                        previous["history"] = history
                        self._archive_version(previous, versions)
                        working[str(supersedes)] = reseal_task(previous)
                        nodes_superseded.append(str(supersedes))
                    document = task_document(
                        spec=spec,
                        task_id=task_id,
                        run_id=run_id,
                        graph_revision=revision,
                        origin="decision",
                        decision_id=str(parsed["decision_id"]),
                        role_ref=spec["role_ref"],
                        parent_task_id=expansion.get("parent_task_id"),
                        supersedes=supersedes,
                    )
                    document["grant_id"] = grant.grant_id
                    document["required_outcomes"] = list(spec["required_outcomes"])
                    document["history"][0]["at"] = now
                    document = reseal_task(document)
                    working[task_id] = document
                    self._archive_version(document, versions)
                    members[task_id] = {
                        "task_id": task_id,
                        "revision": 1,
                        "digest": document["digest"],
                        "item_key": str(item_key),
                        "required_outcomes": list(spec["required_outcomes"]),
                        "required": bool(spec["required"]),
                    }
                    nodes_added.append(task_id)
                expansion["members"] = members
                expansion["batches"] = int(expansion["batches"]) + 1
                working_expansions[expansion_id] = reseal_expansion(expansion)
                continue
            if name == "seal":
                expansion_id = bounded_identifier(
                    action.get("expansion_id"),
                    "SCHEDULING_ACTION_INVALID",
                    f"{pointer}/expansion_id",
                )
                expansion = working_expansions.get(expansion_id)
                if expansion is None:
                    raise SchedulingError("SCHEDULING_EXPANSION_NOT_FOUND")
                self._require_expansion_open(expansion, pointer)
                if expansion["grant_id"] != grant.grant_id:
                    # Possessing the ``seal`` action is not authority over *this*
                    # set. A seal declares that discovery under a particular
                    # grant is complete and fixes that set's members, so only the
                    # grant that owns the set may seal it: a different grant --
                    # even one whose scope overlaps -- would otherwise be able to
                    # close work it never opened, and freeze another role's
                    # incomplete member set as if it were final.
                    raise SchedulingError(
                        "SCHEDULING_EXPANSION_NOT_PERMITTED",
                        fields={
                            "expansion_id": expansion_id,
                            "owning_grant_id": expansion["grant_id"],
                        },
                        diagnostics=[
                            located(
                                "SCHEDULING_EXPANSION_NOT_PERMITTED",
                                f"{pointer}/expansion_id",
                                "only the grant that opened this set may seal it",
                            )
                        ],
                    )
                declared_members = sorted(
                    bounded_identifier(
                        item, "SCHEDULING_ACTION_INVALID", f"{pointer}/members"
                    )
                    for item in self._text_list(
                        action.get("members"), "SCHEDULING_ACTION_INVALID", f"{pointer}/members"
                    )
                )
                known = set(expansion["members"])
                if declared_members != sorted(known):
                    raise SchedulingError(
                        "SCHEDULING_EXPANSION_MEMBERS_MISMATCH",
                        fields={
                            "declared": declared_members[:32],
                            "actual": sorted(known)[:32],
                        },
                        diagnostics=[
                            located(
                                "SCHEDULING_EXPANSION_MEMBERS_MISMATCH",
                                f"{pointer}/members",
                                "a seal names exactly the members actually present",
                            )
                        ],
                    )
                obligations = sorted(
                    bounded_identifier(
                        item, "SCHEDULING_ACTION_INVALID", f"{pointer}/obligations"
                    )
                    for item in self._text_list(
                        action.get("obligations"),
                        "SCHEDULING_ACTION_INVALID",
                        f"{pointer}/obligations",
                    )
                )
                covered: set[str] = set()
                stale: list[str] = []
                for member in expansion["members"].values():
                    covered.update(str(item) for item in member["required_outcomes"])
                    current = working.get(str(member["task_id"]))
                    if current is None or str(current["digest"]) != str(member["digest"]):
                        stale.append(str(member["task_id"]))
                if stale:
                    # The set names a member version that is not the task that
                    # exists now, so sealing it would freeze an identity the run
                    # has already moved past.
                    raise SchedulingError(
                        "SCHEDULING_EXPANSION_MEMBER_STALE",
                        fields={"members": sorted(stale)[:32]},
                        diagnostics=[
                            located(
                                "SCHEDULING_EXPANSION_MEMBER_STALE",
                                f"{pointer}/members",
                                "a member names a version that is no longer current",
                            )
                        ],
                    )
                missing = sorted(set(expansion["required_outcomes"]) - covered)
                if missing or set(obligations) != set(expansion["required_outcomes"]):
                    raise SchedulingError(
                        "SCHEDULING_EXPANSION_OBLIGATIONS_UNCOVERED",
                        fields={
                            "uncovered": missing[:32],
                            "declared": obligations[:32],
                        },
                        diagnostics=[
                            located(
                                "SCHEDULING_EXPANSION_OBLIGATIONS_UNCOVERED",
                                f"{pointer}/obligations",
                                "every obligation of the set must be carried by a member",
                            )
                        ],
                    )
                expansion["state"] = "sealed"
                expansion["sealed_at"] = now
                expansion["sealed_by_decision"] = str(parsed["decision_id"])
                expansion["sealed_revision"] = revision
                expansion["coverage"] = {
                    member["task_id"]: list(member["required_outcomes"])
                    for member in expansion["members"].values()
                }
                working_expansions[expansion_id] = reseal_expansion(expansion)
                sealed[expansion_id] = sorted(known)
                continue
            # The remaining actions target one existing task.
            task_id = bounded_identifier(
                action.get("task_id"), "SCHEDULING_TASK_NOT_FOUND", f"{pointer}/task_id"
            )
            existing: dict[str, Any] | None = working.get(task_id)
            if existing is None:
                raise SchedulingError(
                    "SCHEDULING_TASK_NOT_FOUND",
                    diagnostics=[
                        located("SCHEDULING_TASK_NOT_FOUND", f"{pointer}/task_id", "no such task")
                    ],
                )
            if existing["state"] in {"superseded", "cancelled"}:
                raise SchedulingError(
                    "SCHEDULING_TASK_FROZEN",
                    fields={"task_id": task_id, "state": existing["state"]},
                    diagnostics=[
                        located(
                            "SCHEDULING_TASK_FROZEN",
                            f"{pointer}/task_id",
                            "a retired task is not edited; a new task carries the work",
                        )
                    ],
                )
            if name == "bind_role":
                alias = bounded_identifier(
                    action.get("role_alias"), "SCHEDULING_ACTION_INVALID", f"{pointer}/role_alias"
                )
                if alias not in grant.role_refs:
                    raise SchedulingError(
                        "SCHEDULING_GRANT_ROLE_NOT_PERMITTED", fields={"role_alias": alias}
                    )
                if existing["state"] == "claimed":
                    # A claimed task is frozen: its role binding is what the
                    # execution consumer was handed.
                    raise SchedulingError(
                        "SCHEDULING_TASK_FROZEN",
                        fields={"task_id": task_id},
                        diagnostics=[
                            located(
                                "SCHEDULING_TASK_FROZEN",
                                f"{pointer}/role_alias",
                                "a claimed task's binding is frozen",
                            )
                        ],
                    )
                existing["role_alias"] = alias
                existing["role_ref"] = grant.role_refs[alias]
            elif name == "set_dependencies":
                dependencies = sorted(
                    bounded_identifier(
                        item, "SCHEDULING_ACTION_INVALID", f"{pointer}/depends_on"
                    )
                    for item in self._text_list(
                        action.get("depends_on"),
                        "SCHEDULING_ACTION_INVALID",
                        f"{pointer}/depends_on",
                    )
                )
                unknown = sorted(name for name in dependencies if name not in working)
                if unknown:
                    raise SchedulingError(
                        "SCHEDULING_TASK_NOT_FOUND",
                        fields={"depends_on": unknown[:16]},
                        diagnostics=[
                            located(
                                "SCHEDULING_TASK_NOT_FOUND",
                                f"{pointer}/depends_on",
                                "a dependency names a task in this run",
                            )
                        ],
                    )
                if task_id in dependencies:
                    raise SchedulingError("SCHEDULING_TASK_SELF_DEPENDENCY")
                if existing["state"] == "claimed":
                    raise SchedulingError(
                        "SCHEDULING_TASK_FROZEN",
                        fields={"task_id": task_id},
                        diagnostics=[
                            located(
                                "SCHEDULING_TASK_FROZEN",
                                f"{pointer}/depends_on",
                                "a claimed task's dependencies are frozen",
                            )
                        ],
                    )
                # An edge derived from an input binding is not the author's to
                # remove: the consumer still reads that producer's output, so an
                # edit that dropped the edge would let it run before the value it
                # consumes exists. Declared edges are replaced; derived ones are
                # always retained.
                derived = [str(item) for item in existing.get("inferred_depends_on") or ()]
                existing["declared_depends_on"] = list(dependencies)
                existing["depends_on"] = sorted(set(dependencies) | set(derived))
            elif name == "set_priority":
                existing["priority"] = non_negative_count(
                    action.get("priority"),
                    "SCHEDULING_ACTION_INVALID",
                    f"{pointer}/priority",
                    maximum=1000,
                )
            elif name == "request_dispatch":
                dispatch = action.get("dispatch")
                if dispatch not in {"dispatched", "held"}:
                    raise SchedulingError(
                        "SCHEDULING_ACTION_INVALID",
                        diagnostics=[
                            located(
                                "SCHEDULING_ACTION_INVALID",
                                f"{pointer}/dispatch",
                                "'dispatched' or 'held' is required",
                            )
                        ],
                    )
                existing["dispatch"] = dispatch
            existing["revision"] = int(existing.get("revision", 1)) + 1
            working[task_id] = reseal_task(existing)
            self._archive_version(working[task_id], versions)
            self._refresh_member(working_expansions, working[task_id])
        # The retired nodes and their replacements are settled after every action,
        # so an invalid batch cannot leave a half-retired node behind.
        effective = {
            name: read_task(document)
            for name, document in working.items()
            if document["state"] not in {"superseded", "cancelled"}
        }
        cycle = dependency_cycle(effective)
        if cycle is not None:
            raise SchedulingError(
                "SCHEDULING_DEPENDENCY_CYCLE",
                fields={"cycle": cycle[:16]},
                diagnostics=[
                    located(
                        "SCHEDULING_DEPENDENCY_CYCLE",
                        "scheduling#/actions",
                        "the resulting graph would contain a dependency cycle",
                    )
                ],
            )
        all_tasks = {name: read_task(document) for name, document in working.items()}
        missing = uncovered_obligations(effective, all_tasks)
        if missing:
            raise SchedulingError(
                "SCHEDULING_OBLIGATION_UNCOVERED",
                fields={"uncovered": missing[:32]},
                diagnostics=[
                    located(
                        "SCHEDULING_OBLIGATION_UNCOVERED",
                        "scheduling#/actions",
                        "no live task still carries a promised outcome",
                    )
                ],
            )
        # The Run's own outcomes are reported as still outstanding rather than
        # refused: a decision that expands part of the work is not required to
        # deliver the whole run in one batch, and closing the Run stays an
        # explicit, separately decided act.
        delivered: set[str] = set()
        for task in effective.values():
            if task.live:
                delivered.update(task.obligations)
        outstanding = sorted(
            str(item) for item in run["required_outcomes"] if str(item) not in delivered
        )
        return {
            "working": working,
            "expansions": working_expansions,
            "effective": effective,
            "nodes_added": nodes_added,
            "nodes_superseded": nodes_superseded,
            "nodes_retired": nodes_retired,
            "outstanding_outcomes": outstanding,
            "versions": versions,
            "sealed": sealed,
            "revision": revision,
        }

    @staticmethod
    def _archive_version(
        document: Mapping[str, Any], versions: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Record one task version this decision produced.

        The commit writes these to ``scheduling_task_versions``, so a version a
        seal pinned is readable afterwards even when a later action in the same
        batch moved the live task on.
        """
        task_id = str(document["task_id"])
        recorded = versions.setdefault(task_id, [])
        if all(item["digest"] != document["digest"] for item in recorded):
            recorded.append(dict(document))

    @staticmethod
    def _refresh_member(
        expansions: dict[str, dict[str, Any]], document: Mapping[str, Any]
    ) -> None:
        """Point every *open* set's member record at the task version that exists now.

        A member is pinned by *version*: its revision and its digest. An edit that
        changed the task without refreshing its member entry would leave an open
        set naming a version that no longer exists, and a later seal would then
        freeze the old identity as if it were the finished work.

        A **sealed** set is deliberately not touched. Sealing is the act of
        fixing the member versions, so a later graph revision may change the
        current task without rewriting what the seal declared: the sealed record
        stays exactly as it was published, and the divergence between it and the
        live task is itself a fact a reader can see.
        """
        task_id = str(document["task_id"])
        for name, expansion in expansions.items():
            if expansion["state"] != "open":
                continue
            members = expansion.get("members") or {}
            if task_id not in members:
                continue
            members[task_id] = {
                **members[task_id],
                "revision": int(document.get("revision", 1)),
                "digest": str(document["digest"]),
            }
            expansion["members"] = members
            expansions[name] = reseal_expansion(expansion)

    @staticmethod
    def _require_expansion_open(expansion: Mapping[str, Any], pointer: str) -> None:
        if expansion["state"] != "open":
            raise SchedulingError(
                "SCHEDULING_EXPANSION_SEALED",
                fields={"expansion_id": expansion["expansion_id"]},
                diagnostics=[
                    located(
                        "SCHEDULING_EXPANSION_SEALED",
                        pointer,
                        "a sealed set accepts no further members",
                    )
                ],
            )

    def _commit_decision(
        self,
        *,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        principal: Principal,
        command_key: str,
        digest: str,
        parsed: Mapping[str, Any],
        grant: Grant,
        plan: Mapping[str, Any],
        current_revision: int,
    ) -> dict[str, Any]:
        """Write the revision, the receipt and the queue, or nothing at all."""
        now = self.clock()
        revision = int(plan["revision"])
        working: dict[str, dict[str, Any]] = plan["working"]
        expansions: dict[str, dict[str, Any]] = plan["expansions"]
        effective = plan["effective"]
        cursor = db.execute(
            "UPDATE task_graph_current SET revision=? WHERE project_id=? AND run_id=? "
            "AND revision=?",
            (revision, project_id, run_id, current_revision),
        )
        if cursor.rowcount != 1:
            # Another decision committed first. The graph is untouched: the
            # conflict is reported with the revision the caller must re-read.
            raise SchedulingError(
                "SCHEDULING_GRAPH_REVISION_CONFLICT", current_revision=current_revision
            )
        for name, document in sorted(working.items()):
            db.execute(
                "INSERT INTO scheduling_tasks VALUES (?,?,?,?,?) "
                "ON CONFLICT(project_id, run_id, task_id) DO UPDATE SET "
                "record=excluded.record, digest=excluded.digest",
                (project_id, run_id, name, canonical_json(document), document["digest"]),
            )
            # Every version this batch produced is archived immutably, not only
            # the last one. A seal may pin an intermediate revision — a decision
            # can bind a role and then seal and then hold dispatch — and a pinned
            # digest with no stored body behind it cannot be read back.
            pinned = plan.get("versions", {}).get(name) or [document]
            for version in pinned:
                db.execute(
                    "INSERT INTO scheduling_task_versions VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(project_id, run_id, task_id, revision) DO NOTHING",
                    (
                        project_id,
                        run_id,
                        name,
                        int(version.get("revision", 1)),
                        str(version["digest"]),
                        canonical_json(version),
                    ),
                )
        for name, document in sorted(expansions.items()):
            db.execute(
                "INSERT INTO task_expansions VALUES (?,?,?,?) "
                "ON CONFLICT(project_id, run_id, expansion_id) DO UPDATE SET "
                "record=excluded.record",
                (project_id, run_id, name, canonical_json(document)),
            )
        parent = db.execute(
            "SELECT record FROM task_graph_revisions "
            "WHERE project_id=? AND run_id=? AND revision=?",
            (project_id, run_id, current_revision),
        ).fetchone()
        parent_digest = str(json.loads(parent["record"])["digest"]) if parent is not None else ""
        record = graph_document(
            run_id=run_id,
            revision=revision,
            parent_revision=current_revision,
            parent_digest=parent_digest,
            decision_id=str(parsed["decision_id"]),
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            # The distinction the design requires: the initial user approval is
            # referenceable, and a later revision under a grant is not silently
            # recomputed into a "new approval".
            # The initial user approval is the Run's own authorisation, recorded
            # once at creation. Every accepted *decision* is taken under the
            # grant that authorised it, whether that grant is a top-level one or
            # a delegated one: recomputing a "new approval" here would present a
            # subsequent decision as though the user had approved the graph.
            accepted_under="accepted_under_grant",
            # The grant that authorised this revision, with its depth, so a
            # reader can tell which grant accepted it and that the user's own
            # initial approval is a separate, earlier fact.
            authorizing_grant_id=grant.grant_id,
            authorizing_grant_depth=grant.depth,
            user_authorization_id=grant.root_authorization_id,
            inputs_digest=str(parsed["inputs_digest"]),
            nodes_added=plan["nodes_added"],
            nodes_superseded=plan["nodes_superseded"],
            nodes_retired=plan["nodes_retired"],
            expansion_states={name: str(item["state"]) for name, item in expansions.items()},
            tasks=effective,
            created_at=now,
        )
        db.execute(
            "INSERT INTO task_graph_revisions VALUES (?,?,?,?,?)",
            (project_id, run_id, revision, canonical_json(record), record["digest"]),
        )
        result = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "decision_id": parsed["decision_id"],
            "run_id": run_id,
            "grant_id": grant.grant_id,
            "grant_depth": grant.depth,
            "role_instance": grant.role_instance,
            "term": grant.term,
            "credential_id": principal.credential_id,
            "trigger": parsed["trigger"],
            "reason": parsed["reason"],
            "expected_graph_revision": parsed["expected_graph_revision"],
            "graph_revision": revision,
            "graph_digest": record["digest"],
            "parent_graph_digest": parent_digest,
            "inputs_digest": parsed["inputs_digest"],
            "accepted_under": record["accepted_under"],
            "authorizing_grant_id": grant.grant_id,
            "authorizing_grant_depth": grant.depth,
            "user_authorization_id": grant.root_authorization_id,
            "nodes_added": plan["nodes_added"],
            "nodes_superseded": plan["nodes_superseded"],
            "nodes_retired": plan["nodes_retired"],
            "sealed_expansions": plan["sealed"],
            "task_count": int(record["task_count"]),
            "outstanding_outcomes": plan["outstanding_outcomes"],
            "accepted_at": now,
            # The engine accepted a command; it did not run anything.
            "model_calls": 0,
            "runs_started": 1,
            "physical_execution": "not_started",
            "requires_further_approval": False,
        }
        db.execute(
            "INSERT INTO scheduling_decisions VALUES (?,?,?,?,?)",
            (project_id, run_id, command_key, digest, canonical_json(result)),
        )
        return result

    # --------------------------------------------------------- graph readback

    def task_version(
        self, project_id: str, run_id: str, task_id: str, revision: int, digest: str
    ) -> dict[str, Any]:
        """One immutable task version, by the identity a seal pinned it with.

        A sealed member is named by revision *and* digest. Both are checked, so a
        stored version cannot answer for a different one that happens to share a
        revision number.
        """
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            row = db.execute(
                "SELECT record, digest FROM scheduling_task_versions "
                "WHERE project_id=? AND run_id=? AND task_id=? AND revision=?",
                (project_id, run_id, task_id, revision),
            ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_TASK_VERSION_NOT_FOUND")
        if str(row["digest"]) != str(digest):
            raise SchedulingError(
                "SCHEDULING_TASK_VERSION_MISMATCH",
                fields={
                    "pinned_digest": str(digest)[:64],
                    "stored_digest": str(row["digest"])[:64],
                },
            )
        document = json.loads(row["record"])
        if task_changed(document):
            raise SchedulingError("SCHEDULING_TASK_RECORD_CHANGED")
        return dict(document)

    def task_versions(self, project_id: str, run_id: str, task_id: str) -> list[dict[str, Any]]:
        """Every archived version of one task, oldest first."""
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            rows = db.execute(
                "SELECT record FROM scheduling_task_versions "
                "WHERE project_id=? AND run_id=? AND task_id=? ORDER BY revision",
                (project_id, run_id, task_id),
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    def task(self, project_id: str, run_id: str, task_id: str) -> dict[str, Any]:
        """One exact task, by identity.

        Reading a specific task must not depend on which page of the queue it
        happens to fall in: a run with more tasks than one page holds would
        otherwise make a valid task unreachable.
        """
        task_id = bounded_identifier(
            task_id, "SCHEDULING_TASK_NOT_FOUND", "task#/task_id"
        )
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            row = db.execute(
                "SELECT record, digest FROM scheduling_tasks "
                "WHERE project_id=? AND run_id=? AND task_id=?",
                (project_id, run_id, task_id),
            ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_TASK_NOT_FOUND")
        document = json.loads(row["record"])
        if task_changed(document) or document["digest"] != row["digest"]:
            raise SchedulingError("SCHEDULING_TASK_RECORD_CHANGED")
        return dict(document)

    def _tasks(self, db: sqlite3.Connection, project_id: str, run_id: str) -> dict[str, Any]:
        rows = db.execute(
            "SELECT record, digest FROM scheduling_tasks WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchall()
        tasks: dict[str, Any] = {}
        for row in rows:
            document = json.loads(row["record"])
            if task_changed(document) or document["digest"] != row["digest"]:
                raise SchedulingError("SCHEDULING_TASK_RECORD_CHANGED")
            tasks[str(document["task_id"])] = read_task(document)
        return tasks

    def _expansions(
        self, db: sqlite3.Connection, project_id: str, run_id: str
    ) -> dict[str, dict[str, Any]]:
        rows = db.execute(
            "SELECT record FROM task_expansions WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchall()
        expansions: dict[str, dict[str, Any]] = {}
        for row in rows:
            document = json.loads(row["record"])
            if expansion_changed(document):
                raise SchedulingError("SCHEDULING_EXPANSION_RECORD_CHANGED")
            expansions[str(document["expansion_id"])] = document
        return expansions

    def graph(self, project_id: str, run_id: str) -> dict[str, Any]:
        """The current graph revision, its history and every expansion set."""
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            pointer = db.execute(
                "SELECT revision FROM task_graph_current WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchone()
            revision = int(pointer["revision"]) if pointer is not None else 0
            row = (
                None
                if revision == 0
                else db.execute(
                    "SELECT record FROM task_graph_revisions "
                    "WHERE project_id=? AND run_id=? AND revision=?",
                    (project_id, run_id, revision),
                ).fetchone()
            )
            history = db.execute(
                "SELECT revision, record FROM task_graph_revisions "
                "WHERE project_id=? AND run_id=? ORDER BY revision",
                (project_id, run_id),
            ).fetchall()
            expansions = db.execute(
                "SELECT record FROM task_expansions WHERE project_id=? AND run_id=? "
                "ORDER BY expansion_id",
                (project_id, run_id),
            ).fetchall()
        current = None
        if row is not None:
            document = json.loads(row["record"])
            if graph_changed(document):
                raise SchedulingError("SCHEDULING_GRAPH_RECORD_CHANGED")
            current = document
        return {
            "run_id": run_id,
            "graph_revision": revision,
            "current": current,
            "history": [json.loads(item["record"]) for item in history],
            "expansions": [json.loads(item["record"]) for item in expansions],
        }

    def list_tasks(
        self,
        project_id: str,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Every task of the run, paged, with no duplicate and no omission.

        The page size is a transport bound on one response. The same task set is
        read completely by following the cursor, so a run with more tasks than
        one page holds is never reported as a truncated set.
        """
        size = self._page(limit)
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            tasks = self._tasks(db, project_id, run_id)
        items = [tasks[name] for name in sorted(tasks)]
        if state is not None:
            if state not in {
                "queued",
                "blocked",
                "claimed",
                "completed",
                "failed",
                "cancelled",
                "superseded",
                "unknown",
            }:
                raise SchedulingError("SCHEDULING_INPUT_INVALID")
            items = [task for task in items if task.state == state]
        start = 0
        if cursor is not None:
            names = [task.task_id for task in items]
            if cursor not in names:
                raise SchedulingError("SCHEDULING_CURSOR_INVALID")
            start = names.index(cursor) + 1
        window = items[start : start + size]
        return {
            "run_id": run_id,
            "items": [task.as_document() for task in window],
            "count": len(items),
            "page_size": size,
            "next_cursor": window[-1].task_id if start + size < len(items) and window else None,
            "truncated": False,
        }

    @staticmethod
    def _page(limit: int | None) -> int:
        if limit is None:
            return DEFAULT_PAGE
        return positive_count(
            limit, "SCHEDULING_INPUT_INVALID", "queue#/limit", maximum=MAXIMUM_PAGE
        )

    # -------------------------------------------------- queue and claims (AC5/6)

    def queue(
        self,
        project_id: str,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """The complete ready queue, in the order the engine would dispatch it.

        Every legal task the run contains is reachable here across pages; nothing
        is truncated to a fixed count. A task whose dependencies are unmet, whose
        write zone is held, whose dispatch is held, or whose pool has no room is
        reported in its own state with the reason it is not ready, rather than
        being dropped from the answer.
        """
        size = self._page(limit)
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            tasks = self._tasks(db, project_id, run_id)
            expansions = self._expansions(db, project_id, run_id)
        blocked_by_seal = self._join_blocked(expansions, tasks)
        ready: list[Any] = []
        waiting: list[dict[str, Any]] = []
        for name in sorted(tasks):
            task = tasks[name]
            if task.state in {"completed", "failed", "cancelled", "superseded"}:
                continue
            if task.state == "claimed":
                continue
            reason = self._waiting_reason(
                task=task,
                tasks=tasks,
                join_blocked=blocked_by_seal.get(task.task_id),
            )
            if reason is None:
                ready.append(task)
            else:
                waiting.append({"task_id": task.task_id, "reason": reason, "state": task.state})
        ordered = ready_ordering(ready)
        start = 0
        if cursor is not None:
            names = [task.task_id for task in ordered]
            if cursor not in names:
                raise SchedulingError("SCHEDULING_CURSOR_INVALID")
            start = names.index(cursor) + 1
        window = ordered[start : start + size]
        return {
            "run_id": run_id,
            "items": [task.as_document() for task in window],
            "ready_count": len(ordered),
            "waiting": waiting[:1000],
            "waiting_count": len(waiting),
            "page_size": size,
            "next_cursor": window[-1].task_id if start + size < len(ordered) and window else None,
            "truncated": False,
            "join_blocked": sorted(blocked_by_seal),
        }

    @staticmethod
    def _join_blocked(
        expansions: Mapping[str, Mapping[str, Any]],
        tasks: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """Tasks held back by an expansion set, with the reason each one is held.

        Two different relationships are covered, and they are different things:

        * **A join over an expansion may not converge before the seal.** A task
          that declares a ``joins`` reference to a set is the fan-in, and running
          it while the set is still open would report a result over "the members
          I know about so far" rather than over the run's real work.
        * **A member of an open set is not itself ready.** An open set may still
          receive members, so a member's pending result is not yet the set's
          final answer. Its own dependencies and resources are checked
          separately, exactly as for any other task.

        Membership and being the join are therefore deliberately *not* conflated:
        a task is held because of what it is, not merely because it is near an
        open set.
        """
        blocked: dict[str, str] = {}
        open_sets = {
            name for name, expansion in expansions.items() if expansion["state"] != "sealed"
        }
        for name, expansion in expansions.items():
            if name not in open_sets:
                # A sealed set has fixed its members, so they are ordinary work:
                # the seal is what releases them.
                continue
            for member in expansion.get("members", {}):
                blocked.setdefault(str(member), "expansion_member_open")
        for task in (tasks or {}).values():
            for reference in task.joins:
                joined = expansions.get(reference)
                if joined is None:
                    continue
                if reference in open_sets:
                    blocked[task.task_id] = "join_expansion_open"
                    continue
                # The set is sealed, so its members are fixed — and the join
                # waits for every *required* member's real result, not merely for
                # the seal. A sealed member that has not produced its result yet
                # is exactly the case the seal exists to make checkable, so a
                # fan-in that ran now would report a convergence over work that
                # has not happened.
                live = tasks or {}
                waiting = [
                    str(member["task_id"])
                    for member in joined["members"].values()
                    if str(member["task_id"]) != task.task_id
                    and bool(member.get("required", True))
                    and (
                        (holder := live.get(str(member["task_id"]))) is None
                        or holder.state != "completed"
                    )
                ]
                if waiting:
                    blocked[task.task_id] = "join_member_pending"
        return blocked

    def _waiting_reason(
        self,
        *,
        task: Any,
        tasks: Mapping[str, Any],
        join_blocked: str | None,
    ) -> str | None:
        if join_blocked is not None:
            return join_blocked
        if task.dispatch != "dispatched":
            return "dispatch_held"
        for dependency in task.depends_on:
            other = tasks.get(dependency)
            if other is None:
                return "dependency_unknown"
            if other.state == "completed":
                continue
            if other.state in {"failed", "cancelled", "superseded", "unknown"}:
                return "dependency_failed"
            return "dependency_pending"
        if self._zone_holder(tasks, task.write_zone, exclude=task.task_id) is not None:
            return "write_zone_busy"
        return None

    @staticmethod
    def _zone_holder(
        tasks: Mapping[str, Any], zone: str, *, exclude: str | None = None
    ) -> str | None:
        """Any live holder of the same directory, by path identity rather than spelling.

        A plain string comparison would admit a second writer for the same tree
        whenever the two tasks spelled it differently - a different case, or one
        zone nested inside the other - so the comparison is made on the shared
        path identity instead.
        """
        for name in sorted(tasks):
            if name == exclude:
                continue
            other = tasks[name]
            if other.state not in {"claimed", "unknown"}:
                continue
            if zones_overlap(str(other.write_zone), zone):
                return name
        return None

    def claim(
        self,
        project_id: str,
        run_id: str,
        payload: object,
        *,
        principal: Principal,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Claim one ready task, rechecking every authority fact under the write lock.

        The claim confirms, inside one transaction and against current durable
        rows: that the task exists and is ready, that its dependencies are really
        complete, that its grant and every ancestor grant are live at this
        instant, that its write zone has no other holder, and that the pool it
        draws on has room right now. A stale ready snapshot therefore cannot be
        used to obtain work the run is no longer willing to hand out.
        """
        principal.require_route("claim")
        principal.require_run(run_id)
        fields = self._require_fields(payload, CLAIM_FIELDS, "claim#/payload")
        claim_key = bounded_identifier(
            fields.get("claim_key"), "SCHEDULING_INPUT_INVALID", "claim#/claim_key"
        )
        task_id = bounded_identifier(
            fields.get("task_id"), "SCHEDULING_TASK_NOT_FOUND", "claim#/task_id"
        )
        digest = content_digest(["scheduling.claim", project_id, run_id, claim_key, task_id])
        with self._owned(project_id) as db:
            replay = self._claim_replay(db, project_id, run_id, command_key, digest)
            if replay is not None:
                return replay, False
            self._run_row(db, project_id, run_id)
            tasks = self._tasks(db, project_id, run_id)
            document = tasks.get(task_id)
            if document is None:
                raise SchedulingError("SCHEDULING_TASK_NOT_FOUND")
            if document.state == "claimed":
                # A repeated claim for the same task returns the *same* work: a
                # second claim never creates a second unit of work.
                existing = self._claim_for_task(db, project_id, run_id, task_id)
                if existing is not None:
                    return existing, False
            if document.state in {"completed", "failed", "cancelled", "superseded"}:
                raise SchedulingError(
                    "SCHEDULING_TASK_NOT_READY",
                    fields={"task_id": task_id, "state": document.state},
                    diagnostics=[
                        located(
                            "SCHEDULING_TASK_NOT_READY",
                            "claim#/task_id",
                            "that task is no longer claimable",
                        )
                    ],
                )
            expansions = self._expansions(db, project_id, run_id)
            held_by = self._join_blocked(expansions, tasks).get(task_id)
            if held_by is not None:
                raise SchedulingError(
                    "SCHEDULING_EXPANSION_NOT_SEALED",
                    fields={"task_id": task_id, "waiting_reason": held_by},
                    diagnostics=[
                        located(
                            "SCHEDULING_EXPANSION_NOT_SEALED",
                            "claim#/task_id",
                            "the expansion set this task depends on is still open",
                        )
                    ],
                )
            reason = self._waiting_reason(task=document, tasks=tasks, join_blocked=None)
            if reason is not None:
                raise SchedulingError(
                    "SCHEDULING_TASK_NOT_READY",
                    fields={"task_id": task_id, "waiting_reason": reason},
                    diagnostics=[
                        located(
                            "SCHEDULING_TASK_NOT_READY",
                            "claim#/task_id",
                            "the task is not ready to be handed out",
                        )
                    ],
                )
            grant_id = str(document.document.get("grant_id") or "")
            grant = self._grant_row(db, project_id, run_id, grant_id)
            self._require_chain_live(db, project_id, run_id, grant)
            now = self.clock()
            if grant_expired(grant, now):
                raise SchedulingError("SCHEDULING_GRANT_EXPIRED")
            admission = self._admit(db, project_id, run_id, grant, document)
            if not admission.admitted:
                # The task is *not* dropped and *not* rewritten: it stays exactly
                # as it is, and becomes claimable when the pool frees up.
                raise SchedulingError(
                    admission.reason,
                    fields={"task_id": task_id, **admission.as_document()},
                    diagnostics=[
                        located(
                            admission.reason,
                            "claim#/task_id",
                            "the trusted resource facts do not admit this claim now",
                        )
                    ],
                )
            claim_id = f"claim-{content_digest([project_id, run_id, claim_key])[:24]}"
            claim = {
                "schema_version": CLAIM_SCHEMA_VERSION,
                "claim_id": claim_id,
                "run_id": run_id,
                "task_id": task_id,
                "graph_revision": int(document.graph_revision),
                "grant_id": grant.grant_id,
                "grant_depth": grant.depth,
                "role_alias": document.role_alias,
                "role_ref": document.role_ref,
                "execution_kind_ref": document.execution_kind_ref,
                "model_ref": document.model_ref,
                "tool_refs": list(document.tool_refs),
                "output_contract_ref": document.output_contract_ref,
                "inputs": dict(document.inputs),
                "inputs_digest": content_digest(dict(document.inputs)),
                "depends_on": list(document.depends_on),
                "write_zone": document.write_zone,
                "path_scope": list(document.path_scope),
                "required_outcomes": list(document.required_outcomes),
                "task_digest": document.digest,
                "claimed_by": principal.credential_id,
                "claim_kind": principal.kind,
                "claimed_at": now,
                "attempt_ref": None,
                "admission": admission.as_document(),
                # A claim is a handover of work, not a start and not a
                # completion: both of those are facts only a real execution can
                # produce, and neither is inferred here.
                "physical_execution": "not_started",
                "verified_by_engine": False,
                "model_calls": 0,
            }
            claim["digest"] = content_digest(
                {key: value for key, value in claim.items() if key != "digest"}
            )
            updated = dict(document.document)
            updated["state"] = "claimed"
            updated["claim_id"] = claim_id
            updated["claimed_by"] = principal.credential_id
            updated["claimed_at"] = now
            history = list(updated.get("history") or [])
            history.append(
                {
                    "state": "claimed",
                    "at": now,
                    "by": principal.credential_id,
                    "graph_revision": int(document.graph_revision),
                    "claim_id": claim_id,
                }
            )
            updated["history"] = history
            updated = reseal_task(updated)
            db.execute(
                "UPDATE scheduling_tasks SET record=?, digest=? "
                "WHERE project_id=? AND run_id=? AND task_id=?",
                (canonical_json(updated), updated["digest"], project_id, run_id, task_id),
            )
            db.execute(
                "INSERT INTO task_claims VALUES (?,?,?,?,?)",
                (project_id, run_id, command_key, digest, canonical_json(claim)),
            )
            self._record_consumption(
                db,
                project_id=project_id,
                run_id=run_id,
                claim_id=claim_id,
                task_id=task_id,
                grant=grant,
                now=now,
            )
            return claim, True

    def _claim_replay(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        command_key: str,
        digest: str,
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT digest, record FROM task_claims "
            "WHERE project_id=? AND run_id=? AND claim_key=?",
            (project_id, run_id, command_key),
        ).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise SchedulingError("SCHEDULING_IDEMPOTENCY_CONFLICT")
        claim: dict[str, Any] = json.loads(row["record"])
        return claim

    def _claim_for_task(
        self, db: sqlite3.Connection, project_id: str, run_id: str, task_id: str
    ) -> dict[str, Any] | None:
        rows = db.execute(
            "SELECT record FROM task_claims WHERE project_id=? AND run_id=? ORDER BY claim_key",
            (project_id, run_id),
        ).fetchall()
        for row in rows:
            document = json.loads(row["record"])
            if document["task_id"] == task_id:
                claim: dict[str, Any] = document
                return claim
        return None

    def claim_of(
        self, project_id: str, run_id: str, claim_key: str
    ) -> dict[str, Any]:
        """One historical claim, read back by its key, after any restart.

        A lost response is answered from this durable row rather than by
        performing the handover again.
        """
        with self._read(project_id) as db:
            row = db.execute(
                "SELECT record FROM task_claims WHERE project_id=? AND run_id=? AND claim_key=?",
                (project_id, run_id, claim_key),
            ).fetchone()
        if row is None:
            raise SchedulingError("SCHEDULING_CLAIM_NOT_FOUND")
        claim: dict[str, Any] = json.loads(row["record"])
        return claim

    def list_claims(self, project_id: str, run_id: str) -> list[dict[str, Any]]:
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            rows = db.execute(
                "SELECT record FROM task_claims WHERE project_id=? AND run_id=? ORDER BY claim_key",
                (project_id, run_id),
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    def report_execution(
        self,
        project_id: str,
        run_id: str,
        task_id: str,
        payload: object,
        *,
        principal: Principal,
        command_key: str,
        trusted: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Record what an execution consumer reports about one claimed task.

        This is a **report from a trusted control-protocol consumer**, and it is
        stored as one. The engine does not verify a physical execution, so a
        report of success never becomes a verified completion: the task's
        ``physical_execution`` stays ``not_started``, ``verified_by_engine`` is
        false, and the reported outcome is kept as its own fact beside the
        control-plane state. ``unknown`` deliberately keeps the task's occupancy.
        """
        del command_key
        principal.require_route("reconcile" if trusted else "observe_task")
        # A protocol credential carries the runs it was issued for. A user
        # session carries none: its authority is ownership of the *project*, and
        # that is rechecked below against the run's own durable row, so a
        # session cannot reach another project's run by naming its identity.
        if not principal.is_user:
            principal.require_run(run_id)
        fields = self._require_fields(payload, REPORT_FIELDS, "task#/report")
        # Reconciliation is a *separate authority*, not a field a reporter may set
        # for itself and not something inferred from what the reporter says. A
        # control-protocol consumer records an observation; only the explicitly
        # reconciling entry point may turn one into control-plane truth.
        outcome = fields.get("outcome")
        if outcome not in {"started", "completed", "failed", "unknown", "cancelled"}:
            raise SchedulingError(
                "SCHEDULING_INPUT_INVALID",
                diagnostics=[
                    located(
                        "SCHEDULING_INPUT_INVALID",
                        "task#/report/outcome",
                        "a reported outcome is one of the declared states",
                    )
                ],
            )
        evidence_ref = fields.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not 1 <= len(evidence_ref) <= 256:
            raise SchedulingError(
                "SCHEDULING_EVIDENCE_REF_REQUIRED",
                diagnostics=[
                    located(
                        "SCHEDULING_EVIDENCE_REF_REQUIRED",
                        "task#/report/evidence_ref",
                        "a report names the evidence it is based on",
                    )
                ],
            )
        note = fields.get("note", "")
        if not isinstance(note, str) or len(note) > 8_000:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        attempt_ref = fields.get("attempt_ref")
        if attempt_ref is not None and (not isinstance(attempt_ref, str) or not attempt_ref):
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        with self._owned(project_id) as db:
            self._run_row(db, project_id, run_id)
            tasks = self._tasks(db, project_id, run_id)
            document = tasks.get(task_id)
            if document is None:
                raise SchedulingError("SCHEDULING_TASK_NOT_FOUND")
            # Only a *terminal* reconciliation settles the execution. A trusted
            # "unknown" records that the owner looked and could not settle it, so
            # it must not set the guard that the later, real ending needs.
            reconciled = document.document.get("reconciliation_outcome") in {
                "completed",
                "failed",
                "cancelled",
            }
            if document.claim_id is None:
                raise SchedulingError(
                    "SCHEDULING_TASK_NOT_CLAIMED",
                    diagnostics=[
                        located(
                            "SCHEDULING_TASK_NOT_CLAIMED",
                            "task#/task_id",
                            "only a claimed task can be reported on",
                        )
                    ],
                )
            if not trusted and document.claimed_by != principal.credential_id:
                # A consumer's report is a fact about *its own* execution, so it
                # must hold the claim it speaks for. Trusted reconciliation is a
                # different authority: it acts on the run as its owner, so it is
                # checked by project and run ownership rather than by which
                # consumer happened to take the claim.
                raise SchedulingError(
                    "SCHEDULING_TASK_NOT_OWNED",
                    fields={"task_id": task_id},
                    diagnostics=[
                        located(
                            "SCHEDULING_TASK_NOT_OWNED",
                            "task#/task_id",
                            "this consumer does not hold that claim",
                        )
                    ],
                )
            now = self.clock()
            updated = dict(document.document)
            reports = list(updated.get("reports") or [])
            reports.append(
                {
                    "reported_at": now,
                    "reported_by": principal.credential_id,
                    "reporter_kind": principal.kind,
                    "outcome": str(outcome),
                    "evidence_ref": evidence_ref,
                    "attempt_ref": attempt_ref,
                    "note": note,
                    # The report is the consumer's claim about its own execution.
                    # It is kept as such and is never promoted to a verified
                    # completion by this engine.
                    "verified_by_engine": False,
                    "physical_execution": "not_started",
                    "trusted": False,
                }
            )
            updated["reports"] = reports
            if attempt_ref is not None:
                updated["attempt_ref"] = attempt_ref
            # The reported outcome is written to its own field. The control-plane
            # state changes only where the report is decisive for *eligibility*:
            # a reconciling terminal report releases occupancy, and an unknown
            # one deliberately does not.
            updated["reported_state"] = str(outcome)
            # A reported outcome is an **unverified observation**. It is recorded
            # in full and it is not promoted into control-plane truth: it cannot
            # terminalize the task, satisfy a dependency, or release the capacity
            # the claim holds. Only ``trusted`` -- an explicitly reconciling
            # report -- does that, and the engine never infers it from the
            # reporter's own words.
            updated["untrusted_observations"] = int(
                updated.get("untrusted_observations", 0)
            ) + (0 if trusted else 1)
            if trusted:
                updated["reconciled_at"] = now
                updated["reconciled_by"] = principal.credential_id
                updated["reconciliation_outcome"] = str(outcome)
            if trusted:
                if outcome == "unknown":
                    updated["state"] = "unknown"
                elif outcome == "completed":
                    updated["state"] = "completed"
                elif outcome == "failed":
                    updated["state"] = "failed"
                elif outcome == "cancelled":
                    updated["state"] = "cancelled"
            elif outcome in {"completed", "failed", "cancelled"}:
                # The execution has probably ended, but nothing the engine can
                # verify says so. The task therefore stays claimed: reporting an
                # unverified ending must never free a shared slot for a second
                # writer to occupy while the first is unresolved.
                #
                # A task whose ending was *already* reconciled keeps that
                # reconciled state. A later unverified observation is appended as
                # an observation and changes nothing: reopening it would restore
                # occupancy the reconciliation deliberately released and would
                # contradict a trusted fact with an untrusted one.
                if not reconciled:
                    updated["state"] = "claimed"
                    updated["reconciliation_required"] = True
            history = list(updated.get("history") or [])
            history.append(
                {
                    "state": str(updated["state"]),
                    "at": now,
                    "by": principal.credential_id,
                    "graph_revision": int(document.graph_revision),
                    "reported": True,
                    "reported_outcome": str(outcome),
                    "verified_by_engine": False,
                }
            )
            updated["history"] = history
            updated = reseal_task(updated)
            db.execute(
                "UPDATE scheduling_tasks SET record=?, digest=? "
                "WHERE project_id=? AND run_id=? AND task_id=?",
                (canonical_json(updated), updated["digest"], project_id, run_id, task_id),
            )
            if trusted and not reconciled and outcome in {"completed", "failed", "cancelled"}:
                self._release_consumption(
                    db,
                    project_id=project_id,
                    run_id=run_id,
                    claim_id=str(document.claim_id),
                    outcome=str(outcome),
                    reconciled_by=principal.credential_id,
                    now=now,
                )
            return updated, True

    def _release_consumption(
        self,
        db: sqlite3.Connection,
        *,
        project_id: str,
        run_id: str,
        claim_id: str,
        outcome: str,
        reconciled_by: str,
        now: float,
    ) -> None:
        """Release the occupancy one *reconciling* terminal report ends.

        The record is not deleted: the cost it accumulated stays in the ledger,
        so the budget history is not rewritten by a later release, and the run
        cannot spend a consumed budget a second time by freeing the row. Only
        ``released_at`` moves, which is what the live-occupancy view reads.
        """
        row = db.execute(
            "SELECT record FROM budget_consumption WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None:
            return
        record = json.loads(row["record"])
        if record.get("released_at") is not None:
            return
        record["outcome"] = outcome
        record["reconciled_by"] = reconciled_by
        record["released_at"] = now
        record["digest"] = content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        )
        db.execute(
            "UPDATE budget_consumption SET record=? WHERE claim_id=?",
            (canonical_json(record), claim_id),
        )

    # ------------------------------------------------------------- resources

    def observe_resource(
        self, payload: object, *, principal: Principal, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        """Persist one *trusted* resource observation.

        Only a user-session credential can write one. The scheduler credential
        and the execution consumer are refused here, which is the whole point of
        the separation: a role cannot manufacture the capacity it is admitted
        against, and a consumer cannot raise its own ceiling.
        """
        del command_key
        principal.require_route("resource")
        fields = self._require_fields(payload, OBSERVATION_FIELDS, "queue#/observation")
        pool_id = bounded_identifier(
            fields.get("pool_id"), "SCHEDULING_INPUT_INVALID", "queue#/pool_id"
        )
        window_id = bounded_identifier(
            fields.get("window_id"), "SCHEDULING_INPUT_INVALID", "queue#/window_id"
        )
        metric = fields.get("metric")
        if metric not in {"remaining", "used", "unknown"}:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        amount = fields.get("amount")
        limit = fields.get("limit")
        for label, value in (("amount", amount), ("limit", limit)):
            if value is not None and (
                not isinstance(value, str) or not value.isdigit() or len(value) > 18
            ):
                raise SchedulingError(
                    "SCHEDULING_INPUT_INVALID",
                    diagnostics=[
                        located(
                            "SCHEDULING_INPUT_INVALID",
                            f"queue#/observation/{label}",
                            "a quantity is a non-negative integer string",
                        )
                    ],
                )
        if metric == "unknown" and amount is not None:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        if metric != "unknown" and amount is None:
            raise SchedulingError(
                "SCHEDULING_INPUT_INVALID",
                diagnostics=[
                    located(
                        "SCHEDULING_INPUT_INVALID",
                        "queue#/observation/amount",
                        "a known metric needs a quantity",
                    )
                ],
            )
        source = fields.get("source")
        if source not in TRUSTED_SOURCES:
            raise SchedulingError(
                "SCHEDULING_OBSERVATION_SOURCE_UNTRUSTED",
                fields={"source": str(source)[:32]},
                diagnostics=[
                    located(
                        "SCHEDULING_OBSERVATION_SOURCE_UNTRUSTED",
                        "queue#/observation/source",
                        "a trusted observation names a trusted source",
                    )
                ],
            )
        source_ref = bounded_identifier(
            fields.get("source_ref"), "SCHEDULING_INPUT_INVALID", "queue#/observation/source_ref"
        )
        reset_at = fields.get("reset_at")
        if reset_at is not None:
            reset_at = number(reset_at, "SCHEDULING_INPUT_INVALID", "queue#/observation/reset_at")
        reason = fields.get("adjustment_reason")
        if source == "manual" and not isinstance(reason, str):
            raise SchedulingError(
                "SCHEDULING_INPUT_INVALID",
                diagnostics=[
                    located(
                        "SCHEDULING_INPUT_INVALID",
                        "queue#/observation/adjustment_reason",
                        "a manual adjustment states why",
                    )
                ],
            )
        now = self.clock()
        with self.projects._transaction() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(sequence),0) AS last FROM resource_observations "
                "WHERE pool_id=?",
                (pool_id,),
            ).fetchone()
            sequence = int(row["last"]) + 1
            record = {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "pool_id": pool_id,
                "window_id": window_id,
                "sequence": sequence,
                "metric": metric,
                "amount": None if amount is None else int(amount),
                "limit": None if limit is None else int(limit),
                "source": source,
                "source_ref": source_ref,
                "reset_at": reset_at,
                "adjustment_reason": reason,
                "observed_at": now,
                "trusted": True,
                "recorded_by": principal.credential_id,
                "model_calls": 0,
            }
            record["digest"] = content_digest(record)
            db.execute(
                "INSERT INTO resource_observations VALUES (?,?,?,?,?)",
                (pool_id, sequence, canonical_json(record), 1, record["digest"]),
            )
        return record, True

    def activate_resource_policy(
        self, payload: object, *, principal: Principal, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        """Set the explicit ceiling a pool is admitted against for this workload.

        The policy is an explicit user decision, including when it is lower than
        the number of legal tasks: the run keeps every task and queues the
        remainder. Nothing here substitutes an engine default.
        """
        del command_key
        principal.require_route("resource")
        fields = self._require_fields(payload, RESOURCE_POLICY_FIELDS, "queue#/policy")
        pool_id = bounded_identifier(
            fields.get("pool_id"), "SCHEDULING_INPUT_INVALID", "queue#/pool_id"
        )
        concurrent = non_negative_count(
            fields.get("max_concurrent_claims"),
            "SCHEDULING_INPUT_INVALID",
            "queue#/max_concurrent_claims",
        )
        margin = fields.get("safety_margin")
        if margin is not None and (not isinstance(margin, str) or not margin.isdigit()):
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        require = fields.get("require_observation", True)
        if require is not True and require is not False:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        with self.projects._transaction() as db:
            record = {
                "schema_version": RESOURCE_POLICY_SCHEMA_VERSION,
                "pool_id": pool_id,
                "max_concurrent_claims": concurrent,
                "safety_margin": 0 if margin is None else int(margin),
                "require_observation": require,
                "declared_by": principal.credential_id,
                "declared_at": self.clock(),
                "engine_default": None,
            }
            record["digest"] = content_digest(record)
            db.execute(
                "INSERT INTO resource_policies VALUES (?,?,?) "
                "ON CONFLICT(pool_id) DO UPDATE SET record=excluded.record, digest=excluded.digest",
                (pool_id, canonical_json(record), record["digest"]),
            )
        return record, True

    def resource_state(self, project_id: str, run_id: str) -> dict[str, Any]:
        """What the run holds and what the trusted facts say it may hold."""
        with self._read(project_id) as db:
            self._run_row(db, project_id, run_id)
            tasks = self._tasks(db, project_id, run_id)
            policies = {
                str(row["pool_id"]): json.loads(row["record"])
                for row in db.execute("SELECT pool_id, record FROM resource_policies")
            }
            observations = {
                str(row["pool_id"]): json.loads(row["record"])
                for row in db.execute(
                    "SELECT o.pool_id AS pool_id, o.record AS record FROM resource_observations o "
                    "JOIN (SELECT pool_id, MAX(sequence) AS last FROM resource_observations "
                    "GROUP BY pool_id) m ON m.pool_id=o.pool_id AND m.last=o.sequence"
                )
            }
            occupancy = self._pool_occupancy(db, project_id, run_id, tasks)
            live = sum(occupancy.values())
            # Every task that holds occupancy, so a caller can see *which* work
            # is holding a shared pool rather than only the total.
            holders = sorted(
                task.task_id
                for task in tasks.values()
                if task.state in {"claimed", "unknown"}
            )
        return {
            "run_id": run_id,
            "occupancy": occupancy,
            "live_claims": live,
            "occupying_tasks": holders,
            "policies": policies,
            "observations": observations,
        }

    def _pool_occupancy(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        tasks: Mapping[str, Any],
    ) -> dict[str, int]:
        """Live occupancy per pool, counted across *every* grant of the run.

        Counting per grant would let two sibling grants each occupy a full
        budget, so the count is per pool: sharing a parent budget means sharing
        the ceiling it funds. This view is derived from the tasks for a read of
        the run's present shape; the *authoritative* count for admission comes
        from :meth:`_consumption`, which is a durable ledger and therefore
        survives a graph revision that retires the task it was taken for.
        """
        pools: dict[str, str] = {}
        for task in tasks.values():
            if task.state not in {"claimed", "unknown"}:
                continue
            grant_id = str(task.document.get("grant_id") or "")
            if not grant_id or grant_id in pools:
                continue
            try:
                pools[grant_id] = self._grant_row(
                    db, project_id, run_id, grant_id
                ).resource_policy.pool_id
            except SchedulingError:
                continue
        counts: dict[str, int] = {}
        for task in tasks.values():
            if task.state not in {"claimed", "unknown"}:
                continue
            pool = pools.get(str(task.document.get("grant_id") or ""))
            if pool is None:
                continue
            counts[pool] = counts.get(pool, 0) + 1
        return counts

    def _consumption(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        pool_id: str,
    ) -> "_Consumption":
        """What this run already holds of one shared pool, from the ledger.

        Two facts are returned because two different limits apply. The number of
        live claims decides whether another slot exists, and the accumulated
        ``cost_per_claim`` decides whether the shared budget itself still has
        room. Neither is a stored counter that a new grant or a new revision
        could reset: both are read from the durable consumption rows, which are
        written when a claim is taken and released only by a *reconciling*
        report. A task retired by a later graph revision keeps its row, so
        superseding work does not silently free the budget it holds.
        """
        rows = db.execute(
            "SELECT record FROM budget_consumption "
            "WHERE project_id=? AND run_id=? AND pool_id=?",
            (project_id, run_id, pool_id),
        ).fetchall()
        claims = 0
        cost = 0
        budget_refs: set[str] = set()
        for row in rows:
            record = json.loads(row["record"])
            if record.get("released_at") is not None:
                continue
            claims += 1
            cost += int(record["cost_per_claim"])
            budget_refs.add(str(record["budget_ref"]))
        return _Consumption(
            pool_id=pool_id,
            live_claims=claims,
            consumed_cost=cost,
            budget_refs=budget_refs,
        )

    def _record_consumption(
        self,
        db: sqlite3.Connection,
        *,
        project_id: str,
        run_id: str,
        claim_id: str,
        task_id: str,
        grant: Grant,
        now: float,
    ) -> None:
        """Write the durable occupancy one accepted claim creates."""
        record = {
            "claim_id": claim_id,
            "project_id": project_id,
            "run_id": run_id,
            "task_id": task_id,
            "pool_id": grant.resource_policy.pool_id,
            "budget_ref": grant.resource_policy.budget_ref,
            "root_grant_id": grant.root_grant_id,
            "grant_id": grant.grant_id,
            "grant_depth": grant.depth,
            "cost_per_claim": int(grant.resource_policy.cost_per_claim),
            "claimed_at": now,
            "outcome": "claimed",
            "reported_outcome": None,
            "reconciled_by": None,
            "released_at": None,
        }
        record["digest"] = content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        )
        db.execute(
            "INSERT INTO budget_consumption VALUES (?,?,?,?,?,?,?)",
            (
                claim_id,
                project_id,
                run_id,
                grant.resource_policy.pool_id,
                grant.resource_policy.budget_ref,
                grant.root_grant_id,
                canonical_json(record),
            ),
        )

    def _admit(
        self,
        db: sqlite3.Connection,
        project_id: str,
        run_id: str,
        grant: Grant,
        task: Any,
    ) -> Admitted:
        """Decide whether one claim fits, from durable facts read right now.

        Every ancestor grant's ceiling applies, not only the granting grant's,
        and the pool is shared: sibling grants drawing on the same budget cannot
        each spend it as if alone, because the count is read from the same
        durable ledger for all of them.
        """
        pool_id = grant.resource_policy.pool_id
        del task
        consumption = self._consumption(db, project_id, run_id, pool_id)
        policy_row = db.execute(
            "SELECT record FROM resource_policies WHERE pool_id=?", (pool_id,)
        ).fetchone()
        observation_row = db.execute(
            "SELECT record FROM resource_observations WHERE pool_id=? AND trusted=1 "
            "ORDER BY sequence DESC LIMIT 1",
            (pool_id,),
        ).fetchone()
        ceilings = self._gain_ceilings(db, project_id, run_id, grant)
        if policy_row is None:
            return Admitted(
                admitted=False,
                reason="SCHEDULING_CAPACITY_UNKNOWN",
                pool_id=pool_id,
                occupied=consumption.live_claims,
                capacity=None,
                remaining=None,
                source="none",
            )
        policy = json.loads(policy_row["record"])
        ceiling = int(policy["max_concurrent_claims"])
        if observation_row is None:
            # No trusted reading means the capacity is unknown, which is not the
            # same thing as unlimited.
            return Admitted(
                admitted=False,
                reason="SCHEDULING_CAPACITY_UNKNOWN",
                pool_id=pool_id,
                occupied=consumption.live_claims,
                capacity=min([ceiling, *ceilings]),
                remaining=None,
                source="none",
            )
        observation = json.loads(observation_row["record"])
        if observation["metric"] == "unknown":
            return Admitted(
                admitted=False,
                reason="SCHEDULING_CAPACITY_UNKNOWN",
                pool_id=pool_id,
                occupied=consumption.live_claims,
                capacity=min([ceiling, *ceilings]),
                remaining=None,
                source=str(observation["source"]),
            )
        # The metric says what the reading *means*. ``remaining`` is the
        # available quantity itself; ``used`` is what has been consumed, and it
        # is only meaningful against its own limit. Treating ``used`` as if it
        # were ``remaining`` would read "1 of 1 consumed" as "1 available", which
        # admits work against a pool that is already exhausted.
        remaining: int | None = None
        amount = observation["amount"]
        limit = observation["limit"]
        if observation["metric"] == "remaining" and amount is not None:
            remaining = max(0, int(amount) - int(policy["safety_margin"]))
        elif observation["metric"] == "used":
            if amount is None or limit is None:
                # A used quantity with no compatible limit does not describe a
                # pool this engine can admit against, so it stays unknown rather
                # than being turned into a number by assumption.
                return Admitted(
                    admitted=False,
                    reason="SCHEDULING_CAPACITY_UNKNOWN",
                    pool_id=pool_id,
                    occupied=consumption.live_claims,
                    capacity=min([ceiling, *ceilings]),
                    remaining=None,
                    source=str(observation["source"]),
                )
            remaining = max(0, int(limit) - int(amount) - int(policy["safety_margin"]))
        effective = min([ceiling, *ceilings])
        if consumption.live_claims >= effective:
            return Admitted(
                admitted=False,
                reason="SCHEDULING_CAPACITY_BUSY",
                pool_id=pool_id,
                occupied=consumption.live_claims,
                capacity=effective,
                remaining=remaining,
                source=str(observation["source"]),
            )
        if consumption.consumed_cost + grant.resource_policy.cost_per_claim > effective:
            # The budget a claim spends is real, and it accumulates: the cost is
            # not reset by a new grant, a new graph revision or a restart.
            return Admitted(
                admitted=False,
                reason="SCHEDULING_CAPACITY_BUSY",
                pool_id=pool_id,
                occupied=consumption.live_claims,
                capacity=effective,
                remaining=remaining,
                source=str(observation["source"]),
            )
        if remaining is not None and remaining <= 0:
            return Admitted(
                admitted=False,
                reason="SCHEDULING_CAPACITY_BUSY",
                pool_id=pool_id,
                occupied=consumption.live_claims,
                capacity=effective,
                remaining=remaining,
                source=str(observation["source"]),
            )
        return Admitted(
            admitted=True,
            reason="admitted",
            pool_id=pool_id,
            occupied=consumption.live_claims,
            capacity=effective,
            remaining=remaining,
            source=str(observation["source"]),
        )

    def _gain_ceilings(
        self, db: sqlite3.Connection, project_id: str, run_id: str, grant: Grant
    ) -> list[int]:
        """Every ceiling in force along the grant chain, in claims.

        A child may narrow its ceiling but never raise it, so the binding limit
        is the smallest one any grant in the chain declares. Reading only the
        granting grant's value would let a descendant spend the whole parent
        budget it was deliberately given less of.
        """
        ceilings: list[int] = []
        chain: list[Grant] = [grant]
        parent_id = grant.parent_grant_id
        seen = {grant.grant_id}
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = self._grant_row(db, project_id, run_id, parent_id)
            chain.append(parent)
            parent_id = parent.parent_grant_id
        for item in chain:
            cap = item.resource_policy.max_active_claims
            if cap is not None:
                ceilings.append(int(cap))
        return ceilings

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _validated(record: dict[str, Any], code: str) -> dict[str, Any]:
        declared = record.get("digest")
        if not isinstance(declared, str) or declared != content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        ):
            raise SchedulingError(code)
        return record

    @staticmethod
    def _require_fields(value: object, allowed: frozenset[str], pointer: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise SchedulingError(
                "SCHEDULING_INPUT_INVALID",
                diagnostics=[located("SCHEDULING_INPUT_INVALID", pointer, "expected an object")],
            )
        unknown = sorted(name for name in value if not isinstance(name, str) or name not in allowed)
        if unknown:
            raise SchedulingError(
                "SCHEDULING_INPUT_INVALID",
                diagnostics=[
                    located("SCHEDULING_INPUT_INVALID", pointer, "unknown field")
                    for _ in unknown[:8]
                ],
            )
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _text_list(value: object, code: str, pointer: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise SchedulingError(
                code, diagnostics=[located(code, pointer, "a list of identifiers is required")]
            )
        if len(set(value)) != len(value):
            raise SchedulingError(
                code, diagnostics=[located(code, pointer, "each entry appears once")]
            )
        return list(value)

    # ------------------------------------------------------------- run creation

    def create_expansion(
        self,
        project_id: str,
        run_id: str,
        grant_id: str,
        payload: object,
    ) -> dict[str, Any]:
        """Open one expansion set under a grant, before any member exists.

        The set is created by an authorised expansion action so an open batch has
        a durable identity from the start: a join over its members is held until
        an explicit seal, and a later batch cannot be mistaken for the first one.
        """
        fields = self._require_fields(payload, frozenset(
            {"expansion_id", "goal", "required_outcomes", "member_policy", "parent_task_id"}
        ), "grant#/expansion")
        expansion_id = bounded_identifier(
            fields.get("expansion_id"), "SCHEDULING_EXPANSION_INVALID", "grant#/expansion_id"
        )
        goal = fields.get("goal")
        if not isinstance(goal, str) or not 1 <= len(goal) <= 2000:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        outcomes = sorted(
            self._text_list(
                fields.get("required_outcomes"),
                "SCHEDULING_INPUT_INVALID",
                "grant#/required_outcomes",
            )
        )
        member_policy = fields.get("member_policy")
        if member_policy is not None:
            member_policy = positive_count(
                member_policy, "SCHEDULING_INPUT_INVALID", "grant#/member_policy"
            )
        parent_task_id = fields.get("parent_task_id")
        if parent_task_id is not None:
            parent_task_id = bounded_identifier(
                parent_task_id, "SCHEDULING_INPUT_INVALID", "grant#/parent_task_id"
            )
        with self._owned(project_id) as db:
            self._run_row(db, project_id, run_id)
            grant = self._grant_row(db, project_id, run_id, grant_id)
            self._require_chain_live(db, project_id, run_id, grant)
            if "expand_graph" not in grant.allowed_actions:
                raise SchedulingError("SCHEDULING_GRANT_ACTION_NOT_PERMITTED")
            outside = sorted(set(outcomes) - set(grant.required_outcomes))
            if outside:
                raise SchedulingError(
                    "SCHEDULING_GRANT_OUTCOME_NOT_PERMITTED", fields={"outcomes": outside[:8]}
                )
            existing = db.execute(
                "SELECT 1 FROM task_expansions WHERE project_id=? AND run_id=? AND expansion_id=?",
                (project_id, run_id, expansion_id),
            ).fetchone()
            if existing is not None:
                raise SchedulingError("SCHEDULING_EXPANSION_ALREADY_EXISTS")
            document = expansion_document(
                expansion_id=expansion_id,
                run_id=run_id,
                grant_id=grant_id,
                parent_task_id=parent_task_id,
                goal=goal,
                created_revision=0,
                required_outcomes=outcomes,
                member_policy=member_policy,
            )
            db.execute(
                "INSERT INTO task_expansions VALUES (?,?,?,?)",
                (project_id, run_id, expansion_id, canonical_json(document)),
            )
        return document


def _registered_kind(reference: str) -> dict[str, Any] | None:
    """One registered kind's declared surface, from the module-private registry."""
    try:
        from karajan.workflows.registry import kind_for

        kind = kind_for(reference, location="workflow.yaml#/steps")
    except (WorkflowError, ValueError):
        return None
    return {
        "id": kind.ref,
        "inputs": [item.name for item in kind.inputs],
        "required_inputs": [item.name for item in kind.inputs if item.required],
        "outputs": [item.contract_ref for item in kind.outputs],
        "produces": list(kind.produces),
        "side_effects": kind.side_effects,
        "available": bool(kind.available),
    }


def _declared_contracts() -> dict[str, dict[str, Any]]:
    from karajan.workflows.registry import CONTRACT_SCHEMAS

    return {str(name): dict(schema) for name, schema in CONTRACT_SCHEMAS.items()}


__all__ = [
    "Admitted",
    "SchedulingStore",
    "MAXIMUM_PAGE",
    "DEFAULT_PAGE",
]
