#!/usr/bin/env python3
"""One disposable Drive-to-test-to-draft-PR gate against the installed service.

The default invocation only reads the exact local project and registered Drive
mailbox. --apply stages one new-file changeset and DRAFT command while disarmed,
arms the installed loopback service, publishes READY with the resulting epoch,
observes one terminal operation, independently reads its GitHub/Drive results,
and disarms. No provider write is retried after an uncertain acknowledgement.
Use primary/PASSED and parallel/FAILED once each; both mailboxes must be empty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from runner_app.adapters.drive_docs import (
    MAX_COMMAND_DOC_BYTES,
    DocumentSnapshot,
    extract_document,
    require_registered_doc,
)
from runner_app.adapters.drive_writer import _plain_body_span
from runner_app.domain.commands import validate_shape
from runner_app.engine.integrations import (
    RuntimeIntegrations,
    build_runtime_integrations,
    load_owner_runtime_config,
)
from runner_app.git.argv import run_git
from runner_app.git.changes import MAX_CHANGESET_BYTES, validate_changeset
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database, Paths, open_private_file
from runner_app.storage.ledger import TERMINAL, Ledger

PROJECT = "cdr-ep07-ui-20260928"
OWNER = "ViskyG"
STATE_DIR = Path("/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner")
BASE_URL = "http://127.0.0.1:8765"
EVIDENCE_DIR = ROOT / "verification/implementation/EP-07"
EXPECTED = {"primary": "PASSED", "parallel": "FAILED"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
FOLDER_MIME = "application/vnd.google-apps.folder"


class GateError(Exception):
    """Only a safe stable code may leave this owner-side process."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Preflight:
    paths: Paths
    ledger: Ledger
    integrations: RuntimeIntegrations
    project_id: str
    workspace_id: str
    expected_outcome: str
    head: str
    branch: str
    repository_id: int
    command_id: str
    changeset_id: str
    receipt_id: str
    latest_result_id: str
    workspace_folder_id: str
    command: DocumentSnapshot
    changeset: DocumentSnapshot
    runner_id: str


class Journal:
    """Exclusive owner-only append journal; fsync each intent before its effect."""

    def __init__(self, path: Path) -> None:
        if path.parent != EVIDENCE_DIR or path.suffix != ".jsonl":
            raise GateError("EVIDENCE_PATH_INVALID")
        self.path = path
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(self.fd, 0o600)

    def record(self, event: str, **fields: Any) -> None:
        raw = (json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                           "event": event, **fields}, sort_keys=True,
                          ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        cursor = 0
        while cursor < len(raw):
            cursor += os.write(self.fd, raw[cursor:])
        os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"


def _read(provider: Any, document_id: str, maximum: int
          ) -> tuple[DocumentSnapshot, int, int]:
    document = provider.get_document(document_id, include_tabs_content=True)
    snapshot = extract_document(document, document_id, max_bytes=maximum)
    start, end = _plain_body_span(document, snapshot)
    return snapshot, start, end


def _replace_once(provider: Any, document_id: str, before: DocumentSnapshot,
                  desired: str, maximum: int) -> tuple[DocumentSnapshot, str]:
    current, start, end = _read(provider, document_id, maximum)
    if (current.revision_id != before.revision_id or current.sha256 != before.sha256
            or current.tab_id is None or end - 1 < start or not desired.endswith("\n")):
        raise GateError("DOCUMENT_PRECONDITION_CHANGED")
    requests: list[dict[str, Any]] = []
    if end - 1 > start:
        requests.append({"deleteContentRange": {"range": {
            "startIndex": start, "endIndex": end - 1, "tabId": current.tab_id,
        }}})
    requests.append({"insertText": {"location": {"index": start,
                                                      "tabId": current.tab_id},
                                    "text": desired[:-1]}})
    write_error: Exception | None = None
    try:
        provider.batch_update_document(
            document_id, requests=requests,
            required_revision_id=current.revision_id,
        )
    except Exception as error:  # noqa: BLE001 - exact readback classifies lost ack
        write_error = error
    try:
        observed, _, _ = _read(provider, document_id, maximum)
    except Exception as error:
        raise GateError("DOCUMENT_WRITE_UNCERTAIN") from error
    if (observed.text != desired or observed.sha256 != _sha(desired)
            or observed.revision_id == current.revision_id):
        raise GateError("DOCUMENT_WRITE_UNCONFIRMED")
    return observed, "ACK_LOST_EXACT_READBACK" if write_error else "CONFIRMED"


