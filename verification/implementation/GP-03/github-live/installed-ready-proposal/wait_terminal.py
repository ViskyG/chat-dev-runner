"""Bounded HUD observer for the one approved installed request; never submits work."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DATABASE = Path("/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner/state.sqlite")
REQUEST_ID = "4b9de43b-722e-40c7-bbfa-ac95b980edc7"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "SUSPENDED_RESTART",
            "INTERRUPTED", "NEEDS_RECONCILIATION"}


def main() -> int:
    deadline, previous = time.monotonic() + 1020, None
    while time.monotonic() < deadline:
        connection = sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT status FROM v2_requests WHERE request_id=?",
                                     (REQUEST_ID,)).fetchone()
        finally:
            connection.close()
        state = row[0] if row else "NOT_OBSERVED"
        if state != previous:
            print("@@HUD " + json.dumps({"phase": "Installed request: " + state}), flush=True)
            previous = state
        if state in TERMINAL:
            folder = Path(__file__).resolve().parent
            return subprocess.run([
                sys.executable, str(folder.parent / "check_installed_request.py"),
                "--check", "--database", str(DATABASE), "--expect", "completed",
                "--out", str(folder / "execution" / "completed-readonly.json"),
            ], check=False).returncode
        time.sleep(1)
    print(json.dumps({"status": "TERMINAL_NOT_PROVED", "request_id": REQUEST_ID}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
