# ruff: noqa: E501
"""C/P tests for the no-new-start Issue 107 recovery driver."""

import importlib.util
import json
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest

SOURCE = (
    Path(__file__).parents[2]
    / "examples/go-readonly-reviewer-qualification-20260907/qualification_recovery_driver.py"
)
CONSUMER_SOURCE = SOURCE.with_name("prepare_issue107_consumer.py")
PROJECT_TESTS = Path(__file__).parents[1] / "projects"
SPEC = importlib.util.spec_from_file_location("issue107_recovery", SOURCE)
assert SPEC is not None and SPEC.loader is not None
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def _competing_stage_writer(directory: str, status: str) -> str:
    """Subprocess boundary for the SQLite single-writer receipt test."""
    try:
        driver.ReceiptLedger(Path(directory)).write("positive_observed", {"status": status})
    except driver.RecoveryError:
        return "conflict"
    return "accepted"


class Store:
    def __init__(self, *, expires_at: float = 2000.0, status: str = "passed") -> None:
        self.start = {"id": "start", "qualification_id": "record", "expires_at": expires_at}
        self.record = {"id": "record", "status": status, "reason_codes": [], "revocation": None}
        self.starts = self.gets = self.revokes = 0

    def get_command_start(
        self, project_id: str, command_key: str, *, principal: str
    ) -> dict[str, Any]:
        self.starts += 1
        assert command_key == driver.COMMAND
        return dict(self.start)

    def get(self, project_id: str, observation_id: str, *, principal: str) -> dict[str, Any]:
        self.gets += 1
        return {"record": dict(self.record)}

    def revoke(
        self, project_id: str, observation_id: str, *, principal: str, reason: str
    ) -> dict[str, Any]:
        self.revokes += 1
        self.record["revocation"] = {"reason": reason}
        return {"reason": reason, "id": observation_id}


class NewStore(Store):
    def __init__(self) -> None:
        super().__init__()
        self.created = False

    def get_command_start(
        self, project_id: str, command_key: str, *, principal: str
    ) -> dict[str, Any]:
        self.starts += 1
        if not self.created:
            raise driver.RecoveryError("QUALIFICATION_START_NOT_FOUND")
        return dict(self.start)

    def qualify_runtime_tools(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.created = True
        self.start = {"id": "start", "qualification_id": "record", "expires_at": 2000.0}
        self.record = {"id": "record", "status": "passed", "reason_codes": [], "revocation": None}
        return dict(self.record)


def recovery(
    tmp_path: Path, store: Store, positive: Any = None, negative: Any = None, history: Any = None
) -> Any:
    return driver.QualificationRecovery(
        driver.ReceiptLedger(tmp_path / "receipts"),
        store,
        project_id="project",
        positive_history=(lambda: None) if history is None else history,
        positive=(lambda: {"state": "ready"}) if positive is None else positive,
        negative=(lambda: {"reason": "QUALIFICATION_REVOKED"}) if negative is None else negative,
        now=lambda: 1000.0,
    )


def test_resume_uses_only_original_store_records_and_persists_every_stage(tmp_path: Path) -> None:
    store = Store()
    result = recovery(tmp_path, store).resume()
    assert result["status"] == "completed"
    assert store.starts == 1 and store.gets == 2 and store.revokes == 1
    assert [path.stem for path in (tmp_path / "receipts").glob("*.json")] == [
        "complete",
        "negative_history_observed",
        "positive_observed",
        "preflight",
        "qualification_observed",
        "revoked_observed",
        "start_observed",
    ]


def test_expired_or_unknown_original_record_never_calls_consumer_or_revoke(tmp_path: Path) -> None:
    calls: list[str] = []
    expired = recovery(
        tmp_path / "expired",
        Store(expires_at=999.0),
        lambda: calls.append("positive"),
        lambda: calls.append("negative"),
    )
    assert expired.resume()["reason_code"] == "QUALIFICATION_EXPIRED"
    unknown = recovery(
        tmp_path / "unknown",
        Store(status="failed"),
        lambda: calls.append("positive"),
        lambda: calls.append("negative"),
    )
    assert unknown.resume()["reason_code"] == "QUALIFICATION_UNKNOWN"
    assert calls == []


def test_interruption_after_positive_reuses_its_durable_receipt_without_repeating_consumer(
    tmp_path: Path,
) -> None:
    store = Store()
    positive_calls = 0

    def positive() -> dict[str, Any]:
        nonlocal positive_calls
        positive_calls += 1
        return {"state": "ready"}

    original = store.revoke

    def interrupted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("injected sqlite failure")

    store.revoke = interrupted  # type: ignore[method-assign]
    first = recovery(tmp_path, store, positive=positive).resume()
    assert first["status"] == "unknown" and positive_calls == 1
    store.revoke = original  # type: ignore[method-assign]
    second = recovery(tmp_path, store, positive=positive).resume()
    assert second["status"] == "unknown" and positive_calls == 1
    assert store.revokes == 0


def test_revoke_reply_loss_recovers_the_original_store_receipt_without_a_second_revoke(
    tmp_path: Path,
) -> None:
    store = Store()

    def committed_then_lost(*args: Any, **kwargs: Any) -> dict[str, Any]:
        store.record["revocation"] = {"reason": "issue107-driver-post-positive"}
        raise OSError("injected reply loss after SQLite commit")

    store.revoke = committed_then_lost  # type: ignore[method-assign]
    result = recovery(tmp_path, store).resume()
    assert result["status"] == "completed"
    assert store.revokes == 0
    assert (
        json.loads((tmp_path / "receipts" / "revoked_observed.json").read_text())["status"]
        == "recovered"
    )


def test_revoked_record_with_persisted_positive_recovery_runs_only_negative_history(
    tmp_path: Path,
) -> None:
    store = Store()
    store.record["revocation"] = {"reason": "original-revoke"}
    calls: list[str] = []
    result = recovery(
        tmp_path,
        store,
        positive=lambda: calls.append("positive"),
        negative=lambda: calls.append("negative") or {"reason": "QUALIFICATION_REVOKED"},
        history=lambda: {"state": "ready", "transition": "original"},
    ).resume()
    assert result["status"] == "completed"
    assert calls == ["negative"]
    assert store.revokes == 0


def test_receipt_publication_failure_keeps_the_committed_sqlite_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = driver.ReceiptLedger(tmp_path / "receipts")
    monkeypatch.setattr(
        driver.os, "link", lambda source, destination: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(driver.RecoveryError, match="UNCLASSIFIED"):
        ledger.write("start_observed", {"status": "observed"})
    assert ledger.read("start_observed")["status"] == "observed"


def test_multiprocess_stage_race_preserves_the_first_committed_receipt(tmp_path: Path) -> None:
    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                _competing_stage_writer, [str(tmp_path / "receipts")] * 2, ["passed", "unknown"]
            )
        )
    assert sorted(outcomes) == ["accepted", "conflict"]
    receipt = driver.ReceiptLedger(tmp_path / "receipts").read("positive_observed")
    assert receipt is not None and receipt["status"] in {"passed", "unknown"}


