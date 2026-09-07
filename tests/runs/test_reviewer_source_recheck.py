"""C coverage for Reviewer source observations after a Capacity lock wait."""

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import pytest
from karajan.runs import RunError
from test_reviewer_binding import _activate_reviewer_reservation, _passed_reviewer_subject

pytest_plugins = ["test_reviewer_binding"]


def test_reviewer_source_generation_changed_while_waiting_blocks_reservation(
    binding_case, monkeypatch
) -> None:
    """The held Project reader rejects changed source facts before Capacity writes."""
    _, qualification, intents, (run_id, _), _, _, _ = binding_case
    _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="source-after-capacity-wait"
    )
    capacity = intents.admissions.routing.capacity
    original = capacity.admit

    def admit_after_source_replacement(
        request: dict[str, Any],
        *,
        command_key: str,
        before_reserve: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_reservation_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def source_replaced_under_capacity_lock() -> None:
            qualification.generation += 1
            assert before_reserve is not None
            before_reserve()

        return original(
            request,
            command_key=command_key,
            before_reserve=source_replaced_under_capacity_lock,
            after_capacity_facts=after_capacity_facts,
            before_reservation_write=before_reservation_write,
        )

    monkeypatch.setattr(capacity, "admit", admit_after_source_replacement)
    before = capacity.snapshot()
    blocked = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["REVIEWER_QUALIFICATION_SOURCE_CHANGED"]
    assert capacity.snapshot() == before


def test_reviewer_source_generation_changed_while_waiting_blocks_effect_body(
    binding_case, monkeypatch
) -> None:
    """The same reader reruns under the effect's held Capacity transaction."""
    _, qualification, intents, (run_id, _), _, _, _ = binding_case
    _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="source-before-effect"
        )["id"],
        principal="owner",
    )
    assert reviewer["state"] == "reserved", reviewer["reason_codes"]
    _activate_reviewer_reservation(intents, run_id, reviewer)
    capacity = intents.admissions.routing.capacity
    original = capacity.pre_effect_guard

    @contextmanager
    def source_replaced_before_effect(
        admission_id: str,
        *,
        expected_request: dict[str, Any],
        before_effect: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_effect_yield: Callable[[], None] | None = None,
    ) -> Any:
        def source_replaced_under_capacity_lock() -> None:
            qualification.generation += 1
            assert before_effect is not None
            before_effect()

        with original(
            admission_id,
            expected_request=expected_request,
            before_effect=source_replaced_under_capacity_lock,
            after_capacity_facts=after_capacity_facts,
            before_effect_yield=before_effect_yield,
        ) as held:
            yield held

    monkeypatch.setattr(capacity, "pre_effect_guard", source_replaced_before_effect)
    before = capacity.snapshot()
    with pytest.raises(RunError, match="^REVIEWER_QUALIFICATION_SOURCE_CHANGED$"):
        with intents.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer["id"], principal="owner"
        ):
            pytest.fail("changed Reviewer source reached the effect body")
    assert capacity.snapshot() == before


def test_reviewer_unchanged_material_recheck_allows_effect_body(
    binding_case, projected, monkeypatch
) -> None:
    """An unchanged material-sealed C source reaches the active effect body."""
    _, _, intents, (run_id, _), _, _, _ = binding_case
    _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="unchanged-source-effect"
        )["id"],
        principal="owner",
    )
    assert reviewer["state"] == "reserved", reviewer["reason_codes"]
    _activate_reviewer_reservation(intents, run_id, reviewer)
    credentials = projected["credentials"]
    original_read = credentials._read
    reads = 0

    def count_actual_material_reads(source: Any) -> tuple[bytes, str]:
        nonlocal reads
        reads += 1
        return original_read(source)

    monkeypatch.setattr(credentials, "_read", count_actual_material_reads)
    material_before = projected["secret"].read_bytes()
    with intents.admissions.reviewer_reserved_effect_guard(
        run_id, reviewer["id"], principal="owner"
    ) as held:
        assert held["capacity"]["admission_id"] == reviewer["capacity_receipt"]["admission_id"]
    assert reads >= 1
    assert projected["secret"].read_bytes() == material_before


