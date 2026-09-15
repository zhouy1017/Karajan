"""Reproducible local walkthrough of the R8-P1-02 workflow bundle control plane.

Run from the repository root:

    python examples/workflows/bundle_preview.py

It registers a project, creates a real conversation, publishes two different
workflow bundles through the authenticated HTTP boundary, edits one by table
command, and shows the same-source graph, table, Mermaid diagram and diff.

Everything here is local and deterministic: no provider, credential or model is
contacted, no generation request is issued, and no workflow is activated or run.
The business adapter is exercised directly at the end, as a registered trusted
callable rather than as a step of any workflow.
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
    inputs:
      sources:
        - option-a.output
completion:
  required_steps:
    - option-a
    - option-b
    - comparison
  artifact: comparison.output
"""

SECOND = """schema_version: workflow.v1
id: repair-config-loaders
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.defect_report
roles:
  coordinator: role:repair-coordinator@1
steps:
  - id: triage
    execution_kind: artifact_aggregate@1
    role: coordinator
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.defect_report
completion:
  required_steps:
    - triage
  artifact: triage.output
"""

RESEARCHER = """id: source-researcher
revision: 2
display_name: Source researcher
responsibilities:
  - read approved material
  - record sources
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

COORDINATOR = """id: repair-coordinator
revision: 1
display_name: Repair coordinator
responsibilities:
  - triage the confirmed defects
stop_conditions: []
tool_constraints:
  paths:
    - src/**
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
    with tempfile.TemporaryDirectory(prefix="karajan-workflow-example-") as temporary:
        root = Path(temporary)
        repository = make_repository(root)
        state = root / "state"
        state.mkdir()
        projects = ProjectRegistry(state / "projects.sqlite", [root])
        project = projects.create(
            {
                "name": "Workflow example",
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
            print("conversation:", conversation.status_code, conversation_id)

            first = client.post(
                f"{base}/conversations/{conversation_id}/workflows/{BUNDLE}",
                json={
                    "files": [
                        {"path": "workflow.yaml", "content": WORKFLOW},
                        {"path": "roles/researcher.yaml", "content": RESEARCHER},
                        {"path": "roles/editor.yaml", "content": EDITOR},
                    ],
                    "delivery_kind": "report",
                },
                headers={**headers, "Idempotency-Key": "example-bundle-1"},
            )
            record = first.json()
            print(
                "bundle 1:",
                first.status_code,
                "revision",
                record["revision"],
                "readiness",
                record["readiness"],
                "model_calls",
                record["model_calls"],
            )
            print("  bundle digest  ", record["bundle_digest"][:16])
            print("  compiled digest", record["compiled_digest"][:16])
            print("  manifest file  ", record["manifest_file_digest"][:16])

            second = client.post(
                f"{base}/conversations/{conversation_id}/workflows/repair-config-loaders",
                json={
                    "files": [
                        {"path": "workflow.yaml", "content": SECOND},
                        {"path": "roles/coordinator.yaml", "content": COORDINATOR},
                    ],
                    "delivery_kind": "report",
                },
                headers={**headers, "Idempotency-Key": "example-bundle-2"},
            )
            print("bundle 2:", second.status_code, second.json()["executable"])

            preview = client.get(
                f"{base}/workflows/{BUNDLE}/revisions/1/preview"
            ).json()
            print("graph nodes:", [node["id"] for node in preview["graph"]["nodes"]])
            edges = [(edge["from"], edge["to"]) for edge in preview["graph"]["edges"]]
            print("graph edges:", edges)
            print("roles:", [role["alias"] for role in preview["roles"]])
            print("mermaid:")
            for line in preview["mermaid"].splitlines():
                print("   ", line)

            edited = client.post(
                f"{base}/conversations/{conversation_id}/workflows/{BUNDLE}/edits",
                json={
                    "edits": [
                        {
                            "operation": "set_dependency",
                            "step_id": "comparison",
                            "depends_on": "option-b",
                        }
                    ]
                },
                headers={
                    **headers,
                    "Idempotency-Key": "example-edit",
                    "If-Match": '"1"',
                },
            )
            print(
                "edit:",
                edited.status_code,
                "revision",
                edited.json()["revision"],
                "model_calls",
                edited.json()["model_calls"],
            )

            diff = client.get(
                f"{base}/workflows/{BUNDLE}/revisions/2/preview",
                params={"compare_to": 1},
            ).json()["diff"]
            print(
                "diff:",
                "changed",
                diff["changed"],
                "changed_steps",
                diff["changed_steps"],
                "roles_changed",
                diff["roles_changed"],
            )

            text = client.post(
                f"{base}/conversations/{conversation_id}/workflows/{BUNDLE}/authoring-inputs",
                json={"instruction": "put the editor before the comparison", "base_revision": 2},
                headers={**headers, "Idempotency-Key": "example-authoring"},
            )
            print(
                "authoring:",
                text.status_code,
                "state",
                text.json()["state"],
                "generated",
                text.json()["generated_configuration"],
            )

            catalog = client.get(f"{base}/workflow-execution-kinds").json()
            availability = {
                item["execution_kind_ref"]: item["available"] for item in catalog["kinds"]
            }
            print("execution kinds:", json.dumps(availability, sort_keys=True))

            reopened = create_app(
                state, origin=ORIGIN, bootstrap_token="example-restart", allowed_roots=[root]
            )
            with TestClient(reopened, base_url=ORIGIN) as restarted:
                restarted.post(
                    "/v1/session/bootstrap",
                    json={"token": "example-restart"},
                    headers={"Origin": ORIGIN},
                )
                again = restarted.get(f"{base}/workflows/{BUNDLE}/revisions/1").json()
                print(
                    "after restart:",
                    again["readback"]["source"],
                    "verified",
                    again["readback"]["verified"],
                    "compiled digest matches",
                    again["readback"]["compiled_digest"] == record["compiled_digest"],
                )

        # The registered adapter is a real callable; this is the only place it is
        # invoked, and nothing above ran it.
        from karajan.workflows.registry import artifact_aggregate

        artifact = artifact_aggregate({"sources": ["option-a.output"], "title": "Compare"})
        print(
            "adapter:",
            artifact["ordering"],
            artifact["encoding"],
            "digest",
            artifact["content_digest"][:16],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
