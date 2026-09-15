"""Scheduler grants: explicit, bounded and strictly narrowing authority.

A ``SchedulerGrant`` is what turns a role instance into something the engine will
listen to. The compiled workflow *declares* what a scheduling role would like to
do and grants it nothing (``grants_authority: false``); this module is where an
actual user decision supplies the authority, and where every narrowing rule is
enforced.

Four rules are decided here, and they are the ones a naive implementation gets
wrong:

**A grant binds a subject, not a name.** The subject is a role *instance*, and
the instance is bound to a credential the service issued. Writing
``role_name: Commander`` grants nothing, because nothing here resolves a name to
an identity.

**A child narrows; it never widens.** Every declared dimension - actions, input
references, paths, roles, model and kind references, tools, destination and
budget - must be contained in the parent's. Containment for paths is decided
segment-wise by :func:`karajan.scheduling.values.contains_scope`, so ``src/a``
does not contain ``src/ab`` and cannot be produced by ``src/a/../outside``.

**Delegation is off unless it was explicitly enabled, and always bounded.** The
default is no implicit re-delegation. When it is enabled there is an explicit
maximum depth and an explicit expiry inherited from the parent, so a chain
terminates rather than being limited only by how many decisions someone submits.

**Revocation and expiry propagate downward.** A grant is effective only if it and
every ancestor is live at the moment of use; a revoked or expired parent is a
refusal, not a warning. Occupancy is counted per run and pool rather than per
grant, so a new grant or a new graph revision cannot reset a shared budget.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import SchedulingError, located
from .values import (
    bounded_identifier,
    bounded_reference,
    contains_scope,
    content_digest,
    non_negative_count,
    normalized_scopes,
    number,
    positive_count,
    reference_list,
)

GRANT_SCHEMA_VERSION = "karajan.scheduler-grant.v1"

#: The scheduling actions a grant may name. These are the declared action names
#: from the compiled workflow; naming one here is what makes it usable.
GRANTABLE_ACTIONS = (
    "expand_graph",
    "bind_role",
    "set_dependencies",
    "set_priority",
    "request_dispatch",
    "seal",
)

#: Actions that change the graph. A grant without at least one of these can read
#: but cannot submit a decision that alters anything.
GRAPH_ACTIONS = ("expand_graph", "bind_role", "set_dependencies", "set_priority", "seal")

#: Fields a grant document may carry. An unknown field is refused rather than
#: ignored, so a typo cannot silently drop a constraint.
GRANT_FIELDS = frozenset(
    {
        "grant_id",
        "parent_grant_id",
        "subject_ref",
        "role_instance",
        "allowed_actions",
        "inputs",
        "scope",
        "write_scope",
        "required_outcomes",
        "role_refs",
        "model_refs",
        "execution_kind_refs",
        "tool_refs",
        "data_destinations",
        "artifact",
        "resource_policy",
        "delegation",
        "expires_at",
    }
)

#: The delivery targets a grant may name, in ascending order of external reach.
#: ``report`` produces a local artifact, ``patch`` a candidate change and ``pr``
#: an outward-facing delivery. The order is what makes "a child may lower the
#: target but never raise it" decidable, so a sub-grant cannot acquire external
#: delivery authority its parent never held.
DELIVERY_RANKS: Mapping[str, int] = {"report": 0, "patch": 1, "pr": 2}

#: The delivery target of one grant, as a comparable rank.
DELIVERY_ARTIFACTS = tuple(sorted(DELIVERY_RANKS, key=lambda name: DELIVERY_RANKS[name]))

RESOURCE_POLICY_FIELDS = frozenset(
    {"budget_ref", "pool_id", "cost_per_claim", "max_active_claims"}
)
DELEGATION_FIELDS = frozenset({"allowed", "maximum_depth", "actions"})


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """The shared budget one run's claims draw on.

    ``budget_ref`` and ``pool_id`` name the *shared* resource, which is why a
    child grant cannot substitute its own: replacing the reference would multiply
    the budget rather than narrow it. ``cost_per_claim`` may only rise, and
    ``max_active_claims`` may only fall, in a child.
    """

    budget_ref: str
    pool_id: str
    cost_per_claim: int
    max_active_claims: int | None

    def as_document(self) -> dict[str, Any]:
        return {
            "budget_ref": self.budget_ref,
            "pool_id": self.pool_id,
            "cost_per_claim": self.cost_per_claim,
            "max_active_claims": self.max_active_claims,
        }


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    allowed: bool
    maximum_depth: int
    actions: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "maximum_depth": self.maximum_depth,
            "actions": list(self.actions),
        }


@dataclass(frozen=True, slots=True)
class Grant:
    """One durable grant, as stored and as read back."""

    grant_id: str
    revision: int
    run_id: str
    project_id: str
    issuer_ref: str
    subject_ref: str
    role_instance: str
    parent_grant_id: str | None
    root_grant_id: str
    root_authorization_id: str
    depth: int
    term: int
    valid_from: float
    expires_at: float | None
    allowed_actions: tuple[str, ...]
    inputs: tuple[str, ...]
    scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    required_outcomes: tuple[str, ...]
    role_refs: Mapping[str, str]
    model_refs: tuple[str, ...]
    execution_kind_refs: tuple[str, ...]
    tool_refs: tuple[str, ...]
    data_destinations: tuple[str, ...]
    artifact: str | None
    resource_policy: ResourcePolicy
    delegation: DelegationPolicy
    revoked_at: float | None
    revoked_reason: str | None
    digest: str
    document: Mapping[str, Any]

    @property
    def live(self) -> bool:
        return self.revoked_at is None

    def as_document(self) -> dict[str, Any]:
        return dict(self.document)


def _text_list(value: object, code: str, pointer: str, *, limit: int = 256) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SchedulingError(
            code, diagnostics=[located(code, pointer, "a list of identifiers is required")]
        )
    if len(value) > limit:
        raise SchedulingError(
            code, diagnostics=[located(code, pointer, f"at most {limit} entries are accepted")]
        )
    if len(set(value)) != len(value):
        raise SchedulingError(
            code, diagnostics=[located(code, pointer, "each entry appears once")]
        )
    return list(value)


def validate_resource_policy(
    value: object, pointer: str = "grant#/resource_policy"
) -> ResourcePolicy:
    if value is None:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_INVALID",
                    pointer,
                    "a grant names the shared budget its claims draw on",
                )
            ],
        )
    if not isinstance(value, Mapping):
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[located("SCHEDULING_GRANT_INVALID", pointer, "expected an object")],
        )
    unknown = sorted(set(value) - RESOURCE_POLICY_FIELDS)
    if unknown:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located("SCHEDULING_GRANT_INVALID", pointer, "unknown field") for _ in unknown[:8]
            ],
        )
    budget_ref = bounded_reference(
        value.get("budget_ref"), "SCHEDULING_GRANT_INVALID", f"{pointer}/budget_ref"
    )
    pool_id = bounded_reference(
        value.get("pool_id"), "SCHEDULING_GRANT_INVALID", f"{pointer}/pool_id"
    )
    cost = positive_count(
        value.get("cost_per_claim"), "SCHEDULING_GRANT_INVALID", f"{pointer}/cost_per_claim"
    )
    cap = value.get("max_active_claims")
    maximum = (
        None
        if cap is None
        else non_negative_count(cap, "SCHEDULING_GRANT_INVALID", f"{pointer}/max_active_claims")
    )
    return ResourcePolicy(
        budget_ref=budget_ref, pool_id=pool_id, cost_per_claim=cost, max_active_claims=maximum
    )


def validate_delegation(value: object, pointer: str = "grant#/delegation") -> DelegationPolicy:
    """Read the delegation policy, defaulting to *no* implicit re-delegation.

    An absent policy, an empty object and an explicit ``allowed: false`` all mean
    the same thing, and none of them is ever widened by an omitted field.
    """
    if value is None:
        return DelegationPolicy(allowed=False, maximum_depth=0, actions=())
    if not isinstance(value, Mapping):
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[located("SCHEDULING_GRANT_INVALID", pointer, "expected an object")],
        )
    unknown = sorted(set(value) - DELEGATION_FIELDS)
    if unknown:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located("SCHEDULING_GRANT_INVALID", pointer, "unknown field") for _ in unknown[:8]
            ],
        )
    allowed = value.get("allowed", False)
    if allowed is not True:
        return DelegationPolicy(allowed=False, maximum_depth=0, actions=())
    depth = non_negative_count(
        value.get("maximum_depth"), "SCHEDULING_GRANT_INVALID", f"{pointer}/maximum_depth"
    )
    if depth < 1:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_INVALID",
                    f"{pointer}/maximum_depth",
                    "an enabled delegation needs an explicit bound of at least one",
                )
            ],
        )
    actions = _text_list(
        value.get("actions", []), "SCHEDULING_GRANT_INVALID", f"{pointer}/actions"
    )
    undeclared = sorted(set(actions) - set(GRANTABLE_ACTIONS))
    if undeclared:
        raise SchedulingError(
            "SCHEDULING_GRANT_ACTION_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_ACTION_INVALID",
                    f"{pointer}/actions/id={name}",
                    "a delegable action is one of the declared scheduling actions",
                )
                for name in undeclared
            ],
        )
    return DelegationPolicy(allowed=True, maximum_depth=depth, actions=tuple(sorted(actions)))


def validate_grant_payload(value: object) -> dict[str, Any]:
    """Validate one grant-issuing command, refusing anything it cannot check."""
    if not isinstance(value, Mapping):
        raise SchedulingError("SCHEDULING_GRANT_INVALID")
    unknown = sorted(set(value) - GRANT_FIELDS)
    if unknown:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located("SCHEDULING_GRANT_INVALID", "grant#/payload", "unknown field")
                for _ in unknown[:8]
            ],
        )
    grant_id = bounded_identifier(
        value.get("grant_id"), "SCHEDULING_GRANT_INVALID", "grant#/grant_id"
    )
    subject_ref = bounded_reference(
        value.get("subject_ref"), "SCHEDULING_GRANT_INVALID", "grant#/subject_ref"
    )
    role_instance = bounded_identifier(
        value.get("role_instance"), "SCHEDULING_GRANT_INVALID", "grant#/role_instance"
    )
    actions = _text_list(
        value.get("allowed_actions"), "SCHEDULING_GRANT_INVALID", "grant#/allowed_actions"
    )
    if not actions:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_INVALID",
                    "grant#/allowed_actions",
                    "a grant names the actions it authorises",
                )
            ],
        )
    undeclared = sorted(set(actions) - set(GRANTABLE_ACTIONS))
    if undeclared:
        raise SchedulingError(
            "SCHEDULING_GRANT_ACTION_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_ACTION_INVALID",
                    f"grant#/allowed_actions/id={name}",
                    "an action is one of the declared scheduling actions",
                )
                for name in undeclared
            ],
        )
    data_destinations = reference_list(
        value.get("data_destinations", []),
        "SCHEDULING_GRANT_INVALID",
        "grant#/data_destinations",
    )
    tool_refs = reference_list(
        value.get("tool_refs", []), "SCHEDULING_GRANT_INVALID", "grant#/tool_refs"
    )
    model_refs = reference_list(
        value.get("model_refs", []), "SCHEDULING_GRANT_INVALID", "grant#/model_refs"
    )
    kind_refs = reference_list(
        value.get("execution_kind_refs", []),
        "SCHEDULING_GRANT_INVALID",
        "grant#/execution_kind_refs",
    )
    role_refs = value.get("role_refs", {})
    if not isinstance(role_refs, Mapping) or not role_refs:
        raise SchedulingError(
            "SCHEDULING_GRANT_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_INVALID",
                    "grant#/role_refs",
                    "a grant fixes the role revisions a decision may bind",
                )
            ],
        )
    resolved_roles: dict[str, str] = {}
    for alias, reference in role_refs.items():
        alias_id = bounded_identifier(alias, "SCHEDULING_GRANT_INVALID", "grant#/role_refs")
        resolved_roles[alias_id] = bounded_reference(
            reference, "SCHEDULING_GRANT_INVALID", f"grant#/role_refs/id={alias_id}"
        )
    artifact = value.get("artifact")
    if artifact is not None and artifact not in DELIVERY_RANKS:
        raise SchedulingError(
            "SCHEDULING_GRANT_ARTIFACT_INVALID",
            fields={"artifact": str(artifact)[:64]},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_ARTIFACT_INVALID",
                    "grant#/artifact",
                    "the delivery target is one of the declared artifact kinds",
                )
            ],
        )
    expires_at = value.get("expires_at")
    return {
        "grant_id": grant_id,
        "subject_ref": subject_ref,
        "role_instance": role_instance,
        "allowed_actions": sorted(actions),
        "inputs": sorted(
            _text_list(value.get("inputs"), "SCHEDULING_GRANT_INVALID", "grant#/inputs")
        ),
        "scope": sorted(
            normalized_scopes(value.get("scope"), "SCHEDULING_GRANT_INVALID", "grant#/scope")
        ),
        "write_scope": sorted(
            normalized_scopes(
                value.get("write_scope"), "SCHEDULING_GRANT_INVALID", "grant#/write_scope"
            )
        ),
        "required_outcomes": sorted(
            _text_list(
                value.get("required_outcomes"),
                "SCHEDULING_GRANT_INVALID",
                "grant#/required_outcomes",
            )
        ),
        "role_refs": resolved_roles,
        "model_refs": sorted(model_refs),
        "execution_kind_refs": sorted(kind_refs),
        "tool_refs": sorted(tool_refs),
        "data_destinations": sorted(data_destinations),
        "artifact": artifact,
        "resource_policy": validate_resource_policy(value.get("resource_policy")),
        "delegation": validate_delegation(value.get("delegation")),
        "expires_at": (
            None
            if expires_at is None
            else number(expires_at, "SCHEDULING_GRANT_INVALID", "grant#/expires_at")
        ),
    }


# --------------------------------------------------------------------- narrowing


def _subset(parent: Sequence[str], child: Sequence[str], dimension: str) -> None:
    extra = sorted(set(child) - set(parent))
    if extra:
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": dimension, "not_permitted": extra[:16]},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    f"grant#/{dimension}",
                    "a child grant cannot add to its parent's authority",
                )
            ],
        )


def require_subset(
    parent: Grant, child: Mapping[str, Any], *, root: Grant | None = None
) -> None:
    """Refuse a child grant that is not contained in its parent, in every dimension.

    Paths are compared segment-wise rather than as strings, so a scope of
    ``src/a/**`` cannot be widened into ``src/ab/**`` and ``src/a/../outside``
    never reaches the comparison at all: normalisation refused it earlier.

    ``root`` is the grant at the top of the chain. A delegation bound is a
    property of the whole chain, so the narrowing check consults it when it is
    supplied; a caller that passes only the immediate parent still gets every
    parent-relative check.
    """
    chain_root = parent if root is None else root
    _subset(parent.allowed_actions, child["allowed_actions"], "allowed_actions")
    _subset(parent.inputs, child["inputs"], "inputs")
    _subset(parent.required_outcomes, child["required_outcomes"], "required_outcomes")
    _subset(parent.model_refs, child["model_refs"], "model_refs")
    _subset(parent.execution_kind_refs, child["execution_kind_refs"], "execution_kind_refs")
    _subset(parent.tool_refs, child["tool_refs"], "tool_refs")
    _subset(parent.data_destinations, child["data_destinations"], "data_destinations")
    if not contains_scope(parent.scope, child["scope"]):
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "scope"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/scope",
                    "a child scope is not contained in its parent's paths",
                )
            ],
        )
    if not contains_scope(parent.write_scope, child["write_scope"]):
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "write_scope"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/write_scope",
                    "a child write scope is not contained in its parent's paths",
                )
            ],
        )
    for alias, reference in child["role_refs"].items():
        if parent.role_refs.get(alias) != reference:
            raise SchedulingError(
                "SCHEDULING_GRANT_NOT_A_SUBSET",
                fields={"dimension": "role_refs", "role": alias},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_NOT_A_SUBSET",
                        f"grant#/role_refs/id={alias}",
                        "a child binds the same role revisions its parent permits",
                    )
                ],
            )
    if set(child["role_refs"]) - set(parent.role_refs):
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "role_refs"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/role_refs",
                    "a child cannot bind a role its parent does not permit",
                )
            ],
        )
    # The delivery target is a permission, not a label: a child may lower it (a
    # report instead of a pull request) but it can never raise it, so external
    # delivery authority is not acquired by delegating.
    child_artifact = child["artifact"]
    parent_artifact = parent.artifact
    parent_rank = DELIVERY_RANKS.get(str(parent_artifact)) if parent_artifact else None
    if child_artifact is not None:
        if parent_rank is None:
            raise SchedulingError(
                "SCHEDULING_GRANT_NOT_A_SUBSET",
                fields={"dimension": "artifact"},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_NOT_A_SUBSET",
                        "grant#/artifact",
                        "the parent granted no delivery target to narrow",
                    )
                ],
            )
        if DELIVERY_RANKS[str(child_artifact)] > parent_rank:
            raise SchedulingError(
                "SCHEDULING_GRANT_NOT_A_SUBSET",
                fields={"dimension": "artifact", "parent_artifact": parent_artifact},
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_NOT_A_SUBSET",
                        "grant#/artifact",
                        "a child cannot widen the delivery target it was given",
                    )
                ],
            )
    policy: ResourcePolicy = child["resource_policy"]
    if policy.budget_ref != parent.resource_policy.budget_ref:
        raise SchedulingError(
            "SCHEDULING_GRANT_BUDGET_REBOUND",
            fields={"dimension": "resource_policy.budget_ref"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_BUDGET_REBOUND",
                    "grant#/resource_policy/budget_ref",
                    "a child draws on the same budget, so occupancy is never multiplied",
                )
            ],
        )
    if policy.pool_id != parent.resource_policy.pool_id:
        raise SchedulingError(
            "SCHEDULING_GRANT_BUDGET_REBOUND",
            fields={"dimension": "resource_policy.pool_id"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_BUDGET_REBOUND",
                    "grant#/resource_policy/pool_id",
                    "a child cannot move the same budget to a second pool",
                )
            ],
        )
    if policy.cost_per_claim < parent.resource_policy.cost_per_claim:
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "resource_policy.cost_per_claim"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/resource_policy/cost_per_claim",
                    "a child cannot make a claim cheaper than its parent accounts it",
                )
            ],
        )
    parent_cap = parent.resource_policy.max_active_claims
    if parent_cap is not None and policy.max_active_claims is None:
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "resource_policy.max_active_claims"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/resource_policy/max_active_claims",
                    "a child cannot remove its parent's occupancy ceiling",
                )
            ],
        )
    if (
        parent_cap is not None
        and policy.max_active_claims is not None
        and policy.max_active_claims > parent_cap
    ):
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "resource_policy.max_active_claims"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/resource_policy/max_active_claims",
                    "a child's ceiling is at most its parent's",
                )
            ],
        )
    expires_at = child["expires_at"]
    if parent.expires_at is not None and (expires_at is None or expires_at > parent.expires_at):
        raise SchedulingError(
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            fields={"dimension": "expires_at"},
            diagnostics=[
                located(
                    "SCHEDULING_GRANT_NOT_A_SUBSET",
                    "grant#/expires_at",
                    "a child expires no later than its parent",
                )
            ],
        )
    child_delegation: DelegationPolicy = child["delegation"]
    parent_delegation = parent.delegation
    # Whether the child may hand anything on at all: the parent has to have
    # enabled delegation *and* named the actions it is willing to see delegated.
    if child_delegation.allowed and not parent_delegation.allowed:
        raise SchedulingError(
            "SCHEDULING_DELEGATION_NOT_PERMITTED",
            diagnostics=[
                located(
                    "SCHEDULING_DELEGATION_NOT_PERMITTED",
                    "grant#/delegation",
                    "the parent did not enable further delegation",
                )
            ],
        )
    # A child exists *because* it was delegated to, so its authority comes from
    # what the parent explicitly made delegable - not from everything the parent
    # happens to be allowed to do itself. A parent that authorises ``bind_role``
    # but declares only ``expand_graph`` delegable has deliberately kept the
    # binding power, and handing it on anyway would be a widening.
    if not parent_delegation.allowed:
        raise SchedulingError(
            "SCHEDULING_DELEGATION_NOT_PERMITTED",
            diagnostics=[
                located(
                    "SCHEDULING_DELEGATION_NOT_PERMITTED",
                    "grant#/parent_grant_id",
                    "the parent did not enable delegation",
                )
            ],
        )
    _subset(
        sorted(parent_delegation.actions),
        child["allowed_actions"],
        "allowed_actions",
    )
    # The set of actions a child may pass on is the intersection of its *own*
    # authority and the actions its parent explicitly made delegable.
    delegable = set(parent_delegation.actions) & set(child["allowed_actions"])
    if child_delegation.allowed:
        _subset(sorted(delegable), child_delegation.actions, "delegation.actions")
    # A narrowed delegation policy is itself inherited: a child at depth 1 whose
    # own policy allows only one further step cannot raise that to two, because
    # the chain it creates would then be longer than the chain its parent named.
    effective_parent_depth = min(
        parent_delegation.maximum_depth,
        chain_root.delegation.maximum_depth - parent.depth,
    )
    if child_delegation.allowed and child_delegation.maximum_depth > effective_parent_depth:
        raise SchedulingError(
            "SCHEDULING_DELEGATION_NOT_PERMITTED",
            fields={
                "dimension": "delegation.maximum_depth",
                "parent_maximum_depth": effective_parent_depth,
            },
            diagnostics=[
                located(
                    "SCHEDULING_DELEGATION_NOT_PERMITTED",
                    "grant#/delegation/maximum_depth",
                    "a child cannot lengthen the bounded delegation chain",
                )
            ],
        )


def require_delegation_depth(
    root: Grant,
    parent: Grant,
    child_depth: int,
    *,
    ancestors: Sequence[Grant] = (),
) -> None:
    """Refuse a delegation beyond any bound in force along the chain.

    A delegation bound is a property of the authority chain, not of the task
    count: a chain depth of one is a different constraint from "at most one
    agent", and this function only ever decides the former.

    The bound that applies is the *narrowest* one any ancestor in force declared,
    expressed in steps remaining below that ancestor. Checking only the root's
    original bound would let a parent that deliberately narrowed its own policy to
    one further step be ignored once it becomes an ancestor, so a child could
    quietly restore the wider bound its parent had already given up.
    """
    remaining = root.delegation.maximum_depth - parent.depth
    for ancestor in ancestors:
        ancestor_remaining = ancestor.delegation.maximum_depth - (
            parent.depth - ancestor.depth
        )
        remaining = min(remaining, ancestor_remaining)
    if not parent.delegation.allowed:
        # A parent that cannot delegate at all has no remaining steps, whatever
        # its recorded maximum says.
        remaining = 0
    steps = child_depth - parent.depth
    if steps > remaining:
        raise SchedulingError(
            "SCHEDULING_DELEGATION_DEPTH_EXCEEDED",
            fields={"remaining_steps": max(remaining, 0)},
            diagnostics=[
                located(
                    "SCHEDULING_DELEGATION_DEPTH_EXCEEDED",
                    "grant#/delegation",
                    "the authorised delegation chain is exhausted",
                )
            ],
        )
    if steps != 1:
        raise SchedulingError(
            "SCHEDULING_DELEGATION_DEPTH_EXCEEDED",
            diagnostics=[
                located(
                    "SCHEDULING_DELEGATION_DEPTH_EXCEEDED",
                    "grant#/delegation",
                    "a child is one step below its parent",
                )
            ],
        )


def grant_digest(document: Mapping[str, Any]) -> str:
    return content_digest(
        {key: value for key, value in document.items() if key != "digest"}
    )


def grant_expired(grant: Grant, now: float) -> bool:
    return grant.expires_at is not None and now >= grant.expires_at
