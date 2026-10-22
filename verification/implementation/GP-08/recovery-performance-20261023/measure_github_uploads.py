"""Offline Git-object publication comparison; no credentials or network access."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from runner_app.adapters.github_mailbox_v2 import GitHubResultTransport
from runner_app.git.results_v2 import publish_results
from runner_app.git.state_v2 import publish_state

BASELINE = "df30db3"
DELAY_SECONDS = 0.03


def load_fixture():
    spec = importlib.util.spec_from_file_location("github_upload_fixture", ROOT / "tests/test_github_mailbox_v2.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_baseline():
    raw = subprocess.run(
        ["git", "show", BASELINE + ":runner_app/adapters/github_mailbox_v2.py"],
        cwd=ROOT, check=True, capture_output=True, timeout=10,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="cdr-github-baseline-") as temporary:
        source = Path(temporary) / "baseline.py"
        source.write_bytes(raw)
        spec = importlib.util.spec_from_file_location("github_upload_baseline", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.GitHubResultTransport


def measure(transport_class, fixture_module):
    with tempfile.TemporaryDirectory(prefix="cdr-github-upload-") as temporary:
        fixture = fixture_module.Fixture(Path(temporary))
        directory = fixture.root / "results"
        directory.mkdir()
        for index in range(12):
            (directory / f"artifact-{index:02}.txt").write_text(f"Actual fake-boundary file {index}\n")
        original = fixture.api.request
        lock = threading.Lock()
        active = peak = 0

        def request(method, path, **kwargs):
            nonlocal active, peak
            blob = method == "POST" and path.endswith("/git/blobs")
            if blob:
                with lock:
                    active += 1
                    peak = max(peak, active)
            try:
                time.sleep(DELAY_SECONDS)
                return original(method, path, **kwargs)
            finally:
                if blob:
                    with lock:
                        active -= 1

        fixture.api.request = request
        transport = transport_class(fixture.client, fetch_objects=fixture.fetch)

        def report(started):
            return {
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "http_calls": len(fixture.api.calls),
                "http_methods": dict(Counter(method for method, _, _ in fixture.api.calls)),
                "blob_posts": sum(method == "POST" and path.endswith("/git/blobs")
                                  for method, path, _ in fixture.api.calls),
                "tree_posts": sum(method == "POST" and path.endswith("/git/trees")
                                  for method, path, _ in fixture.api.calls),
                "maximum_concurrent_blob_posts": peak,
            }

        started = time.monotonic()
        result = publish_results(fixture.repo, transport, tested_branch="tested", runner_id="node",
                                 request_id="request", result_dir=directory, record_intent=lambda _: None,
                                 commit_timestamp=1_700_000_000)
        assert result.status == "DELIVERED" and fixture.token_calls == 0 and active == 0
        result_report = report(started)
        assert fixture_module.git(fixture.remote, "show", f"{result.commit}:app.txt") == "tested source"
        fixture.api.calls.clear()
        peak = 0
        state = {"runner_id": "node", "request_id": "request", "project_id": "project",
                 "request_digest": "a" * 64, "updated_at": "2026-10-23T00:00:00Z",
                 "status": "COMPLETED"}
        started = time.monotonic()
        receipt = publish_state(fixture.repo, transport, branch="tested", state=state,
                                record_intent=lambda _: None)
        assert receipt.status == "DELIVERED" and fixture.token_calls == 0 and active == 0
        return {"result": result_report, "terminal_state": report(started)}


if __name__ == "__main__":
    fixture_module = load_fixture()
    value = {
        "scope": "Offline real-Git fake-API comparison; no credentials, provider calls or workload rerun.",
        "baseline_commit": BASELINE,
        "fixed_fake_api_delay_seconds": DELAY_SECONDS,
        "local_bundle_files": 12,
        "published_bundle_files_including_manifest": 13,
        "baseline": measure(load_baseline(), fixture_module),
        "candidate": measure(GitHubResultTransport, fixture_module),
        "candidate_source_sha256": hashlib.sha256(
            (ROOT / "runner_app/adapters/github_mailbox_v2.py").read_bytes()).hexdigest(),
        "limitation": "Elapsed times include fake latency and local Git; they are not live GitHub measurements.",
    }
    destination = Path(__file__).with_name("github-upload-comparison.json")
    destination.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2))
