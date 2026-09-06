"""A fixed diagnostic cannot spend a previous grant again after a lost run."""

import pytest
from karajan.isolation.go_probe import observe_go_tools
from test_go_relay_journal import authorization


@pytest.mark.parametrize("history", ["sent", "revoked", "binding-mismatch"])
def test_stale_grant_is_rejected_before_directory_creation(tmp_path, history):
    auth = authorization(tmp_path)
    if history == "sent":
        auth.journal.begin_call("grant", "lost", capability=auth.capability, binding=auth.binding)
    elif history == "revoked":
        auth.journal.revoke_grant("grant")
    else:
        auth.binding["attempt_id"] = "different-attempt"
    destination = tmp_path / "probe"
    with pytest.raises(ValueError, match="FRESH_ACTIVE_GRANT_REQUIRED"):
        observe_go_tools(
            tmp_path / "not-needed", destination, "synthetic-secret", auth, scenario="edit"
        )
    assert not destination.exists()
    assert auth.journal.snapshot("grant")["request_count"] == (1 if history == "sent" else 0)
