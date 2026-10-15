from __future__ import annotations

import base64
import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import runner_app.adapters.drive_mailbox_v2 as module
from runner_app.adapters.drive_auth import CLIENT_FILENAME, DRIVE_SCOPE, TOKEN_FILENAME
from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError
from runner_app.adapters.drive_google import _bounded_batch
from runner_app.adapters.drive_mailbox_v2 import (
    DocsMailboxError,
    DocsMailboxPort,
    DocsProviderConfig,
)
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.domain.mailbox_v2 import acknowledge_mailbox, parse_mailbox
from runner_app.engine.intake_v2 import Observation
from runner_app.storage.paths import Paths

DOCUMENT = "mailbox-document-123"
TAB = "t.command"
OTHER = "t.other"
INSTALLATION = "aa111111-1111-4111-8111-111111111111"
TOKEN = "bb222222-2222-4222-8222-222222222222"
NEXT_TOKEN = "dd444444-4444-4444-8444-444444444444"


def mailbox() -> bytes:
    return json.dumps({
        "protocol": "cdr-mailbox/2", "runner_id": "runner-1", "project_id": "project-1",
        "slot_token": TOKEN, "dispatch": "READY", "receipt": None,
        "request": {"request_id": None, "action": "SHELL",
                    "source": {"kind": "github_branch", "branch": "tested"},
                    "shell": {"dialect": "bash", "script": "printf 'Привет 🐈'\n"},
                    "execution": {"mode": "native", "expected_os": "linux", "timeout_seconds": 30}},
    }, ensure_ascii=False, indent=2).encode()


def ack(raw: bytes) -> bytes:
    return acknowledge_mailbox(parse_mailbox(raw, installation_id=INSTALLATION,
                                             mailbox_id="docs:exact-tab"), slot_token=NEXT_TOKEN)


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def tab(tab_id: str, payload: bytes) -> dict[str, Any]:
    position = 1
    content: list[dict[str, Any]] = [{"sectionBreak": {}, "startIndex": 0, "endIndex": 1}]
    for paragraph in (payload.decode() + "\n").splitlines(keepends=True):
        end = position + units(paragraph)
        content.append({"startIndex": position, "endIndex": end,
                        "paragraph": {"elements": [{"startIndex": position, "endIndex": end,
                                                       "textRun": {"content": paragraph}}]}})
        position = end
    return {"tabProperties": {"tabId": tab_id}, "childTabs": [],
            "documentTab": {"body": {"content": content}}}


