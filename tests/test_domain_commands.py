"""Pure command validation and owner-granted authority: no provider or executor calls."""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from runner_app.domain.commands import (
    DomainError,
    LocalPolicy,
    Provenance,
    authorize,
    freeze,
    parse_command,
    parse_json,
    validate_shape,
    validate_time,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 0, 5, tzinfo=timezone.utc)
EPOCH = "00000000-0000-4000-8000-000000000000"
ROOT_SOURCE = Provenance("drive", "root-command")
PROJECT_SOURCE = Provenance("drive", "project-command", "toy-project")
WORKSPACE_SOURCE = Provenance("drive", "workspace-command", "toy-project", "experiment-a")
LOCAL_SOURCE = Provenance("local", "authenticated-owner-session")


@pytest.fixture
def command() -> dict[str, Any]:
    value = json.loads((ROOT / "examples/verify.command.json").read_text())
    value["dispatch"] = "READY"
    return value


@pytest.fixture
def project_command() -> dict[str, Any]:
    value = json.loads((ROOT / "examples/create-project.command.json").read_text())
    value["dispatch"] = "READY"
    return value


@pytest.fixture
def policy() -> LocalPolicy:
    return LocalPolicy(
        mailboxes={source.source_id: source for source in
                   (ROOT_SOURCE, PROJECT_SOURCE, WORKSPACE_SOURCE)},
        approved_profiles=frozenset({"python-quick"}),
        approved_templates=frozenset({"python-basic"}),
        import_handles=frozenset({"local-grant"}),
        github_identities=frozenset({"personal"}),
        project_ids=frozenset({"toy-project", "other-project"}),
        workspace_pairs=frozenset({("toy-project", "experiment-a"),
                                   ("other-project", "experiment-a")}),
        approved_github_repositories=frozenset({"approved-owner/example"}),
    )


def raw(command: dict[str, Any]) -> bytes:
    return json.dumps(command, ensure_ascii=False).encode("utf-8")


def check_time(command: dict[str, Any], **options: Any) -> None:
    validate_time(command, runner_id="local-runner", epoch=EPOCH, now=NOW, **options)


@pytest.mark.parametrize("payload,code", [
    (b'{"a":1,"a":2}', "DUPLICATE_JSON_KEY"),
    (b'{"params":{"profile_id":"one","profile_id":"two"}}', "DUPLICATE_JSON_KEY"),
    (b'{"a":NaN}', "INVALID_JSON"), (b'{"a":Infinity}', "INVALID_JSON"),
    (b'{"a":-Infinity}', "INVALID_JSON"), (b'{"a":1e9999}', "INVALID_JSON"),
    (b'[]', "INVALID_JSON"), (b'null', "INVALID_JSON"), (b'false', "INVALID_JSON"),
    (b'{"a":', "INVALID_JSON"), (b'{"a":1} trailing', "INVALID_JSON"),
    (b'\xef\xbb\xbf{}', "INVALID_JSON"), (b'{"a":"\xff"}', "INVALID_UTF8"),
    (b'{"a":"\\ud800"}', "INVALID_UTF8"),
])
def test_strict_json_refuses_ambiguous_or_invalid_bytes(payload: bytes, code: str) -> None:
    with pytest.raises(DomainError) as error:
        parse_json(payload)
    assert error.value.code == code


def test_payload_budget_counts_utf8_bytes(command: dict[str, Any]) -> None:
    command["intent"] = "Привет"
    payload = raw(command)
    assert parse_command(payload, max_bytes=len(payload)).command == command
    with pytest.raises(DomainError) as error:
        parse_command(payload, max_bytes=len(payload) - 1)
    assert error.value.code == "PAYLOAD_TOO_LARGE"
    with pytest.raises(DomainError) as error:
        parse_json(b" " * 262_145)
    assert error.value.code == "PAYLOAD_TOO_LARGE"


def test_deep_json_is_a_bounded_refusal() -> None:
    with pytest.raises(DomainError) as error:
        parse_json(b'{"a":' + b'[' * 200 + b'0' + b']' * 200 + b'}')
    assert error.value.code == "INVALID_JSON"


