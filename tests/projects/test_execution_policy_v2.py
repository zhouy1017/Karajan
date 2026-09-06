"""Owner policy metadata through real registries, Git projects and approved Runs."""

from copy import deepcopy

import pytest
from karajan.projects import ProjectError, ProjectRegistry
from karajan.runs import RunError, RunPlanner
from test_demand_store import approval, approved, case

__all__ = ["approved", "case"]


def v2_document(approved):
    original = approved["projects"].get_execution_policy(
        approved["project_id"], "execution", 1, principal="owner"
    )
    document = {
        key: value
        for key, value in original.items()
        if key
        not in {"project_id", "digest", "registered_by", "registered_at", "activation_allowed"}
    }
    document["schema_version"] = "karajan.execution-policy.v2"
    document["revision"] = 2
    document["context_policy"] = {
        **document["context_policy"],
        "revision": 2,
        "measurement": {
            "method": "reference_tokenizer_estimate",
            "source_sha256": "a" * 64,
            "fixed_margin": 128,
            "ratio_margin_basis_points": 500,
        },
    }
    environment = {"id": "isolated-python", "revision": 1}
    document["validation"] = {
        "id": "repository-validation",
        "revision": 1,
        "checks": [
            {
                "id": "tests",
                "revision": 1,
                "argv": ["python", "-m", "pytest"],
                "environment_ref": deepcopy(environment),
                "timeout_seconds": 60,
            }
        ],
        "environments": [
            {
                **environment,
                "runtime_kind": "isolated-command",
                "platform": "linux_x64",
                "source_sha256": "b" * 64,
                "filesystem": "candidate_copy",
                "network": "none",
                "env": {"PYTHONUTF8": "1"},
                "max_log_bytes": 65536,
            }
        ],
        "review": {
            "id": "independent_review",
            "revision": 1,
            "environment_ref": deepcopy(environment),
            "context_policy": "candidate_and_acceptance_only",
            "independence_policy": "existing_candidate_independence_v1",
        },
    }
    return document


def test_v2_metadata_is_owner_fixed_and_v1_record_is_unchanged_after_reopen(approved, tmp_path):
    projects, project_id = approved["projects"], approved["project_id"]
    old = projects.get_execution_policy(project_id, "execution", 1, principal="owner")
    document = v2_document(approved)
    fixed = projects.register_execution_policy(
        project_id, document, command_key="policy-v2", principal="owner"
    )
    assert {key: fixed[key] for key in document} == document
    assert fixed["activation_allowed"] is False
    assert fixed["digest"] != old["digest"]
    reopened = ProjectRegistry(projects.database, [tmp_path / "fixture"])
    assert reopened.get_execution_policy(project_id, "execution", 1, principal="owner") == old
    assert "measurement" not in old["context_policy"] and "validation" not in old
    assert reopened.get_execution_policy(project_id, "execution", 2, principal="owner") == fixed
    assert (
        reopened.register_execution_policy(
            project_id, deepcopy(document), command_key="policy-v2", principal="owner"
        )
        == fixed
    )


def test_measurement_ratio_above_supported_maximum_is_rejected(approved):
    document = v2_document(approved)
    document["context_policy"]["measurement"]["ratio_margin_basis_points"] = 10_001
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID"):
        approved["projects"].register_execution_policy(
            approved["project_id"], document, command_key="ratio-too-high", principal="owner"
        )


@pytest.mark.parametrize("value", [None, [], "not-an-object"])
def test_nonobject_registration_preserves_the_domain_error_contract(approved, value):
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID"):
        approved["projects"].register_execution_policy(
            approved["project_id"], value, command_key="invalid-object", principal="owner"
        )


@pytest.mark.parametrize("ratio", [0, 10_000])
def test_measurement_ratio_endpoints_are_accepted(approved, ratio):
    document = v2_document(approved)
    document["context_policy"]["measurement"]["ratio_margin_basis_points"] = ratio
    record = approved["projects"].register_execution_policy(
        approved["project_id"], document, command_key="ratio-endpoint", principal="owner"
    )
    assert record["context_policy"]["measurement"]["ratio_margin_basis_points"] == ratio