class FakeDocs:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {
            "id": DOCUMENT, "mimeType": "application/vnd.google-apps.document", "trashed": False,
            "isAppAuthorized": True, "capabilities": {"canEdit": True},
        }
        outer = tab(OTHER, b"Untouched unrelated tab")
        outer["childTabs"] = [tab(TAB, mailbox())]
        self.document: dict[str, Any] = {"documentId": DOCUMENT, "revisionId": "revision-1", "tabs": [outer]}
        self.calls: list[tuple[str, str]] = []
        self.batches: list[tuple[str, list[Mapping[str, Any]], str]] = []
        self.before_batch: Callable[[], None] | None = None
        self.after_batch: Callable[[], None] | None = None
        self.lose_response = False
        self.unavailable_after_batch = False
        self.unavailable = False
        self.reject: str | None = None
        self.counter = 1

    def selected(self) -> dict[str, Any]:
        return self.document["tabs"][0]["childTabs"][0]

    def payload(self) -> bytes:
        return "".join(run["textRun"]["content"]
                       for element in self.selected()["documentTab"]["body"]["content"]
                       if "paragraph" in element
                       for run in element["paragraph"]["elements"]).encode()[:-1]

    def change(self, payload: bytes | None = None, *, other: bool = False) -> None:
        self.counter += 1
        self.document["revisionId"] = f"revision-{self.counter}"
        if other:
            children = self.document["tabs"][0]["childTabs"]
            self.document["tabs"][0] = tab(OTHER, payload or b"New unrelated text")
            self.document["tabs"][0]["childTabs"] = children
        elif payload is not None:
            self.document["tabs"][0]["childTabs"][0] = tab(TAB, payload)

    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]:
        assert file_id == DOCUMENT and fields == FILE_FIELDS
        self.calls.append(("files.get", file_id))
        if self.unavailable:
            raise OSError("Private provider details")
        return copy.deepcopy(self.metadata)

    def get_document(self, file_id: str, *, include_tabs_content: bool = True) -> Mapping[str, Any]:
        assert file_id == DOCUMENT and include_tabs_content is True
        self.calls.append(("documents.get", file_id))
        if self.unavailable:
            raise OSError("Private provider details")
        return copy.deepcopy(self.document)

    def batch_update_document(self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
                              required_revision_id: str) -> Mapping[str, Any]:
        assert file_id == DOCUMENT
        normalized = _bounded_batch(requests)  # Prove compatibility with the actual Google provider.
        self.calls.append(("documents.batchUpdate", file_id))
        self.batches.append((file_id, copy.deepcopy(normalized), required_revision_id))
        if self.before_batch:
            self.before_batch()
        if self.document["revisionId"] != required_revision_id:
            raise DriveError("STALE_REVISION_OR_INVALID_BATCH", "Expected revision is stale")
        if self.reject:
            raise DriveError(self.reject, "Fake refusal")
        old = self.payload().decode()
        assert normalized[0] == {"deleteContentRange": {"range": {
            "startIndex": 1, "endIndex": 1 + units(old), "tabId": TAB,
        }}}
        assert normalized[1]["insertText"]["location"] == {"index": 1, "tabId": TAB}
        self.change(normalized[1]["insertText"]["text"].encode())
        written_revision = self.document["revisionId"]
        if self.after_batch:
            self.after_batch()
        if self.unavailable_after_batch:
            self.unavailable = True
        if self.lose_response:
            raise OSError("Lost accepted write response")
        return {"documentId": DOCUMENT, "writeControl": {"requiredRevisionId": written_revision}}


@pytest.fixture
def provider() -> FakeDocs:
    return FakeDocs()


def port(provider: FakeDocs, **options: Any) -> DocsMailboxPort:
    return DocsMailboxPort(provider, document_id=DOCUMENT, tab_id=TAB, **options)


def test_construction_inert_exact_nested_tab_and_revision_binding(provider: FakeDocs) -> None:
    mailbox_port = port(provider)
    assert provider.calls == []
    observed = mailbox_port.read_current()
    assert observed.payload == mailbox()
    bound = json.loads(base64.urlsafe_b64decode(observed.revision[8:]))
    assert bound == {"document_id": DOCUMENT, "tab_id": TAB, "revision_id": "revision-1",
                     "sha256": hashlib.sha256(mailbox()).hexdigest()}
    assert provider.calls == [("files.get", DOCUMENT), ("documents.get", DOCUMENT)]
    assert provider.batches == []


def test_ack_exact_bytes_utf16_cas_and_other_tabs_preserved(provider: FakeDocs) -> None:
    mailbox_port = port(provider)
    observed = mailbox_port.read_current()
    before_other = copy.deepcopy(provider.document["tabs"][0]["documentTab"])
    desired = ack(observed.payload)
    result = mailbox_port.ack_if_current(observed, desired)
    assert result.state == "APPLIED"
    assert result.payload_sha256 == hashlib.sha256(desired).hexdigest()
    assert result.revision == mailbox_port.read_current().revision
    assert mailbox_port.read_current().payload == desired
    assert not desired.endswith(b"\n")  # No compulsory Docs newline enters the ACK hash.
    assert provider.document["tabs"][0]["documentTab"] == before_other
    assert provider.batches[0][2] == "revision-1"
    assert len(provider.batches) == 1
    assert json.loads(provider.payload())["request"] == json.loads(observed.payload)["request"]
    assert json.loads(provider.payload())["slot_token"] == NEXT_TOKEN


@pytest.mark.parametrize("change", ["revision_only", "payload", "other_tab"])
def test_changed_tab_or_document_revision_is_never_retried(provider: FakeDocs, change: str) -> None:
    mailbox_port = port(provider)
    observed = mailbox_port.read_current()
    if change == "payload":
        replacement = json.loads(observed.payload)
        replacement["request"]["shell"]["script"] = "new user intent"
        provider.change(json.dumps(replacement).encode())
    elif change == "other_tab":
        provider.change(other=True)
    else:
        provider.change()
    after_user = copy.deepcopy(provider.document)
    result = mailbox_port.ack_if_current(observed, ack(observed.payload))
    assert result.state == "CONFLICT"
    assert provider.document == after_user
    assert provider.batches == []