def test_execute_prepares_before_the_one_qualification_call_and_then_reuses_resume(
    tmp_path: Path,
) -> None:
    store = NewStore()
    result = driver.execute(
        recovery(tmp_path, store),
        store,
        profile_ref={"id": "reviewer", "revision": 1},
        suite_ref={"id": "suite", "revision": 1},
        source=lambda: {"origin": "fixture"},
        prepare_fixture=lambda: {"fixture": "prepared"},
    )
    assert result["status"] == "completed"
    assert (tmp_path / "receipts" / "execute_preflight.json").exists()
    assert (tmp_path / "receipts" / "execute_start.json").exists()


def test_qualification_reply_loss_recovers_the_fixed_start_without_a_second_call(
    tmp_path: Path,
) -> None:
    class LostReplyStore(NewStore):
        def qualify_runtime_tools(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            super().qualify_runtime_tools(*args, **kwargs)
            raise OSError("injected reply loss after Store commit")

    store = LostReplyStore()
    result = driver.execute(
        recovery(tmp_path, store),
        store,
        profile_ref={"id": "reviewer", "revision": 1},
        suite_ref={"id": "suite", "revision": 1},
        source=lambda: {"origin": "fixture"},
        prepare_fixture=lambda: {"fixture": "prepared"},
    )
    assert result["status"] == "completed"
    assert store.created is True
    assert store.starts == 2


def test_original_start_and_record_can_be_reopened_from_real_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "qualification.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE facts (kind TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO facts VALUES (?, ?)",
            (
                "start",
                json.dumps({"id": "start", "qualification_id": "record", "expires_at": 999.0}),
            ),
        )
        connection.execute(
            "INSERT INTO facts VALUES (?, ?)",
            (
                "record",
                json.dumps(
                    {"id": "record", "status": "passed", "reason_codes": [], "revocation": None}
                ),
            ),
        )

    class ReadOnlyStore:
        def _read(self, kind: str) -> dict[str, Any]:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                row = connection.execute("SELECT value FROM facts WHERE kind=?", (kind,)).fetchone()
            assert row is not None
            return json.loads(row[0])

        def get_command_start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return self._read("start")

        def get(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"record": self._read("record")}

        def revoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("expired recovery cannot revoke")

    result = recovery(tmp_path, ReadOnlyStore()).resume()
    assert result["reason_code"] == "QUALIFICATION_EXPIRED"


