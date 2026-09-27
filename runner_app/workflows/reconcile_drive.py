"""Read-only Drive proofs for an already attached installation.

The reader only classifies recorded uncertain creates/uploads and CAS document
writes. Every positive proof re-reads an exact scoped file ID or a unique
installation key; a missing lookup is never permission to recreate it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_writer import ObservedOutputDoc, RegisteredOutputDoc
from runner_app.workflows.reconcile import EffectProof


class UploadReader(Protocol):
    def get_file(self, file_id: str, **expected: Any) -> Mapping[str, Any]: ...

    def find_by_key(self, parent_id: str, installation_id: str, key: str,
                    **expected: Any) -> Mapping[str, Any] | None: ...


class DocumentReader(Protocol):
    def registration(self, key: str) -> RegisteredOutputDoc: ...

    def read(self, key: str) -> ObservedOutputDoc: ...


class DriveEffectReader:
    """Trusted read ports; construction is inert and does not create a workspace."""

    def __init__(self, attached: AttachedInstallation, upload: UploadReader,
                 docs: DocumentReader) -> None:
        if not isinstance(attached, AttachedInstallation):
            raise TypeError("A verified existing Drive installation is required")
        self.attached = attached
        self.upload = upload
        self.docs = docs

    def __call__(self, target: Mapping[str, Any],
                 effect: Mapping[str, Any]) -> EffectProof | None:
        if (target.get("status") not in {"BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"}
                or effect.get("kind") != "drive_file"
                or effect.get("state") not in {"INTENDED", "UNKNOWN"}
                or not isinstance(effect.get("effect_id"), str)
                or not isinstance(effect.get("intent"), dict)):
            return None
        intent = effect["intent"]
        if "parent_id" in intent:
            return self._file(target, effect, intent)
        if "file_id" in intent:
            return self._document(target, effect, intent)
        if "receipt_doc_id" in intent:
            return self._root_receipt(target, effect, intent)
        return None

    def _file(self, target: Mapping[str, Any], effect: Mapping[str, Any],
              intent: Mapping[str, Any]) -> EffectProof | None:
        installation = self.attached.index.installation_id
        parent = intent.get("parent_id")
        key = intent.get("key")
        name = intent.get("name")
        mime = intent.get("mime_type")
        if (intent.get("installation_id") != installation
                or not isinstance(parent, str) or not parent
                or not isinstance(key, str) or not key
                or not isinstance(name, str) or not name
                or not isinstance(mime, str) or not mime):
            return None
        project = intent.get("project_id")
        workspace = intent.get("workspace_id")
        if ((project is not None and project != target.get("project_id"))
                or (workspace is not None and not isinstance(workspace, str))
                or (workspace is not None and target.get("workspace_id") is not None
                    and workspace != target.get("workspace_id"))
                or (workspace is not None and target.get("action") == "CREATE_PROJECT"
                    and workspace != "primary")):
            return None
        sha, size, md5 = (
            intent.get("sha256"), intent.get("size_bytes"), intent.get("md5"),
        )
        if ((sha is not None and not isinstance(sha, str))
                or (size is not None and (type(size) is not int or size < 0))
                or (md5 is not None and not isinstance(md5, str))):
            return None
        expected = {
            "expected_parent_id": parent, "expected_installation_id": installation,
            "expected_key": key, "expected_name": name, "expected_mime_type": mime,
            **({"expected_project_id": project} if project is not None else {}),
            **({"expected_workspace_id": workspace} if workspace is not None else {}),
            **({"expected_sha256": sha} if sha is not None else {}),
            **({"expected_size_bytes": size} if size is not None else {}),
            **({"expected_md5": md5} if md5 is not None else {}),
        }
        provider_id = effect.get("provider_id")
        if provider_id is not None:
            if not isinstance(provider_id, str):
                return None
            found = self.upload.get_file(provider_id, **expected)
        else:
            lookup = {
                key: value for key, value in expected.items()
                if key not in {"expected_parent_id", "expected_installation_id",
                               "expected_key"}
            }
            candidate = self.upload.find_by_key(parent, installation, key, **lookup)
            if candidate is None:
                return None
            provider_id = candidate.get("id")
            if not isinstance(provider_id, str):
                return None
            found = self.upload.get_file(provider_id, **expected)
        if found.get("id") != provider_id:
            return None
        properties = found.get("appProperties")
        if not isinstance(properties, Mapping):
            return None
        observed: dict[str, Any] = {
            "parent_id": found["parents"][0],
            "installation_id": properties.get("cdr_installation"),
            "key": properties.get("cdr_key"),
            "name": found.get("name"), "mime_type": found.get("mimeType"),
        }
        if "project_id" in intent:
            observed["project_id"] = properties.get("cdr_project")
        if "workspace_id" in intent:
            observed["workspace_id"] = (
                properties.get("cdr_workspace") if workspace is not None else None
            )
        if "sha256" in intent:
            observed["sha256"] = properties.get("cdr_sha256")
        if "size_bytes" in intent:
            observed["size_bytes"] = int(found["size"])
        if "md5" in intent:
            observed["md5"] = found.get("md5Checksum")
        if observed != intent:
            return None
        return EffectProof(effect["effect_id"], provider_id, observed)

    def _document(self, target: Mapping[str, Any], effect: Mapping[str, Any],
                  intent: Mapping[str, Any]) -> EffectProof | None:
        file_id = intent.get("file_id")
        key = intent.get("key")
        desired = intent.get("desired_sha256")
        if (not isinstance(file_id, str) or not file_id
                or not isinstance(key, str) or not key
                or not isinstance(desired, str) or not desired):
            return None
        if (key.startswith("project-state:")
                and key != f"project-state:{target.get('project_id')}"):
            return None
        if (key.startswith("source-context:")
                and target.get("action") == "CREATE_PROJECT"
                and key != f"source-context:{target.get('project_id')}/primary"):
            return None
        registered = self.docs.registration(key)
        if registered.file_id != file_id:
            return None
        observed = self.docs.read(key)
        if (observed.snapshot.file_id != file_id
                or observed.snapshot.sha256 != desired
                or (effect.get("provider_id") is not None
                    and effect["provider_id"] != file_id)):
            return None
        return EffectProof(effect["effect_id"], file_id, dict(intent))

    def _root_receipt(self, target: Mapping[str, Any], effect: Mapping[str, Any],
                      intent: Mapping[str, Any]) -> EffectProof | None:
        files = self.attached.index.files
        command = files.get("command")
        receipt = files.get("receipt")
        if (command is None or receipt is None
                or target.get("action") != "CREATE_PROJECT"
                or target.get("source_id") != command.file_id
                or intent.get("command_doc_id") != command.file_id
                or intent.get("receipt_doc_id") != receipt.file_id
                or intent.get("request_digest") != target.get("digest")
                or not isinstance(intent.get("desired_sha256"), str)):
            return None
        registered = self.docs.registration("root-receipt")
        if registered.file_id != receipt.file_id:
            return None
        observed = self.docs.read("root-receipt")
        if (observed.snapshot.file_id != receipt.file_id
                or observed.snapshot.sha256 != intent["desired_sha256"]
                or (effect.get("provider_id") is not None
                    and effect["provider_id"] != receipt.file_id)):
            return None
        return EffectProof(effect["effect_id"], receipt.file_id, dict(intent))
