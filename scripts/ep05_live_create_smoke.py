"""Owner-local EP-05 CREATE_PROJECT smoke using the existing Drive installation.

Default invocation reads provider identities only. --apply creates exactly one
new project through Engine and CreateProjectWorkflow. It is an operator tool,
not a deployed runner or a Drive bootstrap command.

Example:
  python scripts/ep05_live_create_smoke.py --project-id cdr-ep05-smoke-20260928 --name "EP-05 Smoke"
  python scripts/ep05_live_create_smoke.py --apply --project-id cdr-ep05-smoke-20260928 --name "EP-05 Smoke"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from runner_app.adapters.drive_auth import DriveAuthError, load_authorized_credentials
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_google import GoogleReadProvider
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    LegacyInstallationManifest,
    attach_existing,
)
from runner_app.adapters.drive_provision import DriveProjectProvisioner, ProjectUploadProvider
from runner_app.adapters.drive_registry import WorkspaceDriveRegistry
from runner_app.adapters.drive_upload import GoogleUploadProvider
from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.adapters.github import (
    GitHubError,
    GitHubREST,
    UrllibGitHubTransport,
    make_provision_marker,
)
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.domain.commands import DomainError, LocalPolicy, Provenance, validate_shape
from runner_app.engine.service import Engine
from runner_app.git.argv import GitError
from runner_app.git.github_transport import OwnerTokenFile
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger, LedgerError
from runner_app.storage.paths import Paths, UnsafePathError, ensure_private_directory
from runner_app.workflows.create_project import CreateProjectWorkflow, GitHubBaselineBootstrap

OWNER = "ViskyG"
OWNER_ID = 12452296
IDENTITY_ALIAS = "viskyg"
TEMPLATE = "python-basic"
PROFILE = "python-quick"
STATE_BASE = Path("/home/visky/Work/GPTRunner/.ep05-smoke")
IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")


@dataclass(frozen=True)
class Preflight:
    summary: dict[str, Any]
    attached: AttachedInstallation
    provider: GoogleReadProvider
    drive_service: Any
    github: GitHubREST
    token_provider: OwnerTokenFile
    paths: Paths


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="admit one real CREATE_PROJECT after read-only preflight")
    parser.add_argument("--project-id", help="new lowercase project and GitHub repository slug")
    parser.add_argument("--name", help="new project's human-readable display name")
    args = parser.parse_args(argv)
    if args.apply and (args.project_id is None or args.name is None):
        parser.error("--apply requires both --project-id and --name")
    if (args.project_id is None) != (args.name is None):
        parser.error("--project-id and --name must be supplied together")
    if args.project_id is not None and _PROJECT_ID.fullmatch(args.project_id) is None:
        parser.error("--project-id must be a lowercase slug of at most 48 characters")
    if args.name is not None and (not args.name.strip()
                                  or len(args.name) > 80 or len(args.name.encode("utf-8")) > 200):
        parser.error("--name must be nonempty, at most 80 characters and 200 UTF-8 bytes")
    return args


def _configuration(paths: Paths) -> tuple[ExistingInstallation, LegacyInstallationManifest]:
    selected = ExistingInstallation.from_mapping({
        "schema_version": "1.0", "mode": "attach_existing",
        "bootstrap_allowed": False, "root_folder_name": "Chat-Dev Runner",
        "root_folder_id": DRIVE_ROOT_ID,
        "installation_index_document_id": DRIVE_INDEX_ID,
    })
    manifest = LegacyInstallationManifest.from_file(
        paths.config_dir / "installation-legacy.json", selected,
    )
    return selected, manifest


def _state_directory(project_id: str) -> Path:
    state = STATE_BASE / project_id
    try:
        state.relative_to(IMPLEMENTATION_ROOT)
    except ValueError:
        return state
    raise UnsafePathError("Smoke state must be outside the implementation repository")


def _repository_state(github: GitHubREST, installation_id: str,
                      project_id: str) -> str:
    """Read exact private repo identity without using a create-capable method."""
    marker = make_provision_marker(
        installation_id, project_id, "read-only-preflight",
    )
    try:
        github.reconcile_private_repository(OWNER, project_id, marker)
    except GitHubError as error:
        if error.code == "REPOSITORY_CREATE_UNKNOWN":
            return "ABSENT"
        if error.code in {"REPOSITORY_COLLISION", "REPOSITORY_NOT_PRIVATE"}:
            return "EXISTS_BLOCKED"
        raise
    return "EXISTS_BLOCKED"


def _preflight(project_id: str | None, name: str | None) -> Preflight:
    """Read owner configuration, exact Drive installation and GitHub identity."""
    discovered = Paths.discover()
    paths = Paths(
        config_dir=discovered.config_dir, state_dir=STATE_BASE,
        cache_dir=STATE_BASE / "cache",
    )
    selected, manifest = _configuration(paths)
    token_provider = OwnerTokenFile(paths.config_dir / "github-token")
    token_provider()  # Owner-only local validation; never log its return value.
    credentials = load_authorized_credentials(paths, refresh=True)
    build = import_module("googleapiclient.discovery").build

    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    provider = GoogleReadProvider(drive, docs)
    attached = attach_existing(provider, selected, legacy_manifest=manifest)
    github = GitHubREST(
        UrllibGitHubTransport(token_provider),
        expected_login=OWNER, expected_user_id=OWNER_ID,
    )
    identity = github.authenticated_identity()
    if identity.login.casefold() != OWNER.casefold() or identity.id != OWNER_ID:
        raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Approved owner identity differs")
    repository_state = "NOT_CHECKED"
    local_state = "NOT_CHECKED"
    if project_id is not None:
        repository_state = _repository_state(
            github, attached.index.installation_id, project_id,
        )
        local_state = (
            "EXISTS_BLOCKED" if os.path.lexists(_state_directory(project_id))
            else "ABSENT"
        )
    return Preflight(
        summary={
            "mode": "READ_ONLY_PREFLIGHT", "cloud_writes": False,
            "installation_mode": "ATTACHED_EXISTING",
            "drive_root_id": DRIVE_ROOT_ID,
            "installation_index_document_id": DRIVE_INDEX_ID,
            "installation_id": attached.index.installation_id,
            "github_owner": OWNER, "github_user_id": identity.id,
            "github_repository": repository_state,
            "local_project_state": local_state,
            "project_id": project_id, "project_name": name,
        },
        attached=attached, provider=provider, drive_service=drive,
        github=github, token_provider=token_provider, paths=paths,
    )


def _command(ledger: Ledger, project_id: str, name: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()),
        "dispatch": "READY", "runner_id": state["runner_id"],
        "epoch": state["epoch"], "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": project_id,
        "workspace_id": None, "expected_head": None,
        "params": {
            "name": name, "source": {"kind": "new"},
            "template_id": TEMPLATE, "github_identity": IDENTITY_ALIAS,
            "visibility": "private",
        },
        "intent": "Create one owner-approved EP-05 private smoke project",
    }
    validate_shape(command)
    return command


def _apply(preflight: Preflight, project_id: str, name: str) -> dict[str, Any]:
    if (preflight.summary["github_repository"] != "ABSENT"
            or preflight.summary["local_project_state"] != "ABSENT"):
        raise ValueError("PROJECT_ID_ALREADY_USED")
    state_dir = _state_directory(project_id)
    ensure_private_directory(STATE_BASE)
    # A partial run must be recovered through its ledger, not replayed with a new ID.
    state_dir.mkdir(mode=0o700)
    ensure_private_directory(state_dir)
    paths = Paths(
        config_dir=preflight.paths.config_dir, state_dir=state_dir,
        cache_dir=state_dir / "cache",
    )
    paths.ensure_private()
    ledger = Ledger(Database(paths.database_path))
    ledger.startup()
    registry = SourceRegistry(ledger.database, state_dir / "managed")
    source = SourceService(ledger, registry)
    drive_registry = WorkspaceDriveRegistry(
        state_dir / "drive-registrations", preflight.attached, preflight.provider,
    )
    upload = GoogleUploadProvider(preflight.drive_service, attached=preflight.attached)
    drive_provisioner = DriveProjectProvisioner(
        ledger, preflight.attached, cast(ProjectUploadProvider, upload), drive_registry,
    )

    def populate(request_id: str, registration: Any, sources: SourceRegistry,
                 repository: Any) -> dict[str, str]:
        writer = DocsCASWriter(
            preflight.provider, preflight.attached,
            extra_docs=registration.state_docs(),
        )
        return drive_provisioner.populate(
            request_id, registration, source_registry=sources, writer=writer,
            repository_id=repository.id, repository_url=repository.html_url,
        )

    workflow = CreateProjectWorkflow(
        ledger, source, preflight.github, drive_provisioner,
        GitHubBaselineBootstrap(registry, preflight.github, preflight.token_provider),
        populate, installation_id=preflight.attached.index.installation_id,
        github_owner=OWNER, github_identity=IDENTITY_ALIAS,
        template_profiles={TEMPLATE: PROFILE},
    )
    engine = Engine(
        ledger,
        policy=LocalPolicy(
            approved_templates=frozenset({TEMPLATE}),
            github_identities=frozenset({IDENTITY_ALIAS}),
        ),
        handlers={"CREATE_PROJECT": workflow},
    )
    ledger.arm()
    command = _command(ledger, project_id, name)
    request_id = command["request_id"]
    try:
        admitted = engine.submit(
            json.dumps(command, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            Provenance("local", "ep05-owner-smoke"),
        )
        if not admitted.execute:
            return {
                "mode": "APPLY", "request_id": request_id,
                "status": admitted.receipt["status"],
                "code": admitted.receipt["code"],
                "state_dir": str(state_dir),
            }
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            operation = ledger.get(request_id)
            if operation is not None and operation["status"] != "RUNNING":
                result = operation.get("result") or {}
                github = result.get("github") or {}
                drive = result.get("drive") or {}
                return {
                    "mode": "APPLY", "request_id": request_id,
                    "status": operation["status"], "code": operation["code"],
                    "state_dir": str(state_dir),
                    "github_repository_id": github.get("repository_id"),
                    "github_repository_url": github.get("html_url"),
                    "drive_project_folder_id": drive.get("project_folder_id"),
                    "drive_project_state_doc_id": drive.get("project_state_doc_id"),
                    "drive_context_doc_id": drive.get("context_doc_id"),
                }
            time.sleep(0.2)
        return {
            "mode": "APPLY", "request_id": request_id, "status": "RUNNING",
            "code": "SMOKE_WAIT_TIMEOUT", "state_dir": str(state_dir),
        }
    finally:
        engine.shutdown(timeout=30)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    preflight = _preflight(args.project_id, args.name)
    print(json.dumps(preflight.summary, sort_keys=True))
    if not args.apply:
        return 0
    if (preflight.summary["github_repository"] != "ABSENT"
            or preflight.summary["local_project_state"] != "ABSENT"):
        print(json.dumps({"mode": "APPLY", "status": "BLOCKED",
                          "code": "PROJECT_ID_ALREADY_USED"}, sort_keys=True))
        return 2
    result = _apply(preflight, args.project_id, args.name)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DriveAuthError, DriveError, GitHubError, GitError, DomainError,
            LedgerError) as error:
        print(json.dumps({"status": "BLOCKED", "code": error.code}, sort_keys=True))
        raise SystemExit(2) from None
    except UnsafePathError:
        print(json.dumps({"status": "BLOCKED", "code": "LOCAL_PATH_UNSAFE"}, sort_keys=True))
        raise SystemExit(2) from None
    except OSError:
        print(json.dumps({"status": "BLOCKED", "code": "LOCAL_STATE_UNAVAILABLE"}, sort_keys=True))
        raise SystemExit(2) from None
    except ValueError as error:
        code = str(error) if str(error) == "PROJECT_ID_ALREADY_USED" else "SMOKE_INVALID"
        print(json.dumps({"status": "BLOCKED", "code": code}, sort_keys=True))
        raise SystemExit(2) from None
    except Exception:  # noqa: BLE001 -- never expose third-party credential-bearing text
        print(json.dumps({"status": "FAULT", "code": "SMOKE_FAILED"}, sort_keys=True))
        raise SystemExit(1) from None