@pytest.mark.parametrize("field,value", [
    ("credential", "do-not-echo-secret"), ("action", "RUN_SHELL"),
    ("action", "RECONCILE"), ("action", "RESUME_PROVISION"),
    ("request_id", "not-a-uuid"), ("request_id", "a" * 32),
    ("request_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ("epoch", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ("expected_head", "main"), ("workspace_id", None),
    ("issued_at", "2026-09-26T00:00:00"), ("expires_at", "2026-13-26T00:15:00Z"),
    ("issued_at", "2026-09-26 00:00:00Z"), ("issued_at", "2026-09-26T00:00Z"),
    ("params", {"profile_id": "python-quick", "argv": ["bash"]}),
    ("params", {"profile_id": "python-quick", "network": True}),
])
def test_schema_rejects_unknown_fields_actions_invalid_ids_and_parameters(
    command: dict[str, Any], field: str, value: Any,
) -> None:
    command[field] = value
    with pytest.raises(DomainError) as error:
        parse_command(raw(command))
    assert error.value.code == ("INVALID_TIME" if field in {"issued_at", "expires_at"}
                                else "INVALID_SCHEMA")
    assert "do-not-echo-secret" not in str(error.value)
    assert "bash" not in str(error.value)


def test_malformed_shape_can_be_frozen_for_terminal_receipt(command: dict[str, Any]) -> None:
    command["action"] = "arbitrary-secret-action"
    parsed = parse_json(raw(command))
    captured = freeze(parsed)
    assert captured.command["request_id"] == command["request_id"]
    assert len(captured.digest) == 64
    with pytest.raises(DomainError) as error:
        validate_shape(parsed)
    assert "arbitrary-secret-action" not in str(error.value)


def test_capture_is_detached_and_preserves_unicode_and_newlines(command: dict[str, Any]) -> None:
    command["intent"] = "é\r\nстрока\n"
    captured = parse_command(raw(command))
    same = freeze(dict(reversed(list(command.items()))))
    assert captured.digest == same.digest
    assert captured.command["intent"] == "é\r\nстрока\n"
    command["params"]["profile_id"] = "changed-original"
    view = captured.command
    view["params"]["profile_id"] = "changed-view"
    assert captured.command["params"]["profile_id"] == "python-quick"
    assert captured.digest == same.digest
    for text in ("e\u0301\r\nстрока\n", "é\nстрока\n"):
        changed = captured.command
        changed["intent"] = text
        assert freeze(changed).digest != captured.digest
    with pytest.raises(FrozenInstanceError):
        captured.digest = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field,value,code", [
    ("dispatch", "DRAFT", "NOT_READY"),
    ("runner_id", "other-runner", "WRONG_RUNNER_EPOCH"),
    ("epoch", "11111111-1111-4111-8111-111111111111", "WRONG_RUNNER_EPOCH"),
    ("expires_at", "2026-09-26T00:05:00Z", "EXPIRED"),
    ("expires_at", "2026-09-26T00:00:00Z", "INVALID_TTL"),
    ("expires_at", "2026-09-25T23:59:59Z", "INVALID_TTL"),
    ("expires_at", "2026-09-26T00:15:01Z", "INVALID_TTL"),
    ("issued_at", "2026-09-26T00:05:31Z", "FUTURE_COMMAND"),
])
def test_time_gate_rejects_stale_expired_or_unready_commands(
    command: dict[str, Any], field: str, value: str, code: str,
) -> None:
    command[field] = value
    with pytest.raises(DomainError) as error:
        check_time(command)
    assert error.value.code == code


def test_offset_timestamps_are_valid_without_payload_normalization(command: dict[str, Any]) -> None:
    command["issued_at"] = "2026-09-26T04:00:00+04:00"
    command["expires_at"] = "2026-09-26T04:15:00+04:00"
    captured = parse_command(raw(command))
    check_time(captured.command)
    assert captured.command["issued_at"] == "2026-09-26T04:00:00+04:00"
    command["issued_at"] = "2026-09-26T00:05:30Z"
    check_time(command)
    with pytest.raises(DomainError) as error:
        validate_time(command, runner_id="local-runner", epoch=EPOCH, now=NOW.replace(tzinfo=None))
    assert error.value.code == "INVALID_TIME"


@pytest.mark.parametrize("path", [
    "../secret", "/etc/passwd", ".env", ".git/config", "a//b", "x\\y",
    "C:/secret", "secrets/token", ".github/workflows/upload.yml",
])
def test_export_paths_cannot_escape_or_select_protected_files(
    command: dict[str, Any], path: str,
) -> None:
    command.update(action="EXPORT_CONTEXT", params={"paths": [path], "include_diff": False})
    with pytest.raises(DomainError):
        parse_command(raw(command))


