"""Strict immutable command capture and transport-independent local authority.

Only trusted transport code constructs provenance. Remote JSON never supplies a
mailbox registration, a local session identity, or an authority grant.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from bridge_reference.protocol import Rejected, safe_relative_path, validate_branch
from runner_app.resources import load_schema

MAX_COMMAND_BYTES = 262_144
_COMMAND_VALIDATOR = Draft202012Validator(load_schema("command"), format_checker=FormatChecker())
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})\Z"
)
ROOT_ACTIONS = frozenset({"CREATE_PROJECT"})
PROJECT_ACTIONS = frozenset({"CREATE_WORKSPACE", "FETCH"})
WORKSPACE_ACTIONS = frozenset({
    "RETARGET_WORKSPACE", "DELIVER_VERIFY_PUBLISH", "VERIFY", "PUBLISH", "SYNC_WORKSPACE",
    "EXPORT_CONTEXT", "ARCHIVE_WORKSPACE", "PUBLISH_REPORT", "CANCEL_OPERATION",
})


class DomainError(ValueError):
    """Stable terminal refusal with a safe explanation, never remote payload text."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class FrozenCommand:
    """Canonical bytes are authoritative; readers receive detached mutable views."""

    canonical_json: str
    digest: str

    @property
    def command(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.canonical_json))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DomainError("DUPLICATE_JSON_KEY", "JSON содержит повторяющееся поле.")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise DomainError("INVALID_JSON", "JSON содержит недопустимое число.")


