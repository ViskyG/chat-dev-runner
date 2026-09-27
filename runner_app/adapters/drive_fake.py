"""Deterministic in-memory Drive/Docs provider for offline contract tests only."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from typing import Any, cast

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError


class FakeDrive:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.denied_files: set[str] = set()
        self.on_document_read: dict[str, Callable[[FakeDrive, int], None]] = {}
        self._document_reads: dict[str, int] = {}
        self._next_revision = 1
        self._lock = threading.RLock()

    def put_file(self, metadata: Mapping[str, Any]) -> None:
        file_id = metadata["id"]
        if not isinstance(file_id, str):
            raise TypeError("Fake file needs an ID")
        with self._lock:
            self.files[file_id] = copy.deepcopy(dict(metadata))

    def put_document(self, file_id: str, text: str, *, revision: str | None = None,
                     tab_id: str = "tab-one") -> None:
        with self._lock:
            version = revision or f"rev-{self._next_revision}"
            self._next_revision += 1
            self.documents[file_id] = {
                "documentId": file_id, "revisionId": version,
                "tabs": [{
                    "tabProperties": {"tabId": tab_id}, "childTabs": [],
                    "documentTab": {"body": {"content": [{
                        "paragraph": {"elements": [{"textRun": {"content": text}}]}
                    }]}}
                }],
            }

    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]:
        with self._lock:
            self.calls.append(("files.get", file_id))
            if file_id in self.denied_files:
                raise PermissionError("fake per-file grant missing")
            if file_id not in self.files:
                raise FileNotFoundError(file_id)
            return copy.deepcopy(self.files[file_id])

    def get_document(self, file_id: str, *,
                     include_tabs_content: bool = True) -> Mapping[str, Any]:
        with self._lock:
            self.calls.append(("documents.get", file_id))
            if not include_tabs_content:
                raise AssertionError("Adapter must request complete tab content")
            if file_id in self.denied_files:
                raise PermissionError("fake per-file grant missing")
            if file_id not in self.documents:
                raise FileNotFoundError(file_id)
            count = self._document_reads.get(file_id, 0) + 1
            self._document_reads[file_id] = count
            hook = self.on_document_read.get(file_id)
            if hook is not None:
                hook(self, count)
            return copy.deepcopy(self.documents[file_id])

    def replace_document_text(
        self, file_id: str, text: str, *, required_revision_id: str,
        lose_ack: bool = False,
    ) -> str:
        """Fake atomic single-Doc CAS; no live API call is implemented here."""
        with self._lock:
            self.calls.append(("documents.batchUpdate", file_id))
            if file_id in self.denied_files:
                raise PermissionError("fake per-file grant missing")
            current = self.documents[file_id]
            if current.get("revisionId") != required_revision_id:
                raise DriveError("STALE_REVISION", "Required Docs revision no longer matches")
            self.put_document(file_id, text, tab_id=current["tabs"][0]["tabProperties"]["tabId"])
            new_revision = cast(str, self.documents[file_id]["revisionId"])
            if lose_ack:
                raise OSError("simulated lost write acknowledgement")
            return new_revision

    def list_children(
        self, parent_id: str, *, page_token: str | None = None, page_size: int = 100,
    ) -> dict[str, Any]:
        """Paginated fake diagnostic listing; dispatch never uses names/listing."""
        if page_size < 1 or page_size > 1000:
            raise ValueError("Invalid fake page size")
        with self._lock:
            self.calls.append(("files.list", parent_id))
            values = sorted(
                (copy.deepcopy(file) for file in self.files.values()
                 if parent_id in file.get("parents", []) and not file.get("trashed")),
                key=lambda item: item["id"],
            )
            offset = int(page_token or "0")
            if offset < 0 or offset > len(values):
                raise ValueError("Invalid fake cursor")
            page = values[offset:offset + page_size]
            next_offset = offset + len(page)
            return {
                "files": page,
                "nextPageToken": str(next_offset) if next_offset < len(values) else None,
            }