@pytest.mark.parametrize("branch", ["-b", "main~1", "a/../b", "a//b", "x.lock", ".hidden"])
def test_git_parameters_are_validated_before_admission(command: dict[str, Any], branch: str) -> None:
    command.update(action="SYNC_WORKSPACE", params={
        "mode": "remote_ff", "remote_ref": branch, "target_commit": "b" * 40,
    })
    with pytest.raises(DomainError) as error:
        parse_command(raw(command))
    assert error.value.code == "INVALID_BRANCH"


def test_existing_examples_and_recipes_keep_their_v1_shape() -> None:
    paths = list((ROOT / "examples").glob("*.command.json"))
    paths += list((ROOT / "drive-seed/00-control/recipes").glob("*.json"))
    assert paths
    for path in paths:
        parse_command(path.read_bytes())


def test_registered_workspace_and_authenticated_local_owner_authorized(
    policy: LocalPolicy, command: dict[str, Any],
) -> None:
    policy.authorize(command, WORKSPACE_SOURCE)
    authorize(command, LOCAL_SOURCE, policy)


@pytest.mark.parametrize("source", [
    Provenance("drive", "unregistered", "toy-project", "experiment-a"),
    Provenance("drive", "workspace-command"),
    Provenance("drive", "workspace-command", "other-project", "experiment-a"),
])
def test_claimed_mailbox_scope_cannot_replace_registered_authority(
    policy: LocalPolicy, command: dict[str, Any], source: Provenance,
) -> None:
    with pytest.raises(DomainError) as error:
        policy.authorize(command, source)
    assert error.value.code == "SOURCE_MISMATCH"


@pytest.mark.parametrize("source", [ROOT_SOURCE, PROJECT_SOURCE])
def test_root_and_project_mailboxes_cannot_run_workspace_commands(
    policy: LocalPolicy, command: dict[str, Any], source: Provenance,
) -> None:
    with pytest.raises(DomainError) as error:
        policy.authorize(command, source)
    assert error.value.code == "SOURCE_SCOPE"


def test_workspace_command_cannot_select_another_project_or_workspace(
    policy: LocalPolicy, command: dict[str, Any],
) -> None:
    for updates in ({"project_id": "other-project"}, {"workspace_id": "other-workspace"}):
        wrong = {**command, **updates}
        with pytest.raises(DomainError) as error:
            policy.authorize(wrong, WORKSPACE_SOURCE)
        assert error.value.code == "SOURCE_SCOPE"


def test_project_mailbox_can_create_only_in_its_own_registered_project(
    policy: LocalPolicy, command: dict[str, Any],
) -> None:
    command.update(action="CREATE_WORKSPACE", workspace_id="brand-new", params={
        "name": "New workspace", "branch": "feature/new", "base_commit": "a" * 40,
        "profile_id": "python-quick",
    })
    policy.authorize(command, PROJECT_SOURCE)
    policy.authorize(command, LOCAL_SOURCE)
    command["project_id"] = "other-project"
    with pytest.raises(DomainError) as error:
        policy.authorize(command, PROJECT_SOURCE)
    assert error.value.code == "SOURCE_SCOPE"


def test_project_fetch_does_not_need_a_workspace_or_execution_profile(
    policy: LocalPolicy, command: dict[str, Any],
) -> None:
    command.update(action="FETCH", workspace_id=None, params={})
    policy.authorize(command, PROJECT_SOURCE)


def test_root_creation_selects_preapproved_template_and_identity(
    policy: LocalPolicy, project_command: dict[str, Any],
) -> None:
    policy.authorize(project_command, ROOT_SOURCE)
    policy.authorize(project_command, LOCAL_SOURCE)
    with pytest.raises(DomainError) as error:
        policy.authorize(project_command, PROJECT_SOURCE)
    assert error.value.code == "SOURCE_SCOPE"


@pytest.mark.parametrize("source,expected", [
    ({"kind": "local", "import_handle": "fake-handle"}, "IMPORT_NOT_APPROVED"),
    ({"kind": "github", "repository": "other-owner/private"}, "REPOSITORY_NOT_APPROVED"),
])
def test_import_reference_cannot_grant_itself_authority(
    policy: LocalPolicy, project_command: dict[str, Any], source: dict[str, str], expected: str,
) -> None:
    project_command["params"]["source"] = source
    with pytest.raises(DomainError) as error:
        policy.authorize(project_command, ROOT_SOURCE)
    assert error.value.code == expected


@pytest.mark.parametrize("source", [
    {"kind": "local", "import_handle": "local-grant"},
    {"kind": "github", "repository": "approved-owner/example"},
])
def test_locally_approved_imports_can_be_selected(
    policy: LocalPolicy, project_command: dict[str, Any], source: dict[str, str],
) -> None:
    project_command["params"]["source"] = source
    policy.authorize(project_command, ROOT_SOURCE)


