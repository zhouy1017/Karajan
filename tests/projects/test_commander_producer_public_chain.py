"""Public Commander producer C/P chain with real local controller stores.

The only substitute is a localhost HTTP peer.  The public factory receives it
through its private composition seam, so the original native/Relay/Journal
path runs while the persisted result remains irrevocably fixture provenance.
"""

import json
import os
import sys
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.orchestration.go_commander_qualification import (
    CommanderCredentialSource,
    CommanderQualificationSettings,
    open_go_commander_qualification_store,
    write_commander_qualification_settings,
)
from karajan.orchestration.planning_admission import PersistentCommanderQualificationReader
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile
from karajan.projects.qualification import QualificationError
from karajan.routing.compiler import digest
from karajan.runs import RunPlanner
from test_qualification_store import apply

pytest_plugins = ["test_qualification_store"]


def _sse(plan: dict) -> bytes:
    rows = [
        {
            "id": "fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "glm-5.3-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": end}],
        }
        for delta, end in (
            ({"role": "assistant"}, None),
            ({"content": json.dumps(plan)}, None),
            ({}, "stop"),
        )
    ]
    rows.append({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    return (
        "".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n"
    ).encode()


@contextmanager
def _local_provider(plan: dict):
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = _sse(plan)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def public_chain(case: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    if sys.platform != "linux":
        pytest.skip("the fixed Commander native binary is Linux-only")
    runtime = os.environ.get("KARAJAN_OPENCODE_LINUX_BINARY")
    tokenizer = os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY")
    if not runtime or not tokenizer:
        pytest.skip("pinned local Commander runtime/tokenizer are not configured")
    configuration = deepcopy(case["configuration"])
    registration = configuration["resources"]["profiles"][0]
    profile = registration["profile"]
    registration["id"] = profile["id"] = "commander"
    profile["auth_ref"] = "commander-auth"
    profile["required_permissions"] = []
    profile["binding"].update(
        model_id="glm-5.3-flash",
        runtime_kind="opencode-go-isolated",
        runtime_version="1.18.29",
        auth_mode="api_key",
        native_settings={
            "suite_ref": {"id": "opencode-go-commander-planning-linux", "revision": 2}
        },
    )
    configuration["resources"]["accounts"][0].update(
        provider_id="opencode-go", secret_ref="commander-auth"
    )
    for row in registration["capability_evidence"]:
        row["profile_digest"] = digest(profile)
    configuration["approved_profile_refs"] = [{"id": "commander", "revision": 1}]
    configuration["rulebook"]["revision"] = 2
    for group in configuration["rulebook"]["profile_groups"].values():
        for ref in group:
            ref["id"] = "commander"
    apply(case, configuration, key="commander-config")

    key = tmp_path / "commander.synthetic.key"
    key.write_text("synthetic-commander-credential-material\n", encoding="ascii")
    key.chmod(0o600)
    private, work, control = tmp_path / "private", tmp_path / "work", tmp_path / "control"
    work.mkdir(mode=0o700)
    control.mkdir(mode=0o700)
    journal_path = tmp_path / "journal.sqlite"
    GoCallJournal(journal_path)
    journal_path.chmod(0o600)
    settings = CommanderQualificationSettings(
        Path(runtime).resolve(),
        Path(tokenizer).resolve(),
        private,
        (
            CommanderCredentialSource(
                case["project_id"], "commander-auth", "synthetic-commander", key
            ),
        ),
        journal_path=journal_path,
        work_root=work,
    )
    write_commander_qualification_settings(control, settings)
    CredentialSourceStore(
        case["projects"],
        sources={(case["project_id"], "commander-auth"): LocalKeyFile("synthetic-commander", key)},
        private_directory=private,
    ).register(case["project_id"], "commander-auth", principal="owner", command_key="credential")
    from karajan.projects.go_commander_suite import probe_spec

    with _local_provider(probe_spec()["cases"]["legal_plan"]["expected_plan"]) as (
        endpoint,
        received,
    ):
        import karajan.adapters.opencode.go_relay as relay

        monkeypatch.setattr(relay, "_UPSTREAM", endpoint)
        store = open_go_commander_qualification_store(
            case["projects"],
            control_directory=control,
            _fixture_client_factory=lambda: httpx.Client(trust_env=False),
        )
        yield {
            **case,
            "store": store,
            "control": control,
            "settings": settings,
            "received": received,
        }


def _qualify(value: dict, key: str = "public-chain") -> dict:
    return value["store"].qualify_commander_planning(
        value["project_id"],
        {"id": "commander", "revision": 1},
        principal="owner",
        command_key=key,
        validity_seconds=60,
    )


def test_public_factory_runs_two_native_scenes_and_replay_is_effect_free(
    public_chain: dict,
) -> None:
    record = _qualify(public_chain)
    assert record["status"] == "passed", (
        record["reason_codes"],
        record["observation"]["reason_codes"],
        [
            request["reason_codes"]
            for row in record["observation"]["scenarios"]
            for request in row["requests"]
        ],
    )
    assert record["provenance"] == "fixture"
    assert "commander_facts" not in record
    assert len(public_chain["received"]) == 2
    journal = record["observation"]["scenarios"]
    assert [row["scenario"] for row in journal] == ["legal_plan", "denied_tool"]
    assert all(row["native_final"]["finish"] == "stop" for row in journal)
    assert all(row["journal"]["state"] == "revoked" for row in journal)
    assert all(payload.get("tools", []) == [] for payload in public_chain["received"])
    start = public_chain["store"].get_command_start(
        public_chain["project_id"], "public-chain", principal="owner"
    )
    assert start["completed"] and len(start["binding"]["execution_start"]["scenarios"]) == 2
    assert _qualify(public_chain) == record
    assert len(public_chain["received"]) == 2
    with pytest.raises(QualificationError, match="IDEMPOTENCY_CONFLICT"):
        public_chain["store"].qualify_commander_planning(
            public_chain["project_id"],
            {"id": "commander", "revision": 1},
            principal="owner",
            command_key="public-chain",
            validity_seconds=61,
        )


def test_persistent_reader_reopens_same_v3_source_but_never_upgrades_fixture(
    public_chain: dict,
) -> None:
    record = _qualify(public_chain)
    projects = ProjectRegistry(
        public_chain["projects"].database, [public_chain["root"]], existing_only=True
    )
    planner = RunPlanner(public_chain["root"] / "runs.sqlite", projects)
    reader = PersistentCommanderQualificationReader(
        planner, public_chain["store"], control_directory=public_chain["control"]
    )
    with projects._transaction() as db:
        current = reader._current_source(
            db, public_chain["project_id"], record["binding"]["profile_binding"], "owner"
        )
    assert current["observation_origin"] == "official_go"
    assert {**current, "observation_origin": "http_fixture"} == record["binding"]["source"]
    public_chain["store"].commander_source = reader._current_source
    with public_chain["store"].commander_facts_guard(
        public_chain["project_id"],
        record["binding"]["profile_binding"]["registration"],
        principal="owner",
        scope="commander_planning.v1",
        reader_version="karajan.commander-qualification-reader.v1",
    ) as facts:
        assert facts is None
