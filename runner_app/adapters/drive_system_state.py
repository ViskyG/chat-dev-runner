"""Publish the configured runner's public state to the indexed SYSTEM STATE Doc.

The document contains only selected local policy and Ledger fields. The writer
owns the exact-ID, revision and installation checks; this adapter never creates
or discovers a Drive file. An ambiguous write is accepted only after an exact
text readback, and is never retried in the same publication attempt.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import UUID

from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_writer import DocsCASWriter, RegisteredOutputDoc
from runner_app.domain.commands import PROJECT_ACTIONS, ROOT_ACTIONS, WORKSPACE_ACTIONS

_RUNNER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_ALIAS = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_POLICY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DRIVE_ACTIONS = ROOT_ACTIONS | PROJECT_ACTIONS | WORKSPACE_ACTIONS


class SystemStateError(ValueError):
    """Stable code only; provider responses and local secrets are never exposed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _names(values: Iterable[str], *, code: str) -> list[str]:
    if isinstance(values, str):
        raise SystemStateError(code)
    try:
        result = sorted(set(values))
    except (TypeError, ValueError):
        raise SystemStateError(code) from None
    if len(result) > 64 or any(
        not isinstance(value, str) or _POLICY_NAME.fullmatch(value) is None
        for value in result
    ):
        raise SystemStateError(code)
    return result


def _name_source(
    values: Iterable[str] | Callable[[], Iterable[str]], *, code: str,
) -> Callable[[], list[str]]:
    if not callable(values):
        selected = _names(values, code=code)
        return lambda: selected

    def current() -> list[str]:
        try:
            observed = values()
        except Exception:  # noqa: BLE001 - never surface callback internals
            raise SystemStateError(code) from None
        return _names(observed, code=code)

    return current


class SystemStatePublisher:
    """Callback-friendly publisher for one attached existing installation."""

    def __init__(
        self, writer: DocsCASWriter, attached: AttachedInstallation, *,
        github_owner: str,
        approved_templates: Iterable[str] | Callable[[], Iterable[str]],
        approved_profiles: Iterable[str] | Callable[[], Iterable[str]],
        available_actions: Iterable[str] | Callable[[], Iterable[str]],
        github_identity: str | None = None,
    ) -> None:
        if (not isinstance(github_owner, str)
                or _GITHUB_OWNER.fullmatch(github_owner) is None):
            raise SystemStateError("SYSTEM_STATE_OWNER_INVALID")
        alias = github_identity if github_identity is not None else github_owner.lower()
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise SystemStateError("SYSTEM_STATE_IDENTITY_INVALID")
        self.github_owner = github_owner
        self.github_identity = alias
        self._templates = _name_source(
            approved_templates, code="SYSTEM_STATE_TEMPLATES_INVALID",
        )
        self._profiles = _name_source(
            approved_profiles, code="SYSTEM_STATE_PROFILES_INVALID",
        )
        self._actions = _name_source(
            available_actions, code="SYSTEM_STATE_ACTIONS_INVALID",
        )
        self.writer = writer
        self.attached = attached
        files = attached.index.files
        control, state = files.get("control"), files.get("state")
        if (control is None or state is None or state.name != "SYSTEM STATE"
                or state.parents != (control.file_id,)):
            raise SystemStateError("SYSTEM_STATE_INSTALLATION_INVALID")
        expected = RegisteredOutputDoc(
            "system-state", "STATE", state.file_id, control.file_id, "state",
        )
        try:
            registered = writer.registration("system-state")
        except Exception:  # noqa: BLE001 - provider detail must not leave this boundary
            raise SystemStateError("SYSTEM_STATE_WRITER_MISMATCH") from None
        if registered != expected:
            raise SystemStateError("SYSTEM_STATE_WRITER_MISMATCH")

    def _text(self, state: Mapping[str, Any]) -> str:
        if not isinstance(state, Mapping):
            raise SystemStateError("SYSTEM_STATE_INPUT_INVALID")
        runner_id, epoch, armed = (
            state.get("runner_id"), state.get("epoch"), state.get("armed"),
        )
        if (not isinstance(runner_id, str) or _RUNNER_ID.fullmatch(runner_id) is None
                or not isinstance(epoch, str) or type(armed) is not bool):
            raise SystemStateError("SYSTEM_STATE_INPUT_INVALID")
        try:
            if str(UUID(epoch)) != epoch:
                raise ValueError("Noncanonical epoch")
        except (ValueError, TypeError):
            raise SystemStateError("SYSTEM_STATE_INPUT_INVALID") from None
        value = {
            "schema_version": "1.0",
            "state": "ARMED" if armed else "DISARMED",
            "runner_armed": armed,
            "runner_id": runner_id,
            "epoch": epoch,
            "installation_id": self.attached.index.installation_id,
            "installation_index_document_id": self.attached.config.index_id,
            "root_folder_id": self.attached.config.root_id,
            "available_actions": sorted(_DRIVE_ACTIONS.intersection(self._actions())),
            "approved_templates": self._templates(),
            "approved_profiles": self._profiles(),
            "github_owner": self.github_owner,
            "github_identity": self.github_identity,
            "chat_repository_access": "MANUAL_REQUIRED",
        }
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")) + "\n"

    def publish(self, state: Mapping[str, Any]) -> None:
        """Publish one state snapshot using read/CAS/exact readback.

        The callback is idempotent for an unchanged Ledger epoch and arm state.
        It never serializes incidental Ledger fields such as timestamps or paths.
        """
        desired = self._text(state)
        try:
            observed = self.writer.read("system-state")
        except Exception:  # noqa: BLE001 - provider errors may contain sensitive data
            raise SystemStateError("SYSTEM_STATE_READ_FAILED") from None
        if observed.snapshot.text == desired:
            return
        try:
            result = self.writer.replace("system-state", desired, expected=observed)
        except Exception:  # noqa: BLE001 - only a stable code crosses the callback
            raise SystemStateError("SYSTEM_STATE_WRITE_FAILED") from None
        try:
            readback = self.writer.read("system-state")
        except Exception:  # noqa: BLE001 - do not retry an ambiguous write
            raise SystemStateError("SYSTEM_STATE_READBACK_FAILED") from None
        if (readback.snapshot.file_id != observed.snapshot.file_id
                or readback.snapshot.text != desired):
            raise SystemStateError("SYSTEM_STATE_READBACK_MISMATCH")
        if result.state not in {"CONFIRMED", "UNCHANGED", "UNCERTAIN"}:
            raise SystemStateError("SYSTEM_STATE_WRITE_UNCONFIRMED")
