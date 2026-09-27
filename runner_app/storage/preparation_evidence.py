"""Owner-local access and manual retention for immutable preparation evidence.

No route or provider adapter imports this store. A caller must already be local
and trusted; the only keys accepted here are IDs in the durable phase ledger.
The receipt remains as compact provenance when individual capture bytes expire.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .database import Database
from .paths import (
    UnsafePathError,
    _absolute,
    _check_private,
    _open_directory,
    ensure_private_directory,
    open_private_file,
)
from .run_evidence import (
    _HEX,
    _MAX_OUTPUT_BYTES,
    EvidenceError,
    OutputHandle,
    RunEvidenceStore,
    _sealed_output_copy,
    _time,
    _uuid,
)

# The finite capture set frozen by run_service._freeze_capture. Prepared build
# files are never evidence outputs and cannot be downloaded through this store.
_CAPTURE_NAMES = {
    "stdout-raw": "stdout.bin",
    "stdout-view": "stdout.txt",
    "stderr-raw": "stderr.bin",
    "stderr-view": "stderr.txt",
    "capture-events": "events.jsonl",
    "capture-metadata": "capture.json",
}
_MAX_RECEIPT_BYTES = 1_048_576
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class PreparationEvidenceStore:
    """Read sealed phase bytes and retire raw outputs under owner policy."""

    def __init__(self, database: Database, evidence_root: Path) -> None:
        self.database = database
        self.evidence_root = _absolute(evidence_root)
        ensure_private_directory(self.evidence_root)
        ensure_private_directory(self.evidence_root / ".locks")
        self.database.migrate()

    def _connection(self) -> sqlite3.Connection:
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        return connection

    def _result(self, run_id: str) -> sqlite3.Row:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT request_id,run_id,evidence_sha256,created_at "
                "FROM preparation_results WHERE run_id=?", (run_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise EvidenceError("PREPARATION_NOT_FOUND", "Preparation evidence is unavailable")
        return cast(sqlite3.Row, row)

    @contextmanager
    def _lock(self, run_id: str, *, exclusive: bool = False) -> Iterator[None]:
        try:
            descriptor = open_private_file(
                self.evidence_root / ".locks" / f"{run_id}.lock", create=True,
            )
        except (OSError, UnsafePathError):
            raise EvidenceError("EVIDENCE_LOCK_UNAVAILABLE", "Preparation lock is unsafe") from None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                            if exclusive else fcntl.LOCK_SH)
            except BlockingIOError:
                raise EvidenceError("EVIDENCE_BUSY", "Preparation evidence is being read") from None
            except OSError:
                raise EvidenceError("EVIDENCE_LOCK_UNAVAILABLE", "Preparation lock failed") from None
            yield
        finally:
            os.close(descriptor)

    def _receipt(self, row: sqlite3.Row) -> tuple[dict[str, Any], bytes]:
        path = self.evidence_root / str(row["run_id"]) / "preparation.json"
        try:
            descriptor = open_private_file(path)
        except (OSError, UnsafePathError):
            raise EvidenceError("PREPARATION_RECEIPT_UNSAFE", "Preparation receipt is unavailable") from None
        try:
            before = os.fstat(descriptor)
            if before.st_size > _MAX_RECEIPT_BYTES:
                raise EvidenceError("PREPARATION_RECEIPT_INVALID", "Preparation receipt exceeds its limit")
            raw = bytearray()
            while chunk := os.read(descriptor, 65_536):
                raw.extend(chunk)
                if len(raw) > _MAX_RECEIPT_BYTES:
                    raise EvidenceError("PREPARATION_RECEIPT_INVALID", "Preparation receipt exceeds its limit")
            after = os.fstat(descriptor)
            if (len(raw) != before.st_size
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                        before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                     after.st_ctime_ns)
                    or hashlib.sha256(raw).hexdigest() != row["evidence_sha256"]):
                raise EvidenceError("PREPARATION_RECEIPT_CHANGED", "Preparation receipt differs from the ledger")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("schema_version") != "1.0"
                    or value.get("phase") != "PREPARATION"
                    or value.get("request_id") != row["request_id"]
                    or value.get("run_id") != row["run_id"]
                    or not isinstance(value.get("outputs"), list)
                    or not 2 <= len(value["outputs"]) <= len(_CAPTURE_NAMES)):
                raise ValueError
            seen: set[str] = set()
            for item in value["outputs"]:
                if not isinstance(item, dict):
                    raise TypeError
                output_id = item.get("output_id")
                if (not isinstance(output_id, str) or output_id not in _CAPTURE_NAMES
                        or output_id in seen
                        or item.get("relative_path") != "capture/" + _CAPTURE_NAMES[output_id]
                        or item.get("publication") != "local_only"
                        or type(item.get("size_bytes")) is not int
                        or not 0 <= item["size_bytes"] <= _MAX_OUTPUT_BYTES
                        or not isinstance(item.get("sha256"), str)
                        or _HEX.fullmatch(item["sha256"]) is None):
                    raise ValueError
                seen.add(output_id)
            if not {"stdout-raw", "stderr-raw"} <= seen:
                raise ValueError
        except (TypeError, ValueError):
            raise EvidenceError("PREPARATION_RECEIPT_INVALID", "Preparation receipt metadata is invalid") from None
        return value, bytes(raw)

    def receipt(self, run_id: str) -> dict[str, Any]:
        """Return verified local metadata; this is not a remote delivery result."""
        run_id = _uuid(run_id)
        row = self._result(run_id)
        with self._lock(run_id):
            value, _ = self._receipt(row)
            return value

    def open_receipt(self, run_id: str) -> OutputHandle:
        """Return a sealed, hash-verified descriptor for owner-local download."""
        run_id = _uuid(run_id)
        row = self._result(run_id)
        with self._lock(run_id):
            _, raw = self._receipt(row)
            descriptor = _sealed_output_copy(
                self.evidence_root / run_id / "preparation.json",
                str(row["evidence_sha256"]), len(raw),
            )
            return OutputHandle(descriptor, len(raw), str(row["evidence_sha256"]),
                                "preparation-receipt")

    def retention_index(self, run_id: str) -> list[dict[str, Any]]:
        """Overlay durable expiry state without editing the receipt."""
        run_id = _uuid(run_id)
        row = self._result(run_id)
        with self._lock(run_id):
            receipt, _ = self._receipt(row)
            connection = self._connection()
            try:
                tombstones = {
                    item["output_id"]: item for item in connection.execute(
                        "SELECT output_id,sha256,size_bytes,reason,created_at "
                        "FROM preparation_output_tombstones WHERE run_id=?", (run_id,),
                    )
                }
            finally:
                connection.close()
            result = []
            for item in receipt["outputs"]:
                tombstone = tombstones.pop(item["output_id"], None)
                if tombstone is not None and (tombstone["sha256"] != item["sha256"]
                                              or tombstone["size_bytes"] != item["size_bytes"]):
                    raise EvidenceError("PREPARATION_RECEIPT_CHANGED", "Tombstone metadata differs from receipt")
                result.append({
                    "output_id": item["output_id"], "sha256": item["sha256"],
                    "size_bytes": item["size_bytes"],
                    "state": "EXPIRED" if tombstone is not None else "AVAILABLE",
                    "tombstone_reason": tombstone["reason"] if tombstone is not None else None,
                    "tombstone_at": tombstone["created_at"] if tombstone is not None else None,
                })
            if tombstones:
                raise EvidenceError("PREPARATION_RECEIPT_CHANGED", "Unknown preparation tombstone")
            return result

    def open_output(self, run_id: str, output_id: str) -> OutputHandle:
        """Seal only a registered frozen capture file, never a prepared build file."""
        run_id = _uuid(run_id)
        if not isinstance(output_id, str) or output_id not in _CAPTURE_NAMES:
            raise EvidenceError("INVALID_OUTPUT_ID", "Preparation output ID is invalid")
        row = self._result(run_id)  # Unknown IDs must not create lock files.
        with self._lock(run_id):
            receipt, _ = self._receipt(row)
            item = next((part for part in receipt["outputs"]
                         if part["output_id"] == output_id), None)
            if item is None:
                raise EvidenceError("OUTPUT_NOT_FOUND", "Preparation output is not registered")
            connection = self._connection()
            try:
                expired = connection.execute(
                    "SELECT 1 FROM preparation_output_tombstones "
                    "WHERE run_id=? AND output_id=?", (run_id, output_id),
                ).fetchone() is not None
            finally:
                connection.close()
            if expired:
                raise EvidenceError("EVIDENCE_EXPIRED", "Preparation output retention has expired")
            descriptor = _sealed_output_copy(
                self.evidence_root / run_id / "capture" / _CAPTURE_NAMES[output_id],
                item["sha256"], item["size_bytes"],
            )
            return OutputHandle(descriptor, item["size_bytes"], item["sha256"], output_id)

    def set_pin(self, run_id: str, *, pinned: bool) -> bool:
        """Record an append-only owner decision; no remote command reaches here."""
        run_id = _uuid(run_id)
        if type(pinned) is not bool:
            raise EvidenceError("RETENTION_POLICY_INVALID", "Pin state must be explicit")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM preparation_results WHERE run_id=?", (run_id,),
            ).fetchone() is None:
                raise EvidenceError("PREPARATION_NOT_FOUND", "Preparation evidence is unavailable")
            if pinned and connection.execute(
                "SELECT 1 FROM preparation_output_tombstones WHERE run_id=? LIMIT 1",
                (run_id,),
            ).fetchone() is not None:
                raise EvidenceError("EVIDENCE_EXPIRED", "Expired preparation evidence cannot be pinned")
            previous = connection.execute(
                "SELECT action FROM preparation_retention_pin_events WHERE run_id=? "
                "ORDER BY event_id DESC LIMIT 1", (run_id,),
            ).fetchone()
            was_pinned = previous is not None and previous[0] == "PIN"
            if was_pinned != pinned:
                connection.execute(
                    "INSERT INTO preparation_retention_pin_events(run_id,action,created_at) "
                    "VALUES(?,?,?)", (run_id, "PIN" if pinned else "UNPIN",
                                   datetime.now(timezone.utc).isoformat()),
                )
            connection.execute("COMMIT")
            return was_pinned != pinned
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _purge(self, run_id: str, output_id: str, digest: str, size: int) -> None:
        path = self.evidence_root / run_id / "capture" / _CAPTURE_NAMES[output_id]
        try:
            directory = _open_directory(path.parent, create=False)
        except (OSError, UnsafePathError):
            raise EvidenceError("EVIDENCE_PURGE_FAILED", "Preparation capture directory is unsafe") from None
        descriptor = -1
        try:
            try:
                descriptor = os.open(path.name, _OPEN_FILE, dir_fd=directory)
            except FileNotFoundError:
                sync = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                               dir_fd=directory)
                try:
                    os.fsync(sync)
                finally:
                    os.close(sync)
                return
            before = os.fstat(descriptor)
            _check_private(before, directory=False)
            if before.st_size != size:
                raise EvidenceError("OUTPUT_CHANGED", "Preparation output changed before purge")
            hasher = hashlib.sha256()
            count = 0
            while chunk := os.read(descriptor, 65_536):
                count += len(chunk)
                if count > _MAX_OUTPUT_BYTES:
                    raise EvidenceError("OUTPUT_CHANGED", "Preparation output exceeds its receipt")
                hasher.update(chunk)
            after = os.fstat(descriptor)
            named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (count != size or hasher.hexdigest() != digest
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                        before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                     after.st_ctime_ns)
                    or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)):
                raise EvidenceError("OUTPUT_CHANGED", "Preparation output changed before purge")
            os.unlink(path.name, dir_fd=directory)
            sync = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                           dir_fd=directory)
            try:
                os.fsync(sync)
            finally:
                os.close(sync)
        except EvidenceError:
            raise
        except (OSError, UnsafePathError):
            raise EvidenceError("EVIDENCE_PURGE_FAILED", "Preparation output could not be purged") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory)

    def expire_output(self, run_id: str, output_id: str, *, retention_days: int = 14,
                      now: datetime | None = None) -> dict[str, str]:
        """Tombstone before unlink; retry completes purge after a crash."""
        run_id = _uuid(run_id)
        if not isinstance(output_id, str) or output_id not in _CAPTURE_NAMES:
            raise EvidenceError("INVALID_OUTPUT_ID", "Preparation output ID is invalid")
        self._result(run_id)
        cutoff, timestamp = RunEvidenceStore._retention_time(now, retention_days)
        with self._lock(run_id, exclusive=True):
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT p.request_id,p.run_id,p.evidence_sha256,p.created_at,o.status,o.action,"
                    "o.delivery_status,o.reconcile_required,o.capacity_held "
                    "FROM preparation_results p JOIN operations o ON o.request_id=p.request_id "
                    "WHERE p.run_id=?", (run_id,),
                ).fetchone()
                if row is None:
                    raise EvidenceError("PREPARATION_NOT_FOUND", "Preparation evidence is unavailable")
                receipt, _ = self._receipt(row)
                item = next((part for part in receipt["outputs"]
                             if part["output_id"] == output_id), None)
                if item is None:
                    raise EvidenceError("OUTPUT_NOT_FOUND", "Preparation output is not registered")
                tombstone = connection.execute(
                    "SELECT sha256,size_bytes,created_at FROM preparation_output_tombstones "
                    "WHERE run_id=? AND output_id=?", (run_id, output_id),
                ).fetchone()
                if tombstone is None:
                    if _time(row["created_at"]) > cutoff:
                        raise EvidenceError("RETENTION_NOT_DUE", "Preparation evidence is too recent")
                    if (row["status"] == "RUNNING" or row["reconcile_required"]
                            or row["capacity_held"]
                            or row["delivery_status"] in {"PENDING", "PARTIAL", "FAILED"}
                            or (row["action"] == "DELIVER_VERIFY_PUBLISH"
                                and row["delivery_status"] != "DELIVERED")):
                        raise EvidenceError("EVIDENCE_REFERENCED", "Preparation operation is unresolved")
                    if connection.execute(
                        "SELECT 1 FROM effects WHERE request_id=? "
                        "AND state IN ('INTENDED','UNKNOWN') LIMIT 1", (row["request_id"],),
                    ).fetchone() is not None:
                        raise EvidenceError("EVIDENCE_REFERENCED", "Preparation has an uncertain effect")
                    if connection.execute(
                        "SELECT 1 FROM runs r JOIN run_delivery_claims c ON c.run_id=r.run_id "
                        "LEFT JOIN run_delivery_claim_releases x "
                        "ON x.delivery_request_id=c.delivery_request_id "
                        "WHERE r.request_id=? AND x.delivery_request_id IS NULL LIMIT 1",
                        (row["request_id"],),
                    ).fetchone() is not None:
                        raise EvidenceError("EVIDENCE_REFERENCED", "Preparation has a pending report")
                    pin = connection.execute(
                        "SELECT action FROM preparation_retention_pin_events WHERE run_id=? "
                        "ORDER BY event_id DESC LIMIT 1", (run_id,),
                    ).fetchone()
                    if pin is not None and pin[0] == "PIN":
                        raise EvidenceError("EVIDENCE_PINNED", "Preparation is pinned by owner policy")
                    connection.execute(
                        "INSERT INTO preparation_output_tombstones"
                        "(run_id,output_id,sha256,size_bytes,reason,created_at) "
                        "VALUES(?,?,?,?,'RETENTION_EXPIRED',?)",
                        (run_id, output_id, item["sha256"], item["size_bytes"], timestamp),
                    )
                    tombstone_at = timestamp
                else:
                    if tombstone["sha256"] != item["sha256"] or tombstone["size_bytes"] != item["size_bytes"]:
                        raise EvidenceError("PREPARATION_RECEIPT_CHANGED", "Tombstone metadata differs from receipt")
                    tombstone_at = str(tombstone["created_at"])
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
            self._purge(run_id, output_id, item["sha256"], item["size_bytes"])
            return {"run_id": run_id, "output_id": output_id,
                    "reason": "RETENTION_EXPIRED", "created_at": tombstone_at}
