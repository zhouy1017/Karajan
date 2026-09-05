"""Temporary negative CI exercise for issue #10; removed before merge."""


def test_quality_gate_rejects_a_failing_test() -> None:
    raise AssertionError("Intentional #10 gate exercise; this commit must not be merged.")
