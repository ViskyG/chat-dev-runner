"""Read-only pending-intent check before this host's approved v2 upgrade."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3

from googleapiclient.discovery import build
from runner_app.adapters.drive_auth import load_authorized_credentials
from runner_app.adapters.drive_check import _selected_installation
from runner_app.adapters.drive_docs import extract_document
from runner_app.adapters.drive_google import GoogleReadProvider
from runner_app.adapters.drive_index import LegacyInstallationManifest
from runner_app.adapters.drive_transport import isolated_request_builder
from runner_app.storage.paths import Paths


def main():
    paths = Paths.discover()
    credentials = load_authorized_credentials(paths, refresh=True)
    builder = isolated_request_builder(credentials)
    provider = GoogleReadProvider(*[
        build(api, version, credentials=credentials, requestBuilder=builder, cache_discovery=False)
        for api, version in (("drive", "v3"), ("docs", "v1"))
    ])
    manifest = LegacyInstallationManifest.from_file(
        paths.config_dir / "installation-legacy.json", _selected_installation(None))
    targets = {manifest.files["command"].file_id}
    for reference in (paths.state_dir / "drive-registrations").glob("*.json"):
        value = json.loads(reference.read_bytes())
        targets.update(value[key] for key in ("command_doc_id", "project_command_doc_id") if value.get(key))
    with sqlite3.connect(f"file:{paths.database_path}?mode=ro&immutable=1", uri=True) as connection:
        terminal = dict(connection.execute(
            "SELECT request_id,status FROM operations WHERE status NOT IN ('RUNNING','ADMITTED')"))

    def observe(document_id):
        document = provider.get_document(document_id, include_tabs_content=True)
        raw = extract_document(document, document_id, max_bytes=262144).raw_bytes
        try:
            value = json.loads(raw)
        except ValueError:
            value = {}
        ready = isinstance(value, dict) and value.get("dispatch") == "READY"
        request_id = value.get("request_id") if isinstance(value, dict) else None
        return {"document_id": document_id, "sha256": hashlib.sha256(raw).hexdigest(),
                "ready": ready, "request_id": request_id, "terminal_local_status": terminal.get(request_id),
                "pending": ready and request_id not in terminal}

    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(observe, sorted(targets)))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "cloud_writes": 0,
              "status": "PENDING_INTENT" if any(item["pending"] for item in observed) else "CLEAR",
              "mailboxes_checked": len(observed), "mailboxes": observed,
              "matching_provider_receipts_rechecked": False}
    Path(__file__).with_name("mailbox-preflight.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "mailboxes"}))
    return 0 if report["status"] == "CLEAR" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "STOPPED", "code": getattr(error, "code", type(error).__name__)}))
        raise SystemExit(1) from None