def new_run(approved, policy, *, checks=None):
    old = approved["planner"].get(approved["run_id"], principal="owner")
    projects = approved["projects"]
    project = projects.get(approved["project_id"])
    receipts = {}
    planner = RunPlanner(
        projects.database.parent / f"new-runs-{policy['revision']}.sqlite",
        projects,
        admissions=receipts.__getitem__,
    )
    authorization = deepcopy(old["authorization_ceiling"])
    if checks is not None:
        authorization["checks"] = checks
    run = planner.create(
        {
            "schema_version": "karajan.create-run.v2",
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "requirement": old["requirement"],
            "participants": old["participants"],
            "execution_policy": {key: policy[key] for key in ("id", "revision", "digest")},
            "authorization": authorization,
        },
        command_key="new-run",
        principal="owner",
    )
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    receipts["planning-fixture"] = {
        "receipt_ref": "planning-fixture",
        "authority_revision": "test-only-planning",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "lead",
        "profile": approved["profile_ref"],
        "budget_ref": "planning",
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="planning-fixture",
        command_key="receipt",
        principal="owner",
    )
    submission = deepcopy(approved["submission"])
    submission["intent_id"] = intent["id"]
    submission["plan"]["authorization"] = authorization
    plan = planner.submit_plan(run["id"], submission, command_key="plan", principal="lead")
    accepted = planner.approve_plan(
        run["id"], approval(plan), command_key="approve", principal="owner"
    )
    return planner, run, plan, accepted


def test_custom_checks_and_exact_v2_policy_are_frozen_by_real_run_approval(approved):
    document = v2_document(approved)
    document["validation"]["checks"][0]["id"] = "repository-contract"
    policy = approved["projects"].register_execution_policy(
        approved["project_id"], document, command_key="policy-v2", principal="owner"
    )
    planner, run, plan, accepted = new_run(
        approved, policy, checks=["repository-contract", "independent_review"]
    )
    current = RunPlanner(planner.database, approved["projects"]).get(run["id"], principal="owner")
    assert current["execution_policy_snapshot"] == policy
    assert accepted["routing_digest"] == plan["routing_digest"]
    assert plan["routing_binding"]["execution_policy"]["digest"] == policy["digest"]
    assert plan["routing_digest"] != approved["plan"]["routing_digest"]
    assert current["dispatch_enabled"] is False


@pytest.mark.parametrize(
    "checks", [["unknown", "independent_review"], ["tests", "tests", "independent_review"]]
)
def test_v2_run_rejects_unknown_or_duplicate_check_ids(approved, checks):
    policy = approved["projects"].register_execution_policy(
        approved["project_id"], v2_document(approved), command_key="policy-v2", principal="owner"
    )
    with pytest.raises(RunError, match="VALIDATION_CHECKS_NOT_RESOLVED"):
        new_run(approved, policy, checks=checks)


@pytest.mark.parametrize(
    "change",
    [
        "empty_executable",
        "duplicate_check",
        "duplicate_environment",
        "review_as_check",
        "unknown_environment",
        "env_case_alias",
    ],
)
def test_ambiguous_validation_definitions_are_rejected_at_registration(approved, change):
    document = v2_document(approved)
    validation = document["validation"]
    if change == "empty_executable":
        validation["checks"][0]["argv"][0] = ""
    elif change == "duplicate_check":
        validation["checks"].append(deepcopy(validation["checks"][0]))
    elif change == "duplicate_environment":
        validation["environments"].append(deepcopy(validation["environments"][0]))
    elif change == "review_as_check":
        validation["checks"][0]["id"] = "independent_review"
    elif change == "unknown_environment":
        validation["checks"][0]["environment_ref"]["revision"] = 5
    else:
        validation["environments"][0]["env"] = {"PATH": "one", "Path": "two"}
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_VALIDATION_INVALID"):
        approved["projects"].register_execution_policy(
            approved["project_id"], document, command_key="invalid", principal="owner"
        )


@pytest.mark.parametrize("component", ["validation", "check", "environment", "review"])
def test_nested_component_identity_cannot_change_under_a_new_policy_revision(approved, component):
    projects, project_id = approved["projects"], approved["project_id"]
    document = v2_document(approved)
    projects.register_execution_policy(project_id, document, command_key="v2", principal="owner")
    changed = deepcopy(document)
    changed["revision"] = 3
    validation = changed["validation"]
    if component == "validation":
        validation["checks"].append({**deepcopy(validation["checks"][0]), "id": "another-check"})
    else:
        validation["revision"] = 2
        if component == "check":
            validation["checks"][0]["argv"].append("-q")
        elif component == "environment":
            validation["environments"][0]["source_sha256"] = "c" * 64
        else:
            validation["environments"].append(
                {**deepcopy(validation["environments"][0]), "id": "other-environment"}
            )
            validation["review"]["environment_ref"]["id"] = "other-environment"
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_COMPONENT_REVISION_CONFLICT"):
        projects.register_execution_policy(
            project_id, changed, command_key="changed", principal="owner"
        )


