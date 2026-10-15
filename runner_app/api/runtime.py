"""Authenticated local read models and immediate finite operation intake."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, Query, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from runner_app import __version__
from runner_app.adapters.drive_registry import (
    DriveRegistryError,
    WorkspaceDriveRegistration,
    WorkspaceDriveRegistry,
)
from runner_app.api.app import create_app
from runner_app.api.auth import AuthenticationError, Sessions
from runner_app.api.editor import EditorLaunchError, launch_editor
from runner_app.api.source_view import SourceViewError, inspect_registered_workspace
from runner_app.config import Settings, setup_requirements
from runner_app.domain.commands import DomainError, Provenance, parse_json
from runner_app.engine.connection_checks import (
    CheckBusy,
    ConnectionChecks,
    unknown_global_checks,
)
from runner_app.engine.service import Engine
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.outputs.inspection import (
    MAX_INSPECTION_BYTES,
    inspect_registered_output,
    inspection_limit,
)
from runner_app.storage.ledger import LedgerError
from runner_app.storage.run_evidence import EvidenceError, RunEvidenceStore

_WEB_ASSETS = {
    "app.js": "text/javascript",
    "api.js": "text/javascript",
    "model.js": "text/javascript",
    "render.js": "text/javascript",
    "v2.js": "text/javascript",
    "styles.css": "text/css",
}
_WEB_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _workspace_drive_links(value: WorkspaceDriveRegistration) -> dict[str, Any]:
    """Construct links only from exact IDs in a freshly revalidated registration."""
    def document(file_id: str) -> dict[str, str]:
        return {"id": file_id, "url": f"https://docs.google.com/document/d/{file_id}/edit"}

    documents = {
        "command": document(value.command_doc_id),
        "changeset": document(value.changeset_doc_id),
        "receipt": document(value.receipt_doc_id),
        "latest_result": document(value.latest_result_doc_id),
    }
    for role, file_id in (
        ("project_state", value.project_state_doc_id),
        ("source_context", value.context_doc_id),
        ("project_command", value.project_command_doc_id),
        ("project_receipt", value.project_receipt_doc_id),
    ):
        if file_id is not None:
            documents[role] = document(file_id)
    return {"project_id": value.project_id, "workspace_id": value.workspace_id,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "workspace_folder": {
                "id": value.workspace_folder_id,
                "url": f"https://drive.google.com/drive/folders/{value.workspace_folder_id}",
            },
            "documents": documents}


def _web_resource(name: str, media_type: str) -> Response:
    """Serve only fixed, packaged assets, never project outputs as same-origin HTML."""
    try:
        content = files("runner_app.web").joinpath(name).read_bytes()
    except FileNotFoundError:
        return JSONResponse({"error": "UI_ASSET_UNAVAILABLE"}, status_code=503)
    return Response(content, media_type=media_type)


async def _body(request: Request, maximum: int = 262144) -> bytes:
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > maximum:
            raise DomainError("PAYLOAD_TOO_LARGE", "Размер запроса превышает допустимый.")
        chunks.extend(chunk)
    return bytes(chunks)


def _require_uuid(value: str) -> None:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("Noncanonical UUID")
    except (ValueError, AttributeError):
        raise DomainError("INVALID_ID", "Требуется корректный идентификатор UUID.") from None


def _require_slug(value: str) -> None:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value) is None:
        raise DomainError("INVALID_ID", "Требуется корректный идентификатор.")


def _evidence_http_status(code: str) -> int:
    if code in {"RUN_NOT_FOUND", "OUTPUT_NOT_FOUND"}:
        return 404
    if code in {"INVALID_OUTPUT_ID", "RETENTION_POLICY_INVALID"}:
        return 422
    if code in {"EVIDENCE_EXPIRED", "RETENTION_NOT_DUE", "EVIDENCE_REFERENCED",
                "EVIDENCE_PINNED", "EVIDENCE_BUSY", "EVIDENCE_LOCK_UNAVAILABLE"}:
        return 409
    return 503


def _status(code: str | None) -> int:
    if code in {"BUSY", "REQUEST_ID_REUSED", "CANCEL_SCOPE_MISMATCH", "TARGET_NOT_RUNNING",
                "INVALID_TRANSITION", "NEEDS_RECONCILIATION", "IMPORT_SOURCE_CHANGED"}:
        return 409
    if code in {"UNARMED", "ACTION_UNAVAILABLE", "WORKER_UNAVAILABLE", "DISPATCH_FAILED",
                "IMPORT_NOT_CONFIGURED", "STORAGE_UNAVAILABLE"}:
        return 503
    if code == "PAYLOAD_TOO_LARGE":
        return 413
    if code in {"INVALID_SCHEMA", "INVALID_JSON", "INVALID_UTF8", "INVALID_TTL", "INVALID_TIME",
                "INVALID_BRANCH", "UNSAFE_PATH", "DUPLICATE_KEY", "DUPLICATE_JSON_KEY", "INVALID_ID",
                "INVALID_IMPORT_PATH", "IMPORT_SOURCE_UNSAFE"}:
        return 422
    return 403


def create_runtime_app(settings: Settings, engine: Engine, sessions: Sessions, *,
                       import_grants: ImportGrants | None = None,
                       source_registry: SourceRegistry | None = None,
                       drive_registry: WorkspaceDriveRegistry | None = None,
                       connection_checks: ConnectionChecks | None = None,
                       run_evidence: RunEvidenceStore | None = None,
                       integration_status: Mapping[str, Any] | None = None,
                       on_session_exchange: Callable[[], None] | None = None) -> FastAPI:
    def runtime_health() -> dict[str, Any]:
        return {"service": "chat-dev-runner", "version": __version__,
                "status": "PARTIALLY_CONFIGURED" if integration_status else "NOT_CONFIGURED",
                "armed": engine.ledger.state()["armed"],
                "capabilities": ["local_api", "durable_admission", *engine.capabilities()]}

    app = create_app(settings, health_provider=runtime_health)
    hosts = {f"127.0.0.1:{settings.listen_port}", f"localhost:{settings.listen_port}"}
    if settings.listen_port == 80:
        hosts.update({"127.0.0.1", "localhost"})
    origins = {f"http://{host}" for host in hosts}
    prefix = "/api/v1/"
    session_path = prefix + "local/session"

    def is_public_asset(path: str) -> bool:
        return path == "/" or (path.startswith("/assets/")
                               and path.removeprefix("/assets/") in _WEB_ASSETS)

    @app.middleware("http")
    async def session_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # This outer middleware repeats boundary checks so rejection occurs before authentication.
        if len(request.headers.getlist("host")) != 1 or request.headers.get("host") not in hosts:
            return JSONResponse({"error": "INVALID_HOST"}, status_code=403)
        origin = request.headers.getlist("origin")
        if origin and (len(origin) != 1 or origin[0] not in origins):
            return JSONResponse({"error": "INVALID_ORIGIN"}, status_code=403)
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        if mutation:
            if len(origin) != 1:
                return JSONResponse({"error": "ORIGIN_REQUIRED"}, status_code=403)
            content_types = request.headers.getlist("content-type")
            if (len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                return JSONResponse({"error": "JSON_REQUIRED"}, status_code=415)
            lengths = request.headers.getlist("content-length")
            if lengths:
                try:
                    if len(lengths) != 1 or not 0 <= int(lengths[0]) <= 262144:
                        raise ValueError("Invalid length")
                except ValueError:
                    return JSONResponse({"error": "PAYLOAD_TOO_LARGE"}, status_code=413)
        if request.url.path != prefix + "health" and not (
            request.method in {"GET", "HEAD"} and is_public_asset(request.url.path)
        ) and not (request.url.path == session_path and request.method == "POST"):
            try:
                csrf_values = request.headers.getlist("x-csrf-token")
                if len(csrf_values) > 1:
                    raise AuthenticationError("INVALID_CSRF")
                sessions.authenticate(request.cookies.get(sessions.cookie_name),
                                      request.headers.get("x-csrf-token"), mutation=mutation)
            except AuthenticationError as error:
                return JSONResponse({"error": str(error)}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if is_public_asset(request.url.path):
            response.headers["Content-Security-Policy"] = _WEB_CSP
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse({"error": error.code, "message": str(error)}, status_code=_status(error.code))

    @app.exception_handler(LedgerError)
    async def ledger_error(request: Request, error: LedgerError) -> JSONResponse:
        return JSONResponse({"error": error.code}, status_code=_status(error.code))

    @app.get("/")
    def web_index() -> Response:
        return _web_resource("index.html", "text/html")

    @app.get("/assets/{name}")
    def web_asset(name: str) -> Response:
        media_type = _WEB_ASSETS.get(name)
        if media_type is None:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        return _web_resource(name, media_type)

    @app.post(session_path)
    async def session(request: Request) -> Response:
        value = parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096)
        if (set(value) != {"bootstrap"} or not isinstance(value["bootstrap"], str)
                or len(value["bootstrap"]) > 128):
            return JSONResponse({"error": "INVALID_BOOTSTRAP"}, status_code=403)
        try:
            token, authenticated = sessions.exchange(value["bootstrap"])
        except AuthenticationError as error:
            return JSONResponse({"error": str(error)}, status_code=403)
        if on_session_exchange is not None:
            on_session_exchange()
        response = JSONResponse({"csrf_token": authenticated.csrf, "expires_in": sessions.ttl})
        response.set_cookie(sessions.cookie_name, token, max_age=sessions.ttl,
                            httponly=True, samesite="strict", secure=False, path="/")
        return response

    @app.get(session_path)
    def existing_session(request: Request) -> dict[str, Any]:
        """Rehydrate a tab after reload without exposing the HttpOnly session token."""
        authenticated = sessions.authenticate(request.cookies.get(sessions.cookie_name))
        return {"csrf_token": authenticated.csrf,
                "expires_in": max(0, int(authenticated.expires_at - time.monotonic()))}

    @app.get(prefix + "system")
    def system() -> dict[str, Any]:
        integrations = setup_requirements()
        if integration_status is not None:
            integrations["state"] = "PARTIALLY_CONFIGURED"
            integrations["ep05"] = dict(integration_status)
        return {"schema_version": "1.0", **engine.ledger.state(),
                "capabilities": engine.capabilities(), "integrations": integrations,
                "approved": {
                    "templates": sorted(engine.policy.approved_templates),
                    "profiles": sorted(engine.policy.approved_profiles),
                    "github_identities": sorted(engine.policy.github_identities),
                    "github_repositories": sorted(engine.policy.approved_github_repositories),
                },
                "connections": (connection_checks.global_snapshot()
                                if connection_checks is not None else unknown_global_checks()),
                "execution_queue": False}

    @app.post(prefix + "local/check-drive")
    async def check_drive(request: Request) -> Response:
        if parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096) != {}:
            raise DomainError("INVALID_SCHEMA", "Параметры проверки Drive не допускаются.")
        if connection_checks is None:
            return JSONResponse({"error": "CONNECTION_CHECK_UNAVAILABLE"}, status_code=503)
        try:
            result = await asyncio.to_thread(connection_checks.check_drive)
        except CheckBusy:
            return JSONResponse({"error": "CHECK_BUSY"}, status_code=409)
        return JSONResponse(result)

    @app.post(prefix + "local/check-github")
    async def check_github(request: Request) -> Response:
        if parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096) != {}:
            raise DomainError("INVALID_SCHEMA", "Параметры проверки GitHub не допускаются.")
        if connection_checks is None:
            return JSONResponse({"error": "CONNECTION_CHECK_UNAVAILABLE"}, status_code=503)
        try:
            result = await asyncio.to_thread(connection_checks.check_github_api)
        except CheckBusy:
            return JSONResponse({"error": "CHECK_BUSY"}, status_code=409)
        return JSONResponse(result)

    @app.post(prefix + "local/check-git-remote")
    async def check_git_remote(request: Request) -> Response:
        value = parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096)
        if (set(value) != {"project_id", "workspace_id"}
                or not isinstance(value["project_id"], str)
                or not isinstance(value["workspace_id"], str)):
            raise DomainError("INVALID_SCHEMA", "Нужны ID проекта и рабочей области.")
        project_id, workspace_id = value["project_id"], value["workspace_id"]
        _require_slug(project_id)
        _require_slug(workspace_id)
        if connection_checks is None or source_registry is None:
            return JSONResponse({"error": "CONNECTION_CHECK_UNAVAILABLE"}, status_code=503)
        if source_registry.get_workspace(project_id, workspace_id) is None:
            return JSONResponse({"error": "WORKSPACE_NOT_FOUND"}, status_code=404)
        try:
            result = await asyncio.to_thread(
                connection_checks.check_git_remote, project_id, workspace_id,
            )
        except CheckBusy:
            return JSONResponse({"error": "CHECK_BUSY"}, status_code=409)
        return JSONResponse(result)

    @app.post(prefix + "local/open-editor")
    async def open_editor(request: Request) -> Response:
        value = parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096)
        if (set(value) != {"project_id", "workspace_id"}
                or not isinstance(value["project_id"], str)
                or not isinstance(value["workspace_id"], str)):
            raise DomainError("INVALID_SCHEMA", "Нужны ID проекта и рабочей области.")
        project_id, workspace_id = value["project_id"], value["workspace_id"]
        _require_slug(project_id)
        _require_slug(workspace_id)
        if source_registry is None:
            return JSONResponse({"error": "EDITOR_UNAVAILABLE"}, status_code=503)
        try:
            result = await asyncio.to_thread(
                launch_editor, source_registry, project_id, workspace_id,
            )
        except EditorLaunchError as error:
            status = (404 if error.code == "WORKSPACE_NOT_FOUND" else
                      409 if error.code in {"WORKSPACE_UNAVAILABLE", "WORKSPACE_PATH_UNSAFE"}
                      else 503)
            return JSONResponse({"error": error.code}, status_code=status)
        return JSONResponse(result, status_code=202)

    @app.post(prefix + "local/import-handles")
    async def import_handle(request: Request) -> dict[str, str]:
        value = parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096)
        if set(value) != {"path"} or not isinstance(value["path"], str):
            raise DomainError("INVALID_SCHEMA", "Требуется абсолютный локальный путь.")
        grants = import_grants if import_grants is not None else engine.import_grants
        if grants is None:
            raise DomainError("IMPORT_NOT_CONFIGURED", "Локальный импорт не настроен владельцем.")
        handle = await asyncio.to_thread(grants.grant, value["path"])
        return {"import_handle": handle}

    @app.post(prefix + "local/arm")
    async def arm(request: Request) -> dict[str, Any]:
        if parse_json(await _body(request)) != {}:
            raise DomainError("INVALID_SCHEMA", "Параметры запуска не допускаются.")
        if not engine.capabilities():
            raise DomainError("ACTION_UNAVAILABLE", "Исполнение пока не настроено.")
        return engine.ledger.arm()

    @app.post(prefix + "local/disarm")
    async def disarm(request: Request) -> dict[str, Any]:
        if parse_json(await _body(request)) != {}:
            raise DomainError("INVALID_SCHEMA", "Параметры остановки не допускаются.")
        return engine.ledger.disarm()

    @app.post(prefix + "operations")
    async def operations(request: Request) -> Response:
        raw = await asyncio.wait_for(_body(request), timeout=10)
        admission = await asyncio.to_thread(
            engine.submit, raw, Provenance(kind="local", source_id="local-owner")
        )
        receipt = admission.receipt
        status = (200 if admission.duplicate else
                  202 if admission.execute else _status(receipt["code"]))
        return JSONResponse(receipt, status_code=status)

    @app.get(prefix + "operations")
    def operation_list(limit: int = Query(default=50, ge=1, le=100),
                       cursor: str | None = None) -> dict[str, Any]:
        if cursor is not None and (not cursor.isdecimal() or len(cursor) > 18):
            raise DomainError("INVALID_ID", "Курсор списка некорректен.")
        return engine.ledger.list_operations(limit=limit, cursor=cursor)

    @app.get(prefix + "operations/{request_id}")
    def operation(request_id: str) -> Response:
        _require_uuid(request_id)
        value = engine.ledger.get(request_id)
        return JSONResponse(value) if value is not None else JSONResponse({"error": "NOT_FOUND"}, 404)

    @app.get(prefix + "projects")
    def projects() -> dict[str, Any]:
        return {"items": engine.ledger.list_projects(), "next_cursor": None}

    @app.get(prefix + "projects/{project_id}")
    def project(project_id: str) -> Response:
        _require_slug(project_id)
        value = engine.ledger.get_project(project_id)
        return JSONResponse(value) if value is not None else JSONResponse({"error": "NOT_FOUND"}, 404)

    @app.get(prefix + "projects/{project_id}/workspaces")
    def workspaces(project_id: str) -> Response:
        _require_slug(project_id)
        if engine.ledger.get_project(project_id) is None:
            return JSONResponse({"error": "NOT_FOUND"}, 404)
        return JSONResponse({"items": engine.ledger.list_workspaces(project_id), "next_cursor": None})

    @app.get(prefix + "projects/{project_id}/workspaces/{workspace_id}/source")
    async def workspace_source(project_id: str, workspace_id: str) -> Response:
        _require_slug(project_id)
        _require_slug(workspace_id)
        if source_registry is None:
            return JSONResponse({"error": "SOURCE_UNAVAILABLE"}, status_code=503)
        try:
            result = await asyncio.to_thread(
                inspect_registered_workspace, source_registry, project_id, workspace_id,
            )
        except SourceViewError as error:
            status = 404 if error.code == "SOURCE_NOT_FOUND" else 409
            return JSONResponse({"error": error.code}, status_code=status)
        return JSONResponse(result)

    @app.get(prefix + "projects/{project_id}/workspaces/{workspace_id}/connections")
    def workspace_connections(project_id: str, workspace_id: str) -> Response:
        _require_slug(project_id)
        _require_slug(workspace_id)
        if source_registry is None:
            return JSONResponse({"error": "WORKSPACE_NOT_FOUND"}, status_code=404)
        workspace = source_registry.get_workspace(project_id, workspace_id)
        if workspace is None:
            return JSONResponse({"error": "WORKSPACE_NOT_FOUND"}, status_code=404)
        if connection_checks is None:
            return JSONResponse({"error": "CONNECTION_CHECK_UNAVAILABLE"}, status_code=503)
        check = connection_checks.remote_snapshot(project_id, workspace_id)
        observation = check["observation"]
        current: bool | None = None
        if (isinstance(observation, dict)
                and {"branch", "local_head", "source_generation"} <= observation.keys()):
            try:
                live = inspect_registered_workspace(source_registry, project_id, workspace_id)
            except SourceViewError:
                current = None
            else:
                current = (
                    live["matches_recorded"] is True and live["dirty"] is False
                    and live["observed_branch"] == observation["branch"]
                    and live["observed_head"] == observation["local_head"]
                    and live["recorded_generation"] == observation["source_generation"]
                )
        check["workspace_snapshot_current"] = current
        return JSONResponse({
            "git_remote": check,
            "git_fetch": engine.ledger.project_fetch_snapshot(project_id),
        })

    @app.get(prefix + "projects/{project_id}/workspaces/{workspace_id}/drive-docs")
    async def workspace_drive_docs(project_id: str, workspace_id: str) -> Response:
        _require_slug(project_id)
        _require_slug(workspace_id)
        if source_registry is None or source_registry.get_workspace(project_id, workspace_id) is None:
            return JSONResponse({"error": "WORKSPACE_NOT_FOUND"}, status_code=404)
        if drive_registry is None:
            return JSONResponse({"error": "DRIVE_REGISTRY_UNAVAILABLE"}, status_code=503)
        try:
            registration = await asyncio.to_thread(drive_registry.get, project_id, workspace_id)
        except DriveRegistryError as error:
            status = (404 if error.code == "REGISTRATION_UNAVAILABLE" else
                      503 if error.code in {"REGISTRATION_ACCESS_MISSING", "REGISTRY_NOT_PRIVATE"}
                      else 409)
            return JSONResponse({"error": error.code}, status_code=status)
        return JSONResponse(_workspace_drive_links(registration))

    @app.get(prefix + "runs/{run_id}")
    def run(run_id: str) -> Response:
        _require_uuid(run_id)
        value = engine.ledger.get_run(run_id)
        return JSONResponse(value) if value is not None else JSONResponse({"error": "NOT_FOUND"}, 404)

    @app.get(prefix + "runs/{run_id}/retention")
    async def run_retention(run_id: str) -> Response:
        _require_uuid(run_id)
        if run_evidence is None:
            return JSONResponse({"error": "OUTPUT_STORAGE_UNAVAILABLE"}, status_code=503)
        try:
            return JSONResponse(await asyncio.to_thread(run_evidence.run_retention_status, run_id))
        except EvidenceError as error:
            return JSONResponse({"error": error.code}, status_code=_evidence_http_status(error.code))

    @app.post(prefix + "runs/{run_id}/retention/pin")
    async def run_pin(run_id: str, request: Request) -> Response:
        _require_uuid(run_id)
        value = parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096)
        if set(value) != {"pinned"} or type(value["pinned"]) is not bool:
            raise DomainError("INVALID_SCHEMA", "Нужно явно указать закрепление запуска.")
        if run_evidence is None:
            return JSONResponse({"error": "OUTPUT_STORAGE_UNAVAILABLE"}, status_code=503)
        try:
            changed = await asyncio.to_thread(
                run_evidence.set_run_pin, run_id, pinned=value["pinned"],
            )
            result = await asyncio.to_thread(run_evidence.run_retention_status, run_id)
        except EvidenceError as error:
            return JSONResponse({"error": error.code}, status_code=_evidence_http_status(error.code))
        return JSONResponse({**result, "changed": changed})

    @app.post(prefix + "runs/{run_id}/outputs/{output_id}/expire")
    async def expire_output(run_id: str, output_id: str, request: Request) -> Response:
        _require_uuid(run_id)
        _require_slug(output_id)
        if parse_json(await asyncio.wait_for(_body(request, 4096), timeout=5), max_bytes=4096) != {}:
            raise DomainError("INVALID_SCHEMA", "Параметры очистки вывода не допускаются.")
        if run_evidence is None:
            return JSONResponse({"error": "OUTPUT_STORAGE_UNAVAILABLE"}, status_code=503)
        try:
            result = await asyncio.to_thread(run_evidence.expire_output, run_id, output_id)
        except EvidenceError as error:
            return JSONResponse({"error": error.code}, status_code=_evidence_http_status(error.code))
        return JSONResponse(result)

    @app.get(prefix + "runs/{run_id}/outputs")
    def outputs(run_id: str) -> Response:
        _require_uuid(run_id)
        if engine.ledger.get_run(run_id) is None:
            return JSONResponse({"error": "NOT_FOUND"}, 404)
        return JSONResponse({"items": engine.ledger.list_outputs(run_id), "next_cursor": None})

    @app.get(prefix + "runs/{run_id}/outputs/{output_id}/inspection")
    def output_inspection(run_id: str, output_id: str) -> Response:
        _require_uuid(run_id)
        _require_slug(output_id)
        if run_evidence is None:
            return JSONResponse({"error": "OUTPUT_STORAGE_UNAVAILABLE"}, status_code=503)
        manifest = next((item for item in engine.ledger.list_outputs(run_id)
                         if item["output_id"] == output_id), None)
        if manifest is None:
            return JSONResponse({"error": "OUTPUT_NOT_FOUND"}, status_code=404)
        if manifest["size_bytes"] > MAX_INSPECTION_BYTES:
            return JSONResponse(inspection_limit(manifest))
        try:
            handle = run_evidence.open_output(run_id, output_id)
        except EvidenceError as error:
            return JSONResponse({"error": error.code}, status_code=_evidence_http_status(error.code))
        try:
            raw = bytearray()
            while chunk := os.read(handle.descriptor, 64 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_INSPECTION_BYTES:
                    return JSONResponse(inspection_limit(manifest))
            if (len(raw) != handle.size_bytes or handle.size_bytes != manifest["size_bytes"]
                    or handle.sha256 != manifest["sha256"]):
                return JSONResponse({"error": "OUTPUT_CHANGED"}, status_code=409)
            return JSONResponse(inspect_registered_output(manifest, bytes(raw)))
        finally:
            handle.close()

    @app.get(prefix + "runs/{run_id}/outputs/{output_id}")
    def output(run_id: str, output_id: str) -> Response:
        _require_uuid(run_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", output_id):
            return JSONResponse({"error": "INVALID_OUTPUT_ID"}, 422)
        if run_evidence is None:
            return JSONResponse({"error": "OUTPUT_STORAGE_UNAVAILABLE"}, 503)
        try:
            handle = run_evidence.open_output(run_id, output_id)
        except EvidenceError as error:
            status = 404 if error.code == "OUTPUT_NOT_FOUND" else 409
            return JSONResponse({"error": error.code}, status)

        def chunks() -> Iterator[bytes]:
            try:
                while data := os.read(handle.descriptor, 64 * 1024):
                    yield data
            finally:
                handle.close()

        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{output_id}.bin"',
            "Content-Length": str(handle.size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        })

    @app.get(prefix + "events")
    async def events(request: Request, after: int = Query(default=0, ge=0, le=999_999_999_999_999_999)) -> Response:
        cursor = after
        resume = request.headers.get("last-event-id")
        if resume is not None:
            if not resume.isdecimal() or len(resume) > 18:
                return JSONResponse({"error": "INVALID_EVENT_CURSOR"}, 422)
            cursor = int(resume)

        async def stream() -> AsyncIterator[str]:
            # Finite resumable batch avoids holding an unbounded read transaction or executor slot.
            # EventSource reconnects with Last-Event-ID; durable DB truth is never lost.
            rows = await asyncio.to_thread(engine.ledger.events, after=cursor, limit=100)
            for item in rows:
                yield f"id: {item['event_id']}\nevent: journal\ndata: {json.dumps(item)}\n\n"
            yield ": end-of-current-events\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    return app
