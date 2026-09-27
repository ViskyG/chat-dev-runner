"""Read-only, owner-local recovery of exact recorded external effect identities."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.domain.commands import DomainError, LocalPolicy, Provenance, freeze, parse_command
from runner_app.engine.service import Engine
from runner_app.storage.database import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.reconcile import EffectProof, ReconcileWorkflow

OWNER = Provenance("local", "owner-session")
MARKER = "cdr:v1:" + "a" * 64
COMMIT = "b" * 40


def _command(ledger: Ledger, action: str, *, target_id: str | None = None,
             project: str = "notes-lab", workspace: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    params: dict[str, Any] = ({
        "name": "Notes Lab", "source": {"kind": "new"},
        "template_id": "python-basic", "github_identity": "owner", "visibility": "private",
    } if action == "CREATE_PROJECT" else {
        "reconcile_version": "1.0", "target_request_id": target_id,
    })
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": project, "workspace_id": workspace,
        "expected_head": None, "params": params, "intent": "Inspect recorded effect identities",
    }


def _target(tmp_path: Path, effects: list[tuple[str, str, dict[str, Any]]]
            ) -> tuple[Ledger, dict[str, Any], list[str]]:
    ledger = Ledger(Database(tmp_path / "runner.sqlite"))
    ledger.startup()
    ledger.arm()
    command = _command(ledger, "CREATE_PROJECT")
    admission = ledger.admit(command, freeze(command).digest, "root-command",
                             ("repo:notes-lab", "global:owner", "global:drive-installation"))
    assert admission.execute
    ids = [ledger.begin_effect(command["request_id"], step, kind, intent)
           for step, kind, intent in effects]
    ledger.finish(command["request_id"], "BLOCKED", "PROVISION_PARTIAL")
    assert ledger.holds_lock(command["request_id"], "repo:notes-lab")
    ledger.disarm()
    return ledger, command, ids


class FakeReadPort:
    def __init__(self, provider_id: str | None, *, change_intent: bool = False,
                 fail: bool = False) -> None:
        self.provider_id = provider_id
        self.change_intent = change_intent
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, target: Mapping[str, Any], effect: Mapping[str, Any]
                 ) -> EffectProof | None:
        assert target["request_id"]
        self.calls.append(effect["effect_id"])
        if self.fail:
            raise RuntimeError("untrusted provider response")
        if self.provider_id is None:
            return None
        intent = dict(effect["intent"])
        if self.change_intent:
            intent["ref"] = "refs/heads/wrong"
        return EffectProof(effect["effect_id"], self.provider_id, intent)


def _ports(overrides: Mapping[str, FakeReadPort] | None = None) -> dict[str, FakeReadPort]:
    values = {kind: FakeReadPort(None) for kind in
              ("github_repository", "git_ref", "pull_request", "drive_file")}
    values.update(overrides or {})
    return values


def _submit(ledger: Ledger, target: Mapping[str, Any], ports: Mapping[str, FakeReadPort],
            *, provenance: Provenance = OWNER, project: str = "notes-lab",
            workspace: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = ReconcileWorkflow(ledger, owner_source_ids=frozenset({OWNER.source_id}),
                                 ports=ports)
    engine = Engine(ledger, policy=LocalPolicy(), handlers={"RECONCILE": workflow})
    command = _command(ledger, "RECONCILE", target_id=target["request_id"],
                       project=project, workspace=workspace)
    receipt = engine.submit(json.dumps(command).encode(), provenance)
    if receipt.execute:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = ledger.get(command["request_id"])
            assert observed is not None
            if observed["status"] != "RUNNING":
                break
            time.sleep(0.01)
    observed = ledger.get(command["request_id"])
    assert observed is not None
    return command, observed


@pytest.mark.parametrize("kind,step,intent,provider_id", [
    ("github_repository", "github-repository",
     {"owner": "owner", "name": "notes-lab", "private": True, "marker": MARKER}, "42"),
    ("git_ref", "baseline-main",
     {"repository_id": 42, "ref": "refs/heads/main", "commit": COMMIT}, COMMIT),
    ("pull_request", "draft-pull-request",
     {"repository_id": 42, "run_id": str(uuid4()), "head": "work/notes-lab", "base": "main",
      "commit": COMMIT, "title": "Review", "body": "Exact run"}, "81"),
    ("drive_file", "drive-project-folder",
     {"parent_id": "parent123", "installation_id": str(uuid4()),
      "key": "project:notes-lab", "name": "Notes Lab",
      "mime_type": "application/vnd.google-apps.folder",
      "project_id": "notes-lab", "workspace_id": None}, "folder123"),
])
def test_lost_acknowledgement_confirms_exact_positive_read_only_proof(
    tmp_path: Path, kind: str, step: str, intent: dict[str, Any], provider_id: str,
) -> None:
    ledger, target, ids = _target(tmp_path, [(step, kind, intent)])
    port = FakeReadPort(provider_id)
    command, result = _submit(ledger, target, _ports({kind: port}))
    assert result["status"] == "COMPLETED" and result["result"]["ready_for_resume"] is True
    assert result["result"]["confirmed_effect_ids"] == ids
    assert port.calls == ids
    assert ledger.get(target["request_id"])["effects"][0]["provider_id"] == provider_id
    assert ledger.holds_lock(target["request_id"], "repo:notes-lab")
    assert not ledger.holds_lock(command["request_id"], "repo:notes-lab")
    assert ledger.state()["armed"] is False


@pytest.mark.parametrize("kind,step,intent,provider_id", [
    ("github_repository", "github-repository",
     {"owner": "owner", "name": "notes-lab", "private": True, "marker": MARKER}, "42"),
    ("git_ref", "baseline-main",
     {"repository_id": 42, "ref": "refs/heads/main", "commit": COMMIT}, "c" * 40),
    ("pull_request", "draft-pull-request",
     {"repository_id": 42, "head": "work/notes-lab", "base": "main",
      "commit": COMMIT, "title": "Review", "body": "Exact run"}, "81"),
    ("drive_file", "drive-project-folder",
     {"parent_id": "parent123", "installation_id": str(uuid4()),
      "key": "project:notes-lab"}, "file12345"),
])
def test_negative_or_wrong_identity_never_confirms_or_replays(
    tmp_path: Path, kind: str, step: str, intent: dict[str, Any], provider_id: str,
) -> None:
    ledger, target, ids = _target(tmp_path, [(step, kind, intent)])
    port = FakeReadPort(provider_id, change_intent=kind in {"github_repository", "pull_request", "drive_file"})
    _, result = _submit(ledger, target, _ports({kind: port}))
    assert result["status"] == "COMPLETED"
    assert result["result"]["ready_for_resume"] is False
    assert result["result"]["remaining_effect_ids"] == ids
    assert ledger.get(target["request_id"])["effects"][0]["state"] == "UNKNOWN"
    assert ledger.holds_lock(target["request_id"], "repo:notes-lab")


def test_unobserved_and_unavailable_reads_keep_identity_uncertain(tmp_path: Path) -> None:
    effects = [
        ("github-repository", "github_repository",
         {"owner": "owner", "name": "notes-lab", "private": True, "marker": MARKER}),
        ("baseline-main", "git_ref",
         {"repository_id": 42, "ref": "refs/heads/main", "commit": COMMIT}),
    ]
    ledger, target, ids = _target(tmp_path, effects)
    ports = _ports({"github_repository": FakeReadPort(None),
                    "git_ref": FakeReadPort(COMMIT, fail=True)})
    _, result = _submit(ledger, target, ports)
    assert result["result"]["remaining_effect_ids"] == ids
    assert [item["code"] for item in result["result"]["unresolved_effects"]] == [
        "REMOTE_EFFECT_NOT_OBSERVED", "PROVIDER_READ_UNAVAILABLE",
    ]
    assert all(item["state"] == "UNKNOWN" for item in
               ledger.get(target["request_id"])["effects"])


def test_owner_only_exact_scope_and_version_are_enforced(tmp_path: Path) -> None:
    ledger, target, ids = _target(tmp_path, [
        ("github-repository", "github_repository",
         {"owner": "owner", "name": "notes-lab", "private": True, "marker": MARKER}),
    ])
    ports = _ports({"github_repository": FakeReadPort("42")})
    _, wrong_scope = _submit(ledger, target, ports, project="other-project")
    assert wrong_scope["status"] == "REJECTED"
    assert wrong_scope["code"] == "RECONCILE_TARGET_SCOPE_MISMATCH"
    _, drive = _submit(ledger, target, ports,
                       provenance=Provenance("drive", "root-command"))
    assert drive["status"] == "REJECTED" and drive["code"] == "SOURCE_SCOPE"
    assert ports["github_repository"].calls == []
    command = _command(ledger, "RECONCILE", target_id=target["request_id"])
    command["params"]["reconcile_version"] = "2.0"
    with pytest.raises(DomainError) as error:
        parse_command(json.dumps(command).encode())
    assert error.value.code == "INVALID_SCHEMA"
    command["params"]["reconcile_version"] = "1.0"
    command["schema_version"] = "2.0"
    with pytest.raises(DomainError):
        parse_command(json.dumps(command).encode())
    assert ledger.get(target["request_id"])["effects"][0]["effect_id"] == ids[0]


def test_unproved_local_workspace_remains_explicitly_unresolved(tmp_path: Path) -> None:
    ledger, target, ids = _target(tmp_path, [
        ("local-project", "local_workspace", {"project_id": "notes-lab", "kind": "new"}),
    ])
    _, result = _submit(ledger, target, _ports())
    assert result["result"]["remaining_effect_ids"] == ids
    assert result["result"]["ready_for_resume"] is False
    assert result["result"]["unresolved_effects"][0]["code"] == "EFFECT_KIND_UNSUPPORTED"


def test_report_link_lost_ack_requires_exact_existing_pr_and_body(tmp_path: Path) -> None:
    folder = "runfolder123"
    url = f"https://drive.google.com/drive/folders/{folder}"
    body = "Source run: exact\n"
    intent = {
        "repository_id": 42, "run_id": str(uuid4()), "pull_request_id": 81,
        "commit": COMMIT, "run_folder_id": folder, "url": url,
        "title": "Review", "previous_body": body,
        "body": body + f"Report: {url}\n",
    }
    ledger, target, ids = _target(tmp_path, [
        ("attach-run-report-link", "pull_request", intent),
    ])
    _, result = _submit(ledger, target, _ports({"pull_request": FakeReadPort("81")}))
    assert result["result"]["confirmed_effect_ids"] == ids
    assert ledger.get(target["request_id"])["effects"][0]["provider_id"] == "81"


def test_drive_document_lost_ack_requires_saved_id_and_hash(tmp_path: Path) -> None:
    intent = {"file_id": "document123", "key": "latest-result",
              "base_revision": "17", "desired_sha256": "a" * 64}
    ledger, target, ids = _target(tmp_path, [
        ("drive-doc:fixed", "drive_file", intent),
    ])
    _, result = _submit(ledger, target, _ports({"drive_file": FakeReadPort("document123")}))
    assert result["result"]["confirmed_effect_ids"] == ids
    assert ledger.get(target["request_id"])["effects"][0]["provider_id"] == "document123"


def test_wrong_owner_source_and_workspace_never_read_provider(tmp_path: Path) -> None:
    ledger, target, _ = _target(tmp_path, [
        ("github-repository", "github_repository",
         {"owner": "owner", "name": "notes-lab", "private": True, "marker": MARKER}),
    ])
    port = FakeReadPort("42")
    ports = _ports({"github_repository": port})
    _, wrong_owner = _submit(ledger, target, ports,
                             provenance=Provenance("local", "unregistered-session"))
    assert wrong_owner["status"] == "BLOCKED"
    assert wrong_owner["code"] == "RECONCILE_ADMISSION_MISMATCH"
    _, wrong_workspace = _submit(ledger, target, ports, workspace="primary")
    assert wrong_workspace["status"] == "REJECTED"
    assert wrong_workspace["code"] == "RECONCILE_TARGET_SCOPE_MISMATCH"
    assert port.calls == []


def test_reconcile_does_not_start_any_new_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, target, ids = _target(tmp_path, [
        ("github-repository", "github_repository",
         {"owner": "owner", "name": "notes-lab", "private": True, "marker": MARKER}),
    ])

    def fail_new_effect(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("Recovery attempted a new effect")

    monkeypatch.setattr(ledger, "begin_effect", fail_new_effect)
    _, result = _submit(ledger, target, _ports({"github_repository": FakeReadPort("42")}))
    assert result["status"] == "COMPLETED"
    assert result["result"]["confirmed_effect_ids"] == ids
    assert len(ledger.get(target["request_id"])["effects"]) == 1
    assert ledger.holds_lock(target["request_id"], "repo:notes-lab")
