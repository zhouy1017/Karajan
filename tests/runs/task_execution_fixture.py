"""Controller-owned offline Host fixture. Never used by the production bootstrap.

SyntheticSuite supplies external qualification facts; real Project/Run/Capacity/
Host/Journal/CAS and actual Linux native tools remain exercised. HTTP transport
is hard-bound to loopback, and its entry/code/config are included in fixture source.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import httpx
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.candidates import CandidateStore
from karajan.capacity import CapacityStore
from karajan.execution import ProcessSpec, RunnerHost
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.go_execution_intent import GoExecutionIntents, GoLaunchSpec
from karajan.orchestration.go_task_binding import execution_source, task_runner_source
from karajan.orchestration.go_task_execution import GoTaskServices
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.routing.compiler import digest
from karajan.runs import RunPlanner
from test_projected_qualification_store import SyntheticSuite


def source(config, accounting):
    current = task_runner_source(Path(config["runtime"]), accounting)
    current["test_harness"] = {
        "external_qualification": "explicit SyntheticSuite, no official provider evidence",
        "config_digest": digest(config),
        "sources": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("task_execution_fixture.py", "_task_execution_fixture_entry.py")
        },
    }
    return current


def open_fixture(directory, *, accounting=None):
    config = json.loads((directory / "test-bootstrap.json").read_text())
    assert config["fixture_only"] is True
    assert type(config["port"]) is int and 1 <= config["port"] <= 65535
    accounting = accounting or GoRequestAccounting(Path(config["tokenizer"]))
    root = Path(config["root"])
    registry = ProjectRegistry(root / "projects.sqlite", [root / "fixture"], existing_only=True)
    credentials = CredentialSourceStore(
        registry,
        sources={
            (config["project_id"], "secret:go"): LocalKeyFile("synthetic", root / "synthetic.key")
        },
        private_directory=root / "credential-private",
        existing_only=True,
    )

    class LostGrantReply(GoCallJournal):
        """Explicit crash-boundary fixture: committed material cannot be reissued."""

        def create_grant(self, binding, *, grant_id):
            super().create_grant(binding, grant_id=grant_id)
            raise RuntimeError("fixture lost grant reply")

    journal_type = LostGrantReply if config["fault"] == "grant_reply_lost" else GoCallJournal
    journal = journal_type(root / "journal.sqlite", existing_only=True)
    suite = SyntheticSuite(journal)
    suite.source_value = config["synthetic_qualification_source"]
    qualification = ProfileQualificationStore(registry, credentials=credentials, go_suite=suite)
    planner = RunPlanner(root / "approved-runs.sqlite", registry, existing_only=True)
    capacity = CapacityStore(root / "capacity.sqlite", existing_only=True)
    estimates = AttemptEstimateStore(planner)
    routing = ApprovedRunRouting(planner, qualification, capacity, estimates=estimates)
    admissions = ApprovedTaskAdmission(root / "admission.sqlite", routing, existing_only=True)
    candidates = CandidateStore(root / "candidates", existing_only=True)
    host = RunnerHost(root / "host", existing_only=True)

    def fixed_spec(run_id, operation_id, principal, timeout):
        return ProcessSpec(
            (
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_task_execution_fixture_entry.py")),
                run_id,
                operation_id,
                principal,
            ),
            directory,
            timeout + 60,
        )

    def compile_launch(operation):
        task = next(
            t
            for t in operation["workspace"]["source_binding"]["plan"]["plan"]["tasks"]
            if t["id"] == operation["task_id"]
        )
        return GoLaunchSpec(
            fixed_spec(
                operation["run_id"],
                operation["id"],
                operation["execution"]["intent"]["owner"],
                task["duration_seconds"],
            ),
            digest(config),
        )

    def current_source():
        return source(config, accounting)

    intents = GoExecutionIntents(
        admissions,
        source=execution_source(current_source()),
        host=host,
        launch_compiler=compile_launch,
        journal=journal,
        candidates=candidates,
    )

    class LoopbackOnly(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            assert request.url.host == "opencode.ai"
            local = httpx.Request(
                request.method,
                f"http://127.0.0.1:{config['port']}/fixture",
                content=request.content,
                headers={"content-type": "application/json"},
            )
            assert local.url.host == "127.0.0.1"
            return self.inner.handle_request(local)

        def close(self):
            self.inner.close()

    def client_factory():
        return httpx.Client(transport=LoopbackOnly(), trust_env=False, timeout=30)

    assert client_factory is not None
    return GoTaskServices(
        intents,
        candidates,
        journal,
        credentials,
        Path(config["runtime"]),
        accounting,
        root / "w",
        current_source,
        fixed_spec,
        client_factory,
    )


def approved_fixture(
    projected,
    root,
    runtime,
    accounting,
    artifacts,
    port,
    *,
    duration=120,
    fault="none",
):
    """Build all approval/forecast/window inputs before reservation through real APIs."""
    from karajan.orchestration.workspace import ApprovedTaskWorkspace
    from test_planning import ScriptedAdmissionReader
    from test_routing_authorization import (
        approve_request,
        policy_request,
        request_v2,
        submit_request,
    )

    projects, project_id = projected["projects"], projected["project_id"]
    projected["clock"][0] = time.time()
    projects.clock = time.time
    projected["store"].clock = time.time
    projected["suite"].journal.clock = time.time
    native = task_runner_source(runtime, accounting)["native_task"][
        "qualified_mechanism_descriptor"
    ]
    suite_source = projected["suite"].source_value
    suite_source.update(
        runtime_source=native, runtime_digest=digest(native), probe_spec=native["probe_spec"]
    )
    configuration = projects.get_configuration(project_id)["configuration"]
    for row in configuration["resources"]["profiles"]:
        for evidence in row["capability_evidence"]:
            evidence.update(
                profile_digest=digest(row["profile"]),
                runtime_version="1.18.29",
                provenance="fixture",
                evidence_ref="synthetic-planning-metadata",
            )
    preview = projects.preview_configuration(
        project_id, configuration, principal="owner", command_key="fixture-catalog"
    )
    projects.apply_configuration(
        project_id,
        preview["preview_id"],
        principal="owner",
        command_key="fixture-apply",
        expected_revision=projects.get(project_id)["revision"],
    )
    projected["store"].qualify_runtime_tools(
        project_id,
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key="fixture-qualification",
        suite_ref=suite_source["suite_ref"],
        validity_seconds=600,
    )
    configured = projects.get(project_id)
    policy = policy_request(configured)
    policy.update(schema_version="karajan.execution-policy.v2", max_context_tokens=16384)
    policy["constraints"].update(tools=["read", "edit"], data_destinations=["opencode-go"])
    policy["channel_destinations"] = {"fixture-channel": "opencode-go"}
    policy["tool_policy"]["tool_permissions"] = {"read": ["read"], "edit": ["edit"]}
    policy["context_policy"].update(
        reserved_output_tokens=4096,
        measurement={
            "method": "reference_tokenizer_estimate",
            "source_sha256": digest(accounting.source()),
            "fixed_margin": 2048,
            "ratio_margin_basis_points": 2000,
        },
    )
    env = {"id": "validation", "revision": 1}
    policy["validation"] = {
        "id": "validation",
        "revision": 1,
        "checks": [
            {
                "id": "tests",
                "revision": 1,
                "argv": ["python", "-m", "pytest"],
                "environment_ref": env,
                "timeout_seconds": 60,
            }
        ],
        "environments": [
            {
                **env,
                "runtime_kind": "isolated-command",
                "platform": "linux_x64",
                "source_sha256": "e" * 64,
                "filesystem": "candidate_copy",
                "network": "none",
                "env": {},
                "max_log_bytes": 65536,
            }
        ],
        "review": {
            "id": "independent_review",
            "revision": 1,
            "environment_ref": env,
            "context_policy": "candidate_and_acceptance_only",
            "independence_policy": "existing_candidate_independence_v1",
        },
    }
    registered = projects.register_execution_policy(
        project_id, policy, principal="owner", command_key="policy"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(root / "approved-runs.sqlite", projects, admissions=authority)
    creation = request_v2(configured, registered)
    creation["authorization"].update(
        tools=["read", "edit"],
        data_destinations=["opencode-go"],
        max_attempt_duration_seconds=duration,
        stage_permissions={
            name: {"normal": True, "quality_indices": []}
            for name in ("mechanical-worker", "critical-worker", "standard-review")
        },
    )
    run = planner.create(creation, principal="owner", command_key="run")
    planning = planner.planning_intent(run["id"], term=1, principal="lead", command_key="planning")
    planner.attach_planning_receipt(
        run["id"],
        planning["id"],
        receipt_ref=authority.grant(planning),
        principal="owner",
        command_key="receipt",
    )
    submission = submit_request(run, planning)
    for task in submission["plan"]["tasks"]:
        task.update(
            tools=["read", "edit"],
            complexity="T1",
            risk="standard",
            context_tokens=12288,
            duration_seconds=duration,
        )
    plan = planner.submit_plan(run["id"], submission, principal="lead", command_key="plan")
    planner.approve_plan(
        run["id"], approve_request(plan), principal="owner", command_key="approval"
    )
    capacity = CapacityStore(root / "capacity.sqlite")
    configuration = run["configuration_snapshot"]["configuration"]
    for pool in configuration["resources"]["quota_pools"]:
        capacity.register_pool(
            {**{k: pool[k] for k in ("id", "account_id", "kind", "unit")}, "window_kind": "fixed"},
            command_key="pool-" + pool["id"],
        )
        capacity.observe(
            {
                "pool_id": pool["id"],
                "window_id": "fixture-window",
                "observed_at": time.time(),
                "reset_at": time.time() + 600,
                "source": "fixture",
                "source_ref": "synthetic-quota-observer",
                "metric": "remaining",
                "amount": "80",
                "limit": "100",
                "covered_usage_ids": [],
            },
            command_key="observe-" + pool["id"],
        )
    for row in configuration["resources"]["profiles"]:
        capacity.register_profile(
            {
                "id": row["id"],
                "revision": row["revision"],
                "account_id": row["profile"]["binding"]["account_id"],
                "pool_ids": row["quota_pool_refs"],
            },
            command_key="profile-" + row["id"],
        )
    for account in configuration["resources"]["accounts"]:
        capacity.activate_policy(
            {
                "account_id": account["id"],
                "max_active_attempts": 4,
                "max_attempt_duration_seconds": duration,
                "observation_max_age_seconds": 600,
                "require_official_observation": False,
                "safety_margin": {},
                "lead_reserve": {},
                "lead_reserved_slots": 1,
                "conservative_mode": {
                    "enabled": True,
                    "max_local_active_attempts": 4,
                    "max_attempt_duration_seconds": duration,
                    "observation_max_age_seconds": 600,
                    "cooldown_seconds": 10,
                },
            },
            expected_revision=0,
            command_key="capacity-policy-" + account["id"],
        )
    estimates = AttemptEstimateStore(planner)
    estimates.register(
        run["id"],
        "implement",
        {"id": "fixture-profile", "revision": 1},
        {
            "id": "fixture-forecast",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 600,
            "measurement_semantics": "window_independent_attempt",
            "demand": [
                {"pool_id": p["id"], "unit": p["unit"], "window_kind": "fixed", "amount": "3"}
                for p in configuration["resources"]["quota_pools"]
            ],
            "completion_seconds": None,
            "basis": "Synthetic local fixture forecast",
        },
        principal="owner",
        command_key="estimate",
    )
    routing = ApprovedRunRouting(planner, projected["store"], capacity, estimates=estimates)
    admissions = ApprovedTaskAdmission(root / "admission.sqlite", routing)
    operation = admissions.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    operation = admissions.advance(run["id"], operation["id"], principal="owner")
    assert operation["state"] == "reserved", operation["reason_codes"]
    candidates = CandidateStore(root / "candidates")
    ApprovedTaskWorkspace(admissions, candidates).prepare(
        run["id"], operation["id"], principal="owner"
    )
    RunnerHost(root / "host")
    (root / "w").mkdir(mode=0o700)
    deployment = root / "fixture-deployment"
    deployment.mkdir(mode=0o700)
    config = {
        "fixture_only": True,
        "fault": fault,
        "root": str(root),
        "project_id": project_id,
        "runtime": str(runtime),
        "tokenizer": str(artifacts),
        "port": port,
        "synthetic_qualification_source": suite_source,
    }
    (deployment / "test-bootstrap.json").write_text(json.dumps(config))
    services = open_fixture(deployment, accounting=accounting)
    (root / "source-manifest.json").write_text(json.dumps(services.fresh_source(), indent=2))
    return services, run["id"], operation["id"]
