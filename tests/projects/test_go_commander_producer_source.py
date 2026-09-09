"""Protected Commander source and semantic-probe regression coverage."""

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from karajan.isolation.go_commander_probe import _prompt, _semantically_valid
from karajan.orchestration.go_commander_qualification import (
    CommanderCredentialSource,
    CommanderQualificationSettings,
    read_commander_qualification_settings,
    write_commander_qualification_settings,
)
from karajan.projects.go_commander_suite import probe_spec
from karajan.runs.planning_output import parse_planning_output
from karajan.runs.routing_authorization import PlanV2


def _settings(tmp_path: Path) -> CommanderQualificationSettings:
    return CommanderQualificationSettings(
        runtime=tmp_path / "runtime",
        tokenizer_directory=tmp_path / "tokenizer",
        credential_private_directory=tmp_path / "private",
        credential_sources=(
            CommanderCredentialSource("project", "auth", "source", tmp_path / "key"),
        ),
        journal_path=tmp_path / "journal.sqlite",
        work_root=tmp_path / "work",
    )


def test_settings_versions_have_exact_key_sets(tmp_path: Path) -> None:
    legacy = _settings(tmp_path).document()
    legacy["schema_version"] = "karajan.commander-qualification-settings.v2"
    legacy.pop("journal_path")
    legacy.pop("work_root")
    assert CommanderQualificationSettings.from_document(legacy).journal_path is None
    legacy["journal_path"] = "/controller/journal.sqlite"
    with pytest.raises(Exception, match="COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID"):
        CommanderQualificationSettings.from_document(legacy)


@pytest.mark.skipif(
    os.name != "posix", reason="Windows DACL fixture requires host ACL provisioning"
)
def test_protected_descriptor_round_trip_reads_without_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    settings = _settings(tmp_path)
    path = write_commander_qualification_settings(control, settings)
    calls: list[int] = []
    original = os.fsync

    def tracked(fd: int) -> None:
        calls.append(fd)
        original(fd)

    monkeypatch.setattr(os, "fsync", tracked)
    observed, digest = read_commander_qualification_settings(control)
    assert observed.document() == settings.document()
    assert len(digest) == 64
    assert calls == []
    assert path.read_bytes()


@pytest.mark.parametrize("scenario", ["legal_plan", "denied_tool"])
def test_probe_prompt_hides_expected_plan_and_rejects_plausible_escalation(scenario: str) -> None:
    spec = probe_spec()
    expected = spec["cases"][scenario]["expected_plan"]
    prompt = _prompt(scenario, spec)
    # IDs and dependency edges are public input constraints; private summary
    # and acceptance prose are not an answer oracle.
    assert expected["summary"] not in prompt
    parsed = parse_planning_output(json.dumps(expected), version="v2").model_dump(mode="json")
    assert _semantically_valid(parsed, spec, scenario)
    wrong = deepcopy(parsed)
    wrong["authorization"]["tools"] = ["shell"]
    wrong["tasks"][1]["tools"] = ["shell"]
    assert not _semantically_valid(wrong, spec, scenario)


@pytest.mark.parametrize("scenario", ["legal_plan", "denied_tool"])
def test_probe_prompt_supplies_exact_parser_schema_and_fixed_authorization(
    scenario: str,
) -> None:
    spec = probe_spec()
    prompt = _prompt(scenario, spec)
    supplied = json.loads(prompt.split("\n", 2)[2])
    expected_schema = PlanV2.model_json_schema(mode="validation")
    expected_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    expected_schema["x-karajan-output-version"] = "v2"

    assert supplied["schema"] == expected_schema
    assert supplied["schema"]["additionalProperties"] is False
    assert supplied["constraints"] == spec["cases"][scenario]["expected_plan"]["authorization"]
    assert supplied["constraints"]["tools"] == []
    assert supplied["constraints"]["data_destinations"] == ["controller"]
    assert all(
        expected["acceptance"][0] not in prompt
        for expected in spec["cases"][scenario]["expected_plan"]["tasks"]
    )


@pytest.mark.parametrize("scenario", ["legal_plan", "denied_tool"])
def test_probe_accepts_distinct_schema_valid_prose_but_rejects_missing_or_wrong_requirements(
    scenario: str,
) -> None:
    spec = probe_spec()
    first = parse_planning_output(
        json.dumps(spec["cases"][scenario]["expected_plan"]), version="v2"
    ).model_dump(mode="json")
    second = deepcopy(first)
    second["summary"] = "A differently worded bounded inline plan."
    second["tasks"][0]["acceptance"] = ["Describe the parser boundary in original wording."]
    assert _semantically_valid(first, spec, scenario)
    assert _semantically_valid(second, spec, scenario)
    missing = deepcopy(second)
    missing["tasks"] = missing["tasks"][:-1]
    assert not _semantically_valid(missing, spec, scenario)
    dependency = deepcopy(second)
    dependency["tasks"][2]["depends_on"] = []
    assert not _semantically_valid(dependency, spec, scenario)
    destination = deepcopy(second)
    destination["authorization"]["data_destinations"] = ["internet"]
    assert not _semantically_valid(destination, spec, scenario)


@pytest.mark.skipif(os.name != "posix", reason="Pinned Linux source material is required")
def test_suite_rechecks_the_actual_descriptor_before_a_new_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karajan.projects.go_commander_suite import FixedGoCommanderSuite

    runtime = os.environ.get("KARAJAN_OPENCODE_LINUX_BINARY")
    tokenizer = os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY")
    if runtime is None or tokenizer is None:
        pytest.skip("Pinned Linux source material is not configured")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("first", encoding="utf-8")
    digest = hashlib.sha256(descriptor.read_bytes()).hexdigest()
    suite = FixedGoCommanderSuite(
        Path(runtime), Path(tokenizer), digest, descriptor_path=descriptor
    )
    bound = {"registration": {"profile": {"id": "commander", "revision": 1}}}
    auth = {"generation": "synthetic", "source": {"id": "synthetic"}}
    suite.source(bound, auth)
    descriptor.write_text("second", encoding="utf-8")
    with pytest.raises(Exception, match="COMMANDER_SOURCE_CHANGED"):
        suite.source(bound, auth)
