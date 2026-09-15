"""Reproducible local walkthrough of the R8-P1-04 role-directed scheduling plane.

Run from the repository root:

    python examples/workflows/role_scheduling.py

It publishes a real workflow bundle, deploys it, creates a Run from the active
loaded definition, issues a scheduler grant and two narrow protocol credentials
through the authenticated management boundary, submits a scheduling decision
with that role credential, seals the expansion set, establishes a trusted
resource policy and observation, hands one task to an execution consumer, and
reads the whole chain back.

Everything here is local and deterministic. No provider, credential or model is
contacted, no process is started, and no business step is executed: a claim is a
handover of work, and the reported outcome of an attempt is recorded as a report
rather than as a verified completion.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from karajan.projects import ProjectRegistry  # noqa: E402
from karajan.web import create_app  # noqa: E402

ORIGIN = "http://127.0.0.1:8765"
BUNDLE = "compare-options"
SLOT = "default"

WORKFLOW = """schema_version: workflow.v1
id: compare-options
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.option_a
  - requirement.option_b
roles:
  researcher: role:source-researcher@2
  editor: role:technical-editor@1
steps:
  - id: option-a
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_a
  - id: option-b
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_b
  - id: comparison
    execution_kind: artifact_aggregate@1
    role: editor
    output_contract: aggregated-report@1
    depends_on:
      - option-a
      - option-b
    join: all_required
    inputs:
      sources:
        - option-a.output
        - option-b.output
completion:
  required_steps:
    - option-a
    - option-b
    - comparison
  artifact: comparison.output
"""

RESEARCHER = """id: source-researcher
revision: 2
display_name: Source researcher
responsibilities:
  - read approved material
stop_conditions:
  - material is outside the approved scope
required_capabilities:
  - read_only_research
