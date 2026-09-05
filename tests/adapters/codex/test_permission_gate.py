"""Public PermissionGate boundary; no internal state inspection."""

import json
import unittest
from pathlib import Path

from karajan.adapters.codex import PermissionGate
from karajan.adapters.codex.models import DecisionStep, NativeStep, ReplayDocument

ROOT = Path(__file__).resolve().parents[3]


class PermissionGateTests(unittest.TestCase):
    def test_missing_native_request_identity_returns_a_refusal(self) -> None:
        fixture = ReplayDocument.model_validate_json(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )
        gate = PermissionGate(fixture.attempt, fixture.authorization)

        outcome = gate.register({}, expires_at=None, now=fixture.steps[0].at)

        self.assertEqual(outcome["reason"], "NATIVE_REQUEST_INVALID")
        self.assertNotIn("response", outcome)

    def test_registered_request_is_a_snapshot_when_the_caller_mutates_its_copy(self) -> None:
        fixture = ReplayDocument.model_validate_json(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )
        raw = json.loads(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )
        gate = PermissionGate(fixture.attempt, fixture.authorization)
        native = fixture.steps[2]
        request = raw["steps"][2]["message"]
        gate.register(request, expires_at=native.expires_at, now=native.at)
        request["id"] = 999

        responses = gate.invalidate()

        self.assertEqual(responses, [{"id": 301, "result": {"decision": "cancel"}}])

    def test_decision_cannot_predate_the_registered_challenge(self) -> None:
        fixture = ReplayDocument.model_validate_json(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )
        native = fixture.steps[2]
        decision = fixture.steps[3]
        assert isinstance(native, NativeStep)
        assert isinstance(decision, DecisionStep)
        gate = PermissionGate(fixture.attempt, fixture.authorization)
        gate.register(native.message, expires_at=native.expires_at, now=native.at)

        outcome = gate.decide(decision.decision, now=fixture.steps[0].at)

        self.assertEqual(outcome["reason"], "EVENT_TIME_REVERSED")
        self.assertEqual(outcome["response"]["result"], {"decision": "cancel"})

    def test_native_cancel_closes_the_gate_for_later_requests(self) -> None:
        fixture = ReplayDocument.model_validate_json(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )
        native = fixture.steps[2]
        decision = fixture.steps[3]
        assert isinstance(native, NativeStep)
        assert isinstance(decision, DecisionStep)
        gate = PermissionGate(fixture.attempt, fixture.authorization)
        gate.register(native.message, expires_at=native.expires_at, now=native.at)
        cancel = decision.decision.model_copy(update={"decision": "cancel"})
        gate.decide(cancel, now=decision.at)

        outcome = gate.register(native.message, expires_at=native.expires_at, now=decision.at)

        self.assertEqual(outcome["reason"], "ATTEMPT_INACTIVE")

    def test_rejected_wide_decision_closes_the_gate_when_it_returns_native_cancel(self) -> None:
        fixture = ReplayDocument.model_validate_json(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )
        native = fixture.steps[2]
        decision = fixture.steps[3]
        assert isinstance(native, NativeStep)
        assert isinstance(decision, DecisionStep)
        gate = PermissionGate(fixture.attempt, fixture.authorization)
        gate.register(native.message, expires_at=native.expires_at, now=native.at)
        wide = decision.decision.model_copy(update={"decision": "acceptForSession"})
        gate.decide(wide, now=decision.at)

        outcome = gate.register(native.message, expires_at=native.expires_at, now=decision.at)

        self.assertEqual(outcome["reason"], "ATTEMPT_INACTIVE")
