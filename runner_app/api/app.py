"""Health-only EP-00 application. No execution, credential or setup mutation endpoints."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from runner_app import __version__
from runner_app.config import Settings


def create_app(
    settings: Settings | None = None,
    *,
    health_provider: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    config = settings or Settings()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {f"127.0.0.1:{config.listen_port}", f"localhost:{config.listen_port}"}
    if config.listen_port == 80:
        hosts.update({"127.0.0.1", "localhost"})
    origins = {f"http://{host}" for host in hosts}

    @app.middleware("http")
    async def local_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host_values = request.headers.getlist("host")
        origin_values = request.headers.getlist("origin")
        if len(host_values) != 1 or host_values[0] not in hosts:
            return JSONResponse({"error": "INVALID_HOST"}, status_code=403)
        if origin_values and (len(origin_values) != 1 or origin_values[0] not in origins):
            return JSONResponse({"error": "INVALID_ORIGIN"}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })
        # Keep a route's stronger policy, e.g. sandbox on a downloaded output.
        if "content-security-policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )
        return response

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        if health_provider is not None:
            return health_provider()
        return {"service": "chat-dev-runner", "version": __version__, "status": "NOT_CONFIGURED",
                "armed": False, "capabilities": ["health_only"]}

    return app
