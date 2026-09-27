"""Transactional no-wait admission and request identity. SQLite is local authority."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from .protocol import Rejected

TERMINAL = {"COMPLETED", "BLOCKED", "FAULT", "CANCELLED", "INTERRUPTED", "REJECTED"}

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

@dataclass(frozen=True)
class Admission:
    request_id: str
    status: str
    execute: bool
    code: str | None = None

class Ledger:
    def __init__(self, path: Path, max_parallel: int = 2):
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.path, self.max_parallel = Path(path), max_parallel
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS operations (
                request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                scope_key TEXT NOT NULL, status TEXT NOT NULL,
                code TEXT, received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_running_scope
                ON operations(scope_key) WHERE status='RUNNING';
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def admit(self, request_id: str, digest: str, scope_key: str) -> Admission:
        """Persist immediate RUNNING or terminal REJECTED; never create a waiting row."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if old:
                db.commit()
                if old["digest"] != digest:
                    raise Rejected("REQUEST_ID_REUSED", "Same ID has different immutable content")
                return Admission(request_id, old["status"], False, old["code"])
            busy = db.execute("SELECT 1 FROM operations WHERE scope_key=? AND status='RUNNING'",
                              (scope_key,)).fetchone()
            running = db.execute("SELECT count(*) FROM operations WHERE status='RUNNING'").fetchone()[0]
            reject = bool(busy or running >= self.max_parallel)
            status, code = ("REJECTED", "BUSY") if reject else ("RUNNING", None)
            now = utcnow()
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, digest, scope_key, status, code, now, now, None))
            db.commit()
        return Admission(request_id, status, not reject, code)

    def finish(self, request_id: str, status: str, result: dict[str, Any] | None = None,
               code: str | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("A terminal status is required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["status"] != "RUNNING":
                db.rollback()
                raise Rejected("INVALID_TRANSITION", "Only a running operation can finish")
            db.execute("UPDATE operations SET status=?,code=?,updated_at=?,result_json=? WHERE request_id=?",
                       (status, code, utcnow(), json.dumps(result) if result is not None else None, request_id))
            db.commit()

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def list_running(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM operations WHERE status='RUNNING'")]

    def confirm_interrupted(self, request_id: str, *, processes_confirmed_stopped: bool) -> None:
        """No automatic re-execution after a crash. Production must reconcile external effects."""
        if not processes_confirmed_stopped:
            raise Rejected("PROCESS_STATE_UNKNOWN", "Confirm worker processes stopped before releasing scope")
        self.finish(request_id, "INTERRUPTED", code="EXPLICIT_RECOVERY")
