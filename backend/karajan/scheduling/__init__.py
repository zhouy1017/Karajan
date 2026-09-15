"""Role-directed scheduling: grants, versioned task graphs, resources and claims.

This package is the control plane for [#178](https://github.com/zhouy1017/Karajan/issues/178):
an authorised scheduling role submits a structured decision, the engine validates
it against the grant that authorises it and against the frozen deployment, and
the result is committed atomically as a new immutable task-graph revision with a
decision receipt and a complete pending queue. Trusted resource observations and
an execution consumer's write-zone claims decide when that work is actually
handed out.

It starts no process, calls no model and executes no business step. A claim is a
handover of work, not a start and not a completion; the reported outcome of an
attempt is recorded as a *report* and never as a verified completion. See
[角色主导的任务拆分与调度](../../../docs/architecture/11-role-directed-scheduling.md)
and the implementation record
[R8-P1-04](../../../docs/implementation/r8-phase1-role-scheduling.md).
"""

from .credentials import (
    ROUTES_BY_KIND,
    CredentialRecord,
    IssuedCredential,
    Principal,
    token_digest,
)
from .errors import CONFLICT_CODES, SchedulingError
from .grants import (
    GRANTABLE_ACTIONS,
    DelegationPolicy,
    Grant,
    ResourcePolicy,
    require_delegation_depth,
    require_subset,
    validate_delegation,
    validate_grant_payload,
    validate_resource_policy,
)
from .graph import (
    RETIRED_STATES,
    TASK_STATES,
    Task,
    dependency_cycle,
    task_identity,
    uncovered_obligations,
    validate_decision_payload,
)
from .store import DEFAULT_PAGE, MAXIMUM_PAGE, Admitted, SchedulingStore

__all__ = [
    "CONFLICT_CODES",
    "DEFAULT_PAGE",
    "GRANTABLE_ACTIONS",
    "MAXIMUM_PAGE",
    "RETIRED_STATES",
    "ROUTES_BY_KIND",
    "TASK_STATES",
    "Admitted",
    "CredentialRecord",
    "DelegationPolicy",
    "Grant",
    "IssuedCredential",
    "Principal",
    "ResourcePolicy",
    "SchedulingError",
    "SchedulingStore",
    "Task",
    "dependency_cycle",
    "require_delegation_depth",
    "require_subset",
    "task_identity",
    "token_digest",
    "uncovered_obligations",
    "validate_decision_payload",
    "validate_delegation",
    "validate_grant_payload",
    "validate_resource_policy",
]