def test_actual_store_views_use_bound_start_expiry_and_separate_revoke_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qualification.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE facts (kind TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO facts VALUES (?, ?)",
            (
                "start",
                json.dumps(
                    {
                        "id": "record",
                        "binding": {
                            "execution_start": {"qualification_id": "record", "expires_at": 2000.0}
                        },
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO facts VALUES (?, ?)",
            ("record", json.dumps({"id": "record", "status": "passed", "reason_codes": []})),
        )
        connection.execute(
            "INSERT INTO facts VALUES (?, ?)", ("revocation", json.dumps({"reason": "retained"}))
        )

    class ActualStore:
        def _read(self, kind: str) -> dict[str, Any]:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                row = connection.execute("SELECT value FROM facts WHERE kind=?", (kind,)).fetchone()
            assert row is not None
            return json.loads(row[0])

        def get_command_start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return self._read("start")

        def get(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"record": self._read("record"), "revocation": self._read("revocation")}

        def revoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("persisted revocation must be recovered, not repeated")

    calls: list[str] = []
    result = recovery(
        tmp_path,
        ActualStore(),
        positive=lambda: calls.append("positive"),
        negative=lambda: calls.append("negative"),
        history=lambda: {"state": "ready", "membership_only": True},
    ).resume()
    assert result["status"] == "completed"
    assert calls == ["negative"]


def test_positive_history_reads_a_real_admission_receipt_without_fixture_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "consumer-fixture"
    fixture.mkdir()
    with sqlite3.connect(fixture / "admission.sqlite") as connection:
        connection.execute("CREATE TABLE operations (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO operations VALUES (?, ?)",
            (
                "issue107-fixed-consumer-operation",
                json.dumps(
                    {
                        "state": "controller_fixture_only",
                        "validation": {
                            "review_binding_status": {
                                "state": "ready",
                                "assessment": {"actual_reviewer_attempt": None},
                            },
                            "subject_transition": {
                                "phase": "ready",
                                "receipt": {"id": "candidate"},
                                "binding": {
                                    "reviewer_sources": [
                                        {
                                            "reviewer": {
                                                "qualification_ref": "fixed-go-qualification:record"
                                            }
                                        }
                                    ]
                                },
                            },
                        },
                    }
                ),
            ),
        )
    monkeypatch.syspath_prepend(str(CONSUMER_SOURCE.parent))
    consumer_spec = importlib.util.spec_from_file_location(
        "issue107_consumer_history", CONSUMER_SOURCE
    )
    assert consumer_spec is not None and consumer_spec.loader is not None
    consumer = importlib.util.module_from_spec(consumer_spec)
    consumer_spec.loader.exec_module(consumer)
    assert consumer.positive_history(tmp_path) == {
        "state": "ready",
        "transition": {
            "phase": "ready",
            "receipt": {"id": "candidate"},
            "binding": {
                "reviewer_sources": [
                    {"reviewer": {"qualification_ref": "fixed-go-qualification:record"}}
                ]
            },
        },
        "membership_only": True,
        "actual_reviewer_attempt": None,
        "qualification_ref": "fixed-go-qualification:record",
    }


def test_real_profile_store_revoke_commit_reply_loss_is_read_back_without_repeating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public SQLite Store, not a mock, is the lost-reply authority."""
    monkeypatch.syspath_prepend(str(PROJECT_TESTS))
    from test_qualification_store import case as case_fixture
    from test_qualification_store import qualify

    actual_case = case_fixture.__wrapped__(tmp_path)
    qualify(actual_case, key=driver.COMMAND)
    actual_store = actual_case["projects"]

    class LostReplyStore:
        def _store(self) -> Any:
            from karajan.projects.qualification import ProfileQualificationStore

            return ProfileQualificationStore(actual_store, clock=lambda: actual_case["clock"][0])

        def get_command_start(
            self, project_id: str, command_key: str, *, principal: str
        ) -> dict[str, Any]:
            return self._store().get_command_start(project_id, command_key, principal="owner")

        def get(self, project_id: str, observation_id: str, *, principal: str) -> dict[str, Any]:
            return self._store().get(project_id, observation_id, principal="owner")

        def revoke(
            self, project_id: str, observation_id: str, *, principal: str, reason: str
        ) -> dict[str, Any]:
            self._store().revoke(project_id, observation_id, principal="owner", reason=reason)
            raise OSError("injected reply loss after ProfileQualificationStore commit")

    result = driver.QualificationRecovery(
        driver.ReceiptLedger(tmp_path / "recovery" / "receipts"),
        LostReplyStore(),
        project_id=actual_case["project_id"],
        positive_history=lambda: None,
        positive=lambda: {"state": "ready"},
        negative=lambda: {"reason": "QUALIFICATION_REVOKED"},
        now=lambda: actual_case["clock"][0],
    ).resume()
    assert result["status"] == "completed"
    start = LostReplyStore().get_command_start(
        actual_case["project_id"], driver.COMMAND, principal="ignored"
    )
    persisted = LostReplyStore().get(actual_case["project_id"], start["id"], principal="ignored")
    assert persisted["revocation"]["reason"] == "issue107-driver-post-positive"
