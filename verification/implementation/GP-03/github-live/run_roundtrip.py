"""One reviewed, foreground GitHub/rootless v2 PASS route.

Default invocation prepares local review artifacts only. --execute additionally
requires the exact approved plan SHA256. No installed service/state is changed.
The sole permitted create-ref is the fresh acceptance SOURCE branch below.
Every remote POST/PATCH has a durable local intent before the request is sent.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app.adapters.github import GitHubHTTPResponse, GitHubRepository
from runner_app.adapters.github_mailbox_v2 import BoundedGitHubV2HTTP, GitHubV2Client
from runner_app.config_v2 import parse_v2_config
from runner_app.domain.contracts_v2 import validate_schema
from runner_app.engine.runtime_v2 import V2Assembly
from runner_app.executors.volume_provider import VOLUME_BASE, load_python_quick_grant
from runner_app.git.argv import run_git, run_git_bytes
from runner_app.git.github_transport import GitHubGitTransport, OwnerTokenFile
from runner_app.git.results_v2 import COMMIT_AUTHOR, _Bundle, _bundle_tree, _write_object
from runner_app.git.templates import starter_files
from runner_app.storage.database import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths

HERE = Path(__file__).resolve().parent
IMPLEMENTATION = HERE.parents[3]
PRODUCTION_BASE = "9005b6255888e0a9ae579d6b42b1bf8eecce4bde"
PREFLIGHT = Path("/home/visky/Work/GPTRunner/chat-dev-runner-gp-v2/verification/implementation/GP-03/github-live/preflight.json")
PROFILE_DIR = Path("/home/visky/Work/GPTRunner/owner-profiles")
BRANCH = "acceptance/gp03-v2-pass-20261015-a1"
MAIN_HEAD = "1eac4fb222d5ace7af33e086e0e19cde65c13bb1"
PROJECT = "gp03-github-pass"
RUNNER = "gp03-linux-github"
BINDING = "gp03-github-slot"
SLOT = f".runner/commands/{RUNNER}.json"
PLAN = HERE / "review-plan.json"
REPOSITORY = GitHubRepository(
    1392469828, "R_kgDOUv9nRA", "ViskyG", 12452296, "cdr-ep07-ui-20260928",
    "ViskyG/cdr-ep07-ui-20260928", "https://github.com/ViskyG/cdr-ep07-ui-20260928",
    "https://github.com/ViskyG/cdr-ep07-ui-20260928.git",
    "git@github.com:ViskyG/cdr-ep07-ui-20260928.git", True, "main",
    "cdr:v1:71289ae8c91be70d3715e2dcf3ae3adbd61f1febf9c416c39113f82659c674b7",
)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git_hash(kind: str, raw: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw,
                        usedforsecurity=False).hexdigest()


def failure_code(error: BaseException) -> str:
    code = str(error) if type(error) is RuntimeError else getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
        return code
    return type(error).__name__


def write_new(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def git(repo: Path, *args: str) -> str:
    return run_git(repo, list(args)).strip()


def check_implementation() -> str:
    import runner_app

    if Path(runner_app.__file__).resolve().parent.parent != IMPLEMENTATION:
        raise RuntimeError("WRONG_IMPLEMENTATION_IMPORT")
    git(IMPLEMENTATION, "merge-base", "--is-ancestor", PRODUCTION_BASE, "HEAD")
    if git(IMPLEMENTATION, "diff", PRODUCTION_BASE, "--", "runner_app"):
        raise RuntimeError("PRODUCTION_SOURCE_DIFFERS_FROM_REVIEW_BASE")
    return git(IMPLEMENTATION, "rev-parse", "HEAD")


def source_files(plan: Mapping[str, Any]) -> dict[str, bytes]:
    profiles, grant = load_python_quick_grant(PROFILE_DIR)
    profile = profiles.require("python-quick")
    preparation = profiles.require(grant.preparation_profile_id)
    if (profile.revision_sha256 != plan["profile_revision_sha256"]
            or preparation.revision_sha256 != plan["preparation_revision_sha256"]):
        raise RuntimeError("APPROVED_PROFILE_CHANGED")
    files = starter_files("python-basic", PROJECT)
    project = {"protocol": "cdr-project/2", "project_id": PROJECT, "tasks": {"test": {
        "language": "python", "profile_id": "python-quick", "steps": [{
            "id": "pytest", "phase": "test", "argv": list(profile.argv), "cwd": ".",
            "timeout_seconds": profile.timeout_seconds,
        }],
        "outputs": [{"id": item.id, "source_root": "out", "path": item.path,
                     "kind": item.kind, "format": item.format, "required": item.required,
                     "publish": "git_if_fits"} for item in profile.collect],
        "quality_gate": {"test_required": True, "minimum_executed": 1, "required_outputs": ["junit"]},
    }}}
    draft = {"protocol": "cdr-mailbox/2", "runner_id": RUNNER, "project_id": PROJECT,
             "slot_token": plan["draft_slot_token"], "dispatch": "DRAFT", "receipt": None,
             "request": {"request_id": plan["request_id"], "action": "RUN_TASK", "task": "test",
                         "source": {"kind": "github_branch", "branch": BRANCH},
                         "execution": {"mode": "container", "backend_id": "rootless-docker",
                                       "profile_id": "python-quick", "expected_os": "linux",
                                       "timeout_seconds": profile.timeout_seconds + preparation.timeout_seconds}}}
    validate_schema(project, "v2-project")
    validate_schema(draft, "v2-mailbox")
    files[".runner/project.json"], files[SLOT] = canonical(project), canonical(draft)
    return files


def build_local_objects(repo: Path, files: dict[str, bytes], plan: Mapping[str, Any]) -> tuple[str, str]:
    tree = _bundle_tree(repo, _Bundle(files, "source-setup-only"))
    identity = f"{COMMIT_AUTHOR} {plan['commit_timestamp']} +0000"
    raw = (f"tree {tree}\nparent {MAIN_HEAD}\nauthor {identity}\ncommitter {identity}\n\n"
           f"{plan['source_commit_message']}").encode()
    return tree, _write_object(repo, "commit", raw)


def prepare() -> None:
    implementation_revision = check_implementation()
    preflight = json.loads(PREFLIGHT.read_bytes())
    if (preflight["repository"]["id"] != REPOSITORY.id
            or preflight["repository"]["owner_id"] != REPOSITORY.owner_id
            or preflight["repository"]["node_id"] != REPOSITORY.node_id
            or preflight["repository"]["private"] is not True
            or preflight["default_head"]["sha"] != MAIN_HEAD or preflight["writes"] != 0):
        raise RuntimeError("READ_ONLY_PREFLIGHT_ANCHOR_DIFFERS")
    profiles, grant = load_python_quick_grant(PROFILE_DIR)
    profile = profiles.require("python-quick")
    preparation = profiles.require(grant.preparation_profile_id)
    acceptance_id = str(uuid4())
    plan: dict[str, Any] = {
        "state": "PREPARED_NOT_EXECUTED", "scope": "foreground temporary V2Assembly; not installed service or ChatGPT connector acceptance",
        "repository": asdict(REPOSITORY), "main_head_must_equal": MAIN_HEAD,
        "branch": BRANCH, "project_id": PROJECT, "runner_id": RUNNER, "binding_id": BINDING,
        "command_path": SLOT, "acceptance_id": acceptance_id, "request_id": str(uuid4()),
        "draft_slot_token": str(uuid4()), "commit_timestamp": int(time.time()),
        "source_commit_message": f"GP-03 disposable v2 source acceptance\n\nAcceptance-Id: {acceptance_id}\n",
        "profile_dir": str(PROFILE_DIR), "profile_revision_sha256": profile.revision_sha256,
        "preparation_profile_id": preparation.profile_id,
        "preparation_revision_sha256": preparation.revision_sha256,
        "image": profile.image, "platform": profile.platform,
        "script_sha256": sha256(Path(__file__).read_bytes()),
        "production_base": PRODUCTION_BASE, "prepared_implementation_revision": implementation_revision,
        "read_only_preflight_sha256": sha256(PREFLIGHT.read_bytes()),
        "ref_operations": [
            {"method": "POST", "endpoint": "/git/refs", "ref": f"refs/heads/{BRANCH}", "only_if_absent": True,
             "purpose": "ONE fresh acceptance SOURCE branch; parent is unchanged main anchor"},
            {"method": "PATCH", "endpoint": f"/git/refs/heads/{BRANCH}", "force": False,
             "purposes": ["baseline DRAFT token rotation", "operator READY", "coordinator ACK", "additive same-branch result"]},
        ],
        "excluded_operations": ["no Drive calls", "no new repositories or PRs", "no force pushes",
                                "no main/existing-branch changes", "no installed service/state changes"],
    }
    files = source_files(plan)
    with tempfile.TemporaryDirectory(prefix="gp03-github-plan-") as temporary:
        root = Path(temporary)
        git(root, "init", "--bare", str(root / "repo.git"))
        plan["source_tree"], plan["source_commit"] = build_local_objects(root / "repo.git", files, plan)
    plan["files"] = [{"path": path, "bytes": len(raw), "sha256": sha256(raw), "git_blob": git_hash("blob", raw)}
                     for path, raw in sorted(files.items())]
    write_new(HERE / "planned-project.json", files[".runner/project.json"])
    write_new(HERE / "planned-draft.json", files[SLOT])
    write_new(PLAN, canonical(plan))
    print(json.dumps({"state": plan["state"], "branch": BRANCH, "plan_sha256": sha256(PLAN.read_bytes()),
                      "plan": str(PLAN), "script": str(Path(__file__))}, sort_keys=True))


class AuditHTTP:
    def __init__(self, token_provider: Callable[[], str], journal: Path, *, readonly: bool = False,
                 source_commit: str | None = None) -> None:
        self.transport = BoundedGitHubV2HTTP(token_provider)
        self.journal, self.readonly = journal, readonly
        self.source_commit = source_commit
        self._ref_creation_attempted = False
        self.phase = "PREFLIGHT"

    def record(self, event: str, **detail: Any) -> None:
        raw = (json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "phase": self.phase,
                           "event": event, **detail}, sort_keys=True) + "\n").encode()
        descriptor = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise RuntimeError("AUDIT_FILE_NOT_PRIVATE")
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self.journal.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        prefix = f"/repos/{REPOSITORY.full_name}"
        if not path.startswith(prefix + "/") and path != prefix:
            raise RuntimeError("UNAPPROVED_REPOSITORY_PATH")
        if method != "GET":
            allowed_object = method == "POST" and path in {
                prefix + "/git/blobs", prefix + "/git/trees", prefix + "/git/commits",
            }
            allowed_create = (method == "POST" and path == prefix + "/git/refs" and body is not None
                              and body.get("ref") == f"refs/heads/{BRANCH}"
                              and body.get("sha") == self.source_commit
                              and self.phase == "SOURCE_SETUP" and not self._ref_creation_attempted)
            allowed_patch = (method == "PATCH" and path == prefix + f"/git/refs/heads/{BRANCH}"
                             and body is not None and body.get("force") is False
                             and self.phase in {"BASELINE_ACK", "OPERATOR_READY", "INTAKE_ACK",
                                                "ACTUAL_ROOTLESS_EXECUTION_AND_RESULT"})
            if self.readonly or not (allowed_object or allowed_create or allowed_patch):
                raise RuntimeError("UNAPPROVED_PROVIDER_WRITE")
            intent = str(uuid4())
            self.record("WRITE_INTENT", intent_id=intent, method=method, path=path,
                        body_sha256=sha256(canonical(body)), ref=body.get("ref") if body else None,
                        commit=body.get("sha") if body else None, force=body.get("force") if body else None,
                        parents=body.get("parents") if body else None,
                        base_tree=body.get("base_tree") if body else None)
            if allowed_create:
                self._ref_creation_attempted = True
        else:
            intent = None
        try:
            response = self.transport.request(method, path, params=params, body=body)
        except Exception:
            if intent is not None:
                self.record("WRITE_RESPONSE_UNKNOWN", intent_id=intent, method=method, path=path)
            raise
        if intent is not None:
            data = response.data if isinstance(response.data, dict) else {}
            self.record("WRITE_RESPONSE", intent_id=intent, status=response.status,
                        object_sha=data.get("sha"), ref=data.get("ref"),
                        ref_sha=data.get("object", {}).get("sha") if isinstance(data.get("object"), dict) else None)
        return response


def exact_object_write(client: GitHubV2Client, audit: AuditHTTP, kind: str, expected: str,
                       create: Callable[[], str]) -> str:
    try:
        observed = create()
        if observed != expected:
            raise RuntimeError("SETUP_OBJECT_SHA_DIFFERS")
        return observed
    except Exception:  # noqa: BLE001 - exact immutable readback or stop; never repeat POST
        # Immutable exact SHA readback or stop. Never repeat a potentially applied POST.
        response = client.request("GET", f"/git/{kind}s/{expected}")
        if response.status != 200 or not isinstance(response.data, dict) or response.data.get("sha") != expected:
            raise RuntimeError("SETUP_OBJECT_OUTCOME_UNKNOWN") from None
        audit.record("SETUP_OBJECT_RECONCILED", kind=kind, sha=expected)
        return expected


def create_source(client: GitHubV2Client, audit: AuditHTTP, plan: Mapping[str, Any],
                  files: dict[str, bytes]) -> str:
    if client.head(BRANCH) is not None or client.head("main") != MAIN_HEAD:
        raise RuntimeError("BRANCH_ALREADY_EXISTS_OR_MAIN_CHANGED")
    audit.phase = "SOURCE_SETUP"
    for path, raw in sorted(files.items()):
        expected = git_hash("blob", raw)
        audit.record("SOURCE_FILE_INTENT", path=path, bytes=len(raw), sha256=sha256(raw), blob=expected)
        exact_object_write(client, audit, "blob", expected, lambda raw=raw: client.create_blob(raw))
    entries = [{"path": path, "mode": "100644", "type": "blob", "sha": git_hash("blob", raw)}
               for path, raw in sorted(files.items())]
    tree = exact_object_write(client, audit, "tree", plan["source_tree"], lambda: client.create_tree(
        entries, expected=plan["source_tree"]))
    commit = exact_object_write(client, audit, "commit", plan["source_commit"], lambda: client.create_commit(
        tree, MAIN_HEAD, plan["source_commit_message"], plan["commit_timestamp"], expected=plan["source_commit"]))
    client.ensure_identity()
    if client.head(BRANCH) is not None or client.head("main") != MAIN_HEAD:
        raise RuntimeError("REF_CHANGED_BEFORE_SOURCE_BRANCH_CREATION")
    try:
        response = client.request("POST", "/git/refs", body={"ref": f"refs/heads/{BRANCH}", "sha": commit})
        if response.status != 201:
            raise RuntimeError("SOURCE_REF_RESPONSE_NOT_CREATED")
    except Exception:  # noqa: BLE001 - lost create-ref response requires exact ref readback
        if client.head(BRANCH) != commit:
            raise RuntimeError("SOURCE_REF_OUTCOME_UNKNOWN") from None
        audit.record("SOURCE_REF_RECONCILED", commit=commit)
    if client.head(BRANCH) != commit:
        raise RuntimeError("SOURCE_REF_READBACK_DIFFERS")
    return commit


def publish_ready(client: GitHubV2Client, audit: AuditHTTP, port: Any, expected: Any,
                  desired: bytes, request_id: str) -> str:
    audit.phase = "OPERATOR_READY"
    current = port.read_current()
    if current != expected:
        raise RuntimeError("DRAFT_CHANGED_BEFORE_OPERATOR_READY")
    revision = json.loads(base64.urlsafe_b64decode(current.revision[10:]))
    parent, base = revision["head"], revision["tree"]
    blob_sha = git_hash("blob", desired)
    blob = exact_object_write(client, audit, "blob", blob_sha, lambda: client.create_blob(desired))
    # Tree creation is deterministic and harmless until the existing-ref PATCH;
    # an unavailable response has no safe known new tree identity, so stop here.
    tree = client.create_tree([{"path": SLOT, "mode": revision["mode"], "type": "blob", "sha": blob}], base=base)
    timestamp = int(time.time())
    message = f"GP-03 operator READY submission\n\nRequest-Id: {request_id}\n"
    identity = f"{COMMIT_AUTHOR} {timestamp} +0000"
    raw = (f"tree {tree}\nparent {parent}\nauthor {identity}\ncommitter {identity}\n\n{message}").encode()
    candidate_sha = git_hash("commit", raw)
    candidate = exact_object_write(client, audit, "commit", candidate_sha,
                                  lambda: client.create_commit(tree, parent, message, timestamp, expected=candidate_sha))
    if port.read_current() != expected or client.head(BRANCH) != parent:
        raise RuntimeError("DRAFT_CHANGED_BEFORE_READY_REF_UPDATE")
    try:
        response = client.patch_existing(BRANCH, candidate)
        if response.status != 200:
            raise RuntimeError("READY_REF_RESPONSE_NOT_APPLIED")
    except Exception:  # noqa: BLE001 - lost PATCH response requires exact content/ref readback
        if client.head(BRANCH) != candidate or port.read_current().payload != desired:
            raise RuntimeError("READY_REF_OUTCOME_UNKNOWN") from None
        audit.record("READY_REF_RECONCILED", commit=candidate)
    if client.head(BRANCH) != candidate or port.read_current().payload != desired:
        raise RuntimeError("READY_REF_READBACK_DIFFERS")
    return candidate


def remote_file(client: GitHubV2Client, commit: str, path: str, cache: dict[str, Any]) -> bytes:
    key = "commit:" + commit
    if key not in cache:
        cache[key] = client.commit_tree(commit)
    root = cache[key]
    parts = path.split("/")
    for index, part in enumerate(parts):
        if root not in cache:
            cache[root] = {entry["path"]: entry for entry in client.tree(root)}
        entry = cache[root][part]
        if index == len(parts) - 1:
            if entry["type"] != "blob" or entry["mode"] != "100644":
                raise RuntimeError("REMOTE_RESULT_FILE_MODE_DIFFERS")
            return client.blob(entry["sha"], client.limits.blob_bytes)
        if entry["type"] != "tree":
            raise RuntimeError("REMOTE_RESULT_PARENT_NOT_TREE")
        root = entry["sha"]
    raise RuntimeError("REMOTE_RESULT_FILE_MISSING")


def execute(approved: str) -> None:
    implementation_revision = check_implementation()
    raw_plan = PLAN.read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", approved) or sha256(raw_plan) != approved:
        raise RuntimeError("REVIEWED_PLAN_APPROVAL_REQUIRED")
    plan = json.loads(raw_plan)
    if plan["script_sha256"] != sha256(Path(__file__).read_bytes()) or plan["branch"] != BRANCH:
        raise RuntimeError("REVIEWED_SCRIPT_OR_BRANCH_CHANGED")
    if os.geteuid() == 0:
        raise RuntimeError("ORDINARY_USER_REQUIRED")
    files = source_files(plan)
    if plan["files"] != [{"path": path, "bytes": len(raw), "sha256": sha256(raw), "git_blob": git_hash("blob", raw)}
                         for path, raw in sorted(files.items())]:
        raise RuntimeError("REVIEWED_SOURCE_BYTES_CHANGED")
    run_root = HERE / ("run-" + plan["acceptance_id"])
    run_root.mkdir(mode=0o700)  # Existing run directory fences script replay.
    token_provider = OwnerTokenFile(Paths.discover().config_dir / "github-token")
    audit = AuditHTTP(token_provider, run_root / "provider-journal.jsonl", source_commit=plan["source_commit"])
    audit.record("APPROVED_PLAN", plan_sha256=approved, script_sha256=plan["script_sha256"], branch=BRANCH)
    client = GitHubV2Client(REPOSITORY, token_provider, transport=audit)
    assembly: V2Assembly | None = None
    started = time.monotonic()
    summary: dict[str, Any] = {"status": "STARTED", "branch": BRANCH, "request_id": plan["request_id"],
                               "repository_id": REPOSITORY.id, "owner_id": REPOSITORY.owner_id,
                               "started_at": datetime.now(timezone.utc).isoformat(),
                               "implementation_revision": implementation_revision, "production_base": PRODUCTION_BASE,
                               "scope": plan["scope"], "installed_service_modified": False, "drive_calls": 0}
    try:
        client.ensure_identity()
        if client.head("main") != MAIN_HEAD:
            raise RuntimeError("MAIN_ANCHOR_CHANGED")
        inventory_parent = run_root / "inventory" / PROJECT
        inventory_parent.mkdir(parents=True, mode=0o700)
        git(inventory_parent, "init", "--bare", str(inventory_parent / "repo.git"))
        inventory_repo = inventory_parent / "repo.git"
        inventory_repo.chmod(0o700)
        reader = GitHubGitTransport(REPOSITORY, token_provider)
        initial_heads = reader.list_heads(inventory_repo)
        if initial_heads.get("main") != MAIN_HEAD or BRANCH in initial_heads:
            raise RuntimeError("INITIAL_BRANCH_INVENTORY_DIFFERS")
        write_new(run_root / "heads-before.json", canonical(initial_heads))
        summary["setup_source_commit"] = create_source(client, audit, plan, files)
        runtime_root = run_root / "temporary-runtime"
        runtime_root.mkdir(mode=0o700)
        paths = Paths(runtime_root / "config", runtime_root / "state", runtime_root / "cache", runtime_root / "runtime")
        paths.ensure_private()
        configuration = parse_v2_config(canonical({
            "protocol": "cdr-runtime/2", "runner_id": RUNNER, "enable_v1": False,
            "github_token_file": str(Paths.discover().config_dir / "github-token"),
            "rootless_profile_dir": str(PROFILE_DIR), "queue_limit": 1, "poll_seconds": 2,
            "projects": [{"project_id": PROJECT, "github": {"id": REPOSITORY.id,
                "node_id": REPOSITORY.node_id, "owner": REPOSITORY.owner,
                "owner_id": REPOSITORY.owner_id, "name": REPOSITORY.name},
                "container_profiles": ["python-quick"]}],
            "mailboxes": [{"id": BINDING, "provider": "github", "project_id": PROJECT,
                           "branch": BRANCH, "path": SLOT}],
        }))
        ledger = Ledger(Database(paths.database_path), runner_id=RUNNER,
                        max_parallel=1, single_execution_lane=True)
        ledger.startup()
        assembly = V2Assembly(configuration, paths, ledger,
                              github_factory=lambda repository, token: GitHubV2Client(repository, token, transport=audit))
        if not assembly.configured or "rootless-docker" not in assembly.handler.container_backends:
            raise RuntimeError("ACTUAL_BACKEND_NOT_CONFIGURED")
        ledger.arm()
        port = assembly.service.bindings[0][1]
        audit.phase = "BASELINE_ACK"
        baseline = assembly.service.coordinator.poll_once(BINDING, port)
        if baseline.kind != "BASELINED":
            raise RuntimeError("DRAFT_BASELINE_NOT_CONFIRMED")
        observed = port.read_current()
        baseline_value = json.loads(observed.payload)
        if (baseline_value["dispatch"] != "DRAFT" or baseline_value["slot_token"] == plan["draft_slot_token"]
                or baseline_value["request"]["request_id"] != plan["request_id"]):
            raise RuntimeError("DRAFT_BASELINE_BYTES_DIFFER")
        write_new(run_root / "baseline.json", observed.payload)
        ready = dict(baseline_value)
        ready["dispatch"] = "READY"
        ready["receipt"] = None
        ready_bytes = canonical(ready)
        write_new(run_root / "operator-ready.json", ready_bytes)
        summary["operator_ready_commit"] = publish_ready(client, audit, port, observed, ready_bytes, plan["request_id"])
        audit.phase = "INTAKE_ACK"
        accepted = assembly.service.coordinator.poll_once(BINDING, port)
        if accepted.kind != "ACCEPTED" or accepted.request_id != plan["request_id"] or accepted.sequence != 1:
            raise RuntimeError("ACTUAL_GITHUB_ACK_NOT_CONFIRMED")
        acknowledged = port.read_current()
        ack_value = json.loads(acknowledged.payload)
        if (ack_value["dispatch"] != "ACCEPTED" or ack_value["slot_token"] == ready["slot_token"]
                or ack_value["request"] != ready["request"]):
            raise RuntimeError("ACK_CONTENT_OR_ROTATION_DIFFERS")
        write_new(run_root / "acknowledged.json", acknowledged.payload)
        summary["tested_source_commit"] = client.head(BRANCH)
        audit.phase = "ACTUAL_ROOTLESS_EXECUTION_AND_RESULT"
        task_started = time.monotonic()
        if not assembly.service.worker.run_once():
            raise RuntimeError("ACTUAL_WORKER_DID_NOT_CLAIM")
        summary["worker_duration_seconds"] = time.monotonic() - task_started
        operation = assembly.store.get(plan["request_id"])
        write_new(run_root / "operation.json", canonical(operation))
        if operation is None or operation["status"] != "COMPLETED":
            raise RuntimeError("ACTUAL_OPERATION_NOT_COMPLETED")
        result = operation["result"]
        manifest, delivery = result["manifest"], result["delivery"]
        if (manifest["source"]["commit"] != summary["tested_source_commit"]
                or manifest["source"]["branch"] != BRANCH
                or manifest["execution"] != {"outcome": "PASSED", "exit_code": 0,
                                             "quality_gate": "PASSED", "cleanup": "CONFIRMED"}
                or manifest["environment"]["mode"] != "container"
                or delivery["status"] != "DELIVERED"):
            raise RuntimeError("ACTUAL_EXECUTION_OR_DELIVERY_NOT_PASSED")
        summary["result_commit"] = delivery["commit"]
        summary["bundle_sha256"] = result["bundle_sha256"]
        with sqlite3.connect(paths.database_path) as connection:
            connection.row_factory = sqlite3.Row
            events = [dict(row) for row in connection.execute(
                "SELECT * FROM v2_journal WHERE request_id=? ORDER BY rowid", (plan["request_id"],))]
            lane_count = connection.execute("SELECT count(*) FROM execution_lane").fetchone()[0]
        write_new(run_root / "request-events.json", canonical(events))
        released = [json.loads(event["detail_json"]) for event in events if event["event"] == "ROOTLESS_VOLUME_RELEASED"]
        if {item["phase"] for item in released} != {"PREPARATION", "TEST"} or lane_count != 0:
            raise RuntimeError("ACTUAL_CLEANUP_OR_LANE_PROOF_INCOMPLETE")
        summary["cleanup"] = {"manifest": "CONFIRMED", "lane_count": lane_count, "released": released}
        for item in released:
            try:
                (VOLUME_BASE / item["run_id"]).lstat()
            except FileNotFoundError:
                continue
            raise RuntimeError("RELEASED_VOLUME_PATH_STILL_PRESENT")
        audit.phase = "INDEPENDENT_REMOTE_VERIFICATION"
        independent = GitHubV2Client(REPOSITORY, token_provider,
                                    transport=AuditHTTP(token_provider, run_root / "independent-journal.jsonl", readonly=True))
        independent.ensure_identity()
        result_commit = delivery["commit"]
        if independent.head(BRANCH) != result_commit:
            raise RuntimeError("REMOTE_RESULT_HEAD_DIFFERS")
        cache: dict[str, Any] = {}
        prefix = f".runner/results/{RUNNER}/{plan['request_id']}"
        remote_manifest_raw = remote_file(independent, result_commit, prefix + "/.runner-bundle.json", cache)
        if sha256(remote_manifest_raw) != result["bundle_sha256"]:
            raise RuntimeError("REMOTE_BUNDLE_DIGEST_DIFFERS")
        remote_manifest = json.loads(remote_manifest_raw)
        if remote_manifest["runner_id"] != RUNNER or remote_manifest["request_id"] != plan["request_id"]:
            raise RuntimeError("REMOTE_BUNDLE_IDENTITY_DIFFERS")
        for item in remote_manifest["files"]:
            raw = remote_file(independent, result_commit, prefix + "/" + item["path"], cache)
            if len(raw) != item["bytes"] or sha256(raw) != item["sha256"]:
                raise RuntimeError("REMOTE_BUNDLE_FILE_DIGEST_DIFFERS")
        remote_result = json.loads(remote_file(independent, result_commit, prefix + "/result.json", cache))
        if remote_result != manifest:
            raise RuntimeError("REMOTE_IMMUTABLE_RESULT_DIFFERS")
        junit = next(item for item in manifest["artifacts"] if item["format"] == "junit-xml" and item["complete"])
        raw_junit = remote_file(independent, result_commit, prefix + "/" + junit["remote_path"], cache)
        write_new(run_root / "raw-junit.xml", raw_junit)
        write_new(run_root / "remote-bundle-manifest.json", remote_manifest_raw)
        verify_parent = run_root / "independent-git" / PROJECT
        verify_parent.mkdir(parents=True, mode=0o700)
        verify_repo = verify_parent / "repo.git"
        git(verify_parent, "init", "--bare", str(verify_repo))
        verify_repo.chmod(0o700)
        independent.fetch_objects(verify_repo, BRANCH, result_commit)
        if git(verify_repo, "rev-parse", f"{result_commit}^") != summary["tested_source_commit"]:
            raise RuntimeError("RESULT_PARENT_DIFFERS_FROM_TESTED_SOURCE")
        diff = run_git_bytes(verify_repo, ["diff-tree", "--no-commit-id", "--name-status", "--no-renames",
                                          "-r", "-z", summary["tested_source_commit"], result_commit]).split(b"\0")[:-1]
        if not diff or len(diff) % 2 or any(status != b"A" or not path.startswith((prefix + "/").encode())
                                           for status, path in zip(diff[::2], diff[1::2], strict=True)):
            raise RuntimeError("RESULT_COMMIT_CHANGED_TESTED_SOURCE")
        expected_source = {path: git_hash("blob", raw) for path, raw in files.items()}
        expected_source[SLOT] = git_hash("blob", acknowledged.payload)
        actual_source: dict[str, str] = {}
        for entry in run_git_bytes(verify_repo, ["ls-tree", "-r", "-z", summary["tested_source_commit"]]).split(b"\0"):
            if entry:
                header, name = entry.split(b"\t", 1)
                mode, kind, oid = header.decode().split(" ")
                if (mode, kind) != ("100644", "blob"):
                    raise RuntimeError("TESTED_SOURCE_MODE_DIFFERS")
                actual_source[name.decode()] = oid
        if actual_source != expected_source:
            raise RuntimeError("TESTED_SOURCE_BYTES_DIFFER_FROM_REVIEWED_PLAN")
        final_heads = reader.list_heads(inventory_repo)
        write_new(run_root / "heads-after.json", canonical(final_heads))
        if final_heads != {**initial_heads, BRANCH: result_commit}:
            raise RuntimeError("PREEXISTING_BRANCH_HEAD_CHANGED")
        summary.update({"status": "VERIFIED_ON_TARGET", "execution": "PASSED", "quality_gate": "PASSED",
                        "delivery": "DELIVERED", "source_preserved": True, "preexisting_branches_preserved": True,
                        "verified_bundle_files": len(remote_manifest["files"]),
                        "raw_report": str(run_root / "raw-junit.xml")})
    except BaseException as error:
        summary.update({"status": "FAILED_OR_UNKNOWN", "failure_phase": audit.phase,
                        "failure_code": failure_code(error)})
        raise
    finally:
        if assembly is not None:
            try:
                assembly.close(deadline=time.monotonic() + 20)
            except Exception:  # noqa: BLE001 - preserve evidence even if runtime close fails
                summary.update({"status": "FAILED_OR_UNKNOWN", "runtime_close": "UNKNOWN"})
            else:
                summary["runtime_close"] = "CONFIRMED"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["duration_seconds"] = time.monotonic() - started
        write_new(run_root / "summary.json", canonical(summary))
        print(json.dumps({"status": summary["status"], "summary": str(run_root / "summary.json")}, sort_keys=True))
        if summary.get("runtime_close") == "UNKNOWN":
            raise RuntimeError("RUNTIME_CLOSE_UNKNOWN") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    arguments = parser.parse_args()
    try:
        if arguments.execute:
            execute(arguments.approved_plan_sha256 or "")
        else:
            prepare()
        return 0
    except BaseException as error:  # noqa: BLE001 - never print provider/credential exception text
        print(json.dumps({"status": "STOPPED", "code": failure_code(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