tool_constraints:
  paths:
    - docs/**
  network: denied
"""

EDITOR = """id: technical-editor
revision: 1
display_name: Technical editor
responsibilities:
  - assemble the comparison
stop_conditions:
  - sources are incomplete
tool_constraints:
  paths:
    - reports/**
"""


def make_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    for arguments in (
        ["init", "--initial-branch=main", str(repository)],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "example",
        ],
    ):
        subprocess.run(["git", *arguments], check=True, capture_output=True)
    return repository


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="karajan-scheduling-example-") as temporary:
        root = Path(temporary)
        repository = make_repository(root)
        state = root / "state"
        state.mkdir()
        projects = ProjectRegistry(state / "projects.sqlite", [root])
        project = projects.create(
            {
                "name": "Scheduling example",
                "repository_path": str(repository),
                "base_ref": "main",
                "target_branch": "main",
                "allowed_target_branches": ["main"],
            },
            command_key="project",
            principal="owner",
        )
        app = create_app(
            state, origin=ORIGIN, bootstrap_token="example-bootstrap", allowed_roots=[root]
        )
        with TestClient(app, base_url=ORIGIN) as client:
            session = client.post(
                "/v1/session/bootstrap",
                json={"token": "example-bootstrap"},
                headers={"Origin": ORIGIN},
            )
            headers = {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
            base = f"/v1/projects/{project['id']}"
            conversation = client.post(
                f"{base}/conversations",
                json={"title": "Design session"},
                headers={**headers, "Idempotency-Key": "example-conversation"},
            ).json()["id"]

            # 1. A real bundle, deployed and loaded by this process.
            client.post(
                f"{base}/conversations/{conversation}/workflows/{BUNDLE}",
                json={
                    "files": [
                        {"path": "workflow.yaml", "content": WORKFLOW},
                        {"path": "roles/researcher.yaml", "content": RESEARCHER},
                        {"path": "roles/editor.yaml", "content": EDITOR},
                    ],
                    "delivery_kind": "report",
                },
                headers={**headers, "Idempotency-Key": "bundle"},
            )
            preview = client.get(
                f"{base}/workflows/{BUNDLE}/revisions/1/preview"
            ).json()["preview_id"]
            deployed = client.post(
                f"{base}/conversations/{conversation}/workflows/{BUNDLE}"
                "/revisions/1/deployments",
                json={
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": 0,
                    "preview_id": preview,
                },
                headers={**headers, "Idempotency-Key": "deploy"},
            ).json()
            print(
                "deployed: revision",
                deployed["deployment"]["bundle_revision"],
                "slot",
                deployed["slot"]["slot_revision"],
                "readiness",
                deployed["readiness"],
            )

            # 2. A Run created from that active definition, with concrete inputs.
            run = client.post(
                f"{base}/workflow-runs",
                json={
                    "conversation_id": conversation,
                    "slot": SLOT,
                    "expected_active_revision": 1,
                    "deployment_id": deployed["deployment"]["deployment_id"],
                    "inputs": {
                        "requirement.option_a": "Option A summary",
                        "requirement.option_b": "Option B summary",
                    },
                    "requirement": {
                        "goal": "compare the two options",
                        "acceptance": ["both options are read"],
                    },
                    "required_outcomes": ["comparison-report"],
                    "title": "Compare options",
                },
                headers={**headers, "Idempotency-Key": "run"},
            ).json()
            run_id = run["run_id"]
            print(
                "run:",
                run_id,
                "frozen revision",
                run["source"]["bundle_revision"],
                "graph revision",
                run["graph_revision"],
                "model calls",
                run["model_calls"],
            )
            print(
                "  frozen deployment",
                run["source"]["deployment_id"][:12],
                "compiled",
                run["source"]["compiled_digest"][:16],
            )

            # 3. The user authorises a scheduler role, through management.
            grant = client.post(
                f"{base}/workflow-runs/{run_id}/grants",
                json={
                    "grant_id": "configuration-repair-scope",
                    "subject_ref": "subject:role-instance-1",
                    "role_instance": "role-instance-1",
                    "allowed_actions": [
                        "expand_graph",
                        "set_priority",
                        "seal",
                        "bind_role",
                    ],
                    "inputs": ["requirement.option_a", "requirement.option_b"],
                    "scope": ["src/config/**"],
                    "write_scope": ["src/config/**"],
                    "required_outcomes": ["comparison-report", "config-defect-triage"],
                    "role_refs": {"researcher": "role:source-researcher@2"},
                    "execution_kind_refs": ["artifact_aggregate@1"],
                    "tool_refs": ["read"],
                    "data_destinations": ["local"],
                    "artifact": "report",
                    "resource_policy": {
                        "budget_ref": "budget-user-approved",
                        "pool_id": "pool-a",
                        "cost_per_claim": 1,
                    },
                },
                headers={**headers, "Idempotency-Key": "grant"},
            ).json()
            print(
                "grant:",
                grant["grant_id"],
                "depth",
                grant["depth"],
                "actions",
                grant["allowed_actions"],
                "delegation",
                grant["delegation"]["allowed"],
            )

            # An expansion set is opened explicitly, so an open batch has a
            # durable identity before any member exists.
            client.post(
                f"{base}/workflow-runs/{run_id}/expansions",
                json={
                    "grant_id": "configuration-repair-scope",
                    "expansion_id": "defects",
                    "goal": "the confirmed configuration defects",
                    "required_outcomes": ["config-defect-triage"],
                },
                headers={**headers, "Idempotency-Key": "expansion"},
            )

            # 4. Two narrow credentials, issued once each, with their raw tokens
            #    returned only by the response that issued them.
            role = client.post(
                f"{base}/workflow-runs/{run_id}/credentials",
                json={
                    "kind": "role_protocol",
                    "run_id": run_id,
                    "grant_id": "configuration-repair-scope",
                    "role_instance": "role-instance-1",
                },
                headers={**headers, "Idempotency-Key": "credential-role"},
            ).json()
            consumer = client.post(
                f"{base}/workflow-runs/{run_id}/credentials",
                json={"kind": "execution_consumer", "run_id": run_id},
                headers={**headers, "Idempotency-Key": "credential-consumer"},
            ).json()
            role_token = role["token"]
            consumer_token = consumer["token"]
            print(
                "credentials:",
                role["credential"]["credential_id"],
                "routes",
                role["credential"]["routes"],
                "raw token stored",
                role["credential"]["stores_raw_token"],
            )
            replay = client.post(
                f"{base}/workflow-runs/{run_id}/credentials",
                json={
                    "kind": "role_protocol",
                    "run_id": run_id,
                    "grant_id": "configuration-repair-scope",
                    "role_instance": "role-instance-1",
                },
                headers={**headers, "Idempotency-Key": "credential-role"},
            ).json()
            print(
                "credential replay:",
                replay["replayed"],
                "same identity",
                replay["credential"]["credential_id"]
                == role["credential"]["credential_id"],
                "re-reveals token",
                "token" in replay,
            )

            # 5. The role submits a structured decision with its own credential.
            protocol = {"Authorization": f"Bearer {role_token}"}
            decision = client.post(
                f"/v1/scheduling/protocol/projects/{project['id']}"
                f"/runs/{run_id}/grants/configuration-repair-scope/decisions",
                json={
                    "decision_id": "decision-1",
                    "grant_id": "configuration-repair-scope",
                    "term": 0,
                    "expected_graph_revision": 0,
                    "inputs_digest": run["inputs_digest"],
                    "trigger": "approved_input",
                    "reason": "one shared defect across the two options",
                    "actions": [
                        {
                            "action": "expand_graph",
                            "expansion_id": "defects",
                            "tasks": [
                                {
                                    "item_key": "option-a",
                                    "role_alias": "researcher",
                                    "execution_kind_ref": "artifact_aggregate@1",
                                    "output_contract_ref": "aggregated-report@1",
                                    "inputs": {"sources": "requirement.option_a"},
                                    "write_zone": "src/config/option-a",
                                    "path_scope": ["src/config/**"],
                                    "required_outcomes": ["config-defect-triage"],
                                    "expansion_id": "defects",
                                },
                                {
                                    "item_key": "option-b",
                                    "role_alias": "researcher",
                                    "execution_kind_ref": "artifact_aggregate@1",
                                    "output_contract_ref": "aggregated-report@1",
                                    "inputs": {"sources": "requirement.option_b"},
                                    "write_zone": "src/config/option-b",
                                    "path_scope": ["src/config/**"],
                                    "required_outcomes": ["config-defect-triage"],
                                    "expansion_id": "defects",
                                },
                            ],
                        }
                    ],
                },
                headers={**protocol, "Idempotency-Key": "decision-1"},
            ).json()
            print(
                "decision:",
                decision["decision_id"],
                "graph revision",
                decision["graph_revision"],
                "accepted under",
                decision["accepted_under"],
                "tasks",
                decision["task_count"],
                "approval required",
                decision["requires_further_approval"],
            )
            print(
                "  outstanding outcomes",
                decision["outstanding_outcomes"],
                "nodes added",
                decision["nodes_added"],
            )

            # 6. The role seals the set, fixing the member versions.
            sealed = client.post(
                f"/v1/scheduling/protocol/projects/{project['id']}"
                f"/runs/{run_id}/grants/configuration-repair-scope/decisions",
                json={
                    "decision_id": "decision-2",
                    "grant_id": "configuration-repair-scope",
                    "term": 0,
                    "expected_graph_revision": 1,
                    "inputs_digest": run["inputs_digest"],
                    "trigger": "discovery_result",
                    "reason": "discovery is complete for this batch",
                    "actions": [
                        {
                            "action": "seal",
                            "expansion_id": "defects",
                            "members": ["decision-1.option-a", "decision-1.option-b"],
                            "obligations": ["config-defect-triage"],
                        }
                    ],
                },
                headers={**protocol, "Idempotency-Key": "decision-2"},
            ).json()
            print("seal:", sealed["sealed_expansions"], "graph revision", sealed["graph_revision"])

            # 7. Trusted capacity: an explicit policy and a real reading.
            client.post(
                "/v1/scheduling/resource-policies",
                json={
                    "pool_id": "pool-a",
                    "max_concurrent_claims": 1,
                    "safety_margin": "0",
                },
                headers={**headers, "Idempotency-Key": "policy"},
            )
            observation = client.post(
                "/v1/scheduling/resource-observations",
                json={
                    "pool_id": "pool-a",
                    "window_id": "window-1",
                    "metric": "remaining",
                    "amount": "1",
                    "source": "local_ledger",
                    "source_ref": "ledger-1",
                },
                headers={**headers, "Idempotency-Key": "observation"},
            ).json()
            print(
                "resource:",
                observation["pool_id"],
                observation["metric"],
                observation["amount"],
                "source",
                observation["source"],
            )

            # 8. An execution consumer reads the queue and takes one task.
            queue = client.get(
                f"/v1/scheduling/protocol/projects/{project['id']}/runs/{run_id}/queue",
                headers={"Authorization": f"Bearer {consumer_token}"},
            ).json()
            print(
                "queue: ready",
                queue["ready_count"],
                "waiting",
                queue["waiting_count"],
                "[" + ", ".join(item["task_id"] for item in queue["items"]) + "]",
            )
            claim = client.post(
                f"/v1/scheduling/protocol/projects/{project['id']}"
                f"/runs/{run_id}/claims",
                json={"claim_key": "claim-1", "task_id": "decision-1.option-a"},
                headers={
                    "Authorization": f"Bearer {consumer_token}",
                    "Idempotency-Key": "claim-1",
                },
            ).json()
            print(
                "claim:",
                claim["claim_id"],
                "write zone",
                claim["write_zone"],
                "physical execution",
                claim["physical_execution"],
                "verified by engine",
                claim["verified_by_engine"],
            )

            # The second task shares the pool, which the policy caps at one.
            blocked = client.post(
                f"/v1/scheduling/protocol/projects/{project['id']}"
                f"/runs/{run_id}/claims",
                json={"claim_key": "claim-2", "task_id": "decision-1.option-b"},
                headers={
                    "Authorization": f"Bearer {consumer_token}",
                    "Idempotency-Key": "claim-2",
                },
            )
            print(
                "second claim:",
                blocked.status_code,
                blocked.json()["reason_code"],
                "capacity",
                blocked.json()["capacity"],
                "occupied",
                blocked.json()["occupied"],
            )

            # 9. The consumer reports; the owner reconciles; the slot is released.
            reported = client.post(
                f"/v1/scheduling/protocol/projects/{project['id']}"
                f"/runs/{run_id}/tasks/decision-1.option-a/reports",
                json={
                    "outcome": "completed",
                    "evidence_ref": "evidence:consumer-observation",
                    "attempt_ref": "attempt-1",
                },
                headers={
                    "Authorization": f"Bearer {consumer_token}",
                    "Idempotency-Key": "report-1",
                },
            ).json()
            print(
                "consumer report:",
                reported["state"],
                "reported",
                reported["reported_state"],
                "verified by engine",
                reported["reports"][-1]["verified_by_engine"],
            )
            reconciled = client.post(
                f"{base}/workflow-runs/{run_id}"
                "/tasks/decision-1.option-a/reconciliation",
                json={"outcome": "completed", "evidence_ref": "evidence:owner-reconciled"},
                headers={**headers, "Idempotency-Key": "reconcile-1"},
            ).json()
            print(
                "reconciled by the owner:",
                reconciled["state"],
                "released at",
                reconciled["reconciled_at"] is not None,
            )
            after = client.get(f"{base}/workflow-runs/{run_id}/resources").json()
            print("run resource state: live claims", after["live_claims"])
            retry = client.post(
                f"/v1/scheduling/protocol/projects/{project['id']}"
                f"/runs/{run_id}/claims",
                json={"claim_key": "claim-2", "task_id": "decision-1.option-b"},
                headers={
                    "Authorization": f"Bearer {consumer_token}",
                    "Idempotency-Key": "claim-2",
                },
            )
            print("waiting task after release:", retry.status_code, retry.json()["task_id"])

            # 10. Read the whole chain back.
            current = client.get(f"{base}/workflow-runs/{run_id}/graph").json()
            print(
                "graph: revision",
                current["graph_revision"],
                "revisions",
                [item["revision"] for item in current["history"]],
                "expansions",
                [(item["expansion_id"], item["state"]) for item in current["expansions"]],
            )
            tasks = client.get(f"{base}/workflow-runs/{run_id}/tasks").json()
            print(
                "tasks:",
                tasks["count"],
                "paged",
                len(tasks["items"]),
                "next cursor",
                tasks["next_cursor"],
            )
            sealed_members = current["expansions"][0]["members"]["decision-1.option-a"]
            print(
                "sealed member pin: revision",
                sealed_members["revision"],
                "digest",
                sealed_members["digest"][:16],
            )
            pinned = client.get(
                f"{base}/workflow-runs/{run_id}/tasks/decision-1.option-a"
                f"/versions/{sealed_members['revision']}",
                params={"digest": sealed_members["digest"]},
            ).json()
            print(
                "pinned version readback:",
                pinned["task_id"],
                "revision",
                pinned["revision"],
                "inputs",
                pinned["inputs"],
            )
            print(
                "activation:",
                json.dumps({"slot": SLOT, "action": "deploy_only", "model_calls": 0}),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