@pytest.mark.parametrize("parameter,code", [
    ("template_id", "TEMPLATE_NOT_APPROVED"), ("github_identity", "IDENTITY_NOT_APPROVED"),
])
def test_new_project_cannot_select_unapproved_capabilities(
    policy: LocalPolicy, project_command: dict[str, Any], parameter: str, code: str,
) -> None:
    project_command["params"][parameter] = "unapproved"
    with pytest.raises(DomainError) as error:
        policy.authorize(project_command, ROOT_SOURCE)
    assert error.value.code == code


def test_workspace_cannot_select_unapproved_profile(policy: LocalPolicy, command: dict[str, Any]) -> None:
    command["params"]["profile_id"] = "host-shell"
    with pytest.raises(DomainError) as error:
        policy.authorize(command, WORKSPACE_SOURCE)
    assert error.value.code == "PROFILE_NOT_APPROVED"


def test_local_owner_still_requires_registered_target(policy: LocalPolicy, command: dict[str, Any]) -> None:
    command["project_id"] = "unregistered"
    with pytest.raises(DomainError) as error:
        policy.authorize(command, LOCAL_SOURCE)
    assert error.value.code == "PROJECT_NOT_REGISTERED"
    command["project_id"] = "toy-project"
    command["workspace_id"] = "unknown"
    with pytest.raises(DomainError) as error:
        policy.authorize(command, LOCAL_SOURCE)
    assert error.value.code == "WORKSPACE_NOT_REGISTERED"


def test_local_selected_scope_cannot_be_spoofed(policy: LocalPolicy, command: dict[str, Any]) -> None:
    source = Provenance("local", "session", "other-project", "experiment-a")
    with pytest.raises(DomainError) as error:
        policy.authorize(command, source)
    assert error.value.code == "SOURCE_SCOPE"


def test_cancellation_requires_scope_but_not_profile(command: dict[str, Any]) -> None:
    command.update(action="CANCEL_OPERATION", params={
        "target_request_id": "22222222-2222-4222-8222-222222222222",
    })
    policy = LocalPolicy(
        mailboxes={WORKSPACE_SOURCE.source_id: WORKSPACE_SOURCE},
        project_ids=frozenset({"toy-project"}),
        workspace_pairs=frozenset({("toy-project", "experiment-a")}),
    )
    check_time(command)
    policy.authorize(command, WORKSPACE_SOURCE)
    policy.authorize(command, LOCAL_SOURCE)


def test_defaults_cannot_authorize_local_or_remote_commands(
    command: dict[str, Any], project_command: dict[str, Any],
) -> None:
    for value, source in ((command, LOCAL_SOURCE), (command, WORKSPACE_SOURCE),
                          (project_command, LOCAL_SOURCE), (project_command, ROOT_SOURCE)):
        with pytest.raises(DomainError):
            LocalPolicy().authorize(value, source)


def test_mailbox_grants_are_immutable_copies() -> None:
    registrations = {ROOT_SOURCE.source_id: ROOT_SOURCE}
    approved = {"python-basic"}
    policy = LocalPolicy(mailboxes=registrations, approved_templates=approved)  # type: ignore[arg-type]
    registrations["new-root"] = Provenance("drive", "new-root")
    approved.add("host-shell")
    assert "new-root" not in policy.mailboxes
    assert "host-shell" not in policy.approved_templates
    with pytest.raises(TypeError):
        policy.mailboxes["new-root"] = registrations["new-root"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        ROOT_SOURCE.project_id = "other-project"  # type: ignore[misc]


def test_invalid_mailbox_registration_or_missing_project_is_refused() -> None:
    with pytest.raises(DomainError):
        LocalPolicy(mailboxes={"wrong-source-id": ROOT_SOURCE})
    with pytest.raises(DomainError):
        Provenance("drive", "document", workspace_id="workspace")


def test_nested_unknown_fields_are_rejected_without_echoing_values(
    policy: LocalPolicy, project_command: dict[str, Any],
) -> None:
    original = copy.deepcopy(project_command)
    project_command["params"]["source"]["credential"] = "token-do-not-echo"
    with pytest.raises(DomainError) as error:
        policy.authorize(project_command, ROOT_SOURCE)
    assert error.value.code == "INVALID_SCHEMA"
    assert "token-do-not-echo" not in str(error.value)
    assert original["params"]["source"] == {"kind": "new"}