def _json_values(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise DomainError("INVALID_JSON", "Превышена допустимая вложенность JSON.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainError("INVALID_JSON", "Ключ JSON должен быть строкой.")
            _json_values(key, depth + 1)
            _json_values(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_values(item, depth + 1)
    elif isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise DomainError("INVALID_UTF8", "Текст должен содержать корректный Unicode.") from None
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise DomainError("INVALID_JSON", "JSON содержит недопустимое число.")
    elif value is not None and not isinstance(value, (bool, int)):
        raise DomainError("INVALID_JSON", "Значение не соответствует JSON.")


def parse_json(raw: bytes, max_bytes: int = MAX_COMMAND_BYTES) -> dict[str, Any]:
    """Parse syntax separately so a safely parsed identity can retain a shape refusal."""
    if len(raw) > max_bytes:
        raise DomainError("PAYLOAD_TOO_LARGE", "Команда превышает допустимый размер.")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=_pairs, parse_constant=_constant)
    except UnicodeDecodeError:
        raise DomainError("INVALID_UTF8", "Команда должна быть в кодировке UTF-8.") from None
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, DomainError):
            raise
        raise DomainError("INVALID_JSON", "Команда содержит некорректный JSON.") from None
    if not isinstance(value, dict):
        raise DomainError("INVALID_JSON", "Команда должна быть объектом JSON.")
    _json_values(value)
    return cast(dict[str, Any], value)


def freeze(command: dict[str, Any]) -> FrozenCommand:
    """Capture even a shape-invalid object without changing Unicode or line endings."""
    if not isinstance(command, dict):
        raise DomainError("INVALID_JSON", "Команда должна быть объектом JSON.")
    _json_values(command)
    canonical = json.dumps(command, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return FrozenCommand(canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _timestamp(value: str) -> datetime:
    try:
        if not _TIMESTAMP.fullmatch(value):
            raise ValueError
        result = datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:] in {"z", "Z"}
                                        else value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise DomainError("INVALID_TIME", "Время должно содержать корректный часовой пояс.") from None


def validate_shape(command: dict[str, Any]) -> None:
    """Validate schema and syntax only; policy, epoch and handlers are separate gates."""
    _json_values(command)
    if not _COMMAND_VALIDATOR.is_valid(command):
        raise DomainError("INVALID_SCHEMA", "Команда не соответствует схеме версии 1.0.")
    params = command["params"]
    for value in (command["request_id"], command["epoch"],
                  *(params[key] for key in ("source_run_id", "target_request_id") if key in params)):
        if not _canonical_uuid(value):
            raise DomainError("INVALID_SCHEMA", "Идентификатор UUID должен иметь канонический вид.")
    _timestamp(command["issued_at"])
    _timestamp(command["expires_at"])
    try:
        for key in ("branch", "expected_branch", "target_branch", "remote_ref"):
            if key in params:
                validate_branch(params[key])
        if command["action"] == "EXPORT_CONTEXT":
            for path in params["paths"]:
                safe_relative_path(path)
    except Rejected as exc:
        raise DomainError(exc.code, "Путь или ссылка Git не разрешены локальным контрактом.") from None


def parse_command(raw: bytes, max_bytes: int = MAX_COMMAND_BYTES) -> FrozenCommand:
    command = parse_json(raw, max_bytes=max_bytes)
    validate_shape(command)
    return freeze(command)


def validate_time(command: dict[str, Any], *, runner_id: str, epoch: str,
                  now: datetime | None = None, max_ttl: int = 900) -> None:
    """Call under the engine's epoch/admission transaction after shape validation."""
    if command["dispatch"] != "READY":
        raise DomainError("NOT_READY", "Черновик команды не исполняется.")
    if command["runner_id"] != runner_id or command["epoch"] != epoch:
        raise DomainError("WRONG_RUNNER_EPOCH", "Команда адресована другой сессии исполнителя.")
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise DomainError("INVALID_TIME", "Текущее время должно содержать часовой пояс.")
    issued = _timestamp(command["issued_at"])
    expires = _timestamp(command["expires_at"])
    if not 0 < (expires - issued).total_seconds() <= max_ttl:
        raise DomainError("INVALID_TTL", "Срок действия команды превышает локальное разрешение.")
    if (issued - current).total_seconds() > 30:
        raise DomainError("FUTURE_COMMAND", "Время выпуска команды слишком далеко в будущем.")
    if expires <= current:
        raise DomainError("EXPIRED", "Срок действия истёк; требуется новая команда.")


@dataclass(frozen=True)
class Provenance:
    kind: Literal["local", "drive"]
    source_id: str
    project_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"local", "drive"} or not self.source_id:
            raise DomainError("INVALID_SOURCE", "Источник команды не зарегистрирован.")
        if self.workspace_id is not None and self.project_id is None:
            raise DomainError("INVALID_SOURCE", "Рабочая область должна принадлежать проекту.")


@dataclass(frozen=True)
class LocalPolicy:
    """Owner-approved grants, all denied by default and detached from caller containers."""

    mailboxes: Mapping[str, Provenance] = field(default_factory=dict)
    approved_profiles: frozenset[str] = frozenset()
    approved_templates: frozenset[str] = frozenset()
    import_handles: frozenset[str] = frozenset()
    github_identities: frozenset[str] = frozenset()
    project_ids: frozenset[str] = frozenset()
    workspace_pairs: frozenset[tuple[str, str]] = frozenset()
    approved_github_repositories: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        registered = dict(self.mailboxes)
        for source_id, provenance in registered.items():
            if provenance.kind != "drive" or provenance.source_id != source_id:
                raise DomainError("INVALID_SOURCE", "Регистрация почтового документа некорректна.")
        object.__setattr__(self, "mailboxes", MappingProxyType(registered))
        for name in ("approved_profiles", "approved_templates", "import_handles",
                     "github_identities", "project_ids", "workspace_pairs",
                     "approved_github_repositories"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))

    def authorize(self, command: dict[str, Any], provenance: Provenance) -> None:
        validate_shape(command)
        action = command["action"]
        project = command["project_id"]
        workspace = command["workspace_id"]
        if action in {"RECONCILE", "RESUME_PROVISION"}:
            # Recovery authority is an authenticated local owner session only.
            # A partial CREATE_PROJECT may have no registered project/workspace yet.
            if provenance.kind != "local":
                raise DomainError("SOURCE_SCOPE", "Восстановление доступно только локальному владельцу.")
            if ((provenance.project_id is not None and provenance.project_id != project)
                    or (provenance.workspace_id is not None
                        and provenance.workspace_id != workspace)):
                raise DomainError("SOURCE_SCOPE", "Команда не соответствует выбранной локальной области.")
            return
        if provenance.kind == "drive":
            registered = self.mailboxes.get(provenance.source_id)
            if registered is None or registered != provenance:
                raise DomainError("SOURCE_MISMATCH", "Источник не соответствует регистрации документа.")
            if registered.project_id is None:
                allowed = action in ROOT_ACTIONS
            elif registered.workspace_id is None:
                allowed = action in PROJECT_ACTIONS and project == registered.project_id
            else:
                allowed = (action in WORKSPACE_ACTIONS and project == registered.project_id
                           and workspace == registered.workspace_id)
            if not allowed:
                raise DomainError("SOURCE_SCOPE", "Команда выходит за область зарегистрированного источника.")
        elif ((provenance.project_id is not None and provenance.project_id != project)
              or (provenance.workspace_id is not None and provenance.workspace_id != workspace)):
            raise DomainError("SOURCE_SCOPE", "Команда не соответствует выбранной локальной области.")
        if action not in ROOT_ACTIONS and project not in self.project_ids:
            raise DomainError("PROJECT_NOT_REGISTERED", "Проект не зарегистрирован локально.")
        if action in WORKSPACE_ACTIONS and (project, workspace) not in self.workspace_pairs:
            raise DomainError("WORKSPACE_NOT_REGISTERED", "Рабочая область не зарегистрирована локально.")
        params = command["params"]
        if "profile_id" in params and params["profile_id"] not in self.approved_profiles:
            raise DomainError("PROFILE_NOT_APPROVED", "Профиль исполнения не разрешён владельцем.")
        if action == "CREATE_PROJECT":
            if params["template_id"] not in self.approved_templates:
                raise DomainError("TEMPLATE_NOT_APPROVED", "Шаблон проекта не разрешён владельцем.")
            if params["github_identity"] not in self.github_identities:
                raise DomainError("IDENTITY_NOT_APPROVED", "Учётная запись GitHub не разрешена владельцем.")
            source = params["source"]
            if source["kind"] == "local" and source["import_handle"] not in self.import_handles:
                raise DomainError("IMPORT_NOT_APPROVED", "Локальный источник импорта не разрешён владельцем.")
            if (source["kind"] == "github"
                    and source["repository"] not in self.approved_github_repositories):
                raise DomainError("REPOSITORY_NOT_APPROVED", "Репозиторий импорта не разрешён владельцем.")


def authorize(command: dict[str, Any], provenance: Provenance, policy: LocalPolicy) -> None:
    policy.authorize(command, provenance)
