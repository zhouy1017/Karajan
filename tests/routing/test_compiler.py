"""Compile public rule documents without deriving execution permission from syntax."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from karajan.projects.registry import encoded
from karajan.routing.compiler import RoutingError, compile_rulebook, digest, parse
from karajan.routing.fixture import fixture_from_configuration
from karajan.routing.models import PolicySnapshot, Rulebook


def document() -> dict:
    return json.loads(Path("docs/architecture/examples/rulebook.v1.json").read_bytes())


def test_compile_separates_display_version_and_execution_fields_without_mutation() -> None:
    rules = document()
    before = copy.deepcopy(rules)
    compiled = compile_rulebook(rules)
    assert rules == before
    assert compiled["issues"] == []
    assert compiled["warnings"]
    assert compiled["activation_allowed"] is False
    for field, value in {
        "description": "新的说明",
        "status": "draft",
        "revision": 2,
        "id": "another-book",
    }.items():
        changed = copy.deepcopy(rules)
        changed[field] = value
        assert compile_rulebook(changed)["rulebook_sha256"] == compiled["rulebook_sha256"]
    rules["rules"][0]["priority"] += 1
    assert compile_rulebook(rules)["rulebook_sha256"] != compiled["rulebook_sha256"]


@pytest.mark.parametrize("field", ["eligible_groups", "capabilities_all"])
def test_empty_routing_requirements_cannot_be_published(field: str) -> None:
    rules = document()
    rules["rules"][0][field] = []
    assert any(
        issue["code"] == "RULE_REQUIREMENT_EMPTY" for issue in compile_rulebook(rules)["issues"]
    )


def test_domain_conjunctions_can_overlap_even_when_names_differ() -> None:
    rules = document()
    first = copy.deepcopy(rules["rules"][2])
    first["when"]["domains_all"] = ["backend"]
    second = copy.deepcopy(first)
    second["id"] = "other-domain"
    second["when"]["domains_all"] = ["frontend"]
    rules["rules"] = [first, second]
    assert any(issue["code"] == "RULE_AMBIGUOUS" for issue in compile_rulebook(rules)["issues"])


@pytest.mark.parametrize(
    "case",
    ["duplicate-stage", "empty-rules", "empty-queue", "numeric-permission", "incompatible-purpose"],
)
def test_structural_contract_gaps_are_rejected(case: str) -> None:
    rules = document()
    if case == "duplicate-stage":
        rules["rules"][2]["quality_escalation_groups"] = ["standard_qualified"] * 2
    elif case == "empty-rules":
        rules["rules"] = []
    elif case == "empty-queue":
        rules["resource_policy"]["queue_order"] = []
    elif case == "numeric-permission":
        rules["global_constraints"]["require_enabled_profile"] = 1
    else:
        rules["rules"][2]["when"]["purpose"] = "lead"
    with pytest.raises(RoutingError):
        compile_rulebook(rules)


def test_same_priority_overlap_is_not_ambiguous_when_a_higher_rule_always_wins() -> None:
    rules = document()
    first = copy.deepcopy(rules["rules"][2])
    second = copy.deepcopy(first)
    second["id"] = "second"
    higher = copy.deepcopy(first)
    higher.update(id="higher", priority=101)
    rules["rules"] = [first, second, higher]
    assert compile_rulebook(rules)["issues"] == []
    higher["when"]["domains_all"] = ["specific"]
    assert any(issue["code"] == "RULE_AMBIGUOUS" for issue in compile_rulebook(rules)["issues"])


def test_bad_unicode_and_unknown_fields_produce_safe_structured_errors() -> None:
    rules = document()
    rules["rules"][0]["id"] = "\ud800"
    with pytest.raises(RoutingError, match="ROUTING_INPUT_INVALID"):
        compile_rulebook(rules)
    rules = document()
    rules["rules"][0]["authorization"] = "SECRET-CANARY"
    with pytest.raises(RoutingError) as error:
        compile_rulebook(rules)
    assert "SECRET-CANARY" not in str(error.value.issues)


def test_profile_fingerprint_matches_existing_catalog_for_non_ascii_bindings() -> None:
    config = json.loads(Path("examples/projects/offline-configuration.json").read_bytes())
    profile = config["resources"]["profiles"][0]["profile"]
    profile["binding"]["native_settings"]["label"] = "本机模型配置"
    existing_fingerprint = hashlib.sha256(encoded(profile).encode()).hexdigest()
    assert digest(profile) == existing_fingerprint


@pytest.mark.parametrize("case", ["rulebook", "policy", "false-permission"])
def test_preparsed_documents_do_not_convert_numbers_into_permission_booleans(case: str) -> None:
    rules = document()
    if case == "false-permission":
        rules["rules"][1]["lead_reserve_access"] = 0
    else:
        rules["global_constraints"]["require_enabled_profile"] = 1
    if case == "policy":
        config = json.loads(Path("examples/projects/offline-configuration.json").read_bytes())
        _, policy, _ = fixture_from_configuration(config, as_of=1000.0)
        policy["rulebook"] = rules
        with pytest.raises(RoutingError):
            parse(PolicySnapshot, policy, "POLICY_INPUT_INVALID")
    else:
        with pytest.raises(RoutingError):
            parse(Rulebook, rules, "RULEBOOK_INVALID")
