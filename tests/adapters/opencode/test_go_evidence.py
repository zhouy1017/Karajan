from copy import deepcopy

import pytest
from karajan.adapters.opencode.go_evidence import check_fixture, evaluate_observation


def good_record():
    return {
        "scenario": "edit",
        "runtime_version": "1.18.29",
        "configuration_accepted": True,
        "workspace_root_matches": True,
        "effective_model": "opencode-go/glm-5.3-flash",
        "effective_enabled_providers": ["opencode-go"],
        "assistant_messages": [
            {
                "modelID": "glm-5.3-flash",
                "providerID": "opencode-go",
                "finish": "stop",
                "completed": True,
                "error_type": None,
            }
        ],
        "provider_requests": [{"protocol_passed": True, "upstream_status": 200}],
        "session_error_names": [],
        "tool_results": [
            {"tool": "read", "status": "completed", "path": "fixture.py"},
            {"tool": "edit", "status": "completed", "path": "fixture.py"},
        ],
        "workspace_files": ["blocked.txt", "fixture.py"],
        "blocked_file_unchanged": True,
        "fixture_file_changed": True,
        "function_cases_passed": [True, True, True, True],
        "process_cleanup": {"status": "exited", "errors": []},
        "relay_cleanup": {"status": "closed", "errors": []},
        "credential_scan": {"completed": True, "leak_files": [], "errors": []},
    }


def test_edit_requires_actual_tools_and_function_results():
    assert evaluate_observation(good_record()) == []
    record = good_record()
    record["tool_results"] = []
    assert "EDIT_TOOL_EVIDENCE_MISSING" in evaluate_observation(record)


def test_denied_read_requires_permission_semantics_without_requiring_function_fix():
    record = good_record()
    record.update(
        scenario="denied_read",
        fixture_file_changed=False,
        function_cases_passed=None,
        tool_results=[
            {
                "tool": "read",
                "status": "error",
                "path": "blocked.txt",
                "error_category": "permission_denied_by_rule",
            }
        ],
    )
    assert evaluate_observation(record) == []
    record["tool_results"][0]["error_category"] = "other_tool_error"
    assert "PERMISSION_DENIAL_NOT_OBSERVED" in evaluate_observation(record)


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"timed_out": True}, "NATIVE_EXECUTION_INCOMPLETE"),
        ({"assistant_messages": []}, "NATIVE_EXECUTION_INCOMPLETE"),
        ({"provider_requests": []}, "PROVIDER_PROTOCOL_INCOMPLETE"),
        ({"process_cleanup": {"status": "unknown", "errors": []}}, "CLEANUP_INCOMPLETE"),
        ({"credential_scan": {"completed": False}}, "CREDENTIAL_SCAN_FAILED"),
        ({"effective_model": "other/model"}, "CONFIGURATION_MISMATCH"),
        ({"workspace_files": ["blocked.txt", "fixture.py", "extra.py"]}, "WORKSPACE_CHANGED"),
    ],
)
def test_incomplete_observations_cannot_pass(changes, reason):
    record = deepcopy(good_record())
    record.update(changes)
    assert reason in evaluate_observation(record)


def test_fixture_is_interpreted_without_executing_model_code():
    assert check_fixture(
        "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
    ) == [True, True, True, True]
    assert check_fixture(
        "def clamp(value, low, high):\n    return min(low, max(value, high))\n"
    ) != [True, True, True, True]
    assert check_fixture("import os\nos.system('anything')") is None
    assert (
        check_fixture("def clamp(value=print('side effect'), low=0, high=1):\n return value")
        is None
    )
    assert (
        check_fixture(
            "def clamp[T: unexpected_callable()](value, low, high):\n"
            "    return max(low, min(value, high))\n"
        )
        is None
    )
