"""Public delivery protocol with real local bare Git and explicit test gateways."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from karajan.delivery import DeliveryCoordinator, DeliveryError, LocalGitRemote, RemoteUnknown


def request() -> dict:
    return {
        "run_id": "run-1",
        "delivery_revision": 1,
        "repository_id": "fixture-repo",
        "managed_branch": "codex/karajan-run-1",
        "base_branch": "main",
        "tested_base_sha": "a" * 40,
        "candidate_id": "candidate-1",
        "content_sha256": "b" * 64,
        "tree_sha": "c" * 40,
        "commit_sha": "d" * 40,
        "authorization_sha256": "e" * 64,
        "evidence_sha256": "f" * 64,
        "verification_ref": "not-yet-qualified",
        "expected_old_sha": None,
        "require_ci": False,
    }


def test_delivery_intent_is_persistent_but_missing_qualification_cannot_activate(
    tmp_path: Path,
) -> None:
    coordinator = DeliveryCoordinator(tmp_path / "delivery.sqlite")
    intent = coordinator.plan(request(), command_key="plan-1", principal="controller")
    assert DeliveryCoordinator(tmp_path / "delivery.sqlite").get(intent["id"]) == intent
    blocked = coordinator.advance(intent["id"], principal="controller")
    assert blocked["state"] == "blocked"
    assert blocked["reason"] == "DELIVERY_QUALIFICATION_NOT_RUN"
    assert blocked["operations"] == []
    assert blocked["production_qualified"] is False


@pytest.mark.parametrize("principal", ["worker", "reviewer", "check"])
def test_execution_roles_cannot_borrow_the_delivery_entry(tmp_path: Path, principal: str) -> None:
    coordinator = DeliveryCoordinator(tmp_path / "delivery.sqlite")
    with pytest.raises(DeliveryError, match="DELIVERY_ACTOR_FORBIDDEN"):
        coordinator.plan(request(), command_key="forbidden", principal=principal)


def test_repeated_plan_is_one_immutable_run_revision_and_command(tmp_path: Path) -> None:
    coordinator = DeliveryCoordinator(tmp_path / "delivery.sqlite")
    first = coordinator.plan(request(), command_key="same", principal="controller")
    assert coordinator.plan(request(), command_key="same", principal="controller") == first
    assert (
        coordinator.plan(request(), command_key="another-key", principal="controller")["id"]
        == first["id"]
    )
    changed = dict(request(), candidate_id="different")
    with pytest.raises(DeliveryError, match="IDEMPOTENCY_CONFLICT"):
        coordinator.plan(changed, command_key="same", principal="controller")
    with pytest.raises(DeliveryError, match="DELIVERY_REVISION_CONFLICT"):
        coordinator.plan(changed, command_key="new-key", principal="controller")


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "core.hooksPath=/dev/null", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path, dict]:
    source = tmp_path / "trusted"
    source.mkdir()
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "app.txt").write_bytes(b"base\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(source, "clone", "--bare", str(source), str(remote))
    git(source, "checkout", "-qb", "candidate")
    (source / "app.txt").write_bytes(b"candidate\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "candidate")
    binding = dict(
        request(),
        tested_base_sha=base,
        commit_sha=git(source, "rev-parse", "HEAD"),
        tree_sha=git(source, "rev-parse", "HEAD^{tree}"),
    )
    return source, remote, binding


def test_local_bare_push_creates_only_the_exact_absent_managed_ref(
    repository: tuple[Path, Path, dict],
) -> None:
    source, remote, binding = repository
    adapter = LocalGitRemote(source, remote, repository_id="fixture-repo")
    before = adapter.inspect(binding)
    assert before["head_sha"] is None
    assert before["base_sha"] == binding["tested_base_sha"]
    adapter.push(binding)
    assert adapter.inspect(binding)["head_sha"] == binding["commit_sha"]


@pytest.mark.parametrize("variant", ["wrong_tree", "changed_base", "external_head"])
def test_push_rechecks_content_base_and_exact_remote_head(
    repository: tuple[Path, Path, dict], variant: str
) -> None:
    source, remote, binding = repository
    adapter = LocalGitRemote(source, remote, repository_id="fixture-repo")
    changed = dict(binding)
    if variant == "wrong_tree":
        changed["tree_sha"] = "0" * 40
        reason = "CANDIDATE_COMMIT_MISMATCH"
    elif variant == "changed_base":
        changed["tested_base_sha"] = binding["commit_sha"]
        reason = "TESTED_BASE_CHANGED"
    else:
        git(
            remote,
            "update-ref",
            "refs/heads/" + binding["managed_branch"],
            binding["tested_base_sha"],
        )
        reason = "REMOTE_HEAD_CHANGED"
    with pytest.raises(DeliveryError, match=reason):
        adapter.push(changed)
    expected = binding["tested_base_sha"] if variant == "external_head" else None
    assert adapter.inspect(binding)["head_sha"] == expected
    assert git(remote, "rev-parse", "refs/heads/main") == binding["tested_base_sha"]


def test_explicit_lease_does_not_authorize_rewriting_managed_history(
    repository: tuple[Path, Path, dict],
) -> None:
    source, remote, binding = repository
    adapter = LocalGitRemote(source, remote, repository_id="fixture-repo")
    adapter.push(binding)
    git(source, "checkout", "-q", "main")
    (source / "alternative.txt").write_bytes(b"unrelated branch\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "unrelated")
    rewritten = dict(
        binding,
        expected_old_sha=binding["commit_sha"],
        commit_sha=git(source, "rev-parse", "HEAD"),
        tree_sha=git(source, "rev-parse", "HEAD^{tree}"),
    )
    with pytest.raises(DeliveryError, match="NON_FAST_FORWARD_FORBIDDEN"):
        adapter.push(rewritten)
    assert adapter.inspect(binding)["head_sha"] == binding["commit_sha"]


class FixtureAuthority:
    def __init__(self) -> None:
        self.receipts: dict[str, dict] = {}

    def __call__(self, reference: str) -> dict:
        return self.receipts[reference]

    def grant(self, intent: dict) -> None:
        self.receipts[intent["request"]["verification_ref"]] = {
            "receipt_ref": intent["request"]["verification_ref"],
            "binding_sha256": intent["binding_sha256"],
            "authority_revision": "fixture-validation-v1",
            "decision": "allow",
            "provenance": "fixture",
        }


class FixturePullRequests:
    execution_scope = "offline_fixture"

    def __init__(self, remote: LocalGitRemote) -> None:
        self.remote = remote
        self.items: list[dict[str, Any]] = []

    def lookup(self, binding: dict) -> list[dict]:
        head = self.remote.inspect(binding)["head_sha"]
        return [
            dict(item, head_sha=head)
            for item in self.items
            if item["repository_id"] == binding["repository_id"]
            and item["managed_branch"] == binding["managed_branch"]
        ]

    def publish(self, binding: dict, existing_id: str | None) -> dict:
        if existing_id:
            return next(item for item in self.lookup(binding) if item["id"] == existing_id)
        item = {
            "id": "fixture-pr-1",
            "repository_id": binding["repository_id"],
            "managed_branch": binding["managed_branch"],
            "base_branch": binding["base_branch"],
            "run_id": binding["run_id"],
            "head_sha": binding["commit_sha"],
            "state": "open",
            "merged": False,
            "ci_sha": binding["commit_sha"],
            "ci_status": "pending",
        }
        self.items.append(item)
        return item


def fixture_delivery(tmp_path: Path, repository: tuple[Path, Path, dict]) -> tuple:
    source, remote_path, binding = repository
    remote = LocalGitRemote(source, remote_path, repository_id="fixture-repo")
    authority = FixtureAuthority()
    prs = FixturePullRequests(remote)
    coordinator = DeliveryCoordinator(
        tmp_path / "delivery.sqlite",
        git_remote=remote,
        pr_service=prs,
        verification_reader=authority,
        mode="offline_fixture",
    )
    intent = coordinator.plan(binding, command_key="plan", principal="controller")
    authority.grant(intent)
    return coordinator, intent, remote, prs, authority


def test_verified_fixture_push_has_a_durable_activation_before_pr_step(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    coordinator, intent, remote, prs, _ = fixture_delivery(tmp_path, repository)
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["state"] == "pushed"
    assert result["operations"][0]["step"] == "push"
    assert result["operations"][0]["state"] == "confirmed"
    assert result["operations"][0]["activation"]["binding_sha256"] == intent["binding_sha256"]
    assert remote.inspect(intent["request"])["head_sha"] == intent["request"]["commit_sha"]
    assert prs.items == []
    assert result["production_qualified"] is False


class LostPushReply(LocalGitRemote):
    def __init__(self, source: Path, remote: Path) -> None:
        super().__init__(source, remote, repository_id="fixture-repo")
        self.pushes = 0

    def push(self, binding: dict) -> dict:
        self.pushes += 1
        super().push(binding)
        raise RemoteUnknown("synthetic lost reply after real push")


def test_lost_push_reply_is_reconciled_after_restart_without_second_push(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    source, remote_path, binding = repository
    remote = LostPushReply(source, remote_path)
    authority = FixtureAuthority()
    prs = FixturePullRequests(remote)

    def reopen() -> DeliveryCoordinator:
        return DeliveryCoordinator(
            tmp_path / "delivery.sqlite",
            git_remote=remote,
            pr_service=prs,
            verification_reader=authority,
            mode="offline_fixture",
        )

    coordinator = reopen()
    intent = coordinator.plan(binding, command_key="plan", principal="controller")
    authority.grant(intent)
    uncertain = coordinator.advance(intent["id"], principal="controller")
    assert uncertain["state"] == "reconciling"
    assert remote.inspect(binding)["head_sha"] == binding["commit_sha"]
    result = reopen().advance(intent["id"], principal="controller")
    assert result["state"] == "pushed"
    assert remote.pushes == 1
    assert result["operations"][0]["reconciled"] is True
    assert prs.items == []


def test_pr_publication_has_its_own_activation_and_separate_ci_and_merge(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    coordinator, intent, _, prs, _ = fixture_delivery(tmp_path, repository)
    coordinator.advance(intent["id"], principal="controller")
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["state"] == "delivered"
    assert [item["step"] for item in result["operations"]] == ["push", "pr"]
    assert len({item["activation"]["id"] for item in result["operations"]}) == 2
    assert result["pr"]["id"] == "fixture-pr-1"
    assert result["ci"] == {"sha": intent["request"]["commit_sha"], "status": "pending"}
    assert result["merge"] == {"merged": False}
    assert result["completion"] == {"requirements_satisfied": True, "scope": "offline_fixture"}
    assert coordinator.advance(intent["id"], principal="controller")["state"] == "delivered"
    assert len(prs.items) == 1


class LostPrReply(FixturePullRequests):
    def __init__(self, remote: LocalGitRemote, *, materialize: bool) -> None:
        super().__init__(remote)
        self.materialize = materialize
        self.publishes = 0

    def publish(self, binding: dict, existing_id: str | None) -> dict:
        self.publishes += 1
        if self.materialize:
            super().publish(binding, existing_id)
        raise RemoteUnknown("synthetic PR request lost reply")


@pytest.mark.parametrize("materialize", [True, False])
def test_lost_pr_reply_queries_without_duplicate_even_when_lookup_is_empty(
    tmp_path: Path, repository: tuple[Path, Path, dict], materialize: bool
) -> None:
    source, remote_path, binding = repository
    remote = LocalGitRemote(source, remote_path, repository_id="fixture-repo")
    authority = FixtureAuthority()
    prs = LostPrReply(remote, materialize=materialize)

    def reopen() -> DeliveryCoordinator:
        return DeliveryCoordinator(
            tmp_path / "delivery.sqlite",
            git_remote=remote,
            pr_service=prs,
            verification_reader=authority,
            mode="offline_fixture",
        )

    coordinator = reopen()
    intent = coordinator.plan(binding, command_key="plan", principal="controller")
    authority.grant(intent)
    coordinator.advance(intent["id"], principal="controller")
    uncertain = coordinator.advance(intent["id"], principal="controller")
    assert uncertain["state"] == "reconciling"
    result = reopen().advance(intent["id"], principal="controller")
    assert result["state"] == ("delivered" if materialize else "reconciling")
    assert prs.publishes == 1
    assert len(result["operations"]) == 2
    assert result["operations"][1]["state"] == ("confirmed" if materialize else "send_unknown")


def test_preconfigured_ci_gate_waits_for_the_exact_published_commit(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    source, remote_path, binding = repository
    coordinator, intent, _, prs, _ = fixture_delivery(
        tmp_path, (source, remote_path, dict(binding, require_ci=True))
    )
    coordinator.advance(intent["id"], principal="controller")
    pending = coordinator.advance(intent["id"], principal="controller")
    assert pending["state"] == "awaiting_ci"
    assert pending["completion"]["requirements_satisfied"] is False
    prs.items[0].update(ci_status="success", ci_sha=binding["tested_base_sha"])
    assert coordinator.advance(intent["id"], principal="controller")["state"] == "awaiting_ci"
    prs.items[0]["ci_sha"] = binding["commit_sha"]
    assert coordinator.advance(intent["id"], principal="controller")["state"] == "delivered"
    assert len(prs.items) == 1


@pytest.mark.parametrize("control", ["paused", "cancelled", "revoked"])
def test_control_after_push_blocks_unactivated_pr_creation(
    tmp_path: Path, repository: tuple[Path, Path, dict], control: str
) -> None:
    coordinator, intent, remote, prs, _ = fixture_delivery(tmp_path, repository)
    coordinator.advance(intent["id"], principal="controller")
    coordinator.set_control("run-1", control, command_key="stop", principal="controller")
    stopped = coordinator.advance(intent["id"], principal="controller")
    assert stopped["reason"] == "DELIVERY_CONTROL_" + control.upper()
    assert [item["step"] for item in stopped["operations"]] == ["push"]
    assert remote.inspect(intent["request"])["head_sha"] == intent["request"]["commit_sha"]
    assert prs.items == []
    if control == "paused":
        coordinator.set_control("run-1", "active", command_key="resume", principal="controller")
        assert coordinator.advance(intent["id"], principal="controller")["state"] == "delivered"
    else:
        with pytest.raises(DeliveryError, match="DELIVERY_CONTROL_TERMINAL"):
            coordinator.set_control("run-1", "active", command_key="resume", principal="controller")


def test_run_branch_ownership_spans_delivery_revisions(tmp_path: Path) -> None:
    coordinator = DeliveryCoordinator(tmp_path / "delivery.sqlite")
    coordinator.plan(request(), command_key="first", principal="controller")
    with pytest.raises(DeliveryError, match="MANAGED_BRANCH_OWNED"):
        coordinator.plan(
            dict(request(), run_id="run-2"), command_key="other-run", principal="controller"
        )
    with pytest.raises(DeliveryError, match="RUN_DELIVERY_TARGET_CHANGED"):
        coordinator.plan(
            dict(request(), delivery_revision=2, managed_branch="codex/another"),
            command_key="changed-target",
            principal="controller",
        )


def test_new_delivery_revision_updates_the_same_pr(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    source, _, binding = repository
    coordinator, intent, _, prs, authority = fixture_delivery(tmp_path, repository)
    coordinator.advance(intent["id"], principal="controller")
    first = coordinator.advance(intent["id"], principal="controller")
    (source / "app.txt").write_bytes(b"revision two\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "revision two")
    updated = dict(
        binding,
        delivery_revision=2,
        verification_ref="receipt-v2",
        expected_old_sha=binding["commit_sha"],
        commit_sha=git(source, "rev-parse", "HEAD"),
        tree_sha=git(source, "rev-parse", "HEAD^{tree}"),
    )
    second = coordinator.plan(updated, command_key="second", principal="controller")
    authority.grant(second)
    coordinator.advance(second["id"], principal="controller")
    result = coordinator.advance(second["id"], principal="controller")
    assert result["state"] == "delivered"
    assert result["pr"]["id"] == first["pr"]["id"]
    assert result["pr"]["head_sha"] == updated["commit_sha"]
    assert len(prs.items) == 1


@pytest.mark.parametrize("variant", ["replacement", "missing"])
def test_later_revision_cannot_replace_or_recreate_the_runs_confirmed_pr(
    tmp_path: Path,
    repository: tuple[Path, Path, dict],
    variant: str,
) -> None:
    coordinator, first, remote, prs, authority = fixture_delivery(tmp_path, repository)
    coordinator.advance(first["id"], principal="controller")
    original = coordinator.advance(first["id"], principal="controller")
    second = coordinator.plan(
        dict(
            first["request"],
            delivery_revision=2,
            expected_old_sha=first["request"]["commit_sha"],
            verification_ref="v2",
        ),
        command_key="second",
        principal="controller",
    )
    authority.grant(second)
    coordinator.advance(second["id"], principal="controller")
    if variant == "replacement":
        prs.items[0] = dict(prs.items[0], id="replacement-pr")
    else:
        prs.items.clear()
    reopened = DeliveryCoordinator(
        tmp_path / "delivery.sqlite",
        git_remote=remote,
        pr_service=prs,
        verification_reader=authority,
        mode="offline_fixture",
    )
    result = reopened.advance(second["id"], principal="controller")
    assert result["state"] == "blocked"
    assert result["reason"] == "PR_IDENTITY_CONFLICT"
    assert result["completion"]["requirements_satisfied"] is False
    assert len(result["operations"]) == 1
    assert reopened.get(first["id"])["pr"]["id"] == original["pr"]["id"]
    if variant == "missing":
        assert prs.items == []


@pytest.mark.parametrize("variant", ["head", "base"])
def test_pr_publish_cannot_complete_using_a_git_observation_taken_before_publish(
    tmp_path: Path,
    repository: tuple[Path, Path, dict],
    variant: str,
) -> None:
    _, remote_path, binding = repository
    coordinator, intent, remote, _, _ = fixture_delivery(tmp_path, repository)

    class ChangeDuringPublish(FixturePullRequests):
        def publish(self, binding: dict, existing_id: str | None) -> dict:
            result = super().publish(binding, existing_id)
            branch = binding["managed_branch"] if variant == "head" else binding["base_branch"]
            sha = binding["tested_base_sha"] if variant == "head" else binding["commit_sha"]
            git(remote_path, "update-ref", "refs/heads/" + branch, sha)
            return result

    coordinator.pr_service = ChangeDuringPublish(remote)
    coordinator.advance(intent["id"], principal="controller")
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["completion"]["requirements_satisfied"] is False
    assert result["state"] == "blocked"
    assert result["reason"] == (
        "REMOTE_HEAD_CHANGED" if variant == "head" else "TESTED_BASE_CHANGED"
    )


@pytest.mark.parametrize("variant", ["missing_ci", "merged_string", "not_an_object", "bad_ci_sha"])
def test_malformed_publish_observation_keeps_unknown_without_repeat_write(
    tmp_path: Path,
    repository: tuple[Path, Path, dict],
    variant: str,
) -> None:
    coordinator, intent, remote, _, _ = fixture_delivery(tmp_path, repository)

    class MalformedReply(FixturePullRequests):
        publishes = 0

        def publish(self, binding: dict, existing_id: str | None) -> dict:
            self.publishes += 1
            result = dict(super().publish(binding, existing_id))
            if variant == "missing_ci":
                del result["ci_status"]
            elif variant == "merged_string":
                result["merged"] = "false"
            elif variant == "bad_ci_sha":
                result["ci_sha"] = "not-a-sha"
            else:
                return None
            return result

    prs = MalformedReply(remote)
    coordinator.pr_service = prs
    coordinator.advance(intent["id"], principal="controller")
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["state"] == "reconciling"
    assert result["completion"]["requirements_satisfied"] is False
    assert result["operations"][-1]["state"] == "send_unknown"
    recovered = coordinator.advance(intent["id"], principal="controller")
    assert recovered["state"] == "delivered"
    assert prs.publishes == 1


def test_pr_with_wrong_identity_is_not_adopted_or_duplicated(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    coordinator, intent, _, prs, _ = fixture_delivery(tmp_path, repository)
    coordinator.advance(intent["id"], principal="controller")
    prs.publish(intent["request"], None)
    prs.items[0]["run_id"] = "foreign-run"
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["state"] == "blocked"
    assert result["reason"] == "PR_IDENTITY_CONFLICT"
    assert len(result["operations"]) == 1
    assert len(prs.items) == 1


def test_published_delivery_invalidates_completion_when_remote_head_changes(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    _, remote, binding = repository
    coordinator, intent, _, _, _ = fixture_delivery(tmp_path, repository)
    coordinator.advance(intent["id"], principal="controller")
    coordinator.advance(intent["id"], principal="controller")
    git(remote, "update-ref", "refs/heads/" + binding["managed_branch"], binding["tested_base_sha"])
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["reason"] == "REMOTE_HEAD_CHANGED"
    assert result["completion"]["requirements_satisfied"] is False


def test_unresolved_older_pr_blocks_a_later_revision_without_another_write(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    source, remote_path, binding = repository
    remote = LocalGitRemote(source, remote_path, repository_id="fixture-repo")
    authority = FixtureAuthority()
    prs = LostPrReply(remote, materialize=False)
    coordinator = DeliveryCoordinator(
        tmp_path / "delivery.sqlite",
        git_remote=remote,
        pr_service=prs,
        verification_reader=authority,
        mode="offline_fixture",
    )
    first = coordinator.plan(binding, command_key="first", principal="controller")
    authority.grant(first)
    coordinator.advance(first["id"], principal="controller")
    coordinator.advance(first["id"], principal="controller")
    second = coordinator.plan(
        dict(
            binding,
            delivery_revision=2,
            expected_old_sha=binding["commit_sha"],
            verification_ref="v2",
        ),
        command_key="second",
        principal="controller",
    )
    authority.grant(second)
    blocked = coordinator.advance(second["id"], principal="controller")
    assert blocked["reason"] == "DELIVERY_PREVIOUS_UNRESOLVED"
    assert blocked["operations"] == []
    assert prs.publishes == 1


def test_pause_during_verification_precedes_and_prevents_activation(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    coordinator, intent, remote, prs, authority = fixture_delivery(tmp_path, repository)

    def revoke_before_receipt(reference: str) -> dict:
        coordinator.set_control(
            "run-1", "paused", command_key="pause-during-verify", principal="controller"
        )
        return authority(reference)

    coordinator.verification_reader = revoke_before_receipt
    blocked = coordinator.advance(intent["id"], principal="controller")
    assert blocked["reason"] == "DELIVERY_CONTROL_PAUSED"
    assert blocked["operations"] == []
    assert remote.inspect(intent["request"])["head_sha"] is None
    assert prs.items == []


def test_lost_pr_reply_after_cancel_is_observed_without_claiming_run_completion(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    source, remote_path, binding = repository
    remote = LocalGitRemote(source, remote_path, repository_id="fixture-repo")
    authority = FixtureAuthority()
    prs = LostPrReply(remote, materialize=True)
    coordinator = DeliveryCoordinator(
        tmp_path / "delivery.sqlite",
        git_remote=remote,
        pr_service=prs,
        verification_reader=authority,
        mode="offline_fixture",
    )
    intent = coordinator.plan(binding, command_key="first", principal="controller")
    authority.grant(intent)
    coordinator.advance(intent["id"], principal="controller")
    coordinator.advance(intent["id"], principal="controller")
    coordinator.set_control("run-1", "cancelled", command_key="cancel", principal="controller")
    reconciled = coordinator.advance(intent["id"], principal="controller")
    assert reconciled["operations"][-1]["state"] == "confirmed"
    assert reconciled["pr"]["id"] == "fixture-pr-1"
    assert reconciled["reason"] == "DELIVERY_CONTROL_CANCELLED"
    assert reconciled["completion"]["requirements_satisfied"] is False
    assert prs.publishes == 1


def test_pr_recovery_checks_actual_git_head_again(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    source, remote_path, binding = repository
    remote = LocalGitRemote(source, remote_path, repository_id="fixture-repo")
    authority = FixtureAuthority()

    class StalePrView(LostPrReply):
        def lookup(self, binding: dict) -> list[dict]:
            return self.items

    prs = StalePrView(remote, materialize=True)
    coordinator = DeliveryCoordinator(
        tmp_path / "delivery.sqlite",
        git_remote=remote,
        pr_service=prs,
        verification_reader=authority,
        mode="offline_fixture",
    )
    intent = coordinator.plan(binding, command_key="first", principal="controller")
    authority.grant(intent)
    coordinator.advance(intent["id"], principal="controller")
    coordinator.advance(intent["id"], principal="controller")
    git(
        remote_path,
        "update-ref",
        "refs/heads/" + binding["managed_branch"],
        binding["tested_base_sha"],
    )
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["reason"] == "REMOTE_HEAD_CHANGED"
    assert result["completion"]["requirements_satisfied"] is False
    assert prs.publishes == 1


@pytest.mark.parametrize("variant", ["revoked_receipt", "production_reopen"])
def test_pr_recovery_records_observation_without_bypassing_current_qualification(
    tmp_path: Path, repository: tuple[Path, Path, dict], variant: str
) -> None:
    source, remote_path, binding = repository
    remote = LocalGitRemote(source, remote_path, repository_id="fixture-repo")
    authority = FixtureAuthority()
    prs = LostPrReply(remote, materialize=True)
    database = tmp_path / "delivery.sqlite"
    coordinator = DeliveryCoordinator(
        database,
        git_remote=remote,
        pr_service=prs,
        verification_reader=authority,
        mode="offline_fixture",
    )
    intent = coordinator.plan(binding, command_key="first", principal="controller")
    authority.grant(intent)
    coordinator.advance(intent["id"], principal="controller")
    coordinator.advance(intent["id"], principal="controller")
    if variant == "revoked_receipt":
        authority.receipts[binding["verification_ref"]]["decision"] = "deny"
        reason = "VERIFICATION_BINDING_NOT_ALLOWED"
    else:
        coordinator = DeliveryCoordinator(
            database, git_remote=remote, pr_service=prs, verification_reader=authority
        )
        reason = "DELIVERY_QUALIFICATION_NOT_RUN"
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["operations"][-1]["state"] == "confirmed"
    assert result["pr"]["id"] == "fixture-pr-1"
    assert result["completion"]["requirements_satisfied"] is False
    assert result["reason"] == reason
    assert prs.publishes == 1


@pytest.mark.parametrize("variant", ["empty_key", "surrogate_key", "surrogate_run"])
def test_malformed_identifiers_are_rejected_before_persistence(
    tmp_path: Path, variant: str
) -> None:
    coordinator = DeliveryCoordinator(tmp_path / "delivery.sqlite")
    binding = request()
    key = "normal"
    if variant == "empty_key":
        key = ""
    elif variant == "surrogate_key":
        key = "\ud800"
    else:
        binding["run_id"] = "\ud800"
    with pytest.raises(DeliveryError, match="DELIVERY_INPUT_INVALID"):
        coordinator.plan(binding, command_key=key, principal="controller")


def test_control_state_is_immediately_readable_and_idempotent(tmp_path: Path) -> None:
    coordinator = DeliveryCoordinator(tmp_path / "delivery.sqlite")
    intent = coordinator.plan(request(), command_key="create", principal="controller")
    first = coordinator.set_control(
        "run-1", "cancelled", command_key="cancel", principal="controller"
    )
    assert (
        coordinator.set_control("run-1", "cancelled", command_key="cancel", principal="controller")
        == first
    )
    snapshot = DeliveryCoordinator(tmp_path / "delivery.sqlite").get(intent["id"])
    assert snapshot["control_state"] == "cancelled"
    assert snapshot["completion"]["requirements_satisfied"] is False
    assert snapshot["operations"] == []


def test_late_unknown_reply_does_not_erase_an_already_confirmed_pr(
    tmp_path: Path, repository: tuple[Path, Path, dict]
) -> None:
    coordinator, intent, remote, _, _ = fixture_delivery(tmp_path, repository)

    class ReorderedReply(FixturePullRequests):
        def publish(self, binding: dict, existing_id: str | None) -> dict:
            super().publish(binding, existing_id)
            observed = coordinator.advance(intent["id"], principal="controller")
            assert observed["state"] == "delivered"
            raise RemoteUnknown("late lost reply after another observer confirmed the PR")

    prs = ReorderedReply(remote)
    coordinator.pr_service = prs
    coordinator.advance(intent["id"], principal="controller")
    result = coordinator.advance(intent["id"], principal="controller")
    assert result["state"] == "delivered"
    assert result["completion"]["requirements_satisfied"] is True
    assert len(prs.items) == 1