def _preflight(workspace_id: str, expected_outcome: str) -> Preflight:
    paths = Paths.discover()
    if paths.state_dir != STATE_DIR or not paths.database_path.is_file():
        raise GateError("WRONG_INSTALLED_STATE")
    config = load_owner_runtime_config(paths)
    if (config is None or config.execution_profile_dir is None
            or config.github_login != OWNER or config.github_identity != "viskyg"
            or config.template_profiles.get("python-basic") != "python-quick"):
        raise GateError("OWNER_POLICY_MISMATCH")
    ledger = Ledger(Database(paths.database_path))
    state = ledger.state()
    if state["armed"] or state["reserved_capacity"]:
        raise GateError("RUNNER_MUST_BE_DISARMED")
    registry = SourceRegistry(ledger.database, paths.state_dir / "managed")
    project = registry.get_project(PROJECT)
    workspace = registry.get_workspace(PROJECT, workspace_id)
    profile = registry.get_workspace_profile(PROJECT, workspace_id)
    remote = registry.get_project_remote(PROJECT)
    if (project is None or project.get("state") != "READY" or workspace is None
            or workspace.get("state") != "ACTIVE" or workspace.get("archived")
            or profile != {"template_id": "python-basic", "profile_id": "python-quick"}
            or remote is None or remote.get("owner") != OWNER
            or remote.get("name") != PROJECT or remote.get("private") is not True
            or type(remote.get("provider_id")) is not int):
        raise GateError("DISPOSABLE_PROJECT_CHANGED")
    head = workspace.get("head")
    branch = workspace.get("branch")
    worktree = Path(workspace["worktree_path"])
    if (not isinstance(head, str) or SHA.fullmatch(head) is None
            or not isinstance(branch, str)
            or branch != ("work/" + PROJECT if workspace_id == "primary"
                          else "feature/ep07-parallel")
            or run_git(worktree, ["rev-parse", "HEAD"]).strip() != head
            or run_git(worktree, ["branch", "--show-current"]).strip() != branch
            or run_git(worktree, ["status", "--porcelain=v1"]) != ""
            or run_git(worktree, ["ls-tree", "--name-only", head, "--",
                                  "src/feature.py", "tests/test_feature.py"]).strip()):
        raise GateError("WORKSPACE_CHANGED")
    integrations = build_runtime_integrations(
        paths, ledger, registry,
        ImportGrants(paths.state_dir / "import-handles", roots=config.import_roots),
        config,
    )
    registration = integrations.drive_registry.get(PROJECT, workspace_id)
    provider = integrations.provider
    require_registered_doc(provider, registration.command_doc_id,
                           registration.workspace_folder_id)
    require_registered_doc(provider, registration.changeset_doc_id,
                           registration.workspace_folder_id)
    command, _, _ = _read(provider, registration.command_doc_id,
                          MAX_COMMAND_DOC_BYTES)
    changeset, _, _ = _read(provider, registration.changeset_doc_id,
                            MAX_CHANGESET_BYTES)
    if command.text.strip("\n") or changeset.text.strip("\n"):
        raise GateError("INPUT_DOC_NOT_EMPTY")
    return Preflight(
        paths, ledger, integrations, PROJECT, workspace_id, expected_outcome,
        head, branch, remote["provider_id"], registration.command_doc_id,
        registration.changeset_doc_id, registration.receipt_doc_id,
        registration.latest_result_doc_id, registration.workspace_folder_id,
        command, changeset, state["runner_id"],
    )


def _changeset(preflight: Preflight, change_id: str) -> dict[str, Any]:
    expected_assertion = "42" if preflight.expected_outcome == "PASSED" else "43"
    change = {
        "schema_version": "1.0", "change_id": change_id,
        "project_id": preflight.project_id, "workspace_id": preflight.workspace_id,
        "base_head": preflight.head,
        "files": [
            {"op": "upsert", "path": "src/feature.py", "expected_sha256": None,
             "content_utf8": "def double(value: int) -> int:\n    return value * 2\n"},
            {"op": "upsert", "path": "tests/test_feature.py", "expected_sha256": None,
             "content_utf8": ("from src.feature import double\n\n\n"
                              "def test_double() -> None:\n"
                              f"    assert double(21) == {expected_assertion}\n")},
        ],
    }
    validate_changeset(change, project_id=preflight.project_id,
                       workspace_id=preflight.workspace_id,
                       expected_head=preflight.head)
    return change


