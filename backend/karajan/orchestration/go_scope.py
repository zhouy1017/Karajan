"""Resolve a qualified Go mechanism into the limits of one approved Task."""

from typing import Any, Literal

from pydantic import ValidationError, field_validator

from karajan.adapters.opencode.go_journal import GoQualificationLimits
from karajan.contracts.probe import Contract
from karajan.projects.models import ProfileRef
from karajan.routing.compiler import digest

SUITE = {"id": "opencode-go-native-read-edit-linux", "revision": 2}


class ProjectedGoScope(Contract):
    schema_version: Literal["karajan.go-projected-executor-scope.v1"]
    suite_ref: ProfileRef
    projection: Literal["existing_regular_files"]
    new_files_supported: Literal[False]
    tools: list[Literal["read", "edit"]]
    supported_roles: list[Literal["worker"]]
    task_classes: list[Literal["T1"]]
    context: GoQualificationLimits
    max_requests: Literal[6]
    candidate_capture: Literal[True]

    @field_validator("new_files_supported", "candidate_capture", mode="before")
    @classmethod
    def exact_boolean(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("scope flag must be a boolean")
        return value

    @field_validator("max_requests", mode="before")
    @classmethod
    def exact_integer(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("request limit must be an integer")
        return value


def resolve_go_execution(
    registration: dict[str, Any],
    observation: dict[str, Any] | None,
    task: dict[str, Any],
    execution: dict[str, Any],
    effective_class: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Narrow qualified limits; never turn a scope declaration into qualification.

    The input observation comes only from the current qualification Store held
    by ApprovedRunRouting. Native startup still needs the approved Workspace,
    current execution intent/fence, credentials and real capacity guard.
    """
    profile = registration.get("profile")
    if not isinstance(profile, dict):
        return None, []
    binding = profile["binding"]
    if not (
        binding["runtime_kind"] == "opencode-go-isolated"
        and binding["native_settings"].get("suite_ref") == SUITE
    ):
        return None, []
    if observation is None:
        return None, ["PROJECTED_EXECUTOR_SCOPE_REQUIRED"]
    try:
        scope = ProjectedGoScope.model_validate(observation.get("executor_scope"))
    except ValidationError:
        return None, ["PROJECTED_EXECUTOR_SCOPE_INVALID"]
    if (
        scope.suite_ref.model_dump() != SUITE
        or scope.tools != ["read", "edit"]
        or scope.supported_roles != ["worker"]
        or scope.task_classes != ["T1"]
        or scope.context.reserved_output_tokens != 4096
        or scope.context.operating_context_tokens != 16384
        or observation.get("qualification_scope") != "projected_native_tools"
        or observation.get("runtime_tools_status") != "passed"
        or observation["facts"]["provenance"] != "imported_observation"
        or observation["facts"]["context_tokens"] != scope.context.operating_context_tokens
    ):
        return None, ["PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED"]
    if (
        task["role"] not in scope.supported_roles
        or effective_class not in scope.task_classes
        or not set(task["tools"]) <= set(scope.tools)
    ):
        return None, ["PROJECTED_TASK_SCOPE_UNQUALIFIED"]
    context_policy = execution["context_policy"]
    if execution["schema_version"] != "karajan.execution-policy.v2" or not isinstance(
        context_policy.get("measurement"), dict
    ):
        return None, ["PROJECTED_CONTEXT_POLICY_REQUIRED"]
    measurement = context_policy["measurement"]
    if (
        measurement["method"] != "reference_tokenizer_estimate"
        or measurement["source_sha256"] != scope.context.source_sha256
        or measurement["fixed_margin"] < scope.context.fixed_margin
        or measurement["ratio_margin_basis_points"] < scope.context.ratio_margin_basis_points
    ):
        return None, ["PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED"]
    # The native model configuration currently always requests 4096 output
    # tokens. A smaller policy allowance is not an implemented runtime setting.
    operating = min(execution["max_context_tokens"], scope.context.operating_context_tokens)
    if (
        context_policy["reserved_output_tokens"] != scope.context.reserved_output_tokens
        or task["context_tokens"] > scope.context.approved_input_tokens
        or task["context_tokens"] + context_policy["reserved_output_tokens"] > operating
    ):
        return None, ["PROJECTED_CONTEXT_LIMIT_UNQUALIFIED"]
    try:
        context = GoQualificationLimits.model_validate(
            {
                "source_sha256": measurement["source_sha256"],
                "approved_input_tokens": task["context_tokens"],
                "reserved_output_tokens": context_policy["reserved_output_tokens"],
                "operating_context_tokens": operating,
                "fixed_margin": measurement["fixed_margin"],
                "ratio_margin_basis_points": measurement["ratio_margin_basis_points"],
            }
        ).model_dump()
    except ValidationError:
        return None, ["PROJECTED_CONTEXT_LIMIT_UNQUALIFIED"]
    return {
        "schema_version": "karajan.go-task-execution-limits.v1",
        "execution_policy_digest": execution["digest"],
        "executor_scope_digest": digest(scope.model_dump()),
        "qualification_ref": observation["facts"]["evidence_ref"],
        "context": context,
        "max_requests": scope.max_requests,
    }, []
