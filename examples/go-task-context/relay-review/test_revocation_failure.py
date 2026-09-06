"""Additional independent fault boundary; real HTTP/SQLite, synthetic credentials."""

from test_go_relay import answer, post
from test_independent_context_relay import accounting, artifacts, metered

__all__ = ["accounting", "artifacts"]


def test_failed_revocation_never_allows_the_same_relay_to_send_again(
    tmp_path, accounting, monkeypatch
):
    def missing_usage(request, journal):
        return answer()

    with metered(tmp_path, accounting, missing_usage) as (relay, journal, sent):
        original = journal.revoke_grant
        attempts = []

        def temporary_revocation_failure(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("synthetic one-off revocation storage failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(journal, "revoke_grant", temporary_revocation_failure)
        assert post(relay).status_code == 502
        assert len(sent) == 1
        post(relay)
        assert relay.close()["status"] == "closed"
        assert len(sent) == 1, journal.snapshot("task")
