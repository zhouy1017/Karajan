"""Controller-owned versioned Go qualification with fixed mechanism observations.

Revision 2 observes projected existing files, accounting and stopped capture.
A concrete Task still requires its own approved scope and execution admission.
"""

import copy
import hashlib
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation.go_probe import go_runtime_source, observe_go_tools, source_digest

from .credential_sources import ResolvedCredential

if TYPE_CHECKING:
    from karajan.adapters.opencode.go_context import GoRequestAccounting

SUITE_REF = {"id": "opencode-go-native-read-edit-linux", "revision": 1}
V2_SUITE_REF = {"id": "opencode-go-native-read-edit-linux", "revision": 2}


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("PROJECTED_EVIDENCE_INVALID")


class FixedGoSuite:
    def __init__(
        self,
        runtime: Path,
        work_root: Path,
        journal: GoCallJournal,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        clock: Callable[[], float] = time.time,
        suite_ref: dict[str, Any] | None = None,
        accounting: "GoRequestAccounting | None" = None,
    ) -> None:
        chosen = dict(SUITE_REF) if suite_ref is None else copy.deepcopy(suite_ref)
        if (
            type(chosen) is not dict
            or set(chosen) != {"id", "revision"}
            or chosen["id"] != SUITE_REF["id"]
            or type(chosen["revision"]) is not int
            or chosen["revision"] not in (1, 2)
        ):
            raise ValueError("FIXED_GO_SUITE_UNSUPPORTED")
        if chosen["revision"] == 2:
            from karajan.adapters.opencode.go_context import GoRequestAccounting

            if not isinstance(accounting, GoRequestAccounting):
                raise ValueError("PROJECTED_GO_ACCOUNTING_REQUIRED")
        elif accounting is not None:
            raise ValueError("FIXED_GO_ACCOUNTING_UNSUPPORTED")
        self._suite_ref = chosen
        self.accounting = accounting
        self.runtime = runtime.resolve()
        self.work_root = work_root.resolve()
        self.journal = journal
        self.client_factory = client_factory
        self.clock = clock

    def source(self) -> dict[str, Any]:
        projected = self._suite_ref == V2_SUITE_REF
        if projected:
            from karajan.isolation.go_projected_probe import projected_runtime_source

            if self.accounting is None:
                raise ValueError("PROJECTED_GO_ACCOUNTING_REQUIRED")
            runtime = projected_runtime_source(self.runtime, self.accounting)
        else:
            runtime = go_runtime_source(self.runtime)
        fixture = self.client_factory is not None
        scope = "projected_native_tools" if projected else "fixed_native_tools"
        result = {
            "schema_version": "karajan.fixed-go-suite-source.v2"
            if projected
            else "karajan.fixed-go-suite-source.v1",
            "suite_ref": dict(self._suite_ref),
            "observation_origin": "http_fixture" if fixture else "official_go",
            "qualification_scope": scope + "_fixture" if fixture else scope,
            "runtime_source": runtime,
            "runtime_digest": source_digest(runtime),
            "work_root": {"path": str(self.work_root)},
            "journal": {"path": str(self.journal.path.resolve())},
            "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        if projected:
            result["probe_spec"] = copy.deepcopy(runtime["probe_spec"])
        return result

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
                and binding["native_settings"] == {"suite_ref": self._suite_ref}
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
                start["suite_ref"] == self._suite_ref
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
                expected = {
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
                if self._suite_ref == V2_SUITE_REF:
                    expected.update(
                        schema_version="karajan.go-qualification-grant.v2",
                        probe_spec_digest=source_digest(start["source"]["probe_spec"]),
                        scenario=scenario["scenario"],
                        context=start["source"]["probe_spec"]["context"],
                    )
                valid = valid and scenario["grant_binding"] == expected
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
            "schema_version": "karajan.fixed-go-suite-observation.v2"
            if self._suite_ref == V2_SUITE_REF
            else "karajan.fixed-go-suite-observation.v1",
            "suite_ref": dict(self._suite_ref),
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
                if self._suite_ref == V2_SUITE_REF:
                    from karajan.isolation.go_projected_probe import observe_go_projected_tools

                    if self.accounting is None:
                        raise ValueError("PROJECTED_GO_ACCOUNTING_REQUIRED")
                    report = observe_go_projected_tools(
                        self.runtime,
                        directory / str(index),
                        credential.reveal(),
                        authorization,
                        scenario=scenario["scenario"],
                        accounting=self.accounting,
                        client_factory=self.client_factory,
                    )
                else:
                    report = observe_go_tools(
                        self.runtime,
                        directory / str(index),
                        credential.reveal(),
                        authorization,
                        scenario=scenario["scenario"],
                        client_factory=self.client_factory,
                    )
                issues = self._validate_report(start, scenario, report, directory / str(index))
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
        if self._suite_ref == V2_SUITE_REF:
            del result["validation"]["fixed_native_tools"]
            result["validation"].update(
                projected_native_tools=result["status"],
                candidate_capture=result["status"],
                context_accounting=result["status"],
            )
        return result

    def _validate_report(
        self,
        start: dict[str, Any],
        scenario: dict[str, Any],
        report: dict[str, Any],
        directory: Path,
    ) -> list[str]:
        projected = self._suite_ref == V2_SUITE_REF
        journal = self.journal.snapshot(scenario["grant_id"])
        issues = []
        if (
            report.get("schema_version")
            != (
                "karajan.projected-opencode-go-observation.v1"
                if projected
                else "karajan.isolated-opencode-go-observation.v1"
            )
            or report.get("scope")
            != (start["source"]["qualification_scope"] if projected else "fixed_native_tools")
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
        ):
            issues.append("NATIVE_RESULT_INCOMPLETE")
        if projected:
            issues.extend(self._validate_projected(start, scenario, report, journal, directory))
            return issues
        if "NATIVE_RESULT_INCOMPLETE" not in issues and (
            report.get("workspace_files") != ["blocked.txt", "fixture.py"]
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

    def _validate_projected(
        self,
        start: dict[str, Any],
        scenario: dict[str, Any],
        report: dict[str, Any],
        journal: dict[str, Any],
        directory: Path,
    ) -> list[str]:
        spec = start["source"]["probe_spec"]
        issues = []
        if report.get("probe_spec") != spec or report.get("probe_spec_digest") != source_digest(
            spec
        ):
            issues.append("PROJECTED_SPEC_MISMATCH")
        issues.extend(self._validate_projected_context(spec, scenario, report, journal))
        try:
            self._validate_projected_capture(spec, scenario, report, directory)
        except Exception:
            # Only a stable reason leaves this controller boundary. Candidate
            # paths, artifact bodies and filesystem exceptions are not evidence.
            issues.append("PROJECTED_CAPTURE_EVIDENCE_INVALID")
        tools = report.get("tools", [])
        if scenario["scenario"] == "edit":
            valid_tools = (
                report.get("fixture_changed") is True
                and report.get("fixture_cases") == [True] * 4
                and {item.get("tool") for item in tools} == {"read", "edit"}
                and {item.get("path") for item in tools if item.get("tool") == "read"}
                == {"reference.md", "src/fixture.py"}
                and all(
                    item.get("status") == "completed"
                    and (item.get("tool") != "edit" or item.get("path") == "src/fixture.py")
                    for item in tools
                )
            )
        else:
            valid_tools = (
                report.get("fixture_changed") is False
                and report.get("fixture_cases") is None
                and bool(tools)
                and all(
                    item.get("tool") == "read"
                    and item.get("path") == "blocked.txt"
                    and item.get("status") == "error"
                    and item.get("permission_denied") is True
                    for item in tools
                )
            )
        if not valid_tools:
            issues.append("PROJECTED_TOOL_EVIDENCE_INCOMPLETE")
        return issues

    @staticmethod
    def _validate_projected_context(
        spec: dict[str, Any],
        scenario: dict[str, Any],
        report: dict[str, Any],
        journal: dict[str, Any],
    ) -> list[str]:
        from karajan.adapters.opencode.go_context import ContextMeasurement

        try:
            retention = report["retention"]
            _require(retention["schema_version"] == "karajan.projected-wire-retention.v1")
            _require(retention["source"] == "measured_final_payload")
            _require(retention["initial_input_retained"] is True)
            _require(retention["tool_history_retained"] is True)
            edit = scenario["scenario"] == "edit"
            _require(retention["reference_input_observed"] is edit)
            _require(retention["target_input_observed"] is edit)
            calls = journal["calls"]
            _require(len(calls) == len(report["requests"]) == len(retention["calls"]))
            previous_count = previous_tools = 0
            reference_seen = target_seen = False
            for call, receipt, retained in zip(
                calls, report["requests"], retention["calls"], strict=True
            ):
                measured = ContextMeasurement.model_validate(call["request_context"]).model_dump()
                _require(measured == receipt["request_context"])
                _require(all(measured[key] == value for key, value in spec["context"].items()))
                _require(
                    measured["requested_output_tokens"] == spec["context"]["reserved_output_tokens"]
                )
                usage = call["outcome"]["usage"]
                _require(type(usage["prompt_tokens"]) is int)
                _require(0 <= usage["prompt_tokens"] <= measured["accounted_input_tokens"])
                _require(type(usage["completion_tokens"]) is int)
                _require(0 <= usage["completion_tokens"] <= measured["requested_output_tokens"])
                _require(retained["sequence"] == call["sequence"])
                _require(retained["request_digest"] == measured["request_digest"])
                _require(re.fullmatch(r"[a-f0-9]{64}", retained["messages_digest"]) is not None)
                _require(retained["initial_input_retained"] is True)
                _require(retained["prior_messages_retained"] is True)
                count, tool_count = retained["message_count"], retained["tool_message_count"]
                _require(type(count) is int and count > previous_count)
                _require(type(tool_count) is int and previous_tools <= tool_count <= count)
                if previous_count:
                    _require(tool_count > previous_tools)
                previous_count, previous_tools = count, tool_count
                _require(type(retained["reference_tool_result_observed"]) is bool)
                _require(type(retained["target_tool_result_observed"]) is bool)
                reference_seen |= retained["reference_tool_result_observed"]
                target_seen |= retained["target_tool_result_observed"]
            _require(reference_seen is edit and target_seen is edit)
            _require(retention["calls"][-1]["reference_tool_result_observed"] is edit)
            _require(retention["calls"][-1]["target_tool_result_observed"] is edit)
        except Exception:
            return ["PROJECTED_CONTEXT_OR_RETENTION_INVALID"]
        return []

    @staticmethod
    def _validate_projected_capture(
        spec: dict[str, Any], scenario: dict[str, Any], report: dict[str, Any], directory: Path
    ) -> None:
        from karajan.adapters.opencode.go_evidence import check_fixture
        from karajan.candidates.store import CandidateStore, manifest_digest

        # The location comes from the controller, never from reported paths.
        state = directory / "candidates"
        _require((state / "candidates.sqlite").is_file())
        _require((state / "objects.git").is_dir())
        store = CandidateStore(state)
        capture = report["capture"]
        baseline = store.get_baseline(capture["baseline_id"])
        candidate = store.get(capture["candidate_id"])

        def public_manifest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "path": entry["path"],
                    "mode": entry["mode"],
                    "blob_sha": entry["blob_sha"],
                    "sha256": entry["artifact"]["sha256"],
                    "bytes": entry["artifact"]["size"],
                }
                for entry in entries
            ]

        before, after = (
            public_manifest(baseline["manifest"]),
            public_manifest(candidate["manifest"]),
        )
        _require(before == spec["baseline_manifest"])
        _require(capture["status"] == "passed")
        _require(capture["baseline_manifest"] == before)
        _require(capture["candidate_manifest"] == after)
        _require(capture["baseline_tree_sha"] == baseline["tree_sha"])
        _require(capture["baseline_manifest_sha256"] == manifest_digest(baseline["manifest"]))
        _require(capture["candidate_tree_sha"] == candidate["tree_sha"])
        _require(capture["candidate_manifest_sha256"] == candidate["manifest_sha256"])
        _require(candidate["manifest_sha256"] == manifest_digest(candidate["manifest"]))
        _require(capture["candidate_revision"] == candidate["revision"] == 1)
        expected_changed = ["src/fixture.py"] if scenario["scenario"] == "edit" else []
        _require(capture["changed_paths"] == candidate["changed_paths"] == expected_changed)
        before_paths, after_paths = (
            {item["path"]: item for item in before},
            {item["path"]: item for item in after},
        )
        _require(before_paths.keys() == after_paths.keys())
        _require(
            all(
                before_paths[path] == after_paths[path]
                for path in before_paths
                if path not in expected_changed
            )
        )
        _require(capture["projection_digest"] == source_digest({"projection": spec["projection"]}))
        _require(
            capture["captured_files"]
            == [
                {key: after_paths[row["path"]][key] for key in ("path", "sha256", "bytes")}
                for row in spec["projection"]
            ]
        )
        materialized = directory / "materialized"
        files = sorted(path for path in materialized.rglob("*") if path.is_file())
        _require(
            [path.relative_to(materialized).as_posix() for path in files] == sorted(after_paths)
        )
        for path in files:
            _require(not path.is_symlink())
            row = after_paths[path.relative_to(materialized).as_posix()]
            data = path.read_bytes()
            _require(
                hashlib.sha256(data).hexdigest() == row["sha256"] and len(data) == row["bytes"]
            )
            _require(path.stat().st_mode & 0o777 == (0o755 if row["mode"] == "100755" else 0o644))
            if path.relative_to(materialized).as_posix() == "src/fixture.py" and expected_changed:
                _require(check_fixture(data.decode()) == report["fixture_cases"] == [True] * 4)
        request = candidate["request"]
        probe_digest = source_digest(spec)
        _require(request["baseline_id"] == baseline["id"])
        _require(request["input_sha256"] == candidate["input_sha256"] == probe_digest)
        _require(
            request["series_id"]
            == (
                "qualification:"
                + scenario["grant_binding"]["qualification_id"]
                + ":"
                + scenario["scenario"]
            )
        )
        _require(request["allowed_paths"] == ["src/fixture.py"])
        _require(request["task_class"] == "T1")
        _require(
            request["writer"]
            == {
                "attempt_id": scenario["attempt_id"],
                "fence": scenario["fence"],
                "stopped": True,
                "observation_ref": "projected-stop:" + scenario["grant_id"],
            }
        )
        _require(
            request["authors"]
            == [
                {
                    "attempt_id": scenario["attempt_id"],
                    "fence": scenario["fence"],
                    "profile_id": "qualification-profile",
                    "profile_revision": 1,
                    "model_family": None,
                    "context_id": "projected-context:" + scenario["attempt_id"],
                    "provenance_ref": "projected-probe:" + probe_digest,
                }
            ]
        )
        _require([item["id"] for item in request["policy"]["checks"]] == ["fixture_check"])
        _require(request["policy"]["review"]["approved_reviewers"] == [])
        current = {
            key: candidate[key]
            for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
        }
        gate = store.gate(candidate["id"], current=current)
        _require(gate["local_gate_passed"] is False)
        _require(
            gate["reasons"] == ["CHECK_EVIDENCE_MISSING:fixture_check", "REVIEW_EVIDENCE_MISSING"]
        )
        _require(
            capture["validation_gate"]
            == {key: gate[key] for key in ("local_gate_passed", "reasons")}
        )
        _require(
            all(
                capture[key] is True
                for key in (
                    "readonly_unchanged",
                    "outside_projection_unchanged",
                    "full_baseline_preserved",
                    "materialization_matches",
                    "local_stop_confirmed",
                )
            )
        )