def _command(preflight: Preflight, request_id: str, change_revision: str,
             *, dispatch: str, runner_id: str, epoch: str) -> dict[str, Any]:
    issued = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": request_id,
        "dispatch": dispatch, "runner_id": runner_id, "epoch": epoch,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=10)).isoformat(),
        "action": "DELIVER_VERIFY_PUBLISH", "project_id": preflight.project_id,
        "workspace_id": preflight.workspace_id, "expected_head": preflight.head,
        "params": {"changeset_doc_id": preflight.changeset_id,
                   "changeset_revision": change_revision,
                   "profile_id": "python-quick", "publication": "draft_always"},
        "intent": ("EP-07 deployed-service disposable "
                   + preflight.expected_outcome.lower() + " verification"),
    }
    validate_shape(command)
    return command


def _session(paths: Paths) -> httpx.Client:
    descriptor = open_private_file(paths.token_dir / "session-bootstrap.json")
    try:
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(raw) > 4096:
        raise GateError("BOOTSTRAP_INVALID")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise GateError("BOOTSTRAP_INVALID") from error
    if not isinstance(value, dict) or set(value) != {"bootstrap", "expires_in_seconds"}:
        raise GateError("BOOTSTRAP_INVALID")
    secret = value.get("bootstrap")
    if not isinstance(secret, str) or not 1 <= len(secret) <= 128:
        raise GateError("BOOTSTRAP_INVALID")
    client = httpx.Client(base_url=BASE_URL, headers={"Origin": BASE_URL}, timeout=15.0,
                          trust_env=False)
    try:
        response = client.post("/api/v1/local/session", json={"bootstrap": secret})
        if response.status_code != 200:
            raise GateError("SESSION_EXCHANGE_FAILED")
        body = response.json()
        csrf = body.get("csrf_token") if isinstance(body, dict) else None
        if not isinstance(csrf, str) or not csrf or "cdr_session" not in client.cookies:
            raise GateError("SESSION_EXCHANGE_FAILED")
        client.headers["X-CSRF-Token"] = csrf
        return client
    except BaseException:
        client.close()
        raise
    finally:
        del secret


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.get(path)
    if response.status_code != 200:
        raise GateError("LOCAL_API_READ_FAILED")
    try:
        value = response.json()
    except ValueError as error:
        raise GateError("LOCAL_API_READ_FAILED") from error
    if not isinstance(value, dict):
        raise GateError("LOCAL_API_READ_FAILED")
    return value