@pytest.mark.parametrize("other_tab", [False, True])
def test_required_revision_rejects_race_and_preserves_newer_text(provider: FakeDocs, other_tab: bool) -> None:
    mailbox_port = port(provider)
    observed = mailbox_port.read_current()
    replacement = json.loads(observed.payload)
    replacement["request"]["shell"]["script"] = "newer intent"
    raw = json.dumps(replacement).encode()
    provider.before_batch = lambda: provider.change(raw, other=other_tab)
    result = mailbox_port.ack_if_current(observed, ack(observed.payload))
    assert result.state == "CONFLICT"
    assert len(provider.batches) == 1
    assert provider.payload() == (observed.payload if other_tab else raw)


@pytest.mark.parametrize("unavailable", [False, True])
def test_lost_write_response_requires_exact_readback_without_retry(provider: FakeDocs, unavailable: bool) -> None:
    mailbox_port = port(provider)
    observed = mailbox_port.read_current()
    desired = ack(observed.payload)
    provider.lose_response = True
    provider.unavailable_after_batch = unavailable
    result = mailbox_port.ack_if_current(observed, desired)
    assert result.state == ("UNKNOWN" if unavailable else "APPLIED")
    assert len(provider.batches) == 1
    assert provider.payload() == desired


def test_accepted_write_followed_by_edit_is_unknown_and_new_text_survives(provider: FakeDocs) -> None:
    mailbox_port = port(provider)
    observed = mailbox_port.read_current()
    changed = json.loads(observed.payload)
    changed["request"]["shell"]["script"] = "new post-ACK intent"
    replacement = json.dumps(changed).encode()
    provider.after_batch = lambda: provider.change(replacement)
    result = mailbox_port.ack_if_current(observed, ack(observed.payload))
    assert result.state == "UNKNOWN"
    assert provider.payload() == replacement
    assert len(provider.batches) == 1


@pytest.mark.parametrize("mutation", ["request", "slot", "other_doc", "other_tab"])
def test_ack_cannot_change_user_payload_or_cross_binding(provider: FakeDocs, mutation: str) -> None:
    mailbox_port = port(provider)
    observed = mailbox_port.read_current()
    desired = json.loads(ack(observed.payload))
    if mutation == "request":
        desired["request"]["shell"]["script"] = "unasked command"
    elif mutation == "slot":
        desired["slot_token"] = TOKEN
    else:
        bound = json.loads(base64.urlsafe_b64decode(observed.revision[8:]))
        bound["document_id" if mutation == "other_doc" else "tab_id"] = "unregistered-other"
        observed = Observation(observed.payload, "docs-v2:" + base64.urlsafe_b64encode(json.dumps(bound).encode()).decode())
    result = mailbox_port.ack_if_current(observed, json.dumps(desired).encode())
    assert result.state == "FAILED"
    assert provider.batches == []
    assert provider.payload() == mailbox()


@pytest.mark.parametrize("metadata", [
    {"id": "different-document"}, {"mimeType": "text/plain"}, {"trashed": True},
    {"isAppAuthorized": False}, {"capabilities": {"canEdit": False}},
])
def test_metadata_identity_and_grant_fail_closed(provider: FakeDocs, metadata: dict[str, Any]) -> None:
    provider.metadata.update(metadata)
    with pytest.raises(DocsMailboxError):
        port(provider).read_current()
    assert provider.batches == []


