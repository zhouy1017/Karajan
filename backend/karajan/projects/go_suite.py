"""Controller-owned fixed Go fixture suite, never general task qualification."""

import copy
import hashlib
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation.go_probe import go_runtime_source, observe_go_tools, source_digest

from .credential_sources import ResolvedCredential

SUITE_REF = {"id": "opencode-go-native-read-edit-linux", "revision": 1}


class FixedGoSuite:
    def __init__(
        self,
        runtime: Path,
        work_root: Path,
        journal: GoCallJournal,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime = runtime.resolve()
        self.work_root = work_root.resolve()
        self.journal = journal
        self.client_factory = client_factory
        self.clock = clock

    def source(self) -> dict[str, Any]:
        runtime = go_runtime_source(self.runtime)
        fixture = self.client_factory is not None
        return {
            "schema_version": "karajan.fixed-go-suite-source.v1",
            "suite_ref": dict(SUITE_REF),
            "observation_origin": "http_fixture" if fixture else "official_go",
            "qualification_scope": "fixed_native_tools_fixture"
            if fixture
            else "fixed_native_tools",
            "runtime_source": runtime,
            "runtime_digest": source_digest(runtime),
            "work_root": {"path": str(self.work_root)},
            "journal": {"path": str(self.journal.path.resolve())},
            "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }

    def validate_profile(self, profile_binding: dict[str, Any]) -> None:
        try:
            registration = profile_binding["registration"]
            profile = registration["profile"]
            binding = profile["binding"]
            account, channel = profile_binding["account"], profile_binding["channel"]
            valid = (
                registration["id"] == profile["id"]
                and registration["revision"] == profile["revision"]
                and sorted(profile["required_permissions"]) == ["edit", "read"]
                and binding["model_id"] == "glm-5.3-flash"
                and binding["runtime_kind"] == "opencode-go-isolated"
                and binding["runtime_version"] == "1.18.29"
                and binding["auth_mode"] == "api_key"
                and binding["native_settings"] == {"suite_ref": SUITE_REF}
                and binding["account_id"] == account["id"] == channel["account_id"]
                and account["provider_id"] == "opencode-go"
                and profile["auth_ref"] == account["secret_ref"]
                and binding["channel_id"] == channel["id"]
                and binding["billing_path"] == channel["billing_path"]
                and channel["approved_data_destination"] is True
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError("FIXED_GO_PROFILE_UNSUPPORTED")

    def _validate_start(self, start: dict[str, Any], credential: ResolvedCredential) -> None:
        self.validate_profile(start.get("profile_binding", {}))
        try:
            profile = start["profile_binding"]["registration"]["profile"]
            authentication = start["authentication_source"]
            started, expires, now = start["started_at"], start["expires_at"], self.clock()
            valid = (
                start["suite_ref"] == SUITE_REF
                and start["source"] == self.source()
                and start["profile_digest"] == source_digest(profile)
                and isinstance(credential, ResolvedCredential)
                and credential.project_id == start["project_id"] == authentication["project_id"]
                and credential.auth_ref == profile["auth_ref"] == authentication["auth_ref"]
                and credential.generation
                == start["auth_generation"]
                == authentication["generation"]
                and credential.source_id
                == start["credential_source_id"]
                == authentication["source"]["id"]
                and authentication["source"]["kind"] == "controller_local_key_file"
                and authentication["schema_version"] == "karajan.credential-generation.v1"
                and all(
                    type(value) in (int, float) and math.isfinite(value)
                    for value in (started, expires, now)
                )
                and 0 < started <= now < expires <= started + 420
                and [item["scenario"] for item in start["scenarios"]] == ["edit", "denied_read"]
            )
            ids = [start["qualification_id"], start["project_id"]]
            attempts, grants = [], []
            for scenario in start["scenarios"]:
                attempts.append(scenario["attempt_id"])
                grants.append(scenario["grant_id"])
                ids.extend((scenario["attempt_id"], scenario["grant_id"]))
                valid = valid and type(scenario["fence"]) is int and 0 < scenario["fence"] < 2**63
                valid = valid and scenario["grant_binding"] == {
                    "qualification_id": start["qualification_id"],
                    "attempt_id": scenario["attempt_id"],
                    "fence": scenario["fence"],
                    "profile_digest": start["profile_digest"],
                    "runtime_digest": start["source"]["runtime_digest"],
                    "channel": profile["binding"]["channel_id"],
                    "model": "glm-5.3-flash",
                    "auth_generation": start["auth_generation"],
                    "expires_at": expires,
                    "max_requests": 6,
                }
            valid = (
                valid
                and len(set(attempts)) == len(set(grants)) == 2
                and all(
                    isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
                    for value in ids
                )
            )
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("FIXED_GO_START_BINDING_MISMATCH")

    def observe(self, start: dict[str, Any], credential: ResolvedCredential) -> dict[str, Any]:
        """Consume persisted identities once; never resume a lost capability or send.

        Only the credential object's matching in-memory generation is consumed.
        The qualification store owns current-generation checks before and after
        this bounded call; an already revealed string cannot be recalled.
        """
        start = copy.deepcopy(start)
        self._validate_start(start, credential)
        directory = self.work_root / ("s" + source_digest({"id": start["qualification_id"]})[:16])
        if len(str(directory / "0" / "inference.sock").encode()) > 107:
            raise ValueError("CONTROLLER_SOCKET_PATH_TOO_LONG")
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            raise ValueError("NEW_CONTROLLER_DIRECTORY_REQUIRED") from None
        result: dict[str, Any] = {
            "schema_version": "karajan.fixed-go-suite-observation.v1",
            "suite_ref": dict(SUITE_REF),
            "qualification_id": start["qualification_id"],
            "source": start["source"],
            "observation_origin": start["source"]["observation_origin"],
            "qualification_scope": start["source"]["qualification_scope"],
            "runtime_tools_status": "not_run",
            "dispatch_eligible": False,
            "provider_remote_stop": "unknown",
            "billing_limit_qualification": "not_run",
            "scenarios": [
                {
                    "scenario": item["scenario"],
                    "attempt_id": item["attempt_id"],
                    "fence": item["fence"],
                    "grant_id": item["grant_id"],
                    "status": "not_run",
                    "reason_codes": ["PRIOR_STAGE_INCOMPLETE"],
                }
                for item in start["scenarios"]
            ],
            "reason_codes": [],
            "grant_cleanup": [],
        }
        grants: list[dict[str, Any]] = []
        try:
            for scenario in start["scenarios"]:
                grant = self.journal.create_grant(
                    scenario["grant_binding"], grant_id=scenario["grant_id"]
                )
                if grant["capability"] is None:
                    raise ValueError("FRESH_GRANT_CAPABILITY_REQUIRED")
                grants.append(grant)
            for index, scenario in enumerate(start["scenarios"]):
                # Re-read source and time before each fixed model effect.
                self._validate_start(start, credential)
                authorization = GoRelayAuthorization(
                    self.journal,
                    scenario["grant_id"],
                    scenario["grant_binding"],
                    grants[index]["capability"],
                )
                result["scenarios"][index].update(
                    status="failed", reason_codes=["SCENARIO_OBSERVATION_INCOMPLETE"]
                )
                report = observe_go_tools(
                    self.runtime,
                    directory / str(index),
                    credential.reveal(),
                    authorization,
                    scenario=scenario["scenario"],
                    client_factory=self.client_factory,
                )
                issues = self._validate_report(start, scenario, report)
                result["scenarios"][index].update(
                    status="failed" if issues else "passed", reason_codes=issues, observation=report
                )
                if issues:
                    result["reason_codes"].append("FIXED_SCENARIO_FAILED")
                    break
        except Exception as error:
            result["reason_codes"].append("FIXED_SUITE_EXECUTION_FAILED")
            result["error_type"] = type(error).__name__
        finally:
            # IDs are durable before create_grant. A lost return still revokes.
            for scenario in start["scenarios"]:
                cleanup = {"grant_id": scenario["grant_id"], "state": "unknown"}
                try:
                    actual = self.journal.snapshot(scenario["grant_id"])
                    if actual["binding"] != scenario["grant_binding"]:
                        cleanup["state"] = "not_owned"
                        result["reason_codes"].append("GRANT_CLEANUP_BINDING_MISMATCH")
                    else:
                        # Existing journal bindings are immutable. Matching a
                        # lost create return still permits its own cleanup.
                        self.journal.revoke_grant(scenario["grant_id"])
                        cleanup["state"] = "revoked"
                except GoJournalError as error:
                    if str(error) == "GRANT_NOT_FOUND":
                        cleanup["state"] = "not_created"
                    else:
                        result["reason_codes"].append("GRANT_REVOCATION_FAILED")
                except Exception:
                    result["reason_codes"].append("GRANT_REVOCATION_FAILED")
                result["grant_cleanup"].append(cleanup)
        try:
            if start["source"] != self.source():
                result["reason_codes"].append("SUITE_SOURCE_CHANGED")
        except Exception:
            result["reason_codes"].append("SUITE_SOURCE_UNAVAILABLE")
        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"]))
        result["status"] = (
            "passed"
            if (
                not result["reason_codes"]
                and all(item["status"] == "passed" for item in result["scenarios"])
            )
            else "failed"
        )
        result["validation"] = {
            "fixed_native_tools": result["status"],
            "runtime_tools": "not_run",
            "budget": "unknown",
            "context_capacity": "unknown",
            "dispatch": False,
        }
        return result

    def _validate_report(
        self, start: dict[str, Any], scenario: dict[str, Any], report: dict[str, Any]
    ) -> list[str]:
        journal = self.journal.snapshot(scenario["grant_id"])
        issues = []
        if (
            report.get("schema_version") != "karajan.isolated-opencode-go-observation.v1"
            or report.get("scope") != "fixed_native_tools"
            or report.get("status") != "passed"
            or report.get("reason_codes") != []
            or report.get("scenario") != scenario["scenario"]
            or report.get("grant_id") != scenario["grant_id"]
            or report.get("runtime_source") != start["source"]["runtime_source"]
            or report.get("runtime_digest") != start["source"]["runtime_digest"]
            or report.get("observation_origin") != start["source"]["observation_origin"]
            or report.get("runtime_tools_status") != "not_run"
            or report.get("dispatch_eligible") is not False
            or report.get("real_credential_passed_to_runtime") is not False
            or report.get("provider_remote_stop") != "unknown"
            or report.get("billing_limit_qualification") != "not_run"
        ):
            issues.append("OBSERVATION_BINDING_OR_RESULT_INVALID")
        if (
            report.get("journal") != journal
            or journal["binding"] != scenario["grant_binding"]
            or journal["state"] != "revoked"
            or not 1 <= journal["request_count"] <= 6
        ):
            issues.append("JOURNAL_OBSERVATION_MISMATCH")
        receipts = report.get("requests", [])
        calls = journal["calls"]
        if len(receipts) != len(calls) or len(calls) != journal["request_count"]:
            issues.append("JOURNAL_CALL_CORRELATION_FAILED")
        else:
            for receipt, call in zip(receipts, calls, strict=True):
                outcome = call["outcome"] or {}
                if (
                    receipt.get("journal_call_id") != call["call_id"]
                    or receipt.get("sequence") != call["sequence"]
                    or call["state"] != "response_received"
                    or receipt.get("upstream_send_attempted") is not True
                    or receipt.get("upstream_response_complete") is not True
                    or receipt.get("protocol_passed") is not True
                    or any(
                        receipt.get(key) != outcome.get(key)
                        for key in (
                            "upstream_status",
                            "response_bytes",
                            "usage",
                            "protocol_passed",
                            "reason_codes",
                        )
                    )
                ):
                    issues.append("JOURNAL_CALL_CORRELATION_FAILED")
                    break
        if (
            report.get("native_cleanup", {}).get("local_stop") != "confirmed"
            or report.get("native_cleanup", {}).get("namespace_init_stopped") is not True
            or report.get("relay_cleanup", {}).get("status") != "closed"
        ):
            issues.append("LOCAL_CLEANUP_INCOMPLETE")
        tools, assistants = report.get("tools", []), report.get("assistants", [])
        if (
            not assistants
            or not assistants[-1].get("completed")
            or not assistants[-1].get("stopped")
            or any(not item.get("model_matches") or item.get("error") for item in assistants)
            or report.get("workspace_files") != ["blocked.txt", "fixture.py"]
            or report.get("blocked_file_unchanged") is not True
        ):
            issues.append("NATIVE_RESULT_INCOMPLETE")
        if scenario["scenario"] == "edit":
            valid_tools = (
                report.get("fixture_changed") is True
                and report.get("fixture_cases") == [True] * 4
                and {tool.get("tool") for tool in tools} == {"read", "edit"}
                and all(
                    tool.get("path") == "fixture.py" and tool.get("status") == "completed"
                    for tool in tools
                )
            )
        else:
            valid_tools = (
                report.get("fixture_changed") is False
                and bool(tools)
                and all(
                    tool.get("tool") == "read"
                    and tool.get("path") == "blocked.txt"
                    and tool.get("status") == "error"
                    and tool.get("permission_denied") is True
                    for tool in tools
                )
            )
        if not valid_tools:
            issues.append("FIXED_TOOL_EVIDENCE_INCOMPLETE")
        return issues
