"""Future bounded #107 runner with an explicit qualification→binding→revoke order.

``execute`` is intentionally not run by this artifact's preparation step.  It
is a single-start command for a separately authorized future run.  No endpoint,
prompt, client, credential value, or alternate provider can be supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

from karajan.projects.qualification import QualificationError

import prepare_issue107_consumer as consumer
import run_official_issue107 as controller


COMMAND = "issue107-official-go-reviewer-20260907-ordered-attempt4"


def sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan() -> dict[str, Any]:
    """The no-effect, statically checkable order for one future authorized start."""
    return {
        "schema_version": "karajan.issue107-ordered-driver-plan.v1",
        "command": COMMAND,
        "effect_free": True,
        "phases": [
            {"id": "qualification", "call": "ProfileQualificationStore.qualify_runtime_tools",
             "provider_effect": "fixed official three-scenario suite only", "assertions": [
                 "passed record", "three scenarios", "each request_count <= 6", "total <= 18",
             ]},
            {"id": "replay", "call": "qualify_runtime_tools with same command",
             "provider_effect": "none", "assertions": ["same record", "zero new journal requests"]},
            {"id": "positive_binding", "call": "ApprovedReviewerBindings.advance twice",
             "provider_effect": "none", "assertions": [
                 "prepared then ready", "internal current_locked consumed current Store facts",
                 "membership_only", "actual_reviewer_attempt is null",
             ]},
            {"id": "revoke", "call": "ProfileQualificationStore.revoke",
             "provider_effect": "none", "assertions": ["qualification record is revoked only after positive binding"]},
            {"id": "negative_and_history", "call": "ApprovedReviewerBindings.advance; get; same-key replay",
             "provider_effect": "none", "assertions": [
                 "QUALIFICATION_REVOKED", "historical record readable", "same key has zero new requests",
             ]},
        ],
        "separation": {
            "suite_grant_cleanup": "owned by FixedGoReviewerSuite during qualification",
            "qualification_record_revoke": "this driver invokes it only after positive_binding",
        },
    }


def verify_static_order() -> dict[str, int]:
    """No-effect rehearsal: reject a source edit that reorders the critical calls."""
    source = inspect.getsource(execute)
    names = [
        "record = store.qualify_runtime_tools(",
        "replay = store.qualify_runtime_tools(",
        "positive = consumer.positive_result(private_root)",
        "revocation = store.revoke(",
        "consumer.negative(private_root, negative_path)",
        "replay_after_revoke = store.qualify_runtime_tools(",
    ]
    positions = {name: source.index(name) for name in names}
    if list(positions.values()) != sorted(positions.values()):
        raise RuntimeError("ISSUE107_ORDERED_DRIVER_SEQUENCE_INVALID")
    return positions


def _counts(start: dict[str, Any], journal: Any) -> dict[str, int]:
    result = {}
    for row in start["binding"]["execution_start"]["scenarios"]:
        result[hashlib.sha256(row["grant_id"].encode()).hexdigest()] = journal.snapshot(row["grant_id"])["request_count"]
    return result


def execute(private_root: Path, report: Path) -> None:
    """One future run. Any failure stops here; it never starts another command."""
    store, project_id, reviewer, journal = controller.open_controller(private_root)
    try:
        store.get_command_start(project_id, COMMAND, principal=controller.PRINCIPAL)
    except QualificationError as error:
        if error.code != "QUALIFICATION_START_NOT_FOUND":
            raise
    else:
        raise RuntimeError("ISSUE107_ORDERED_COMMAND_ALREADY_EXISTS")

    source = store.reviewer_suite.source()
    record = store.qualify_runtime_tools(
        project_id, {"id": reviewer["id"], "revision": reviewer["revision"]},
        principal=controller.PRINCIPAL, command_key=COMMAND, suite_ref=controller.SUITE,
        validity_seconds=600,
    )
    start = store.get_command_start(project_id, COMMAND, principal=controller.PRINCIPAL)
    counts_before = _counts(start, journal)
    if (
        record["status"] != "passed"
        or len(record["observation"].get("scenarios", [])) != 3
        or any(count > 6 for count in counts_before.values())
        or sum(counts_before.values()) > 18
    ):
        raise RuntimeError("ISSUE107_ORDERED_QUALIFICATION_INCOMPLETE")

    replay = store.qualify_runtime_tools(
        project_id, {"id": reviewer["id"], "revision": reviewer["revision"]},
        principal=controller.PRINCIPAL, command_key=COMMAND, suite_ref=controller.SUITE,
        validity_seconds=600,
    )
    counts_after_replay = _counts(start, journal)
    if replay != record or counts_after_replay != counts_before:
        raise RuntimeError("ISSUE107_ORDERED_REPLAY_EFFECT")

    # This is the required real consumer positive control. It has no model port.
    positive = consumer.positive_result(private_root)
    revocation = store.revoke(project_id, record["id"], principal=controller.PRINCIPAL,
                              reason="issue107-ordered-post-positive-revoke")
    negative_path = report.with_name(report.stem + "-consumer-negative.json")
    consumer.negative(private_root, negative_path)
    negative = json.loads(negative_path.read_text(encoding="utf-8"))
    if not negative["expected_revoke_reason_observed"]:
        raise RuntimeError("ISSUE107_ORDERED_REVOKE_NEGATIVE_MISSING")
    historical = store.get(project_id, record["id"], principal=controller.PRINCIPAL)
    replay_after_revoke = store.qualify_runtime_tools(
        project_id, {"id": reviewer["id"], "revision": reviewer["revision"]},
        principal=controller.PRINCIPAL, command_key=COMMAND, suite_ref=controller.SUITE,
        validity_seconds=600,
    )
    counts_final = _counts(start, journal)
    if historical["record"] != record or replay_after_revoke != record or counts_final != counts_before:
        raise RuntimeError("ISSUE107_ORDERED_HISTORY_OR_REPLAY_CHANGED")
    report.write_text(json.dumps({
        "schema_version": "karajan.issue107-ordered-driver-evidence.v1", "command": COMMAND,
        "source_digest": controller.digest(source), "record_sha256": sha(record),
        "counts": {"after_qualification": counts_before, "after_same_key_replay": counts_after_replay,
                   "after_revoke_replay": counts_final},
        "positive_binding": positive,
        "revocation": {"reason": revocation["reason"], "history_readable": historical["record"] == record},
        "negative": {"expected_revoke_reason_observed": negative["expected_revoke_reason_observed"],
                     "reason_codes": negative["result"]["reason_codes"]},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("dry-run", "execute"))
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "dry-run":
        document = plan()
        document["static_order_assertions"] = verify_static_order()
        args.report.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        execute(args.private_root, args.report)


if __name__ == "__main__":
    main()
