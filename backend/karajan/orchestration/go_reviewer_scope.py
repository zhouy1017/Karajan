"""Narrow current readonly Reviewer qualification to one approved task's limits."""

from typing import Any, Literal

from pydantic import ValidationError, field_validator

from karajan.adapters.opencode.go_journal import GoQualificationLimits
from karajan.candidates.review_output import PARSER_REVISION
from karajan.contracts.probe import Contract
from karajan.projects.models import ProfileRef
from karajan.routing.compiler import digest

SUITE = {"id": "opencode-go-readonly-review-linux", "revision": 1}


class ReadonlyReviewerScope(Contract):
    schema_version: Literal["karajan.go-readonly-reviewer-executor-scope.v1"]
    suite_ref: ProfileRef
    projection: Literal["existing_regular_files"]
    new_files_supported: Literal[False]
    tools: list[Literal["read"]]
    supported_roles: list[Literal["reviewer"]]
    task_classes: list[Literal["T1"]]
    context: GoQualificationLimits
    output_policy: Literal["fixed_native_limit"]
    max_requests: Literal[6]
    candidate_capture: Literal[False]
    output_parser_revision: str

    @field_validator("new_files_supported", "candidate_capture", mode="before")
    @classmethod
    def exact_boolean(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("boolean scope flag required")
        return value

    @field_validator("max_requests", mode="before")
    @classmethod
    def exact_integer(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("integer request limit required")
        return value


def resolve_go_reviewer_execution(
    registration: dict[str, Any],
    observation: dict[str, Any] | None,
    task: dict[str, Any],
    execution: dict[str, Any],
    effective_class: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Consume a trusted Store observation; this function cannot grant qualification.

    The concrete Suite and Store bind Profile, runtime, parser, credentials and
    complete observations. The binding consumer calls this inside the same Project
    guard, before giving membership any roles or capabilities. The returned limits
    prepare a binding only; they do not authorize a Task effect or reserve capacity.
    """
    if observation is None:
        return None, ["READONLY_REVIEWER_SCOPE_REQUIRED"]
    try:
        scope = ReadonlyReviewerScope.model_validate(observation.get("executor_scope"))
        facts = observation["facts"]
        profile = registration["profile"]
        if (
            scope.suite_ref.model_dump() != SUITE
            or scope.tools != ["read"]
            or scope.supported_roles != ["reviewer"]
            or scope.task_classes != ["T1"]
            or scope.output_parser_revision != PARSER_REVISION
            or scope.context.approved_input_tokens != 12288
            or scope.context.reserved_output_tokens != 4096
            or scope.context.operating_context_tokens != 16384
            or scope.context.fixed_margin != 2048
            or scope.context.ratio_margin_basis_points != 2000
            or observation["qualification_scope"] != "readonly_reviewer_tools"
            or observation["runtime_tools_status"] != "passed"
            or facts["provenance"] != "imported_observation"
            or facts["profile"] != {"id": registration["id"], "revision": registration["revision"]}
            or facts["profile_digest"] != digest(profile)
            or facts["runtime_version"] != profile["binding"]["runtime_version"]
            or facts["roles"] != ["reviewer"]
            or facts["tools"] != ["read"]
            or facts["context_tokens"] != scope.context.operating_context_tokens
        ):
            return None, ["READONLY_REVIEWER_SCOPE_UNQUALIFIED"]
        if (
            task["role"] != "reviewer"
            or effective_class != "T1"
            or not set(task["tools"]) <= {"read"}
        ):
            return None, ["READONLY_REVIEWER_TASK_SCOPE_UNQUALIFIED"]
        policy = execution["context_policy"]
        measurement = policy.get("measurement")
        if execution["schema_version"] != "karajan.execution-policy.v2" or not isinstance(
            measurement, dict
        ):
            return None, ["READONLY_REVIEWER_CONTEXT_POLICY_REQUIRED"]
        if (
            measurement["method"] != "reference_tokenizer_estimate"
            or measurement["source_sha256"] != scope.context.source_sha256
            or measurement["fixed_margin"] < scope.context.fixed_margin
            or measurement["ratio_margin_basis_points"] < scope.context.ratio_margin_basis_points
        ):
            return None, ["READONLY_REVIEWER_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED"]
        operating = min(execution["max_context_tokens"], scope.context.operating_context_tokens)
        if (
            policy["reserved_output_tokens"] != 4096
            or type(task["context_tokens"]) is not int
            or task["context_tokens"] > scope.context.approved_input_tokens
            or task["context_tokens"] + 4096 > operating
        ):
            return None, ["READONLY_REVIEWER_CONTEXT_LIMIT_UNQUALIFIED"]
        context = GoQualificationLimits.model_validate(
            {
                "source_sha256": measurement["source_sha256"],
                "approved_input_tokens": task["context_tokens"],
                "reserved_output_tokens": 4096,
                "operating_context_tokens": operating,
                "fixed_margin": measurement["fixed_margin"],
                "ratio_margin_basis_points": measurement["ratio_margin_basis_points"],
            }
        ).model_dump()
        return {
            "schema_version": "karajan.go-readonly-reviewer-limits.v1",
            "execution_policy_digest": execution["digest"],
            "executor_scope_digest": digest(scope.model_dump()),
            "qualification_ref": facts["evidence_ref"],
            "context": context,
            "max_requests": 6,
        }, []
    except (ValidationError, KeyError, TypeError, ValueError):
        return None, ["READONLY_REVIEWER_SCOPE_INVALID"]
