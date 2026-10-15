"""Explicit read-only installed-boundary snapshot; importing this module is inert.

Run only after ROOT confirms the final preview restart. This checker never sends
READY, acknowledges a mailbox, starts a runtime, or migrates a database. A pending
snapshot is evidence, not permission to poll repeatedly or to publish a command.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_roundtrip import (
    BRANCH,
    PROJECT,
    REPOSITORY,
    SLOT,
    canonical,
    failure_code,
    git_hash,
    remote_file,
    sha256,
    write_new,
)

from runner_app.adapters.github import GitHubHTTPResponse
from runner_app.adapters.github_mailbox_v2 import BoundedGitHubV2HTTP, GitHubV2Client
from runner_app.git.github_transport import OwnerTokenFile
from runner_app.storage.paths import Paths

REQUEST_ID = "4b9de43b-722e-40c7-bbfa-ac95b980edc7"
RUNNER_ID = "local-runner"


class ReadOnlyHTTP:
    def __init__(self, token: OwnerTokenFile) -> None:
        self.inner = BoundedGitHubV2HTTP(token)

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        prefix = "/repos/" + REPOSITORY.full_name
        if method != "GET" or body is not None or not (path == prefix or path.startswith(prefix + "/")):
            raise RuntimeError("READ_ONLY_EXACT_REPOSITORY_REQUIRED")
        return self.inner.request(method, path, params=params)


def database_snapshot(database: Path) -> dict[str, Any]:
    if not database.is_absolute() or database.is_symlink():
        raise RuntimeError("EXACT_REGULAR_DATABASE_PATH_REQUIRED")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 10:
            raise RuntimeError("INSTALLED_SCHEMA_10_REQUIRED")
        runner = connection.execute("SELECT runner_id FROM runner_state").fetchone()[0]
        if runner != RUNNER_ID:
            raise RuntimeError("EXISTING_LOCAL_RUNNER_IDENTITY_REQUIRED")
        matching = []
        for row in connection.execute("SELECT * FROM v2_mailboxes"):
            coordinates = json.loads(row["coordinates_json"])
            if (row["runner_id"] == RUNNER_ID and row["project_id"] == PROJECT
                    and row["provider"] == "github" and coordinates.get("branch") == BRANCH
                    and coordinates.get("path") == SLOT
                    and coordinates.get("repository_id") == REPOSITORY.id):
                matching.append(dict(row))
        if len(matching) != 1:
            raise RuntimeError("EXACT_INSTALLED_MAILBOX_BINDING_REQUIRED")
        row = connection.execute(
            "SELECT r.request_id,r.runner_id,r.project_id,r.status,r.code,r.digest,r.result_json,q.sequence "
            "FROM v2_requests r LEFT JOIN v2_queue q USING(request_id) WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()
        operation = dict(row) if row is not None else None
        events = [{"event": row["event"], "detail": json.loads(row["detail_json"])}
                  for row in connection.execute(
                      "SELECT event,detail_json FROM v2_journal WHERE request_id=? ORDER BY rowid",
                      (REQUEST_ID,),
                  )]
        return {"schema": version, "runner_id": runner, "binding": matching[0],
                "legacy_operation_count": connection.execute("SELECT count(*) FROM operations").fetchone()[0],
                "v2_request_count": connection.execute("SELECT count(*) FROM v2_requests").fetchone()[0],
                "nonterminal_count": connection.execute(
                    "SELECT count(*) FROM v2_requests WHERE status IN "
                    "('ACK_PENDING','QUEUED','STARTING','RUNNING','CONTROL','CONTROL_RUNNING')",
                ).fetchone()[0],
                "execution_lane_count": connection.execute("SELECT count(*) FROM execution_lane").fetchone()[0],
                "operation": operation, "events": events}


def check(database: Path, expected: str) -> dict[str, Any]:
    local = database_snapshot(database)
    token = OwnerTokenFile(Paths.discover().config_dir / "github-token")
    client = GitHubV2Client(REPOSITORY, token, transport=ReadOnlyHTTP(token))
    file = client.current_file(BRANCH, SLOT)
    slot = json.loads(file.payload)
    if (slot["runner_id"] != RUNNER_ID or slot["project_id"] != PROJECT
            or slot["request"]["request_id"] != REQUEST_ID):
        raise RuntimeError("INSTALLED_REMOTE_SLOT_SCOPE_DIFFERS")
    binding = local.pop("binding")
    operation = local.pop("operation")
    events = local.pop("events")
    report = {"status": "READ_ONLY_SNAPSHOT", "observed_at": datetime.now(timezone.utc).isoformat(),
              "request_id": REQUEST_ID, "database": local,
              "mailbox": {"binding_id": binding["binding_id"], "continuous": bool(binding["continuous"]),
                          "slot_token_matches_database": slot["slot_token"] == binding["slot_token"],
                          "dispatch": slot["dispatch"], "head": file.head, "blob": file.blob,
                          "sha256": sha256(file.payload), "runner_id": slot["runner_id"]},
              "remote_mutations": 0}
    baseline = (bool(binding["continuous"]) and slot["slot_token"] == binding["slot_token"]
                and slot["dispatch"] == "DRAFT" and operation is None
                and local["nonterminal_count"] == local["execution_lane_count"] == 0)
    if expected == "baseline":
        report["status"] = "BASELINE_CONFIRMED" if baseline else "BASELINE_NOT_PROVED"
        return report
    if operation is None or operation["status"] != "COMPLETED" or not operation["result_json"]:
        report.update({"status": "COMPLETION_NOT_PROVED", "operation_status": operation["status"] if operation else None})
        return report
    result = json.loads(operation.pop("result_json"))
    manifest, delivery = result["manifest"], result["delivery"]
    if (operation["runner_id"] != RUNNER_ID or operation["project_id"] != PROJECT
            or manifest["request_id"] != REQUEST_ID or manifest["runner_id"] != RUNNER_ID
            or delivery["status"] != "DELIVERED" or file.head != delivery["commit"]):
        raise RuntimeError("INSTALLED_RESULT_IDENTITY_OR_DELIVERY_DIFFERS")
    prefix = f".runner/results/{RUNNER_ID}/{REQUEST_ID}"
    cache: dict[str, Any] = {}
    raw = remote_file(client, delivery["commit"], prefix + "/.runner-bundle.json", cache)
    if sha256(raw) != result["bundle_sha256"]:
        raise RuntimeError("REMOTE_INSTALLED_BUNDLE_DIGEST_DIFFERS")
    bundle = json.loads(raw)
    if bundle["runner_id"] != RUNNER_ID or bundle["request_id"] != REQUEST_ID:
        raise RuntimeError("REMOTE_INSTALLED_BUNDLE_SCOPE_DIFFERS")
    for item in bundle["files"]:
        raw = remote_file(client, delivery["commit"], prefix + "/" + item["path"], cache)
        if len(raw) != item["bytes"] or sha256(raw) != item["sha256"]:
            raise RuntimeError("REMOTE_INSTALLED_BUNDLE_FILE_DIFFERS")
    if json.loads(remote_file(client, delivery["commit"], prefix + "/result.json", cache)) != manifest:
        raise RuntimeError("REMOTE_INSTALLED_RESULT_DIFFERS")
    commit = client.read("/git/commits/" + delivery["commit"])
    if [parent["sha"] for parent in commit["parents"]] != [manifest["source"]["commit"]]:
        raise RuntimeError("REMOTE_RESULT_PARENT_DIFFERS_FROM_TESTED_SOURCE")
    report.update({"status": "COMPLETED_RESULT_READBACK_CONFIRMED", "operation": operation,
                   "execution": manifest["execution"], "source": manifest["source"],
                   "result_commit": delivery["commit"], "bundle_sha256": result["bundle_sha256"],
                   "verified_bundle_files": len(bundle["files"]),
                   "cleanup_events": [event for event in events if event["event"] in {
                       "ROOTLESS_CONTAINER_CLEANUP", "ROOTLESS_VOLUME_RELEASED"}],
                   "remote_slot_git_hash_verified": git_hash("blob", file.payload) == file.blob})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Explicitly enable read-only DB/remote access")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--expect", choices=("baseline", "completed"))
    parser.add_argument("--out", type=Path, help="New local evidence file; existing files are refused")
    args = parser.parse_args()
    if not args.check:
        parser.print_help()
        return 0
    if args.database is None or args.expect is None:
        parser.error("--check requires explicit --database and --expect")
    try:
        report = check(args.database, args.expect)
        raw = canonical(report)
        if args.out is not None:
            write_new(args.out, raw)
        print(raw.decode(), end="")
        return 0 if report["status"] in {"BASELINE_CONFIRMED", "COMPLETED_RESULT_READBACK_CONFIRMED"} else 2
    except Exception as error:  # noqa: BLE001 - never print private provider exception text
        print(json.dumps({"status": "READ_ONLY_CHECK_STOPPED", "code": failure_code(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
