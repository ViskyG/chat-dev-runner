"""Read-only deployed-service gate: loopback, disarmed state and local API boundary."""
from __future__ import annotations

import argparse
import http.client
import json
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _request(port: int, method: str, path: str, *, host: str | None = None,
             origin: str | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read(1_000_001)
        if len(content) > 1_000_000:
            raise ValueError("Local API response exceeded gate bound")
        return response.status, {key.lower(): value for key, value in response.getheaders()}, content
    finally:
        connection.close()


def _unit_active(unit: str) -> bool | None:
    executable = shutil.which("systemctl")
    if executable is None:
        return None
    try:
        response = subprocess.run([executable, "--user", "is-active", unit],
                                  capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return response.returncode == 0 and response.stdout.strip() == "active"


def _listener_binds(port: int) -> list[str] | None:
    executable = shutil.which("ss")
    if executable is None:
        return None
    try:
        response = subprocess.run([executable, "-H", "-ltn"],
                                  capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if response.returncode:
        return None
    binds = []
    for line in response.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[3].rsplit(":", 1)[-1] == str(port):
            binds.append(fields[3])
    return sorted(binds)


def _lan_connect(port: int) -> dict[str, Any]:
    """Probe this host's routed non-loopback address without sending UDP data."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 9))
            local = route.getsockname()[0]
        if local.startswith("127."):
            return {"status": "NO_NONLOOPBACK_ROUTE"}
        with socket.create_connection((local, port), timeout=2):
            return {"status": "REACHABLE_NONLOOPBACK"}
    except (OSError, TimeoutError) as error:
        if 'local' not in locals():
            return {"status": "NO_NONLOOPBACK_ROUTE"}
        if isinstance(error, ConnectionRefusedError):
            return {"status": "REFUSED_NONLOOPBACK"}
        return {"status": "UNREACHABLE_NONLOOPBACK"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--unit", default="chat-dev-runner.service")
    args = parser.parse_args()
    checks: dict[str, Any] = {}
    failures: list[str] = []
    unit = _unit_active(args.unit)
    checks["user_unit_active"] = unit
    if unit is not True:
        failures.append("USER_UNIT_NOT_ACTIVE")
    binds = _listener_binds(args.port)
    checks["listener_binds"] = binds
    if binds != [f"127.0.0.1:{args.port}"]:
        failures.append("LISTENER_NOT_EXACT_LOOPBACK")
    try:
        health_status, _, health_raw = _request(args.port, "GET", "/api/v1/health")
        health = json.loads(health_raw)
        checks["health"] = {"http": health_status, "service": health.get("service"),
                            "status": health.get("status"), "armed": health.get("armed")}
        if health_status != 200 or health.get("service") != "chat-dev-runner" or health.get("armed") is not False:
            failures.append("SERVICE_NOT_DISARMED")
        shell_status, headers, shell = _request(args.port, "GET", "/")
        checks["public_shell"] = {"http": shell_status,
                                  "csp": "default-src 'none'" in headers.get("content-security-policy", ""),
                                  "bytes": len(shell)}
        if shell_status != 200 or not checks["public_shell"]["csp"]:
            failures.append("SHELL_BOUNDARY_FAILED")
        system_status, _, _ = _request(args.port, "GET", "/api/v1/system")
        checks["unauthenticated_system_http"] = system_status
        if system_status != 403:
            failures.append("PRIVATE_READ_NOT_GATED")
        spoof_status, _, _ = _request(args.port, "GET", "/api/v1/health",
                                      host="attacker.invalid")
        checks["spoofed_host_http"] = spoof_status
        if spoof_status != 403:
            failures.append("HOST_SPOOF_ACCEPTED")
        origin_status, _, origin_body = _request(
            args.port, "POST", "/api/v1/local/session",
            origin="https://attacker.invalid", body=b'{"bootstrap":"invalid"}',
        )
        origin_code = json.loads(origin_body).get("error")
        checks["cross_origin_session"] = {"http": origin_status, "code": origin_code}
        if origin_status != 403 or origin_code != "INVALID_ORIGIN":
            failures.append("CROSS_ORIGIN_MUTATION_BOUNDARY_FAILED")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        checks["http_error_kind"] = type(error).__name__
        failures.append("LOCAL_HTTP_UNAVAILABLE")
    checks["nonloopback_probe"] = _lan_connect(args.port)
    if checks["nonloopback_probe"]["status"] == "REACHABLE_NONLOOPBACK":
        failures.append("NONLOOPBACK_REACHABLE")
    report = {"schema_version": "1.0", "packet": "EP-07",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "mode": "READ_ONLY_DEPLOYED_SERVICE_GATE", "cloud_writes": False,
              "checks": checks, "failures": failures, "result": "PASS" if not failures else "FAIL"}
    path = Path(__file__).with_name("service-gate.json")
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"result": report["result"], "failures": failures, "evidence": str(path)}))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