@pytest.mark.parametrize("field", ["source_sha256", "fixed_margin", "ratio_margin_basis_points"])
def test_measurement_change_requires_new_context_revision_and_new_approval_digest(approved, field):
    projects, project_id = approved["projects"], approved["project_id"]
    document = v2_document(approved)
    original = projects.register_execution_policy(
        project_id, document, command_key="v2", principal="owner"
    )
    old_planner, old_run, old_plan, _ = new_run(approved, original)
    changed = deepcopy(document)
    changed["revision"] = 3
    changed["context_policy"]["measurement"][field] = "d" * 64 if field == "source_sha256" else 999
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_COMPONENT_REVISION_CONFLICT"):
        projects.register_execution_policy(
            project_id, changed, command_key="unchanged-component", principal="owner"
        )
    changed["context_policy"]["revision"] = 3
    updated = projects.register_execution_policy(
        project_id, changed, command_key="new-component", principal="owner"
    )
    _, _, new_plan, _ = new_run(approved, updated)
    assert new_plan["routing_digest"] != old_plan["routing_digest"]
    assert (
        old_planner.get(old_run["id"], principal="owner")["execution_policy_snapshot"] == original
    )
    assert projects.get_execution_policy(project_id, "execution", 2, principal="owner") == original


@pytest.mark.parametrize(
    "change",
    [
        "argv_nul",
        "env_name",
        "env_nul",
        "negative_margin",
        "fractional_ratio",
        "boolean_margin",
        "unknown_method",
        "invalid_source",
        "missing_measurement",
        "missing_validation",
        "qualification_claim",
    ],
)
def test_new_metadata_schema_rejects_invalid_values_without_echoing_input(approved, change):
    document = v2_document(approved)
    measurement = document["context_policy"]["measurement"]
    if change == "argv_nul":
        document["validation"]["checks"][0]["argv"].append("FAKE-SECRET\x00")
    elif change == "env_name":
        document["validation"]["environments"][0]["env"] = {"BAD=NAME": "FAKE-SECRET"}
    elif change == "env_nul":
        document["validation"]["environments"][0]["env"] = {"VALID_NAME": "FAKE-SECRET\x00"}
    elif change == "negative_margin":
        measurement["fixed_margin"] = -1
    elif change == "fractional_ratio":
        measurement["ratio_margin_basis_points"] = 1.5
    elif change == "boolean_margin":
        measurement["fixed_margin"] = True
    elif change == "unknown_method":
        measurement["method"] = "provider_guarantee"
    elif change == "invalid_source":
        measurement["source_sha256"] = "not-a-digest"
    elif change == "missing_measurement":
        document["context_policy"].pop("measurement")
    elif change == "missing_validation":
        document.pop("validation")
    else:
        document["validation"]["environments"][0]["qualified"] = True
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID") as rejected:
        approved["projects"].register_execution_policy(
            approved["project_id"], document, command_key="invalid", principal="owner"
        )
    assert "FAKE-SECRET" not in str(rejected.value)


def test_complete_new_component_versions_are_accepted_without_replacing_old_policy(approved):
    projects, project_id = approved["projects"], approved["project_id"]
    document = v2_document(approved)
    original = projects.register_execution_policy(
        project_id, document, command_key="v2", principal="owner"
    )
    changed = deepcopy(document)
    changed["revision"] = 3
    validation = changed["validation"]
    validation["revision"] = 2
    validation["checks"][0].update(revision=2, argv=["python", "-m", "pytest", "-q"])
    validation["environments"][0].update(revision=2, source_sha256="e" * 64)
    validation["review"]["revision"] = 2
    for consumer in (validation["checks"][0], validation["review"]):
        consumer["environment_ref"]["revision"] = 2
    fixed = projects.register_execution_policy(
        project_id, changed, command_key="v3", principal="owner"
    )
    assert fixed["validation"] == validation
    assert fixed["digest"] != original["digest"]
    assert projects.get_execution_policy(project_id, "execution", 2, principal="owner") == original


def test_policy_registration_requires_owner_and_legacy_schema_cannot_smuggle_new_fields(approved):
    projects, project_id = approved["projects"], approved["project_id"]
    document = v2_document(approved)
    with pytest.raises(ProjectError, match="USER_DECISION_REQUIRED"):
        projects.register_execution_policy(
            project_id, document, command_key="rogue", principal="lead"
        )
    document["schema_version"] = "karajan.execution-policy.v1"
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID"):
        projects.register_execution_policy(
            project_id, document, command_key="smuggled", principal="owner"
        )
