"""Bounded receipt publication; immutable result bundles are never rewritten."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app.git.argv import run_git_bytes, validate_branch
from runner_app.git.results_v2 import (
    COMMIT_AUTHOR,
    ResultError,
    ResultPushRejected,
    ResultTransport,
    _commit_tree,
    _entries,
    _identifier,
    _managed_repo,
    _oid,
    _tree,
    _write_object,
)


@dataclass(frozen=True)
class StatePublication:
    status: str
    code: str
    branch: str
    path: str
    payload_sha256: str
    commit: str | None = None


def _file(repo: Path, tree: str, parts: tuple[bytes, ...]) -> bytes | None:
    for index, part in enumerate(parts):
        entry = _entries(repo, tree).get(part)
        if entry is None:
            return None
        mode, kind, tree = entry
        expected = (b"100644", b"blob") if index == len(parts) - 1 else (b"040000", b"tree")
        if (mode, kind) != expected:
            raise ResultError("STATE_PATH_CONFLICT")
    size = int(run_git_bytes(repo, ["cat-file", "-s", tree]))
    if size > 256 * 1024:
        raise ResultError("STATE_SIZE_LIMIT")
    return run_git_bytes(repo, ["cat-file", "blob", tree])


def _set(repo: Path, tree: str | None, parts: tuple[bytes, ...], blob: str) -> str:
    entries = _entries(repo, tree) if tree else {}
    old = entries.get(parts[0])
    if len(parts) == 1:
        if old is not None and old[:2] != (b"100644", b"blob"):
            raise ResultError("STATE_PATH_CONFLICT")
        entries[parts[0]] = (b"100644", b"blob", blob)
    else:
        if old is not None and old[:2] != (b"040000", b"tree"):
            raise ResultError("STATE_PATH_CONFLICT")
        entries[parts[0]] = (b"040000", b"tree", _set(
            repo, old[2] if old else None, parts[1:], blob))
    return _tree(repo, entries)


def publish_state(repo: Path, transport: ResultTransport, *, branch: str,
                  state: Mapping[str, Any], record_intent: Callable[[Mapping[str, Any]], None],
                  max_conflict_retries: int = 3) -> StatePublication:
    """Publish once; retry only after a definite concurrent-ref rejection.

    Lost responses require exact readback. A later explicit STATUS can repair
    receipt delivery without repeating execution or creating a fallback branch.
    """
    path, digest, intended = "", "", None

    def answer(status: str, code: str) -> StatePublication:
        return StatePublication(status, code, branch, path, digest, intended)

    try:
        runner, request = _identifier(state["runner_id"]), _identifier(state["request_id"])
        branch = validate_branch(branch)
        if type(max_conflict_retries) is not int or not 0 <= max_conflict_retries <= 3:
            raise ResultError("INVALID_RETRY_BUDGET")
        repo = _managed_repo(repo)
        path = f".runner/state/{runner}/{request}.json"
        parts = tuple(item.encode("ascii") for item in path.split("/"))
        raw = (json.dumps(dict(state), sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(raw) > 256 * 1024:
            raise ResultError("STATE_SIZE_LIMIT")
        digest = hashlib.sha256(raw).hexdigest()
        blob = _write_object(repo, "blob", raw)
        marker, timestamp = uuid4().hex, int(time.time())
        for attempt in range(max_conflict_retries + 1):
            head = transport.fetch_head(repo, branch)
            if head is None:
                return answer("LOCAL_ONLY", "TARGET_BRANCH_MISSING")
            tree = _commit_tree(repo, _oid(head))
            existing = _file(repo, tree, parts)
            if existing == raw:
                intended = head
                return answer("DELIVERED", "ALREADY_DELIVERED")
            if existing is not None:
                prior = json.loads(existing)
                if any(prior.get(key) != state[key] for key in (
                        "runner_id", "project_id", "request_id", "request_digest")):
                    return answer("LOCAL_ONLY", "STATE_PATH_CONFLICT")
                if not isinstance(prior.get("updated_at"), str):
                    return answer("LOCAL_ONLY", "STATE_PATH_CONFLICT")
                if prior["updated_at"] > state["updated_at"]:
                    return answer("LOCAL_ONLY", "STATE_SUPERSEDED")
            tree = _set(repo, tree, parts, blob)
            identity = f"{COMMIT_AUTHOR} {timestamp} +0000"
            message = f"Publish runner state: {runner}/{request}\n\nPublication-Marker: {marker}\n"
            intended = _write_object(repo, "commit", (
                f"tree {tree}\nparent {head}\nauthor {identity}\ncommitter {identity}\n\n{message}"
            ).encode())
            record_intent({"branch": branch, "path": path, "payload_sha256": digest,
                           "base_commit": head, "commit": intended, "marker": marker})
            rejected = None
            response_lost = False
            try:
                transport.push_commit(repo, intended, branch)
            except ResultPushRejected as error:
                rejected = error
            except Exception:  # noqa: BLE001 -- read back lost responses without replay
                response_lost = True
            try:
                observed = transport.fetch_head(repo, branch)
                actual = _file(repo, _commit_tree(repo, _oid(observed)), parts) if observed else None
            except Exception:  # noqa: BLE001 -- provider diagnostics remain private
                return answer("LOCAL_ONLY" if rejected else "UNKNOWN",
                              rejected.code if rejected else "STATE_DELIVERY_UNKNOWN")
            if actual == raw:
                return answer("DELIVERED", "DELIVERED_RECONCILED" if response_lost else "DELIVERED")
            if rejected is None:
                return answer("UNKNOWN", "STATE_DELIVERY_UNKNOWN")
            if not rejected.retryable or attempt == max_conflict_retries:
                return answer("LOCAL_ONLY", rejected.code)
        raise AssertionError("Bounded state publication exhausted")
    except Exception as error:  # noqa: BLE001 -- no exception text crosses the boundary
        return answer("LOCAL_ONLY", error.code if isinstance(error, ResultError)
                      else "STATE_PUBLICATION_FAILED")
