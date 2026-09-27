"""Authenticated local read models and immediate finite operation intake."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from fastapi import FastAPI, Query, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from runner_app import __version__
from runner_app.api.app import create_app
from runner_app.api.auth import AuthenticationError, Sessions
from runner_app.config import Settings, setup_requirements
from runner_app.domain.commands import DomainError, Provenance, parse_json
from runner_app.engine.service import Engine
from runner_app.git.imports import ImportGrants
from runner_app.storage.ledger import LedgerError
from runner_app.storage.run_evidence import EvidenceError, RunEvidenceStore


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
                       run_evidence: RunEvidenceStore | None = None) -> FastAPI:
    def runtime_health() -> dict[str, Any]:
        return {"service": "chat-dev-runner", "version": __version__,
                "status": "NOT_CONFIGURED", "armed": engine.ledger.state()["armed"],
                "capabilities": ["local_api", "durable_admission", *engine.capabilities()]}

    app = create_app(settings, health_provider=runtime_health)
    hosts = {f"127.0.0.1:{settings.listen_port}", f"localhost:{settings.listen_port}"}
    if settings.listen_port == 80:
        hosts.update({"127.0.0.1", "localhost"})
    origins = {f"http://{host}" for host in hosts}
    prefix = "/api/v1/"
    session_path = prefix + "local/session"

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
            request.url.path == session_path and request.method == "POST"
        ):
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
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse({"error": error.code, "message": str(error)}, status_code=_status(error.code))

    @app.exception_handler(LedgerError)
    async def ledger_error(request: Request, error: LedgerError) -> JSONResponse:
        return JSONResponse({"error": error.code}, status_code=_status(error.code))

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
        response = JSONResponse({"csrf_token": authenticated.csrf, "expires_in": sessions.ttl})
        response.set_cookie(sessions.cookie_name, token, max_age=sessions.ttl,
                            httponly=True, samesite="strict", secure=False, path="/")
        return response

    @app.get(prefix + "system")
    def system() -> dict[str, Any]:
        return {"schema_version": "1.0", **engine.ledger.state(),
                "capabilities": engine.capabilities(), "integrations": setup_requirements(),
                "execution_queue": False}

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

    @app.get(prefix + "runs/{run_id}")
    def run(run_id: str) -> Response:
        _require_uuid(run_id)
        value = engine.ledger.get_run(run_id)
        return JSONResponse(value) if value is not None else JSONResponse({"error": "NOT_FOUND"}, 404)

    @app.get(prefix + "runs/{run_id}/outputs")
    def outputs(run_id: str) -> Response:
        _require_uuid(run_id)
        if engine.ledger.get_run(run_id) is None:
            return JSONResponse({"error": "NOT_FOUND"}, 404)
        return JSONResponse({"items": engine.ledger.list_outputs(run_id), "next_cursor": None})

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
