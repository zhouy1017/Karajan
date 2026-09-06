"""Independent admission fixture: real stores; explicitly synthetic qualification only."""

import json
from contextlib import contextmanager
from pathlib import Path

from admission_spec_sources import approval, estimate, observe, seeded
from karajan.capacity import CapacityStore
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.routing.compiler import digest
from karajan.runs import RunPlanner

__all__ = ["approval", "observe"]


class IndependentQualificationDouble(ProfileQualificationStore):
    """No model is qualified; retain real project guard while supplying test facts."""

    @contextmanager
    def routing_facts_guard(self, project_id, registrations, **kwargs):
        with super().routing_facts_guard(project_id, registrations, **kwargs) as view:
            for registration, row in zip(registrations, view["profiles"], strict=True):
                profile = registration["profile"]
                row["reason_codes"] = []
                row["qualification"] = {
                    "qualification_scope": "independent_spec_test_double",
                    "dispatch_eligible": False,
                    "capability_evidence": [
                        {
                            "capability": capability,
                            "status": "passed",
                            "profile_digest": digest(profile),
                            "runtime_version": profile["binding"]["runtime_version"],
                            "evidence_ref": "spec-double:" + capability,
                            "provenance": "fixture",
                        }
                        for capability in (
                            "bounded_implementation",
                            "candidate_capture",
                            "controlled_tools",
                        )
                    ],
                    "facts": {
                        "profile": row["profile"],
                        "profile_digest": digest(profile),
                        "runtime_version": profile["binding"]["runtime_version"],
                        "roles": ["worker"],
                        "tools": ["fixture-tools"],
                        "context_tokens": 8192,
                        "data_destination": "synthetic-local",
                        "budget_enforcement": "unknown",
                        "provenance": "fixture",
                        "evidence_ref": "spec-double-no-provider-execution",
                        "observed_at": 1000.0,
                        "valid_until": 1800.0,
                    },
                }
            yield view


def prepare(root: Path, *, qualification_double=True):
    case = seeded(root, custom_rule=True)
    estimate(case)
    if qualification_double:
        case["qualifications"] = IndependentQualificationDouble(
            case["registry"], clock=lambda: case["now"][0]
        )
    routing = ApprovedRunRouting(
        case["planner"], case["qualifications"], case["capacity"], estimates=case["estimates"]
    )
    case["routing"] = routing
    case["admission"] = ApprovedTaskAdmission(root / "admissions.sqlite", routing)
    case["root"] = root
    return case


def reopen(root: Path, *, now=1000.0, qualification_double=True):
    registry = ProjectRegistry(root / "projects.sqlite", [root], clock=lambda: now)
    planner = RunPlanner(root / "runs.sqlite", registry, clock=lambda: now)
    capacity = CapacityStore(root / "capacity.sqlite", clock=lambda: now)
    estimate_store = AttemptEstimateStore(planner, clock=lambda: now)
    qualifier_type = (
        IndependentQualificationDouble if qualification_double else ProfileQualificationStore
    )
    qualifications = qualifier_type(registry, clock=lambda: now)
    routing = ApprovedRunRouting(planner, qualifications, capacity, estimates=estimate_store)
    admission = ApprovedTaskAdmission(root / "admissions.sqlite", routing)
    return admission, routing


def queue(case, key="queue"):
    result = case["admission"].enqueue(
        case["run"]["id"], "implement", principal="owner", command_key=key
    )
    assert result["state"] == "queued", json.dumps(result["reason_codes"])
    return result
