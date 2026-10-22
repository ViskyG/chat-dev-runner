"""One explicitly authorized cleanup of the retained GP08 timeout fixture, never replay."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from runner_app.executors.compose_v2 import ComposeTaskBackend
from runner_app.executors.volume_provider import VOLUME_BASE
from runner_app.storage.database import Database
from runner_app.storage.intake_v2 import IntakeStore

request = "556bfc63-9746-4f93-becd-07d0831f31c0"
scratch = Path("/tmp/cdr-compose-qualification-_vnwh50_")
evidence = Path("/home/visky/Work/GPTRunner/chat-dev-runner-gp-v2/verification/implementation/GP-08/live-timeout-a1")
retained = scratch / "backend-runs/compose-v2" / request
assert (retained / "backend.json").read_bytes() == (evidence / "backend" / request / "backend.json").read_bytes()
proof = json.loads((retained / "backend.json").read_bytes())
assert proof["request_id"] == request
assert hashlib.sha256((retained / "resolved.json").read_bytes()).hexdigest() == proof["resolved_config_sha256"]
store = IntakeStore(Database(scratch / "state.sqlite"))
operation = store.get(request)
assert operation["status"] == "NEEDS_RECONCILIATION"
immutable_result = json.dumps(operation["result"], sort_keys=True)
backend = ComposeTaskBackend(scratch / "profiles", scratch / "backend-runs",
                            require_running=lambda _request: None, record_event=store.record_event)
profile = backend.profile("compose-python")
backend._daemon(proof["daemon_id"])
containers, network = backend._owned(proof["project"], request, proof["nonce"], profile, complete=True)
assert {name: value["Id"] for name, value in containers.items()} == proof["containers"]
assert network["Id"] == proof["network_id"]
assert all(item["State"]["Running"] is False for item in containers.values())
steps = []
for kind, identifier in [("container", identifier) for identifier in proof["containers"].values()] + [("network", proof["network_id"])]:
    backend._daemon(proof["daemon_id"])
    store.record_event(request, "COMPOSE_MANUAL_REMOVE_INTENDED", {"kind": kind, "id": identifier})
    code, out, err = backend._command([kind, "rm", identifier], timeout=15)
    assert code == 0, (kind, identifier, code)
    arguments = [kind, "ls", "--quiet", "--no-trunc"] + (["--all"] if kind == "container" else [])
    list_code, listing, _ = backend._command(arguments)
    assert list_code == 0 and identifier not in listing.decode("ascii").splitlines()
    steps.append({"kind": kind, "id": identifier, "removed": True})
    store.record_event(request, "COMPOSE_MANUAL_RESOURCE_REMOVED", steps[-1])
backend._daemon(proof["daemon_id"])
assert backend._owned(proof["project"], request, proof["nonce"], profile) == ({}, None)
store.record_event(request, "COMPOSE_MANUAL_VOLUME_RELEASE_INTENDED", {"run_id": request})
backend._release(request, profile, VOLUME_BASE / request)
assert not os.path.ismount(VOLUME_BASE / request)
absence = {"process_state": "ABSENT", "request_id": request, "daemon_id": proof["daemon_id"],
           "containers": proof["containers"], "network_id": proof["network_id"],
           "quota_mount_absent": True, "operator_scope": "authorized-disposable-timeout-fixture-only"}
store.reconcile_lane_absent(request, proof=absence)
after = store.get(request)
assert json.dumps(after["result"], sort_keys=True) == immutable_result
with store._connection() as connection:
    lanes = connection.execute("SELECT COUNT(*) FROM execution_lane").fetchone()[0]
    journal = [dict(row) for row in connection.execute("SELECT * FROM v2_journal ORDER BY event_id")]
assert lanes == 0
summary = {"schema_version": "cdr-compose-manual-reconciliation/1", "request_id": request,
           "scope": "exact stopped resources plus quota for prior timeout; no task replay",
           "resources": steps, "daemon_id": proof["daemon_id"], "quota_mount_absent": True,
           "lane_count": lanes, "historical_status": after["status"], "immutable_result_unchanged": True,
           "timestamp": datetime.now(timezone.utc).isoformat()}
for name, value in (("manual-reconciliation.json", summary), ("journal-after-reconciliation.json", journal)):
    target = evidence / name
    with target.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
print(json.dumps(summary, sort_keys=True))