@pytest.mark.parametrize("invalid", ["missing_tab", "duplicate_tab", "inline_object", "bad_span", "markdown", "duplicate_json"])
def test_nonplain_or_nonjson_registered_tab_is_refused(provider: FakeDocs, invalid: str) -> None:
    if invalid == "missing_tab":
        provider.selected()["tabProperties"]["tabId"] = "t.renamed"
    elif invalid == "duplicate_tab":
        provider.document["tabs"].append(tab(TAB, mailbox()))
    elif invalid == "inline_object":
        provider.selected()["documentTab"]["body"]["content"][1]["paragraph"]["elements"][0] = {"inlineObjectElement": {}}
    elif invalid == "bad_span":
        provider.selected()["documentTab"]["body"]["content"][1]["endIndex"] += 1
    elif invalid == "markdown":
        provider.change(b"```json\n" + mailbox() + b"\n```")
    else:
        provider.change(b'{"protocol":"one","protocol":"two"}')
    with pytest.raises(DocsMailboxError):
        port(provider).read_current()
    assert provider.batches == []


def test_local_byte_budget_refuses_read_and_write(provider: FakeDocs) -> None:
    with pytest.raises(DocsMailboxError):
        port(provider, max_bytes=16).read_current()
    observed = port(provider).read_current()
    result = port(provider, max_bytes=16).ack_if_current(observed, ack(observed.payload))
    assert result.state == "FAILED" and provider.batches == []


def owner_config(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    tokens = directory / "tokens"
    tokens.mkdir(mode=0o700)
    # Invalid credential contents prove the config loader never parses credentials.
    for path in (directory / CLIENT_FILENAME, tokens / TOKEN_FILENAME):
        path.write_bytes(b"not credential JSON; never read")
        path.chmod(0o600)
    value = {"oauth_client_file": str(directory / CLIENT_FILENAME),
             "token_file": str(tokens / TOKEN_FILENAME), "scope": DRIVE_SCOPE,
             "root_folder_id": DRIVE_ROOT_ID, "installation_index_document_id": DRIVE_INDEX_ID}
    path = directory / "docs-channel.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value


def test_owner_config_loads_only_references_and_keeps_existing_credential_layout(tmp_path: Path) -> None:
    path, value = owner_config(tmp_path)
    loaded = DocsProviderConfig.from_file(path)
    assert str(loaded.oauth_client_file) == value["oauth_client_file"]
    assert str(loaded.token_file) == value["token_file"]
    paths = Paths(tmp_path / "unused-config", tmp_path / "state", tmp_path / "cache")
    selected = loaded.credential_paths(paths)
    assert selected.config_dir == path.parent
    assert selected.state_dir == paths.state_dir and selected.cache_dir == paths.cache_dir
    assert not paths.state_dir.exists() and not paths.cache_dir.exists()


@pytest.mark.parametrize("invalid", ["mode", "scope", "root", "index", "unknown_field", "linked_token"])
def test_config_rejects_wrong_identity_scope_or_permissions(tmp_path: Path, invalid: str) -> None:
    path, value = owner_config(tmp_path)
    if invalid == "mode":
        path.chmod(0o644)
    elif invalid == "scope":
        value["scope"] = "https://www.googleapis.com/auth/drive.file"
    elif invalid == "root":
        value["root_folder_id"] = "unapproved-root"
    elif invalid == "index":
        value["installation_index_document_id"] = "unapproved-index"
    elif invalid == "unknown_field":
        value["token"] = "must-never-be-in-channel-config"
    else:
        token = Path(value["token_file"])
        alternate = token.with_name("real-owner-token.json")
        token.rename(alternate)
        token.symlink_to(alternate)
    path.write_text(json.dumps(value))
    with pytest.raises(DocsMailboxError):
        DocsProviderConfig.from_file(path)


def test_config_attachment_calls_existing_read_only_gate(tmp_path: Path, provider: FakeDocs, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = owner_config(tmp_path)
    config = DocsProviderConfig.from_file(path)
    calls: list[Any] = []

    def attach(selected: Any, installation: Any, *, legacy_manifest: Any = None) -> Any:
        calls.append((selected, installation, legacy_manifest))
        return "read-only-attachment-proof"

    monkeypatch.setattr(module, "attach_existing", attach)
    assert config.attach(provider) == "read-only-attachment-proof"
    assert len(calls) == 1 and calls[0][0] is provider
    assert calls[0][1].root_id == DRIVE_ROOT_ID and calls[0][1].index_id == DRIVE_INDEX_ID
    assert calls[0][2] is None
    assert provider.calls == [] and provider.batches == []
