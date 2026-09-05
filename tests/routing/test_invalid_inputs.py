"""Malformed facts fail closed before arithmetic or ranking."""

import copy

import pytest
from karajan.routing import RoutingError, evaluate_route

from .test_routing import sample


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-1", "1.0000001", "99999999999999999999", 1.0]
)
def test_non_finite_negative_excess_precision_and_numeric_quota_are_rejected(value: object) -> None:
    task, policy, capacity = sample()
    capacity["pools"][0]["reported_remaining"] = value
    with pytest.raises(RoutingError, match="CAPACITY_SNAPSHOT_INVALID"):
        evaluate_route(task, policy, capacity)


@pytest.mark.parametrize("kind", ["account", "pool", "estimate", "profile", "capability"])
def test_duplicate_fact_identity_is_rejected(kind: str) -> None:
    task, policy, capacity = sample()
    collections = {
        "account": capacity["accounts"],
        "pool": capacity["pools"],
        "estimate": capacity["estimates"],
        "profile": policy["resources"]["profiles"],
        "capability": policy["resources"]["profiles"][0]["capability_evidence"],
    }
    rows = collections[kind]
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(RoutingError, match="SNAPSHOT_IDENTITY_CONFLICT"):
        evaluate_route(task, policy, capacity)


def test_missing_risk_mapping_and_path_aliases_cannot_downgrade_a_task() -> None:
    task, policy, capacity = sample()
    policy["risk_policy"]["path_floors"] = [{"prefix": "Auth", "minimum_class": "T3"}]
    task["paths"] = ["auth/login.py"]
    assert evaluate_route(task, policy, capacity)["effective_class"] == "T3"
    task["paths"] = ["src/../auth/login.py"]
    with pytest.raises(RoutingError, match="ROUTING_PATH_INVALID"):
        evaluate_route(task, policy, capacity)


def test_fixture_builder_cannot_synthesize_live_profile_qualification() -> None:
    import json
    from pathlib import Path

    from karajan.routing import fixture_from_configuration

    config = json.loads(Path("examples/projects/offline-configuration.json").read_bytes())
    config["resources"]["profiles"][0]["profile"]["binding"]["runtime_kind"] = "live-runtime"
    with pytest.raises(RoutingError, match="FIXTURE_CONFIGURATION_REQUIRED"):
        fixture_from_configuration(config, as_of=1000.0)


def test_native_credential_fields_are_rejected_before_snapshots_or_fixture_export() -> None:
    import json
    from pathlib import Path

    from karajan.routing import fixture_from_configuration

    task, policy, capacity = sample()
    canary = "routing-public-fake-secret-canary"
    policy["resources"]["profiles"][0]["profile"]["binding"]["native_settings"] = {
        "nested": {"api_key": canary}
    }
    with pytest.raises(RoutingError, match="CREDENTIAL_VALUE_FORBIDDEN") as caught:
        evaluate_route(task, policy, capacity)
    assert canary not in str(caught.value.issues)
    config = json.loads(Path("examples/projects/offline-configuration.json").read_bytes())
    config["resources"]["profiles"][0]["profile"]["binding"]["native_settings"] = {
        "headers": {"Authorization": canary}
    }
    with pytest.raises(RoutingError, match="CREDENTIAL_VALUE_FORBIDDEN"):
        fixture_from_configuration(config, as_of=1000.0)
