"""Reproducible local walkthrough of the R8-P1-03 deployment control plane.

Run from the repository root:

    python examples/workflows/workflow_deployment.py

It publishes a real workflow bundle, reads its preview identity, confirms a
``deploy_only`` deployment through the authenticated HTTP boundary, reads the
slot back, repeats the same command, deploys a newer revision, rolls back to the
exact historical deployment, and reads the same-source definition handle that
#178 will consume.

Everything here is local and deterministic: no provider, credential or model is
contacted, no Run is created, and no business step is executed. The registered
``artifact_aggregate`` adapter is exercised directly at the end, as a trusted
callable rather than as a step of any deployment.
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

UNSUPPORTED = """schema_version: workflow.v1
id: model-driven
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.topic
roles:
  researcher: role:source-researcher@2
steps:
  - id: research
    execution_kind: agent_task@1
    role: researcher
    output_contract: aggregated-report@1
    inputs: {}
completion:
  required_steps:
    - research
  artifact: research.output
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

EDITED_EDITOR = """id: technical-editor
revision: 1
display_name: Technical editor
responsibilities:
  - assemble the comparison
  - record the decision
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
    with tempfile.TemporaryDirectory(prefix="karajan-deployment-example-") as temporary:
        root = Path(temporary)
        repository = make_repository(root)
        state = root / "state"
        state.mkdir()
        projects = ProjectRegistry(state / "projects.sqlite", [root])
        project = projects.create(
            {
                "name": "Deployment example",
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
            login = client.post(
                "/v1/session/bootstrap",
                json={"token": "example-bootstrap"},
                headers={"Origin": ORIGIN},
            )
            headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
            base = f"/v1/projects/{project['id']}"
            conversation = client.post(
                f"{base}/conversations",
                json={"title": "Design session"},
                headers={**headers, "Idempotency-Key": "example-conversation"},
            )
            conversation_id = conversation.json()["id"]

            def publish(bundle_id: str, files: list[dict[str, str]]) -> dict:
                response = client.post(
                    f"{base}/conversations/{conversation_id}/workflows/{bundle_id}",
                    json={"files": files, "delivery_kind": "report"},
                    headers={**headers, "Idempotency-Key": f"bundle-{bundle_id}"},
                )
                assert response.status_code == 201, response.text
                return response.json()

            def preview(bundle_id: str, revision: int) -> dict:
                return client.get(
                    f"{base}/workflows/{bundle_id}/revisions/{revision}/preview"
                ).json()

            def confirm(
                bundle_id: str,
                revision: int,
                preview_id: str,
                key: str,
                expected_active: int,
                extra: dict | None = None,
            ):
                payload = {
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": expected_active,
                    "preview_id": preview_id,
                    **(extra or {}),
                }
                return client.post(
                    f"{base}/conversations/{conversation_id}/workflows/{bundle_id}"
                    f"/revisions/{revision}/deployments",
                    json=payload,
                    headers={**headers, "Idempotency-Key": key},
                )

            first = publish(
                BUNDLE,
                [
                    {"path": "workflow.yaml", "content": WORKFLOW},
                    {"path": "roles/researcher.yaml", "content": RESEARCHER},
                    {"path": "roles/editor.yaml", "content": EDITOR},
                ],
            )
            print("bundle:", first["revision"], first["readiness"], first["model_calls"])

            # A model-driven template compiles and is reviewable, but its kind has
            # no adapter in this build, so a deployment of it is refused.
            publish(
                "model-driven",
                [
                    {"path": "workflow.yaml", "content": UNSUPPORTED},
                    {"path": "roles/researcher.yaml", "content": RESEARCHER},
                ],
            )
            blocked = confirm(
                "model-driven", 1, preview("model-driven", 1)["preview_id"], "blocked", 0
            )
            print(
                "model template deploy:",
                blocked.status_code,
                blocked.json()["reason_code"],
                blocked.json().get("unavailable_execution_kinds"),
            )

            # A deploy_and_run confirmation is a different product decision.
            unsupported = confirm(
                BUNDLE, 1, preview(BUNDLE, 1)["preview_id"], "unsupported", 0,
                {"action": "deploy_and_run"},
            )
            print(
                "deploy_and_run:",
                unsupported.status_code,
                unsupported.json()["reason_code"],
            )

            deployed = confirm(BUNDLE, 1, preview(BUNDLE, 1)["preview_id"], "deploy-1", 0)
            assert deployed.status_code == 201, deployed.text
            record = deployed.json()["deployment"]
            print(
                "deployed:",
                record["bundle_revision"],
                "slot",
                deployed.json()["slot"]["slot_revision"],
                "readiness",
                deployed.json()["readiness"],
                "runs",
                deployed.json()["runs_started"],
                "business steps",
                deployed.json()["business_steps_executed"],
            )
            print("  bundle digest  ", record["bundle_digest"][:16])
            print("  compiled digest", record["compiled_digest"][:16])
            print("  loader         ", record["load_receipt"]["loader_identity"])
            print("  verify_bytes   ", record["load_receipt"]["verify_bytes"])
            first_deployment = record["deployment_id"]

            replay = confirm(BUNDLE, 1, preview(BUNDLE, 1)["preview_id"], "deploy-1", 0)
            print(
                "replay:",
                replay.status_code,
                "replayed",
                replay.json()["replayed"],
                "same deployment",
                replay.json()["deployment"]["deployment_id"] == first_deployment,
            )

            revised = client.put(
                f"{base}/conversations/{conversation_id}/workflows/{BUNDLE}",
                json={
                    "files": [
                        {"path": "workflow.yaml", "content": WORKFLOW},
                        {"path": "roles/researcher.yaml", "content": RESEARCHER},
                        {"path": "roles/editor.yaml", "content": EDITED_EDITOR},
                    ],
                    "delivery_kind": "report",
                },
                headers={**headers, "Idempotency-Key": "revise", "If-Match": '"1"'},
            )
            assert revised.status_code == 201, revised.text
            print("revised:", revised.json()["revision"], revised.json()["compiled_digest"][:16])

            stale = confirm(BUNDLE, 1, preview(BUNDLE, 1)["preview_id"], "stale", 1)
            print("stale confirmation:", stale.status_code, stale.json()["reason_code"])

            second = confirm(
                BUNDLE, 2, preview(BUNDLE, 2)["preview_id"], "deploy-2", 1
            )
            assert second.status_code == 201, second.text
            second_deployment = second.json()["deployment"]["deployment_id"]
            print(
                "deployed revision 2: slot",
                second.json()["slot"]["slot_revision"],
                second.json()["deployment"]["compiled_digest"][:16],
            )

            rollback = client.post(
                f"{base}/workflow-rollbacks",
                json={"target_deployment_id": first_deployment, "expected_active_revision": 2},
                headers={**headers, "Idempotency-Key": "rollback"},
            )
            assert rollback.status_code == 201, rollback.text
            print(
                "rollback: slot",
                rollback.json()["slot"]["slot_revision"],
                "revision",
                rollback.json()["deployment"]["bundle_revision"],
                "rollback_of",
                rollback.json()["deployment"]["rollback_of"] == first_deployment,
            )

            slot_state = client.get(f"{base}/workflow-deployments/{SLOT}").json()
            print(
                "slot:",
                slot_state["slot_revision"],
                "readiness",
                slot_state["readiness"],
                "active",
                slot_state["active_deployment_id"] == second_deployment or "rolled back",
            )
            print("  pending commands:", [item["command_key"] for item in slot_state["pending"]])
            print(
                "  historical receipt preserved:",
                slot_state["receipt"]["receipt_digest"][:16],
                "verify_bytes",
                slot_state["receipt"]["verify_bytes"],
            )

            definition = client.get(f"{base}/workflow-deployments/{SLOT}/definition").json()
            print(
                "definition handle:",
                definition["deployment"]["bundle_revision"],
                "steps",
                [step["step_id"] for step in definition["definition"]["steps"]],
                "loaded by",
                definition["acquired_load"]["loaded_by_process"],
            )
            print("  digest:", definition["definition_digest"][:16])

            # A second application over the same real state re-loads the active
            # package itself rather than trusting the first one's receipt.
            reopened = create_app(
                state, origin=ORIGIN, bootstrap_token="example-restart", allowed_roots=[root]
            )
            with TestClient(reopened, base_url=ORIGIN) as restarted:
                restarted.post(
                    "/v1/session/bootstrap",
                    json={"token": "example-restart"},
                    headers={"Origin": ORIGIN},
                )
                again = restarted.get(f"{base}/workflow-deployments/{SLOT}").json()
                print(
                    "after restart:",
                    again["readiness"],
                    "loaded by this process",
                    again["current"]["loaded_by_process"] is not None,
                    "verify_bytes",
                    again["current"]["verify_bytes"],
                )

        # The registered adapter is a real callable; this is the only place it is
        # invoked, and no deployment above reached it.
        from karajan.workflows.registry import artifact_aggregate

        artifact = artifact_aggregate(
            {"sources": ["literal:option-a report"], "title": "literal:Compare"}
        )
        print(
            "adapter (not run by any deployment):",
            artifact["ordering"],
            artifact["input_count"],
            artifact["content_digest"][:16],
        )
        print("activation:", json.dumps({"slot": SLOT, "action": "deploy_only"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
