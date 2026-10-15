"""One pinned EP-04 canary; read-only unless explicitly armed after review.

No account text, revisions, credentials, or provider exception messages are printed.
Private originals and scratch state stay outside the repository under /tmp.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from runner_app.adapters.drive_auth import DRIVE_SCOPE, load_authorized_credentials
from runner_app.adapters.drive_check import _build_provider
from runner_app.adapters.drive_docs import FILE_FIELDS, extract_document
from runner_app.adapters.drive_writer import _plain_body_span
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.config_v2 import NativeProfile
from runner_app.domain.mailbox_v2 import validate_mailbox_shape
from runner_app.domain.project_v2 import parse_project
from runner_app.engine.docs_v2 import DocsPortFactory
from runner_app.engine.intake_v2 import IntakeCoordinator
from runner_app.engine.queue_v2 import QueueWorker
from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
from runner_app.executors.task_plan_v2 import plan_task
from runner_app.git.argv import run_git
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.storage.database import Database
from runner_app.storage.intake_v2 import IntakeStore
from runner_app.storage.paths import Paths, open_private_file

DOCUMENT = "1DvKJqROGSKWIh8fginrb9vKu6o14z7KAXmFyyEfm_s8"
FOLDER = "1NIiRdK6VMRyY0Q2o5PIJ4ix6UoawW_oh"
TITLE = "EP-04 CANARY local-chat fd641028b29f"
HISTORICAL_SHA256 = "bf69fe46050822608c542fcdd1e3f6a1e99e00a95d512e24c3ae4d8f3498be52"
OWNER_CONFIG = Path("/home/visky/.config/chat-dev-runner")
RUNNER = "gp04-docs-canary-runner"
PROJECT = "gp04-docs-canary-project"
BINDING = "gp04-existing-canary-exact-tab"


class GateError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateError(code)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def private_write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def private_read(path: Path) -> bytes:
    with os.fdopen(open_private_file(path), "rb") as stream:
        return stream.read(262145)


def stable_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", fallback)
    return (
        code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else fallback
    )


def journal(private: Path, event: str, **details: Any) -> None:
    fd = os.open(
        private / "gate-journal.jsonl",
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "ab") as stream:
        stream.write(json.dumps({"event": event, **details}, sort_keys=True).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_canary(provider: Any) -> tuple[Any, Any, int]:
    metadata = provider.get_file(DOCUMENT, fields=FILE_FIELDS)
    require(metadata.get("id") == DOCUMENT, "TARGET_ID_CHANGED")
    require(metadata.get("name") == TITLE, "TARGET_TITLE_CHANGED")
    require(metadata.get("parents") == [FOLDER], "TARGET_PARENT_CHANGED")
    require(
        metadata.get("mimeType") == "application/vnd.google-apps.document", "TARGET_TYPE_CHANGED"
    )
    require(metadata.get("trashed") is False, "TARGET_TRASHED")
    require(metadata.get("isAppAuthorized") is True, "TARGET_APP_GRANT_MISSING")
    require(
        metadata.get("capabilities", {}).get("canEdit") is True, "TARGET_EDIT_PERMISSION_MISSING"
    )
    document = provider.get_document(DOCUMENT, include_tabs_content=True)
    snapshot = extract_document(document, DOCUMENT, max_bytes=262144)
    _start, end = _plain_body_span(document, snapshot)
    return metadata, snapshot, end


def pending_ready(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        # Historical canary prose must also be free of an apparent READY envelope.
        return bool(re.search(rb'"dispatch"\s*:\s*"READY"', raw))
    return isinstance(value, dict) and value.get("dispatch") == "READY"


def owner_provider(private: Path) -> Any:
    paths = Paths(OWNER_CONFIG, private / "state", private / "cache")
    # The read-only preflight cannot refresh or rewrite the canonical token store.
    credentials = load_authorized_credentials(paths, refresh=False)
    return _build_provider(credentials)


def preflight() -> dict[str, Any]:
    private = Path(tempfile.mkdtemp(prefix="cdr-gp04-docs-"))
    private.chmod(0o700)
    provider = owner_provider(private)
    folder = provider.get_file(FOLDER, fields=FILE_FIELDS)
    require(
        folder.get("id") == FOLDER
        and folder.get("trashed") is False
        and folder.get("mimeType") == "application/vnd.google-apps.folder",
        "CANARY_FOLDER_INVALID",
    )
    metadata, snapshot, _end = read_canary(provider)
    require(not pending_ready(snapshot.raw_bytes), "PENDING_READY_INTENT")
    private_write(private / "original.txt", snapshot.raw_bytes)
    private_write(
        private / "original-identity.json",
        encoded(
            {
                "document_id": DOCUMENT,
                "title": TITLE,
                "folder_id": FOLDER,
                "tab_id": snapshot.tab_id,
                "revision_id": snapshot.revision_id,
                "text_sha256": snapshot.sha256,
            }
        ),
    )
    backup = (private / "original.txt").stat()
    require(
        backup.st_uid == os.geteuid() and stat.S_IMODE(backup.st_mode) == 0o600,
        "BACKUP_PERMISSIONS_INVALID",
    )
    report = {
        "gate": "GP-04",
        "mode": "READ_ONLY_PREFLIGHT",
        "status": "OK",
        "document_id": DOCUMENT,
        "folder_id": FOLDER,
        "title": TITLE,
        "tab_id": snapshot.tab_id,
        "text_bytes": len(snapshot.raw_bytes),
        "text_sha256": snapshot.sha256,
        "payload_sha256": digest(snapshot.raw_bytes[:-1]),
        "historical_text_sha256_matches": snapshot.sha256 == HISTORICAL_SHA256,
        "is_app_authorized": metadata["isAppAuthorized"],
        "can_edit": True,
        "pending_ready_intent": False,
        "cloud_writes": 0,
        "credential_refresh": False,
        "backup_directory": str(private),
        "backup_mode": "0600",
        "backup_directory_mode": "0700",
    }
    private_write(private / "preflight.json", encoded(report))
    return report


def replace_once(provider: Any, current: Any, end: int, desired: bytes) -> str:
    """Replace body text once; the compulsory final newline is never deleted."""
    require(desired.endswith(b"\n"), "RESTORABLE_TEXT_REQUIRED")
    requests: list[dict[str, Any]] = []
    if end - 1 > 1:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": 1,
                        "endIndex": end - 1,
                        "tabId": current.tab_id,
                    }
                }
            }
        )
    content = desired[:-1].decode("utf-8")
    require(bool(content), "EMPTY_CANARY_TEXT_REFUSED")
    requests.append(
        {
            "insertText": {
                "location": {"index": 1, "tabId": current.tab_id},
                "text": content,
            }
        }
    )
    response = provider.batch_update_document(
        DOCUMENT, requests=requests, required_revision_id=current.revision_id
    )
    return str(response["writeControl"]["requiredRevisionId"])


def native_revision(revision: str) -> str:
    require(revision.startswith("docs-v2:"), "DOCS_REVISION_INVALID")
    binding = json.loads(base64.urlsafe_b64decode(revision[8:]))
    require(
        binding["document_id"] == DOCUMENT and binding["tab_id"] == "t.0",
        "DOCS_REVISION_SCOPE_CHANGED",
    )
    return str(binding["revision_id"])


class TrackedPort:
    """Keep intended payload hashes before delegating to the real production port."""

    def __init__(self, port: Any, private: Path, owned: set[str], revisions: set[str]) -> None:
        self.port, self.private, self.owned, self.revisions = port, private, owned, revisions

    def read_current(self) -> Any:
        observation = self.port.read_current()
        require(
            digest(observation.payload + b"\n") in self.owned
            and native_revision(observation.revision) in self.revisions,
            "CANARY_INPUT_CHANGED",
        )
        return observation

    def ack_if_current(self, observed: Any, payload: bytes) -> Any:
        expected = digest(payload + b"\n")
        journal(self.private, "RUNNER_CAS_INTENT", text_sha256=expected)
        self.owned.add(expected)
        result = self.port.ack_if_current(observed, payload)
        if result.state == "APPLIED":
            self.revisions.add(native_revision(result.revision))
        return result


def prepare_native(private: Path, store: IntakeStore, session: str) -> QueueWorker:
    source = private / "calculator-source"
    source.mkdir(mode=0o700)
    (source / ".runner").mkdir(mode=0o700)
    manifest = {
        "protocol": "cdr-project/2",
        "project_id": PROJECT,
        "tasks": {
            "calculate": {
                "language": "python",
                "profile_id": "native-python",
                "steps": [
                    {
                        "id": "calculate",
                        "phase": "run",
                        "cwd": ".",
                        "timeout_seconds": 3,
                        "argv": ["{python}", "calculator.py"],
                    }
                ],
                "outputs": [
                    {
                        "id": "calculation",
                        "source_root": "out",
                        "path": "calculation.json",
                        "kind": "artifact",
                        "format": "json",
                        "required": True,
                        "publish": "local_only",
                    }
                ],
                "quality_gate": {
                    "test_required": False,
                    "minimum_executed": 0,
                    "required_outputs": ["calculation"],
                },
            }
        },
    }
    raw_manifest = encoded(manifest)
    private_write(source / ".runner/project.json", raw_manifest)
    private_write(
        source / "calculator.py",
        (
            b"import json, os\nfrom pathlib import Path\n"
            b"value = sum((19, 23))\n"
            b"(Path(os.environ['CDR_OUTPUT_DIR']) / 'calculation.json').write_text(\n"
            b"    json.dumps({'operation': 'sum', 'inputs': [19, 23], 'value': value}) + '\\n')\n"
            b"print('19 + 23 =', value)\n"
            b"assert value == 42\n"
        ),
    )
    tools = {"python": (Path(sys.executable),)}
    # Validate the exact real recipe before the first cloud write; no subprocess here.
    plan_task(
        parse_project(raw_manifest),
        "calculate",
        workspace=source,
        out=private / "plan-out",
        tool_paths=tools,
        timeout_budget_seconds=10,
        run_id=str(uuid4()),
        source_sha="0" * 40,
    )
    run_git(source, ["init", "--initial-branch=gp04-canary"])
    run_git(source, ["add", ".runner/project.json", "calculator.py"])
    run_git(
        source,
        [
            "-c",
            "user.name=GP04 Canary",
            "-c",
            "user.email=canary@localhost",
            "commit",
            "-qm",
            "Finite private Docs admission calculator",
        ],
    )
    managed = private / "controller.git"
    run_git(private, ["clone", "--bare", "--no-local", str(source), str(managed)])
    resolver = SourceResolver(
        {
            PROJECT: SourceProject(
                managed,
                private / "runs",
                {"canary": LocalWorkspace(source)},
            )
        }
    )
    runner = NativeTaskRunner(
        store,
        resolver,
        {
            PROJECT: TaskProject(managed, private / "results", ("native-python",)),
        },
        {"native-python": NativeProfile(tools, (), {"LANG": "C.UTF-8"}, 10, 16384)},
    )
    return QueueWorker(store, session, runner)


def execute(private: Path, reviewed_sha: str) -> dict[str, Any]:
    require(digest(Path(__file__).read_bytes()) == reviewed_sha, "REVIEWED_SCRIPT_HASH_MISMATCH")
    require(
        private.is_absolute()
        and private.parent == Path("/tmp")
        and private.name.startswith("cdr-gp04-docs-")
        and private.resolve() == private,
        "PRIVATE_BACKUP_PATH_INVALID",
    )
    info = private.stat()
    require(
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        "PRIVATE_BACKUP_PERMISSIONS_INVALID",
    )
    original = private_read(private / "original.txt")
    identity = json.loads(private_read(private / "original-identity.json"))
    require(
        digest(original) == identity["text_sha256"] == HISTORICAL_SHA256,
        "ORIGINAL_BACKUP_HASH_CHANGED",
    )
    require(
        identity["document_id"] == DOCUMENT and identity["tab_id"] == "t.0",
        "ORIGINAL_BACKUP_IDENTITY_CHANGED",
    )
    # Exclusive durable marker prevents accidental re-execution of this scratch gate.
    private_write(
        private / "execution-started.json", encoded({"reviewed_script_sha256": reviewed_sha})
    )
    paths = Paths(OWNER_CONFIG, private / "state", private / "cache")
    config_path = private / "docs-provider.json"
    private_write(
        config_path,
        encoded(
            {
                "oauth_client_file": str(OWNER_CONFIG / "google-oauth-client.json"),
                "token_file": str(OWNER_CONFIG / "tokens/google-drive.json"),
                "legacy_manifest_file": str(OWNER_CONFIG / "installation-legacy.json"),
                "scope": DRIVE_SCOPE,
                "root_folder_id": DRIVE_ROOT_ID,
                "installation_index_document_id": DRIVE_INDEX_ID,
            }
        ),
    )
    store = IntakeStore(Database(private / "scratch.sqlite"))
    session = store.start_session(RUNNER)
    store.bind(
        BINDING,
        runner_id=RUNNER,
        project_id=PROJECT,
        provider="drive",
        coordinates={"document_id": DOCUMENT, "tab_id": identity["tab_id"]},
        session_id=session,
    )
    coordinator = IntakeCoordinator(store, session)
    worker = prepare_native(private, store, session)
    provider = owner_provider(private)
    _metadata, current, end = read_canary(provider)
    require(
        current.raw_bytes == original
        and current.tab_id == identity["tab_id"]
        and current.revision_id == identity["revision_id"],
        "PREFLIGHT_REVISION_CHANGED",
    )
    draft = {
        "protocol": "cdr-mailbox/2",
        "runner_id": RUNNER,
        "project_id": PROJECT,
        "slot_token": str(uuid4()),
        "dispatch": "DRAFT",
        "receipt": None,
        "request": {
            "request_id": None,
            "action": "RUN_TASK",
            "task": "calculate",
            "source": {
                "kind": "local_workspace",
                "workspace_id": "canary",
                "branch": "gp04-canary",
            },
            "execution": {
                "mode": "native",
                "expected_os": "linux",
                "timeout_seconds": 10,
                "profile_id": "native-python",
            },
        },
    }
    validate_mailbox_shape(draft)
    draft_text = encoded(draft) + b"\n"
    owned = {digest(original), digest(draft_text)}
    revisions = {current.revision_id}
    result: dict[str, Any] = {
        "status": "FAILED",
        "gate": "GP-04",
        "mode": "LIVE_CANARY",
        "document_id": DOCUMENT,
        "tab_id": identity["tab_id"],
        "reviewed_script_sha256": reviewed_sha,
        "original_text_sha256": digest(original),
        "restore_verified": False,
    }
    write_started = False
    try:
        journal(private, "OPERATOR_DRAFT_CAS_INTENT", text_sha256=digest(draft_text))
        write_started = True
        revisions.add(replace_once(provider, current, end, draft_text))
        _metadata, current, _end = read_canary(provider)
        require(current.raw_bytes == draft_text, "DRAFT_READBACK_MISMATCH")
        mailbox = SimpleNamespace(
            provider="drive",
            coordinates={
                "document_id": DOCUMENT,
                "tab_id": identity["tab_id"],
            },
        )
        # Actual factory loads canonical credentials and attaches all existing roles read-only.
        port = TrackedPort(DocsPortFactory(paths)(config_path, mailbox), private, owned, revisions)
        baseline = coordinator.poll_once(BINDING, port)
        result["baseline"] = asdict(baseline)
        require(baseline.kind == "BASELINED" and store.list_requests() == [], "BASELINE_FAILED")
        observed = port.read_current()
        ready = json.loads(observed.payload)
        require(
            ready["dispatch"] == "DRAFT" and ready["request"]["request_id"] is None,
            "BASELINE_ENVELOPE_CHANGED",
        )
        # Operator transition preserves the freshly baselined token and all intent fields.
        ready["dispatch"] = "READY"
        ready_text = encoded(ready) + b"\n"
        _metadata, current, end = read_canary(provider)
        require(
            current.raw_bytes[:-1] == observed.payload
            and current.revision_id == native_revision(observed.revision),
            "BASELINE_CHANGED_BEFORE_READY",
        )
        journal(private, "OPERATOR_READY_CAS_INTENT", text_sha256=digest(ready_text))
        owned.add(digest(ready_text))
        revisions.add(replace_once(provider, current, end, ready_text))
        admitted = coordinator.poll_once(BINDING, port)
        result["admission"] = asdict(admitted)
        require(admitted.kind == "ACCEPTED" and admitted.sequence == 1, "ADMISSION_FAILED")
        ack = json.loads(port.read_current().payload)
        require(
            ack["dispatch"] == "ACCEPTED"
            and ack["slot_token"] != ready["slot_token"]
            and ack["receipt"]["request_id"] == admitted.request_id,
            "ACK_PROOF_FAILED",
        )
        require(len(store.list_requests()) == 1, "REQUEST_COUNT_INVALID")
        require(worker.run_once(), "WORKER_DID_NOT_CLAIM")
        operation = store.get(admitted.request_id)
        require(operation is not None, "ADMITTED_REQUEST_MISSING")
        result["operation_status"] = operation["status"]
        result["operation_code"] = operation["code"]
        manifest = operation["result"]["manifest"]
        result["execution"] = manifest["execution"]
        result["environment"] = manifest["environment"]
        result["delivery"] = operation["result"]["delivery"]["status"]
        result["bundle_sha256"] = operation["result"]["bundle_sha256"]
        calculation = next(item for item in manifest["artifacts"] if item["id"] == "calculation")
        fact = json.loads(Path(calculation["local_path"]).read_bytes())
        require(
            fact == {"operation": "sum", "inputs": [19, 23], "value": 42},
            "CALCULATOR_ARTIFACT_INVALID",
        )
        result["calculation"] = fact
        require(
            operation["status"] == "COMPLETED"
            and manifest["execution"]["outcome"] == "PASSED"
            and manifest["execution"]["cleanup"] == "CONFIRMED",
            "NATIVE_EXECUTION_FAILED",
        )
        require(not worker.run_once(), "UNEXPECTED_SECOND_EXECUTION")
        require(coordinator.poll_once(BINDING, port).kind == "IDLE", "ACK_NOT_IDLE")
        result["status"] = "OK"
    except Exception as error:  # noqa: BLE001 -- private diagnostics use stable codes only
        result["code"] = stable_code(error, "LIVE_GATE_FAILED")
    finally:
        if write_started:
            try:
                _metadata, current, end = read_canary(provider)
                require(
                    current.tab_id == identity["tab_id"]
                    and current.sha256 in owned
                    and current.revision_id in revisions,
                    "RESTORE_INPUT_CHANGED",
                )
                if current.raw_bytes != original:
                    journal(private, "RESTORE_CAS_INTENT", text_sha256=digest(original))
                    try:
                        replace_once(provider, current, end, original)
                    except Exception as error:  # noqa: BLE001 -- reconcile once, never replay
                        result["restore_write_code"] = stable_code(error, "RESTORE_WRITE_UNCERTAIN")
                _metadata, restored, _end = read_canary(provider)
                require(
                    restored.raw_bytes == original and restored.tab_id == identity["tab_id"],
                    "RESTORE_READBACK_MISMATCH",
                )
                result["restore_verified"] = True
                result["restored_text_sha256"] = restored.sha256
                journal(private, "RESTORED", text_sha256=restored.sha256)
            except Exception as error:  # noqa: BLE001 -- never overwrite an unknown concurrent edit
                result["restore_code"] = stable_code(error, "RESTORE_UNCERTAIN")
                result["status"] = "NEEDS_RECONCILIATION"
        private_write(private / "execution-report.json", encoded(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Requires a separate root GO.")
    parser.add_argument("--backup-directory", type=Path)
    parser.add_argument("--reviewed-script-sha256")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.execute:
            require(
                args.backup_directory is not None and args.reviewed_script_sha256 is not None,
                "REVIEWED_EXECUTION_ARGUMENTS_REQUIRED",
            )
            result = execute(args.backup_directory, args.reviewed_script_sha256)
        else:
            require(
                args.backup_directory is None and args.reviewed_script_sha256 is None,
                "PREFLIGHT_ARGUMENTS_INVALID",
            )
            result = preflight()
    except Exception as error:  # noqa: BLE001 -- only stable codes can leave this gate
        code = stable_code(error, "GATE_FAILED")
        print(json.dumps({"status": "FAILED", "code": code}))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "OK":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
