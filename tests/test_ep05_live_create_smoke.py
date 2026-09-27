"""The EP-05 operator harness stays inert unless --apply is explicit."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import ep05_live_create_smoke as smoke


def test_default_is_read_only_preflight(monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def preflight(project_id: str | None, name: str | None) -> SimpleNamespace:
        calls.append((project_id, name))
        return SimpleNamespace(summary={
            "mode": "READ_ONLY_PREFLIGHT", "cloud_writes": False,
            "github_repository": "ABSENT", "local_project_state": "ABSENT",
        })

    monkeypatch.setattr(smoke, "_preflight", preflight)
    monkeypatch.setattr(smoke, "_apply", lambda *_: pytest.fail("default invoked --apply"))
    assert smoke.main(["--project-id", "cdr-ep05-smoke-20260928",
                       "--name", "EP-05 Smoke"]) == 0
    assert calls == [("cdr-ep05-smoke-20260928", "EP-05 Smoke")]
    assert json.loads(capsys.readouterr().out)["cloud_writes"] is False


@pytest.mark.parametrize("arguments", [
    ["--apply"], ["--apply", "--project-id", "smoke"],
    ["--apply", "--name", "Smoke"],
    ["--apply", "--project-id", "Bad-Slug", "--name", "Smoke"],
])
def test_apply_requires_explicit_valid_identity(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        smoke._parse_args(arguments)
    assert error.value.code == 2


def test_existing_repository_blocks_apply_before_workflow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(smoke, "_preflight", lambda *_: SimpleNamespace(summary={
        "mode": "READ_ONLY_PREFLIGHT", "cloud_writes": False,
        "github_repository": "EXISTS_BLOCKED", "local_project_state": "ABSENT",
    }))
    monkeypatch.setattr(smoke, "_apply", lambda *_: pytest.fail("collision invoked workflow"))
    assert smoke.main(["--apply", "--project-id", "smoke", "--name", "Smoke"]) == 2
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[-1]["code"] == "PROJECT_ID_ALREADY_USED"


def test_command_uses_isolated_runner_epoch_and_private_new_source() -> None:
    ledger = SimpleNamespace(state=lambda: {"runner_id": "11111111-1111-4111-8111-111111111111", "epoch": "22222222-2222-4222-8222-222222222222"})
    command = smoke._command(ledger, "cdr-ep05-smoke-20260928", "EP-05 Smoke")
    assert command["action"] == "CREATE_PROJECT"
    assert command["runner_id"] == "11111111-1111-4111-8111-111111111111"
    assert command["params"]["github_identity"] == "viskyg"
    assert command["params"]["visibility"] == "private"
    assert command["params"]["source"] == {"kind": "new"}


@pytest.mark.parametrize(("finding", "expected"), [
    ("REPOSITORY_CREATE_UNKNOWN", "ABSENT"),
    ("REPOSITORY_COLLISION", "EXISTS_BLOCKED"),
    ("REPOSITORY_NOT_PRIVATE", "EXISTS_BLOCKED"),
    (None, "EXISTS_BLOCKED"),
])
def test_repository_availability_uses_read_only_reconciliation(
    finding: str | None, expected: str,
) -> None:
    from runner_app.adapters.github import GitHubError

    class FakeGitHub:
        calls: list[tuple[str, str, str]]

        def __init__(self) -> None:
            self.calls = []

        def reconcile_private_repository(self, owner: str, project: str,
                                         marker: str) -> object:
            self.calls.append((owner, project, marker))
            if finding is not None:
                raise GitHubError(finding, finding)
            return object()

    github = FakeGitHub()
    assert smoke._repository_state(
        github, "37c39a84-3856-4da2-8d4f-d09416e2ea5e", "smoke",
    ) == expected
    assert github.calls[0][0:2] == ("ViskyG", "smoke")
    assert github.calls[0][2].startswith("cdr:v1:")


def test_apply_assembles_real_engine_and_create_workflow_with_fake_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A disposable local Git project reaches a terminal result with no cloud API."""
    from pathlib import Path

    from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
    from runner_app.adapters.github import GitHubRepository, RepositoryResult

    state_base = tmp_path / "isolated-smoke"
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    monkeypatch.setattr(smoke, "STATE_BASE", state_base)

    class FakeGitHub:
        def create_or_attach_private(self, owner: str, project: str, marker: str,
                                     *, expected_repo_id: int | None = None
                                     ) -> RepositoryResult:
            assert owner == "ViskyG" and project == "smoke"
            assert expected_repo_id is None
            return RepositoryResult(GitHubRepository(
                id=42, node_id="R_42", owner=owner, owner_id=smoke.OWNER_ID,
                name=project, full_name=f"{owner}/{project}",
                html_url=f"https://github.com/{owner}/{project}",
                clone_url=f"https://github.com/{owner}/{project}.git",
                ssh_url=f"git@github.com:{owner}/{project}.git",
                private=True, default_branch="main", description=marker,
            ), "created")

    class FakeDriveProvisioner:
        def __init__(self, *args: object) -> None:
            assert len(args) == 4

        def provision(self, request_id: str, project_id: str, workspace_id: str,
                      project_name: str) -> WorkspaceDriveRegistration:
            assert request_id and (project_id, workspace_id, project_name) == (
                "smoke", "primary", "Smoke",
            )
            return WorkspaceDriveRegistration(
                "37c39a84-3856-4da2-8d4f-d09416e2ea5e",
                project_id, workspace_id,
                "projectFolder123", "workspaceFolder123", "commandDocument123",
                "changesetDocument123", "receiptDocument123", "latestResultDocument123",
                "projectStateDocument123", "sourceContextDocument123",
                "projectCommandDocument123", "projectReceiptDocument123",
            )

        def populate(self, request_id: str, registration: WorkspaceDriveRegistration,
                     **kwargs: object) -> dict[str, str]:
            assert request_id and registration.project_id == "smoke"
            assert kwargs["repository_id"] == 42
            return {"project_state_sha256": "a" * 64, "context_sha256": "b" * 64}

    monkeypatch.setattr(smoke, "WorkspaceDriveRegistry", lambda *args: object())
    monkeypatch.setattr(smoke, "GoogleUploadProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke, "DriveProjectProvisioner", FakeDriveProvisioner)
    monkeypatch.setattr(smoke, "DocsCASWriter", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        smoke, "GitHubBaselineBootstrap",
        lambda *args: lambda project, head, repository: head,
    )
    attached = SimpleNamespace(index=SimpleNamespace(
        installation_id="37c39a84-3856-4da2-8d4f-d09416e2ea5e",
    ))
    preflight = SimpleNamespace(
        summary={"github_repository": "ABSENT", "local_project_state": "ABSENT"},
        attached=attached, provider=object(), drive_service=object(),
        github=FakeGitHub(), token_provider=lambda: "unused",
        paths=SimpleNamespace(config_dir=Path(config_dir)),
    )
    result = smoke._apply(preflight, "smoke", "Smoke")
    assert result["status"] == "COMPLETED"
    assert result["github_repository_id"] == 42
    assert result["drive_project_folder_id"] == "projectFolder123"
    assert Path(result["state_dir"]).is_dir()
    assert Path(result["state_dir"]).stat().st_mode & 0o777 == 0o700


def test_repository_preflight_does_not_hide_provider_failure() -> None:
    from runner_app.adapters.github import GitHubError

    class DeniedGitHub:
        def reconcile_private_repository(self, *_: object) -> object:
            raise GitHubError("GITHUB_AUTHORIZATION_DENIED", "denied")

    with pytest.raises(GitHubError) as error:
        smoke._repository_state(
            DeniedGitHub(), "37c39a84-3856-4da2-8d4f-d09416e2ea5e", "smoke",
        )
    assert error.value.code == "GITHUB_AUTHORIZATION_DENIED"
