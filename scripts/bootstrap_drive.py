#!/usr/bin/env python3
"""Create the Drive seed using YOUR local OAuth app. Default is a no-network plan.

This installer only creates its own files. It does not arm the runner, issue READY
commands, change sharing, create GitHub repositories, or run project code.
Cloud behavior must be integration-tested before production use.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "https://www.googleapis.com/auth/drive"
DOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"


EXISTING_INSTALLATION = ROOT / "config/installation.existing.json"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class BootstrapRefused(RuntimeError):
    """The local installation policy forbids creating a second workspace."""

    def __init__(self, code: str, policy: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.policy = policy or {}

    def diagnostic(self) -> dict[str, Any]:
        return {
            "stage": "bootstrap_policy",
            "code": self.code,
            "mode": self.policy.get("mode"),
            "bootstrap_allowed": self.policy.get("bootstrap_allowed"),
            "existing_root_folder_id": self.policy.get("root_folder_id"),
            "existing_installation_index_document_id": self.policy.get("installation_index_document_id"),
        }


class BootstrapApiError(RuntimeError):
    """A redacted, machine-readable Google API failure."""

    def __init__(self, operation: str, status: int | None, attempts: int,
                 retryable: bool, effect_uncertain: bool):
        super().__init__(f"Google API {operation} failed")
        self.operation = operation
        self.status = status
        self.attempts = attempts
        self.retryable = retryable
        self.effect_uncertain = effect_uncertain

    def diagnostic(self) -> dict[str, Any]:
        return {
            "stage": "google_api",
            "code": "GOOGLE_API_REQUEST_FAILED",
            "operation": self.operation,
            "http_status": self.status,
            "attempts": self.attempts,
            "retryable": self.retryable,
            "effect_uncertain": self.effect_uncertain,
            "next_step": "RECONCILE_BEFORE_RETRY" if self.effect_uncertain else "CHECK_AUTH_OR_RETRY_LATER",
        }


def ensure_bootstrap_allowed(policy_path: Path = EXISTING_INSTALLATION) -> None:
    """Fail closed on an existing-installation policy before OAuth or state writes."""
    if policy_path.is_symlink():
        raise BootstrapRefused("BOOTSTRAP_POLICY_INVALID")
    if not policy_path.exists():
        return  # A fresh distribution without an installed-workspace record.
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BootstrapRefused("BOOTSTRAP_POLICY_INVALID") from exc
    if not isinstance(policy, dict):
        raise BootstrapRefused("BOOTSTRAP_POLICY_INVALID")
    if policy.get("mode") != "bootstrap_new" or policy.get("bootstrap_allowed") is not True:
        raise BootstrapRefused("BOOTSTRAP_FORBIDDEN_EXISTING_INSTALLATION", policy)


def _http_status(error: Exception) -> int | None:
    status = getattr(getattr(error, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _rate_limit_reason(error: Exception) -> bool:
    content = getattr(error, "content", None)
    if not isinstance(content, (str, bytes)):
        return False
    try:
        body = json.loads(content)
    except (UnicodeError, ValueError):
        return False
    error_body = body.get("error") if isinstance(body, dict) else None
    details = error_body.get("errors", []) if isinstance(error_body, dict) else []
    return isinstance(details, list) and any(
        isinstance(item, dict) and item.get("reason") in RATE_LIMIT_REASONS for item in details)


def _retry_after(error: Exception) -> float:
    response = getattr(error, "resp", None)
    value = response.get("retry-after") if hasattr(response, "get") else None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def execute_api(make_request: Callable[[], Any], *, operation: str, read_only: bool,
                max_attempts: int = 4, max_delay_seconds: float = 8.0,
                sleep: Callable[[float], None] = time.sleep,
                random_fraction: Callable[[], float] = random.random) -> dict[str, Any]:
    """Bound retries to read requests; a failed write may already have succeeded."""
    if max_attempts < 1 or max_attempts > 8 or max_delay_seconds <= 0:
        raise ValueError("Invalid API retry bounds")
    for attempt in range(1, max_attempts + 1):
        try:
            return make_request().execute(num_retries=0)
        except Exception as exc:
            status = _http_status(exc)
            retryable = status in RETRYABLE_STATUS or (status == 403 and _rate_limit_reason(exc))
            if not read_only or not retryable or attempt == max_attempts:
                raise BootstrapApiError(operation, status, attempt, retryable,
                                        effect_uncertain=not read_only) from exc
            delay = min(max_delay_seconds,
                        max(2 ** (attempt - 1) + min(max(random_fraction(), 0.0), 1.0),
                            _retry_after(exc)))
            sleep(delay)
    raise AssertionError("Unreachable API retry state")

def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cdr-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def plan() -> dict[str, Any]:
    manifest = json.loads((ROOT / "drive-seed/bootstrap-manifest.json").read_text())
    for entry in manifest["entries"]:
        if "source" in entry:
            path = (ROOT / entry["source"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
                raise ValueError(f"Invalid seed source: {entry['source']}")
    return manifest

def extract_plain(document: dict) -> tuple[str, str | None]:
    """Reject unexpected multi-tab instructions instead of silently reading the wrong tab."""
    tabs = document.get("tabs", [])
    if tabs:
        if len(tabs) != 1 or tabs[0].get("childTabs"):
            raise RuntimeError("Seed document has unexpected multiple tabs; review manually")
        tab_id = tabs[0]["tabProperties"]["tabId"]
        body = tabs[0].get("documentTab", {}).get("body", {})
    else:
        tab_id, body = None, document.get("body", {})
    text = []
    for element in body.get("content", []):
        if "table" in element:
            raise RuntimeError("Seed document contains unexpected tables")
        for part in element.get("paragraph", {}).get("elements", []):
            text.append(part.get("textRun", {}).get("content", ""))
    return "".join(text), tab_id

def apply(manifest: dict, credentials_file: Path, token_store: Path, state_file: Path) -> dict:
    ensure_bootstrap_allowed()
    # Optional packages and credentials are touched ONLY after policy approval.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    state = json.loads(state_file.read_text()) if state_file.exists() else {
        "installation_id": str(uuid.uuid4()), "entries": {}}
    atomic_json(state_file, state)  # Persist identity before any external side effect.
    creds = Credentials.from_authorized_user_file(str(token_store), [SCOPE]) if token_store.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), [SCOPE])
        creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    atomic_json(token_store, json.loads(creds.to_json()))
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    installation = state["installation_id"]
    fields = "id,name,mimeType,webViewLink,parents,appProperties,trashed"

    def find_owned(key: str) -> dict | None:
        # Keys are trusted local manifest literals, not document instructions.
        if "'" in key: raise ValueError("Invalid manifest key")
        q = (f"trashed=false and appProperties has {{ key='cdr_installation' and value='{installation}' }} "
             f"and appProperties has {{ key='cdr_key' and value='{key}' }}")
        matches, page_token = [], None
        while True:
            result = execute_api(
                lambda page_token=page_token: drive.files().list(q=q, fields=f"nextPageToken,files({fields})",
                                           pageToken=page_token, pageSize=100),
                operation="drive.files.list", read_only=True)
            matches.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token: break
        if len(matches) > 1:
            raise RuntimeError(f"Ambiguous duplicate for {key}; reconcile rather than choosing by name")
        return matches[0] if matches else None

    def initialize_doc(file_id: str, text: str, already_initialized: bool) -> None:
        document = execute_api(
            lambda: docs.documents().get(documentId=file_id, includeTabsContent=True),
            operation="docs.documents.get", read_only=True)
        current, tab_id = extract_plain(document)
        if already_initialized:
            return  # Never overwrite user's edits on a later invocation.
        if current.strip():
            if current.rstrip("\n") == text.rstrip("\n"):
                return  # Prior request succeeded before a network/host crash.
            raise RuntimeError(f"Document {file_id} is nonempty and differs; no automatic overwrite")
        location: dict[str, Any] = {"index": 1}
        if tab_id: location["tabId"] = tab_id
        request: dict[str, Any] = {"documentId": file_id,
            "body": {"requests": [{"insertText": {"location": location, "text": text}}]}}
        if document.get("revisionId"):
            request["body"]["writeControl"] = {"requiredRevisionId": document["revisionId"]}
        execute_api(lambda: docs.documents().batchUpdate(**request),
                    operation="docs.documents.batchUpdate", read_only=False)
        readback, _ = extract_plain(execute_api(
            lambda: docs.documents().get(documentId=file_id, includeTabsContent=True),
            operation="docs.documents.get", read_only=True))
        if readback.rstrip("\n") != text.rstrip("\n"):
            raise RuntimeError(f"Readback mismatch for {file_id}; stop and inspect")

    for entry in manifest["entries"]:
        key = entry["key"]
        existing = find_owned(key)
        expected_parent = state["entries"].get(entry.get("parent"), {}).get("id")
        if existing:
            if expected_parent and expected_parent not in existing.get("parents", []):
                raise RuntimeError(f"Owned file {key} was moved; stop rather than moving it back")
            record = existing
        else:
            mime = FOLDER if entry["kind"] == "folder" else DOC if entry["kind"] == "document" else entry.get("mime_type", "text/plain")
            body: dict[str, Any] = {"name": entry["name"], "mimeType": mime,
                "appProperties": {"cdr_installation": installation, "cdr_key": key}}
            if expected_parent: body["parents"] = [expected_parent]
            kwargs: dict[str, Any] = {"body": body, "fields": fields}
            if entry["kind"] == "file":
                kwargs["media_body"] = MediaFileUpload(str(ROOT / entry["source"]),
                                                       mimetype=mime, resumable=False)
            record = execute_api(lambda kwargs=kwargs: drive.files().create(**kwargs),
                                 operation="drive.files.create", read_only=False)
        prior = state["entries"].get(key, {})
        state["entries"][key] = {**record, "initialized": prior.get("initialized", False)}
        atomic_json(state_file, state)
        if entry["kind"] == "document":
            if entry.get("dynamic") == "installation_index":
                text = "CHAT-DEV RUNNER / INSTALLATION INDEX\n\nRunner is NOT armed. No READY commands were created.\n\n"
                text += json.dumps({"installation_id": installation, "documents": state["entries"]},
                                   ensure_ascii=False, indent=2)
            else:
                text = (ROOT / entry["source"]).read_text(encoding="utf-8")
            initialize_doc(record["id"], text, bool(prior.get("initialized")))
        state["entries"][key]["initialized"] = True
        # Provider metadata readback verifies the actual destination exists.
        metadata = execute_api(lambda record_id=record["id"]: drive.files().get(fileId=record_id, fields=fields),
                               operation="drive.files.get", read_only=True)
        if metadata.get("trashed"):
            raise RuntimeError(f"File {key} is unexpectedly trashed")
        state["entries"][key].update(metadata)
        atomic_json(state_file, state)
    state["complete"] = True
    state["runner_armed"] = False
    atomic_json(state_file, state)
    return state

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Explicit no-network preview (also the default)")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--token-store", type=Path, default=Path.home()/".config/chat-dev-runner/google-token.json")
    parser.add_argument("--state-file", type=Path, default=ROOT/".local-state/drive-bootstrap.json")
    args = parser.parse_args()
    if args.apply and args.plan:
        parser.error("Choose --plan or --apply, not both")
    manifest = plan()
    if not args.apply:
        print(json.dumps({"mode":"PLAN_ONLY", "network_calls":False,"oauth_scope":SCOPE,
                          "entry_count":len(manifest["entries"]),"entries":manifest["entries"]},
                         ensure_ascii=False, indent=2))
        return
    try:
        ensure_bootstrap_allowed()
    except BootstrapRefused as exc:
        parser.exit(2, json.dumps(exc.diagnostic(), ensure_ascii=False) + "\n")
    if not args.credentials or not args.credentials.is_file():
        parser.error("--apply requires an existing local --credentials OAuth desktop-client JSON file")
    try:
        result = apply(manifest, args.credentials, args.token_store, args.state_file)
    except (BootstrapRefused, BootstrapApiError) as exc:
        parser.exit(2, json.dumps(exc.diagnostic(), ensure_ascii=False) + "\n")
    print(json.dumps({"complete":result["complete"], "runner_armed":False,
                      "workspace":result["entries"]["root"],
                      "start_here":result["entries"]["start-here"],
                      "local_manifest":str(args.state_file)}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
