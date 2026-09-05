"""Observe generated canaries in a new temporary root, never real credential paths."""

import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._namespace import READ_ONLY_ROOTS
from ._targets import NetworkCanary
from .qualification import REQUIRED_CHECKS, validate_binding


def _birth(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = raw[raw.rfind(")") + 2 :].split()
    return None if fields[0] == "Z" else fields[19]


def _known_tree(pid: int) -> dict[int, str]:
    pending = [pid]
    observed = {}
    while pending:
        current = pending.pop()
        birth = _birth(current)
        if birth is None or current in observed:
            continue
        observed[current] = birth
        if len(observed) > 16:
            raise ValueError("Unexpected canary process tree")
        children = Path(f"/proc/{current}/task/{current}/children")
        try:
            pending.extend(int(child) for child in children.read_text().split())
        except FileNotFoundError:
            pass
    return observed


def _run_namespace(
    command: list[str],
    environment: dict[str, str],
    workspace: Path,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        close_fds=True,
        start_new_session=True,
    )
    launch = {"supervisor_pid": process.pid, "supervisor_birth": _birth(process.pid)}
    try:
        assert process.stdout is not None
        timed_out = not select.select([process.stdout], [], [], 10)[0]
        line = "" if timed_out else process.stdout.readline()
        if not line:
            if timed_out and process.poll() is None:
                process.kill()
            output, error = process.communicate(timeout=5)
            return subprocess.CompletedProcess(command, process.returncode, output, error), {
                **launch,
                "report_received": False,
                "observation_timeout": timed_out,
            }
        heartbeat = workspace / "heartbeat"
        before = heartbeat.read_bytes() if heartbeat.exists() else b""
        time.sleep(0.08)
        growing = heartbeat.exists() and len(heartbeat.read_bytes()) > len(before)
        was_running = process.poll() is None
        tree = _known_tree(process.pid)
        process.kill()
        output, error = process.communicate(timeout=5)
        stopped_at = heartbeat.read_bytes() if heartbeat.exists() else b""
        time.sleep(0.08)
        stopped = heartbeat.exists() and heartbeat.read_bytes() == stopped_at
        tree_exited = all(_birth(pid) != birth for pid, birth in tree.items())
        return subprocess.CompletedProcess(command, process.returncode, line + output, error), {
            **launch,
            "report_received": True,
            "child_wrote_before_cancel": growing,
            "supervisor_running_before_cancel": was_running,
            "heartbeat_stopped": stopped,
            "supervisor_exit_code": process.returncode,
            "remote_stop": "unknown",
            "runnerhost_writer_lease_integration": "not_run",
            "observed_process_identities": tree,
            "observed_tree_exited": tree_exited,
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-c", "user.name=Canary", "-c", "user.email=canary@invalid", *arguments],
        cwd=directory,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout.strip()


def _linux_probe(directory: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    (directory / "marker").write_text("karajan-generated-canary-v1")
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "allowed.txt").write_text("allowed input")
    protected = directory / "protected"
    protected.mkdir()
    targets = {}
    for name in (
        "platform",
        "git_credentials",
        "other_attempt",
        "broker_admin",
        "subscription_auth",
    ):
        target = protected / name
        target.write_text("synthetic-canary-" + name)
        targets[name] = str(target)
    baseline = {name: Path(target).read_bytes() for name, target in targets.items()}
    remote = protected / "remote.git"
    _git(directory, "init", "--bare", str(remote))
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "add", "allowed.txt")
    _git(workspace, "commit", "--no-gpg-sign", "-m", "synthetic canary")
    _git(workspace, "push", str(remote), "HEAD:refs/heads/main")
    remote_before = _git(directory, "--git-dir", str(remote), "rev-parse", "refs/heads/main")
    (workspace / "outside-link").symlink_to(targets["platform"])
    host_proc_path = f"/proc/{os.getpid()}/root{targets['platform']}"
    baseline_symlink = (workspace / "outside-link").read_bytes() == baseline["platform"]
    baseline_proc = Path(host_proc_path).read_bytes() == baseline["platform"]
    pe_source = Path("/mnt/c/Windows/System32/cmd.exe")
    pe_available = pe_source.is_file()
    baseline_interop = False
    pe_digest = None
    if pe_available:
        pe_target = workspace / "interop-canary.exe"
        shutil.copyfile(pe_source, pe_target)
        pe_target.chmod(0o700)
        pe_digest = hashlib.sha256(pe_target.read_bytes()).hexdigest()
        pe = subprocess.run(
            [str(pe_target), "/D", "/Q", "/C", "echo KARAJAN_WSL_CANARY"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        baseline_interop = pe.returncode == 0 and b"KARAJAN_WSL_CANARY" in pe.stdout
    command = [
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--propagation",
        "private",
        "--pid",
        "--fork",
        "--kill-child=KILL",
        "--net",
        "/usr/bin/python3",
        str(Path(__file__).with_name("_namespace.py")),
        str(directory),
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "KARAJAN_FAKE_SECRET": "synthetic-inherited-secret",
    }
    with NetworkCanary() as network, Path(targets["platform"]).open("rb") as inherited:
        baseline_receipts = network.positive_control()
        identity = os.fstat(inherited.fileno())
        os.set_inheritable(inherited.fileno(), True)
        (directory / "input.json").write_text(
            json.dumps(
                {
                    "protected": targets,
                    "host_proc_path": host_proc_path,
                    "inherited_fd": inherited.fileno(),
                    "inherited_identity": [identity.st_dev, identity.st_ino],
                    "network_port": network.server_port,
                    "git_remote": str(remote),
                    "wsl_pe_available": pe_available,
                }
            )
        )
        positive = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                "import os,json,sys; "
                "print(json.dumps({'fd':os.fstat(int(sys.argv[1])).st_ino, "
                "'env':'KARAJAN_FAKE_SECRET' in os.environ}))",
                str(inherited.fileno()),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
            pass_fds=(inherited.fileno(),),
            timeout=5,
        )
        baseline_inheritance = json.loads(positive.stdout)
        process, cancellation = _run_namespace(command, environment, workspace)
        sandbox_receipts = len(network.receipts) - baseline_receipts
    if not cancellation["report_received"]:
        wrote = (workspace / "allowed.txt").read_text() == "allowed update"
        unavailable = (
            not wrote
            and process.returncode == 1
            and process.stderr.strip()
            in {
                "unshare: unshare failed: Operation not permitted",
                "unshare: unshare failed: Invalid argument",
                "unshare: unshare failed: Function not implemented",
                "unshare: write failed /proc/self/uid_map: Operation not permitted",
            }
        )
        return [], {
            **cancellation,
            "exit_code": process.returncode,
            "setup_stderr": process.stderr,
            "workspace_write_observed": wrote,
            "namespace_unavailable": unavailable,
            "remote_stop": "unknown",
        }
    observation = json.loads(process.stdout)
    outside_unchanged = all(
        Path(target).read_bytes() == baseline[name] for name, target in targets.items()
    )
    allowed = observation["allowed_read"] and observation["allowed_write"]
    denied = (
        not any(observation["protected_reads"].values())
        and not any(observation["protected_writes"].values())
        and outside_unchanged
    )
    checks = [
        {
            "id": "workspace_read_write",
            "status": "passed" if allowed else "failed",
            "enforcement": "os_enforced",
            "evidence": {"observation": observation},
        },
        {
            "id": "protected_files",
            "status": "passed" if denied else "failed",
            "enforcement": "os_enforced",
            "evidence": {
                "baseline_all_readable": len(baseline) == 5,
                "outside_unchanged": outside_unchanged,
                "reads": observation["protected_reads"],
                "writes": observation["protected_writes"],
            },
        },
    ]
    for name, baseline_ok, leaked in (
        ("symlink_escape", baseline_symlink, observation["escapes"]["symlink"]),
        ("host_proc", baseline_proc, observation["escapes"]["host_proc"]),
        ("environment", baseline_inheritance["env"], observation["environment_leaked"]),
        (
            "inherited_fds",
            baseline_inheritance["fd"] == identity.st_ino,
            observation["inherited_fd_leaked"],
        ),
    ):
        checks.append(
            {
                "id": name,
                "status": "passed" if baseline_ok and not leaked else "failed",
                "enforcement": "os_enforced" if name != "environment" else "trusted_setup",
                "evidence": {"baseline_accessible": baseline_ok, "sandbox_accessible": leaked},
            }
        )
    capabilities_clear = (
        all(int(value, 16) == 0 for value in observation["capabilities"].values())
        and observation["no_new_privs"] == "1"
        and not observation["readonly_write"]
        and not observation["chroot_succeeded"]
        and not observation["mount_succeeded"]
    )
    checks.append(
        {
            "id": "capabilities",
            "status": "passed" if capabilities_clear else "failed",
            "enforcement": "os_enforced",
            "evidence": {
                "capabilities": observation["capabilities"],
                "no_new_privs": observation["no_new_privs"],
                "readonly_canary_write": observation["readonly_write"],
                "chroot_succeeded": observation["chroot_succeeded"],
                "mount_succeeded": observation["mount_succeeded"],
            },
        }
    )
    checks.append(
        {
            "id": "wsl_interop",
            "status": (
                "not_run"
                if not pe_available
                else "passed"
                if baseline_interop and not observation["interop_executed"]
                else "failed"
            ),
            "enforcement": "os_enforced",
            "evidence": {
                "fixture_source": str(pe_source),
                "fixture_digest": pe_digest,
                "baseline_executed": baseline_interop,
                "sandbox_executed": observation["interop_executed"],
            },
        }
    )
    network_blocked = (
        baseline_receipts == 4
        and sandbox_receipts == 0
        and not any(observation["network_reachable"].values())
    )
    checks.append(
        {
            "id": "network_endpoints",
            "status": "passed" if network_blocked else "failed",
            "enforcement": "os_enforced",
            "evidence": {
                "baseline_receipts": baseline_receipts,
                "sandbox_receipts": sandbox_receipts,
                "sandbox_connections": observation["network_reachable"],
            },
        }
    )
    remote_unchanged = remote_before == _git(
        directory, "--git-dir", str(remote), "rev-parse", "refs/heads/main"
    )
    git_blocked = (
        observation["git_read_usable"] and observation["git_push_exit"] != 0 and remote_unchanged
    )
    checks.append(
        {
            "id": "git_remote",
            "status": "passed" if git_blocked else "failed",
            "enforcement": "os_enforced",
            "evidence": {
                "workspace_git_usable": observation["git_read_usable"],
                "baseline_pushed_head": remote_before,
                "remote_unchanged": remote_unchanged,
                "sandbox_push_exit": observation["git_push_exit"],
            },
        }
    )
    stopped = (
        cancellation.get("child_wrote_before_cancel")
        and cancellation.get("supervisor_running_before_cancel")
        and cancellation.get("heartbeat_stopped")
        and len(cancellation.get("observed_process_identities", {})) >= 3
        and cancellation.get("observed_tree_exited")
    )
    checks.append(
        {
            "id": "process_cancel",
            "status": "passed" if stopped else "failed",
            "enforcement": "os_enforced",
            "evidence": cancellation,
        }
    )
    if stopped:
        marker = protected / "collector-hook-marker"
        marker.write_text("untouched")
        hook = workspace / ".git/hooks/canary-hook"
        hook.write_text(
            "#!/usr/bin/python3\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('hook executed')\n"
        )
        hook.chmod(0o700)
        _git(workspace, "config", "core.fsmonitor", str(hook))
        _git(workspace, "config", "core.hooksPath", str(hook.parent))
        _git(workspace, "config", "filter.canary.clean", str(hook))
        (workspace / ".gitattributes").write_text("allowed.txt filter=canary\n")
        (workspace / ".mcp.json").write_text(json.dumps({"fixture_command": str(hook)}))
        subprocess.run([str(hook)], check=True, capture_output=True, timeout=5)
        hook_positive = marker.read_text() == "hook executed"
        marker.write_text("untouched")
        candidate = directory / "candidate"
        candidate.mkdir()
        descriptor: int
        if sys.platform == "linux":
            descriptor = os.open(workspace / "allowed.txt", os.O_RDONLY | os.O_NOFOLLOW)
        else:
            raise OSError("Linux file guards are required")
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Only the fixed regular-file candidate may be collected")
            content = source.read(1_000_001)
        if len(content) > 1_000_000:
            raise ValueError("Candidate is too large")
        (candidate / "allowed.txt").write_bytes(content)
        unchanged = marker.read_text() == "untouched"
        checks.append(
            {
                "id": "candidate_collection",
                "status": (
                    "passed"
                    if hook_positive and unchanged and content == b"allowed update"
                    else "failed"
                ),
                "enforcement": "trusted_collector",
                "evidence": {
                    "hook_positive_control": hook_positive,
                    "marker_unchanged_during_collection": unchanged,
                    "candidate_digest": hashlib.sha256(content).hexdigest(),
                    "collected_files": ["allowed.txt"],
                    "executes_git": False,
                },
            }
        )
    else:
        checks.append(
            {
                "id": "candidate_collection",
                "status": "not_run",
                "enforcement": "trusted_collector",
                "evidence": {"reason": "WRITER_NOT_CONFIRMED_STOPPED"},
            }
        )
    return checks, {
        "exit_code": process.returncode,
        "read_only_mounts": list(READ_ONLY_ROOTS),
        "runtime_version": observation["runtime_version"],
        "runtime_executable": observation["runtime_executable"],
        "kernel_release": observation["kernel_release"],
        "python_binary_digest": hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest(),
    }


def run_probe(spec: dict[str, Any], directory: Path) -> dict[str, Any]:
    if (
        not isinstance(spec, dict)
        or set(spec) != {"schema_version", "case_id", "binding"}
        or spec["schema_version"] != "karajan.isolation.probe.v1"
        or not isinstance(spec["case_id"], str)
        or not spec["case_id"].strip()
    ):
        raise ValueError("INVALID_PROBE_SPEC")
    validate_binding(spec["binding"])
    directory = directory.resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if directory == temporary or not directory.is_relative_to(temporary):
        raise ValueError("A new directory inside the system temporary root is required")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    report: dict[str, Any] = {
        "schema_version": "karajan.isolation.report.v1",
        "status": "unsupported",
        "reason_codes": ["LINUX_NAMESPACE_REQUIRED"],
        "case_id": spec["case_id"],
        "binding": json.loads(json.dumps(spec["binding"])),
        "input_digest": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
        "observed_at": datetime.now(UTC).isoformat(),
        "host_os": sys.platform,
        "qualification_scope": "fixed_python_canary",
        "dispatch_eligible": False,
        "runtime_tools_status": "not_run",
        "checks": [],
        "remaining_required_checks": {
            name: "not_run"
            for name in [
                "codex_native_file_tools",
                "opencode_native_tools",
                "native_mcp_and_hooks",
                "real_authentication_storage",
                "runnerhost_writer_lease_integration",
            ]
        },
        "source_digests": {
            source.name: hashlib.sha256(source.read_bytes()).hexdigest()
            for source in Path(__file__).parent.glob("*.py")
        },
    }
    if sys.platform == "linux":
        try:
            checks, execution = _linux_probe(directory)
        except (OSError, subprocess.SubprocessError) as error:
            checks, execution = (
                [],
                {
                    "setup_error": type(error).__name__,
                    "namespace_unavailable": isinstance(error, FileNotFoundError)
                    and error.filename in {"/usr/bin/unshare", "/usr/bin/git", "/usr/bin/python3"},
                },
            )
        report["checks"] = checks
        report["execution"] = execution
        if checks:
            runtime_matches = (
                execution["runtime_version"] == spec["binding"]["runtime_version"]
                and execution["runtime_executable"] == "/usr/bin/python3"
            )
            checks.append(
                {
                    "id": "runtime_binding",
                    "status": "passed" if runtime_matches else "failed",
                    "enforcement": "trusted_setup",
                    "evidence": {
                        "requested_version": spec["binding"]["runtime_version"],
                        "observed_version": execution["runtime_version"],
                        "observed_executable": execution["runtime_executable"],
                    },
                }
            )
        report["status"] = (
            "unsupported"
            if not checks and execution.get("namespace_unavailable")
            else "failed"
            if not checks or any(check["status"] == "failed" for check in checks)
            else "not_run"
            if any(check["status"] != "passed" for check in checks)
            else "passed"
        )
        report["reason_codes"] = (
            ["NAMESPACE_SETUP_UNAVAILABLE"]
            if report["status"] == "unsupported"
            else ["NAMESPACE_EXECUTION_UNCONFIRMED"]
            if not checks
            else ["CANARY_CHECK_FAILED"]
            if report["status"] == "failed"
            else ["CANARY_CHECK_NOT_RUN"]
            if report["status"] == "not_run"
            else []
        )
        if checks and {check["id"] for check in checks} != REQUIRED_CHECKS:
            report["status"] = "failed"
            report["reason_codes"] = ["CANARY_EVIDENCE_INCOMPLETE"]
    if not report["checks"]:
        report["checks"] = [
            {
                "id": name,
                "status": "not_run",
                "enforcement": "unavailable",
                "evidence": {"reason": report["reason_codes"][0]},
            }
            for name in sorted(REQUIRED_CHECKS)
        ]
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