def _post(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.post(path, json={})
    if response.status_code != 200:
        raise GateError("LOCAL_API_ACTION_FAILED")
    try:
        value = response.json()
    except ValueError as error:
        raise GateError("LOCAL_API_ACTION_FAILED") from error
    if not isinstance(value, dict):
        raise GateError("LOCAL_API_ACTION_FAILED")
    return value


def _observe(client: httpx.Client, request_id: str, timeout: int,
             journal: Journal) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    previous: str | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/operations/{request_id}")
        if response.status_code == 404:
            status, operation = "NOT_ADMITTED", None
        elif response.status_code == 200:
            try:
                operation = response.json()
            except ValueError as error:
                raise GateError("OPERATION_READ_FAILED") from error
            if not isinstance(operation, dict) or operation.get("request_id") != request_id:
                raise GateError("OPERATION_IDENTITY_MISMATCH")
            observed_status = operation.get("status")
            if not isinstance(observed_status, str):
                raise GateError("OPERATION_STATUS_INVALID")
            status = observed_status
        else:
            raise GateError("OPERATION_READ_FAILED")
        if status != previous:
            journal.record("OPERATION_STATUS", request_id=request_id,
                           status=status,
                           code=operation.get("code") if operation is not None else None)
            previous = status
        if status in TERMINAL and operation is not None:
            return cast(dict[str, Any], operation)
        time.sleep(2)
    raise GateError("OPERATION_TIMEOUT")


def _verify(preflight: Preflight, operation: dict[str, Any], request_id: str
            ) -> dict[str, Any]:
    result = operation.get("result")
    if (operation.get("status") != "COMPLETED" or operation.get("code") is not None
            or not isinstance(result, dict)):
        raise GateError("OPERATION_NOT_COMPLETED")
    execution = result.get("execution")
    publication = result.get("publication")
    delivery = result.get("delivery")
    report = result.get("report_link")
    if (not isinstance(execution, dict)
            or execution.get("outcome") != preflight.expected_outcome
            or not isinstance(execution.get("exit_code"), int)
            or (execution["exit_code"] == 0) != (preflight.expected_outcome == "PASSED")):
        raise GateError("EXECUTION_OUTCOME_MISMATCH")
    if (not isinstance(publication, dict)
            or publication.get("status") != "DRAFT_PUBLISHED"
            or publication.get("git_status") != "PUBLISHED"
            or not isinstance(publication.get("pull_request_id"), int)
            or not isinstance(publication.get("pull_request_number"), int)
            or not isinstance(publication.get("remote_head"), str)
            or SHA.fullmatch(publication["remote_head"]) is None
            or publication.get("remote_ref") != "refs/heads/" + preflight.branch):
        raise GateError("DRAFT_PUBLICATION_MISSING")
    pr_url = (f"https://github.com/{OWNER}/{PROJECT}/pull/"
              f"{publication['pull_request_number']}")
    if publication.get("pull_request_url") != pr_url:
        raise GateError("DRAFT_PUBLICATION_MISMATCH")
    if (not isinstance(delivery, dict) or delivery.get("status") != "DELIVERED"
            or not isinstance(delivery.get("run_folder_id"), str)
            or not isinstance(delivery.get("request_folder_id"), str)
            or not isinstance(delivery.get("index_file_id"), str)
            or "report.md" not in delivery.get("confirmed_assets", [])):
        raise GateError("DRIVE_DELIVERY_MISSING")
    report_url = ("https://drive.google.com/drive/folders/"
                  + delivery["run_folder_id"])
    if (not isinstance(report, dict) or report.get("status") != "ATTACHED"
            or report.get("url") != report_url
            or report.get("pull_request_id") != publication["pull_request_id"]
            or report.get("pull_request_url") != pr_url):
        raise GateError("REPORT_LINK_MISSING")
    remote = preflight.integrations.github.get_repository(
        OWNER, PROJECT, expected_repo_id=preflight.repository_id,
    )
    if remote.private is not True:
        raise GateError("REPOSITORY_NOT_PRIVATE")
    pr = preflight.integrations.github.reconcile_draft_pr(
        remote, head_branch=preflight.branch,
        base_branch=remote.default_branch or "main",
        expected_head_sha=publication["remote_head"],
        expected_pr_id=publication["pull_request_id"],
    ).pull_request
    if (pr.number != publication["pull_request_number"] or pr.html_url != pr_url
            or report_url not in pr.body
            or (preflight.expected_outcome == "FAILED"
                and not pr.title.startswith("[FAILED]"))):
        raise GateError("DRAFT_PR_INDEPENDENT_READBACK_FAILED")
    provider = preflight.integrations.provider
    run_folder = provider.get_file(delivery["run_folder_id"])
    request_folder = provider.get_file(delivery["request_folder_id"])
    index = provider.get_file(delivery["index_file_id"])
    if (run_folder.get("id") != delivery["run_folder_id"]
            or run_folder.get("mimeType") != FOLDER_MIME
            or run_folder.get("parents") != [delivery["request_folder_id"]]
            or run_folder.get("trashed") is not False
            or request_folder.get("id") != delivery["request_folder_id"]
            or request_folder.get("mimeType") != FOLDER_MIME
            or request_folder.get("parents") != [
                preflight.integrations.attached.index.files["results"].file_id
            ]
            or request_folder.get("trashed") is not False
            or index.get("id") != delivery["index_file_id"]
            or index.get("name") != f"delivery-index-{request_id}.json"
            or index.get("mimeType") != "application/json"
            or index.get("parents") != [delivery["run_folder_id"]]
            or index.get("trashed") is not False):
        raise GateError("DRIVE_DELIVERY_INDEPENDENT_READBACK_FAILED")
    for document_id in (preflight.receipt_id, preflight.latest_result_id):
        snapshot, _, _ = _read(provider, document_id, MAX_COMMAND_DOC_BYTES)
        if request_id not in snapshot.text or delivery["run_folder_id"] not in snapshot.text:
            raise GateError("DRIVE_RESULT_DOCUMENT_MISMATCH")
    return {
        "request_id": request_id, "operation_status": operation["status"],
        "execution_outcome": execution["outcome"],
        "execution_exit_code": execution["exit_code"],
        "draft_pr_url": pr_url, "draft_pr_id": pr.id,
        "private_repository": True, "draft_confirmed": pr.draft,
        "report_link_in_pr": True, "drive_report_url": report_url,
        "drive_delivery_readback": True,
        "drive_result_documents_readback": True,
    }


def _disarm_confirmed(client: httpx.Client) -> bool:
    for _ in range(2):
        try:
            _post(client, "/api/v1/local/disarm")
            if _get(client, "/api/v1/system").get("armed") is False:
                return True
        except Exception:  # noqa: BLE001, S112 - bounded local recovery, no secret text
            continue
    return False


def _apply(preflight: Preflight, args: argparse.Namespace,
           change: dict[str, Any], change_text: str) -> int:
    output = args.output or EVIDENCE_DIR / (
        "live-publish-" + preflight.workspace_id + "-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".jsonl"
    )
    journal = Journal(output)
    client: httpx.Client | None = None
    arm_attempted = False
    accepted = False
    failure: str | None = None
    request_id = str(uuid4())
    try:
        journal.record(
            "INTENT", request_id=request_id, project_id=PROJECT,
            workspace_id=preflight.workspace_id, expected_outcome=preflight.expected_outcome,
            expected_head=preflight.head, branch=preflight.branch,
            repository_id=preflight.repository_id,
            command_doc_id=preflight.command_id,
            command_revision=preflight.command.revision_id,
            command_sha256=preflight.command.sha256,
            changeset_doc_id=preflight.changeset_id,
            changeset_revision=preflight.changeset.revision_id,
            changeset_sha256_before=preflight.changeset.sha256,
            changeset_sha256_desired=_sha(change_text),
            planned_files=[{"path": item["path"],
                            "sha256": _sha(item["content_utf8"])}
                           for item in change["files"]],
            publication_policy="draft_always",
        )
        client = _session(preflight.paths)
        system = _get(client, "/api/v1/system")
        if (system.get("armed") is not False
                or system.get("runner_id") != preflight.runner_id
                or "DELIVER_VERIFY_PUBLISH" not in system.get("capabilities", [])):
            raise GateError("INSTALLED_SERVICE_NOT_READY")
        journal.record("SERVICE_READBACK", runner_id=system["runner_id"],
                       epoch=system["epoch"], armed=False,
                       route_available=True)

        journal.record("CHANGESET_WRITE_INTENT", document_id=preflight.changeset_id,
                       required_revision=preflight.changeset.revision_id,
                       desired_sha256=_sha(change_text))
        change_after, change_ack = _replace_once(
            preflight.integrations.provider, preflight.changeset_id,
            preflight.changeset, change_text, MAX_CHANGESET_BYTES,
        )
        journal.record("CHANGESET_READBACK", document_id=preflight.changeset_id,
                       revision=change_after.revision_id, sha256=change_after.sha256,
                       write_state=change_ack)

        draft = _command(preflight, request_id, change_after.revision_id,
                         dispatch="DRAFT", runner_id=system["runner_id"],
                         epoch=system["epoch"])
        draft_text = _json_text(draft)
        journal.record("DRAFT_WRITE_INTENT", document_id=preflight.command_id,
                       required_revision=preflight.command.revision_id,
                       desired_sha256=_sha(draft_text))
        draft_after, draft_ack = _replace_once(
            preflight.integrations.provider, preflight.command_id,
            preflight.command, draft_text, MAX_COMMAND_DOC_BYTES,
        )
        journal.record("DRAFT_READBACK", document_id=preflight.command_id,
                       revision=draft_after.revision_id, sha256=draft_after.sha256,
                       write_state=draft_ack)

        journal.record("ARM_INTENT", epoch_before=system["epoch"])
        arm_attempted = True
        armed = _post(client, "/api/v1/local/arm")
        system = _get(client, "/api/v1/system")
        if (system.get("armed") is not True
                or system.get("epoch") != armed.get("epoch")
                or system.get("epoch") == draft["epoch"]
                or system.get("runner_id") != draft["runner_id"]):
            raise GateError("ARM_READBACK_MISMATCH")
        journal.record("ARM_READBACK", runner_id=system["runner_id"],
                       epoch=system["epoch"], armed=True)
        ready = _command(preflight, request_id, change_after.revision_id,
                         dispatch="READY", runner_id=system["runner_id"],
                         epoch=system["epoch"])
        ready_text = _json_text(ready)
        journal.record("READY_WRITE_INTENT", document_id=preflight.command_id,
                       required_revision=draft_after.revision_id,
                       desired_sha256=_sha(ready_text), epoch=system["epoch"])
        ready_after, ready_ack = _replace_once(
            preflight.integrations.provider, preflight.command_id,
            draft_after, ready_text, MAX_COMMAND_DOC_BYTES,
        )
        journal.record("READY_READBACK", document_id=preflight.command_id,
                       revision=ready_after.revision_id, sha256=ready_after.sha256,
                       write_state=ready_ack)
        operation = _observe(client, request_id, args.timeout_seconds, journal)
        journal.record("TERMINAL", request_id=request_id,
                       status=operation.get("status"), code=operation.get("code"))
        proof = _verify(preflight, operation, request_id)
        journal.record("INDEPENDENT_PROOF", **proof)
        accepted = True
    except Exception as error:  # noqa: BLE001 - never disclose provider/session exceptions
        raw_code = getattr(error, "code", None)
        failure = raw_code if isinstance(raw_code, str) and CODE.fullmatch(raw_code) else "ACCEPTANCE_FAILED"
        journal.record("FAULT", request_id=request_id, code=failure)
    finally:
        if client is not None:
            if arm_attempted:
                disarmed = _disarm_confirmed(client)
                journal.record("DISARM_READBACK", request_id=request_id,
                               confirmed=disarmed)
                if not disarmed:
                    failure = "DISARM_UNCONFIRMED"
            client.close()
        journal.record("FINAL", request_id=request_id,
                       result="PASS" if accepted and failure is None else "FAIL",
                       code=failure, armed=False if arm_attempted and failure != "DISARM_UNCONFIRMED"
                       else None)
        journal.close()
    result = "PASS" if accepted and failure is None else "FAIL"
    print(json.dumps({"result": result, "request_id": request_id,
                      "code": failure, "evidence": str(output)}, sort_keys=True), flush=True)
    return 0 if result == "PASS" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, choices=tuple(EXPECTED))
    parser.add_argument("--expect", required=True, choices=("PASSED", "FAILED"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path, help="new .jsonl under EP-07 evidence directory")
    args = parser.parse_args()
    if EXPECTED[args.workspace] != args.expect:
        parser.error("Use primary/PASSED or parallel/FAILED once each")
    if not 30 <= args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be 30..3600")
    if args.output is not None and (not args.output.is_absolute()
                                    or args.output.parent != EVIDENCE_DIR
                                    or args.output.suffix != ".jsonl"):
        parser.error("--output must be an absolute new .jsonl path in EP-07 evidence")
    preflight = _preflight(args.workspace, args.expect)
    change = _changeset(preflight, str(uuid4()))
    change_text = _json_text(change)
    plan = {
        "mode": "APPLY" if args.apply else "READ_ONLY",
        "project_id": PROJECT, "workspace_id": args.workspace,
        "expected_outcome": args.expect, "head": preflight.head,
        "branch": preflight.branch, "repository_id": preflight.repository_id,
        "command_doc_id": preflight.command_id,
        "changeset_doc_id": preflight.changeset_id,
        "changeset_sha256_desired": _sha(change_text),
        "cloud_writes": args.apply, "service_arm": args.apply,
    }
    print(json.dumps(plan, sort_keys=True), flush=True)
    if not args.apply:
        return 0
    return _apply(preflight, args, change, change_text)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as error:
        print(json.dumps({"result": "FAIL", "code": error.code}), flush=True)
        raise SystemExit(2) from None
    except Exception:  # noqa: BLE001 - sanitize provider/configuration preflight failures
        print(json.dumps({"result": "FAIL", "code": "PREFLIGHT_FAILED"}), flush=True)
        raise SystemExit(2) from None