def _qualification_rows(
    projected: dict[str, Any],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The material read must not manufacture a revised qualification record."""
    import sqlite3

    with sqlite3.connect(projected["projects"].database) as db:
        starts = db.execute(
            "SELECT id,binding FROM profile_qualification_starts ORDER BY id"
        ).fetchall()
        records = db.execute(
            "SELECT id,record FROM profile_qualification_records ORDER BY id"
        ).fetchall()
    return starts, records


def test_reviewer_material_bytes_changed_under_capacity_lock_blocks_reservation(
    binding_case, projected, monkeypatch
) -> None:
    """A real sealed credential file replacement cannot create a reservation."""
    _, _, intents, (run_id, _), _, _, _ = binding_case
    _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="material-after-capacity-wait"
    )
    capacity = intents.admissions.routing.capacity
    original = capacity.admit
    before_rows = _qualification_rows(projected)
    before_generation = projected["credentials"].get(
        projected["project_id"],
        "secret:go",
        projected["generation"]["generation"],
        principal="owner",
    )
    material = projected["secret"]
    original_bytes = material.read_bytes()
    replacement = b"replacement-material-is-valid-but-not-sealed"
    assert replacement != original_bytes

    def admit_after_material_replacement(
        request: dict[str, Any],
        *,
        command_key: str,
        before_reserve: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_reservation_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def replace_material_under_capacity_lock() -> None:
            material.write_bytes(replacement)
            assert before_reserve is not None
            before_reserve()

        return original(
            request,
            command_key=command_key,
            before_reserve=replace_material_under_capacity_lock,
            after_capacity_facts=after_capacity_facts,
            before_reservation_write=before_reservation_write,
        )

    monkeypatch.setattr(capacity, "admit", admit_after_material_replacement)
    before = capacity.snapshot()
    blocked = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["REVIEWER_QUALIFICATION_SOURCE_CHANGED"]
    assert capacity.snapshot() == before
    assert _qualification_rows(projected) == before_rows
    assert (
        projected["credentials"].get(
            projected["project_id"],
            "secret:go",
            projected["generation"]["generation"],
            principal="owner",
        )
        == before_generation
    )


def test_reviewer_material_bytes_changed_under_capacity_lock_blocks_effect_body(
    binding_case, projected, monkeypatch
) -> None:
    """A real sealed credential replacement cannot reach an active effect body."""
    _, _, intents, (run_id, _), _, _, _ = binding_case
    _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="material-before-effect"
        )["id"],
        principal="owner",
    )
    assert reviewer["state"] == "reserved", reviewer["reason_codes"]
    _activate_reviewer_reservation(intents, run_id, reviewer)
    capacity = intents.admissions.routing.capacity
    original = capacity.pre_effect_guard
    before_rows = _qualification_rows(projected)
    material = projected["secret"]
    original_bytes = material.read_bytes()
    replacement = b"effect-replacement-material-is-valid-but-not-sealed"
    assert replacement != original_bytes

    @contextmanager
    def material_replaced_before_effect(
        admission_id: str,
        *,
        expected_request: dict[str, Any],
        before_effect: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_effect_yield: Callable[[], None] | None = None,
    ) -> Any:
        def replace_material_under_capacity_lock() -> None:
            material.write_bytes(replacement)
            assert before_effect is not None
            before_effect()

        with original(
            admission_id,
            expected_request=expected_request,
            before_effect=replace_material_under_capacity_lock,
            after_capacity_facts=after_capacity_facts,
            before_effect_yield=before_effect_yield,
        ) as held:
            yield held

    monkeypatch.setattr(capacity, "pre_effect_guard", material_replaced_before_effect)
    before = capacity.snapshot()
    with pytest.raises(RunError, match="^REVIEWER_QUALIFICATION_SOURCE_CHANGED$"):
        with intents.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer["id"], principal="owner"
        ):
            pytest.fail("replaced material reached the Reviewer effect body")
    assert capacity.snapshot() == before
    assert _qualification_rows(projected) == before_rows
