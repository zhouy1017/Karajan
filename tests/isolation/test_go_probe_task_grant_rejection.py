"""Task and metered v2 grants cannot enter the legacy fixed observer."""

from pathlib import Path

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation.go_probe import observe_go_tools


@pytest.mark.parametrize("scenario", ["edit", "denied_read"])
@pytest.mark.parametrize("present_as_qualification", [False, True])
@pytest.mark.parametrize("grant_kind", ["task", "qualification_v2"])
def test_task_grant_rejected_before_runtime_files_or_send_authority_are_touched(
    tmp_path: Path, scenario: str, present_as_qualification: bool, grant_kind: str
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    binding = {
        "subject": {
            "kind": "task_attempt",
            "project_id": "project-1",
            "run_id": "run-1",
            "task_id": "task-1",
        },
        "attempt_id": "attempt-1",
        "fence": 1,
        "approval_digest": "a" * 64,
        "execution_policy_digest": "b" * 64,
        "workspace_digest": "c" * 64,
        "authentication_source_digest": "d" * 64,
        "profile_digest": "e" * 64,
        "runtime_digest": "f" * 64,
        "channel": "go-channel",
        "model": "glm-5.3-flash",
        "auth_generation": "generation-1",
        "expires_at": 2000.0,
        "max_requests": 6,
    }
    if grant_kind == "qualification_v2":
        for key in (
            "subject",
            "approval_digest",
            "execution_policy_digest",
            "workspace_digest",
            "authentication_source_digest",
        ):
            binding.pop(key)
        binding.update(
            {
                "qualification_id": "qualification-v2",
                "schema_version": "karajan.go-qualification-grant.v2",
                "probe_spec_digest": "a" * 64,
                "scenario": scenario,
                "context": {
                    "source_sha256": "b" * 64,
                    "approved_input_tokens": 8192,
                    "reserved_output_tokens": 4096,
                    "operating_context_tokens": 16384,
                    "fixed_margin": 100,
                    "ratio_margin_basis_points": 1000,
                },
            }
        )
    grant = journal.create_grant(binding, grant_id="task-grant")
    before = journal.snapshot("task-grant")
    presented = dict(binding)
    if present_as_qualification:
        for key in (
            "subject",
            "approval_digest",
            "execution_policy_digest",
            "workspace_digest",
            "authentication_source_digest",
            "schema_version",
            "probe_spec_digest",
            "scenario",
            "context",
        ):
            presented.pop(key, None)
        presented["qualification_id"] = "unrelated-qualification"
    authorization = GoRelayAuthorization(journal, "task-grant", presented, grant["capability"])
    directory = tmp_path / "uncreated-runtime-directory"
    with pytest.raises(ValueError, match="^QUALIFICATION_GRANT_REQUIRED$"):
        observe_go_tools(
            tmp_path / "missing-native-artifact",
            directory,
            "synthetic-unused-credential",
            authorization,
            scenario=scenario,
        )
    assert not directory.exists()
    assert journal.snapshot("task-grant") == before
