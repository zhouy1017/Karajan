"""C-only lifecycle coverage for the Commander producer store/source slice.

The double is deliberately local to this test.  Its fixture provenance is
persisted and it cannot be read as an official Commander planning fact.
"""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from karajan.projects.go_commander_suite import FixedGoCommanderSuite
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from karajan.routing.compiler import digest
from test_projected_qualification_store import projected
from test_qualification_store import case

__all__ = ["case", "projected"]


class COnlyFixedCommanderSuiteDouble:
    """Controlled test observer; never offered by production composition."""

    effects: list[str]

    def __init__(self) -> None:
        self.effects = []

    def validate_profile(self, bound):
        assert bound["registration"]["profile"]["required_permissions"] == []

    def source(self, bound, authentication):
        profile = bound["registration"]["profile"]
        spec = {"scenarios": ["legal_plan", "denied_tool"], "tools": [], "inline": True}
        return {
            "schema_version": "karajan.commander-qualification-source.v2",
            "suite_ref": {"id": "opencode-go-commander-planning-linux", "revision": 2},
            "qualification_scope": "commander_planning.v1",
            "reader_version": "karajan.commander-qualification-reader.v1",
            "observation_origin": "c_fixed_suite_test_double",
            "profile_binding": deepcopy(bound),
            "profile_sha256": digest(profile),
            "credential_generation": authentication["generation"],
            "credential_source": deepcopy(authentication["source"]),
            "authentication_source": deepcopy(authentication),
            "runtime": {"fixed": "C-only"},
            "tokenizer": {"fixed": "C-only"},
            "controller": {"fixed": "C-only"},
            "probe_spec": spec,
            "probe_spec_digest": digest(spec),
            "limits": {
                "approved_input_tokens": 12288,
                "reserved_output_tokens": 4096,
                "operating_context_tokens": 16384,
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 2000,
                "max_requests_per_scene": 6,
                "max_requests_total": 12,
                "max_seconds_per_scene": 150,
                "max_seconds_per_start": 420,
            },
        }

    def observe(self, start, credential, *, current_guard):
        assert credential.generation == start["auth_generation"]
        for scene in start["scenarios"]:
            with current_guard():
                self.effects.append(scene["scenario"])
        return {"status": "passed", "reason_codes": [], "scenarios": [], "provenance": "c_fixed"}


@pytest.fixture
def commander_case(projected):
    projects = projected["projects"]
    config = projects.get_configuration(projected["project_id"])["configuration"]
    commander = deepcopy(config["resources"]["profiles"][0])
    commander["id"] = commander["profile"]["id"] = "commander"
    commander["profile"]["required_permissions"] = []
    commander["profile"]["binding"]["native_settings"] = {
        "suite_ref": {"id": "opencode-go-commander-planning-linux", "revision": 2}
    }
    for row in commander["capability_evidence"]:
        row["profile_digest"] = digest(commander["profile"])
    config["resources"]["profiles"].append(commander)
    config["approved_profile_refs"].append({"id": "commander", "revision": 1})
    preview = projects.preview_configuration(
        projected["project_id"], config, command_key="commander-preview", principal="owner"
    )
    projects.apply_configuration(
        projected["project_id"],
        preview["preview_id"],
        expected_revision=projects.get(projected["project_id"])["revision"],
        command_key="commander-apply",
        principal="owner",
    )
    suite = COnlyFixedCommanderSuiteDouble()
    return {
        **projected,
        "store": ProfileQualificationStore(
            projects,
            clock=lambda: projected["clock"][0],
            credentials=projected["credentials"],
            commander_suite=suite,
        ),
        "suite": suite,
        "commander": commander,
    }


def qualify(case, key="commander-key"):
    return case["store"].qualify_commander_planning(
        case["project_id"],
        {"id": "commander", "revision": 1},
        principal="owner",
        command_key=key,
        validity_seconds=60,
    )


def test_first_start_seals_fixture_provenance_before_controlled_effect(commander_case):
    case = commander_case
    record = qualify(case)
    start = case["store"].get_command_start(case["project_id"], "commander-key", principal="owner")
    execution = start["binding"]["execution_start"]
    assert record["status"] == "passed"
    assert record["provenance"] == "fixture"
    assert execution["source"] == start["binding"]["source"]
    assert [row["scenario"] for row in execution["scenarios"]] == ["legal_plan", "denied_tool"]
    assert case["suite"].effects == ["legal_plan", "denied_tool"]
    assert qualify(case) == record
    # No commander_facts are created by fixture provenance, so the production
    # current reader contract has nothing to admit.
    assert "commander_facts" not in record


def test_same_key_unknown_never_reobserves_after_lost_record_reply(commander_case, monkeypatch):
    case = commander_case
    original = case["store"]._owned
    failed = False

    @contextmanager
    def lose_record(project_id, principal):
        nonlocal failed
        with original(project_id, principal) as db:

            class Db:
                def __getattr__(self, name):
                    return getattr(db, name)

                def execute(self, sql, values=()):
                    nonlocal failed
                    if "INSERT INTO profile_qualification_records" in sql and not failed:
                        failed = True
                        raise OSError("lost committed reply")
                    return db.execute(sql, values)

            yield Db()

    monkeypatch.setattr(case["store"], "_owned", lose_record)
    with pytest.raises(OSError):
        qualify(case)
    effects = list(case["suite"].effects)
    with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
        qualify(case)
    assert case["suite"].effects == effects


def test_production_suite_is_unavailable_not_a_success_stub(commander_case, tmp_path):
    case = commander_case
    runtime = tmp_path / "opencode"
    runtime.write_bytes(b"fixed runtime")
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    (tokenizer / "tokenizer.json").write_text("{}", encoding="utf-8")
    store = ProfileQualificationStore(
        case["projects"],
        clock=lambda: case["clock"][0],
        credentials=case["credentials"],
        commander_suite=FixedGoCommanderSuite(runtime, tokenizer, "c" * 64),
    )
    record = store.qualify_commander_planning(
        case["project_id"],
        {"id": "commander", "revision": 1},
        principal="owner",
        command_key="native-unavailable",
        validity_seconds=60,
    )
    assert record["status"] == "failed"
    assert record["reason_codes"] == ["COMMANDER_NATIVE_PROBE_UNAVAILABLE"]
    assert "commander_facts" not in record
