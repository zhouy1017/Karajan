"""Independent public guard tests; SyntheticSuite is not real Go qualification.

The Run, approval, credentials, qualification Store, estimates and reservations
are real local stores. Only native qualification and planning admission readers
are explicit test substitutes imported from their public fixture helpers.
"""

import os

import pytest
from test_projected_go_routing import approved_task
from test_projected_qualification_store import case as case
from test_projected_qualification_store import projected as projected
from test_projected_qualification_store import qualify


@pytest.mark.parametrize("change", ["latest_failed", "latest_unknown", "material_changed"])
def test_reserved_guard_never_reuses_stale_pass_after_current_evidence_fails(
    projected, tmp_path, change
):
    admission, routing, run, original = approved_task(projected, tmp_path)
    queued = admission.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    reserved = admission.advance(run["id"], queued["id"], principal="owner")
    assert reserved["state"] == "reserved"
    before = routing.capacity.snapshot()

    if change == "latest_failed":

        def fail(_start, result):
            result["validation"]["candidate_capture"] = "not_run"

        projected["suite"].after = fail
        latest = qualify(projected, "newest-failed")
        assert latest["status"] == "failed"
        expected = "QUALIFICATION_NOT_PASSED"
    elif change == "latest_unknown":

        def stop(_start):
            raise KeyboardInterrupt("synthetic controller interruption")

        projected["suite"].hook = stop
        with pytest.raises(KeyboardInterrupt):
            qualify(projected, "newest-started")
        expected = "QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"
    else:
        secret = projected["secret"]
        original_stat = secret.stat()
        original_size = original_stat.st_size
        secret.write_bytes(b"x" * original_size)
        os.utime(secret, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert secret.stat().st_mtime_ns == original_stat.st_mtime_ns
        assert secret.stat().st_size == original_size
        expected = "AUTHENTICATION_SOURCE_MISMATCH"

    calls = projected["suite"].calls
    # Historical successful receipt remains immutable, but is not fresh authority.
    assert qualify(projected) == original
    for _ in range(2):
        with routing.reserved_execution_guard(
            run["id"], reserved["assessment"]["id"], principal="owner"
        ) as result:
            assert result["state"] == "blocked"
            assert result["route"]["selected_profile"] is None
            assert result["activation_allowed"] is False
            assert result["dispatch_enabled"] is False
            source = result["sources"]["profiles"][0]
            assert source["qualification"] is None
            assert expected in source["reason_codes"]
    assert routing.capacity.snapshot() == before
    assert len(before["reservations"]) == 1
    assert projected["suite"].calls == calls
