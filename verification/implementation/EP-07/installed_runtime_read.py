"""One-time authenticated read of the installed runner; never persist session material."""
from __future__ import annotations

import http.client
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PORT = 8765
BASE = "http://127.0.0.1:8765"
BOOTSTRAP = Path("/home/visky/.config/chat-dev-runner/tokens/session-bootstrap.json")
ROOT_ID = "177IEHeBVezHIlbIijZsIvRDSbTJ42kxe"
INDEX_ID = "1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA"


def _request(method: str, path: str, *, body: bytes | None = None,
             cookie: str | None = None, csrf: str | None = None
             ) -> tuple[int, dict[str, str], dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Host": f"127.0.0.1:{PORT}"}
    if body is not None:
        headers.update({"Origin": BASE, "Content-Type": "application/json"})
    if cookie is not None:
        headers["Cookie"] = cookie
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("response too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("invalid response")
        return response.status, {key.lower(): val for key, val in response.getheaders()}, value
    finally:
        connection.close()


def main() -> int:
    info = BOOTSTRAP.stat()
    if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
            or not stat.S_ISREG(info.st_mode)):
        raise ValueError("unsafe bootstrap handoff")
    handoff = json.loads(BOOTSTRAP.read_bytes())
    secret = handoff["bootstrap"]
    code, headers, session = _request("POST", "/api/v1/local/session",
                                      body=json.dumps({"bootstrap": secret}).encode())
    if code != 200:
        raise ValueError("local session exchange failed")
    cookie = headers["set-cookie"].split(";", 1)[0]
    code, _, state = _request("GET", "/api/v1/system", cookie=cookie)
    if code != 200:
        raise ValueError("authenticated system read failed")
    checks = {}
    for provider in ("drive", "github"):
        check_code, _, observation = _request(
            "POST", f"/api/v1/local/check-{provider}", body=b"{}",
            cookie=cookie, csrf=session["csrf_token"],
        )
        checks[provider] = {"http": check_code, "status": observation.get("status"),
                            "code": observation.get("code")}
    ep05 = state.get("integrations", {}).get("ep05", {})
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "command": "installed_runtime_read.py (one-time owner session; no token persisted)",
        "exit_code": 0,
        "state": state.get("integrations", {}).get("state"),
        "armed": state.get("armed"),
        "capabilities": state.get("capabilities"),
        "attached_existing_root": ep05.get("drive_root_id") == ROOT_ID,
        "attached_existing_index": ep05.get("drive_index_id") == INDEX_ID,
        "execution_readiness": ep05.get("execution_readiness"),
        "draft_pr_capability": ep05.get("draft_pr_capability"),
        "connections": {
            key: value.get("status") if isinstance(value, dict) else None
            for key, value in state.get("connections", {}).items()
        },
        "provider_checks": checks,
        "session_or_bootstrap_persisted": False,
    }
    if (result["armed"] is not False or not result["attached_existing_root"]
            or not result["attached_existing_index"]):
        raise ValueError("installed runtime is not attached and disarmed")
    if any(item["http"] != 200 or item["status"] != "OK" for item in checks.values()):
        raise ValueError("deployed provider read check failed")
    output = Path(__file__).with_name("installed-runtime-read.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"result": "PASS", "evidence": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
