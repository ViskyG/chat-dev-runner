"""Bounded, read-only mailbox latency evidence; never prints commands or credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import runner_app
from googleapiclient.http import HttpRequest
from runner_app.adapters.drive_auth import load_authorized_credentials
from runner_app.adapters.drive_check import _build_provider
from runner_app.adapters.drive_mailbox_v2 import DocsMailboxPort, DocsProviderConfig
from runner_app.adapters.github_mailbox_v2 import GitHubMailboxPort, GitHubV2Client
from runner_app.config_v2 import parse_v2_config
from runner_app.git.github_transport import OwnerTokenFile
from runner_app.storage.paths import Paths


class ProbeDeadline(BaseException):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-root", required=True, type=Path)
    parser.add_argument("--stage", default="installed-baseline")
    parser.add_argument("--concurrent", action="store_true")
    parser.add_argument("--passes", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    os.umask(0o077)
    assert Path(runner_app.__file__).resolve().is_relative_to(args.expected_root.resolve())
    paths = Paths.discover()
    config = parse_v2_config((paths.config_dir / "runtime-v2.json").read_bytes())
    assert len(config.mailboxes) == 10
    source_root = Path(runner_app.__file__).resolve().parent
    source_files = ("adapters/drive_check.py", "adapters/drive_google.py",
                    "adapters/drive_transport.py", "adapters/drive_mailbox_v2.py",
                    "adapters/github_mailbox_v2.py")

    def source_hashes():
        return {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                for name in source_files}

    record = {"schema": "runner-read-latency/1", "stage": args.stage,
              "package_version": version("chat-dev-runner"),
              "started_at": datetime.now(timezone.utc).isoformat(),
              "mode": "concurrent-read-current-only" if args.concurrent else "sequential-read-current-only",
              "scope": "Real port reads through bounded probe scheduling; service lifecycle and ACK are not exercised",
              "passes": args.passes, "max_concurrent": 4 if args.concurrent else 1,
              "max_per_provider": 3 if args.concurrent else 1,
              "cloud_writes": 0, "service_state_writes": 0, "acks": 0,
              "configured_ports": len(config.mailboxes), "ports": [], "cycles": [],
              "source_sha256": source_hashes(),
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "provider_counts": dict(Counter(box.provider for box in config.mailboxes))}
    started = time.monotonic()
    counts = Counter()
    count_lock = threading.Lock()
    thread_counts = threading.local()
    original_execute = HttpRequest.execute

    def count(name):
        with count_lock:
            counts[name] += 1
        current = getattr(thread_counts, "value", None)
        if current is not None:
            current[name] += 1

    def counted_execute(request, *positional, **keyword):
        assert request.method == "GET", "PROBE_BLOCKED_NON_READ_GOOGLE_API"
        count("google_api_requests")
        return original_execute(request, *positional, **keyword)

    def save():
        args.output.write_text(json.dumps(record, indent=2) + "\n")

    def deadline(_signum, _frame):
        raise ProbeDeadline

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(180)
    HttpRequest.execute = counted_execute
    try:
        provider = None
        if config.google_config_file is not None:
            docs_config = DocsProviderConfig.from_file(config.google_config_file)
            # Use the application's authorized credential route; never inspect tokens.
            credentials = load_authorized_credentials(docs_config.credential_paths(paths), refresh=True)
            provider = _build_provider(credentials)
        clients = {}
        ports = []
        for box in config.mailboxes:
            if box.provider == "drive":
                assert provider is not None
                port = DocsMailboxPort(provider, document_id=box.coordinates["document_id"],
                                       tab_id=box.coordinates["tab_id"])
            else:
                if box.project_id not in clients:
                    client = GitHubV2Client(config.projects[box.project_id].repository,
                                            OwnerTokenFile(config.github_token_file))
                    original_request = client._transport.request

                    def counted_request(method, path, *, params=None, body=None,
                                        original=original_request):
                        assert method == "GET" and body is None, "PROBE_BLOCKED_NON_READ_GITHUB_API"
                        count("github_api_requests")
                        return original(method, path, params=params, body=body)

                    client._transport.request = counted_request
                    clients[box.project_id] = client
                port = GitHubMailboxPort(clients[box.project_id], branch=box.coordinates["branch"],
                                         command_path=box.coordinates["path"])
            ports.append((box, port))
        record["setup_seconds"] = round(time.monotonic() - started, 4)
        def measure(box, port, pass_number):
            port_started = time.monotonic()
            thread_counts.value = Counter()
            entry = {"binding_id": box.binding_id, "provider": box.provider, "pass": pass_number}
            try:
                observation = port.read_current()
                entry.update(status="OK", payload_bytes=len(observation.payload),
                             revision_present=bool(observation.revision))
            except Exception as error:
                code = getattr(error, "code", "READ_FAILED")
                entry.update(status="ERROR", code=code if isinstance(code, str)
                             and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "READ_FAILED")
            entry["elapsed_seconds"] = round(time.monotonic() - port_started, 4)
            entry["api_requests"] = dict(thread_counts.value)
            thread_counts.value = None
            return entry

        def completed(entry):
            record["ports"].append(entry)
            save()
            print("@@HUD " + json.dumps({"phase": "Read-only mailbox " + args.stage,
                                         "current": len(record["ports"]),
                                         "total": len(ports) * args.passes, "unit": "mailboxes"}), flush=True)

        for pass_number in range(1, args.passes + 1):
            cycle_started = time.monotonic()
            cycle_entries = []
            peak = 1
            if args.concurrent:
                pending = list(ports)
                active = {}
                active_by_provider = Counter()
                results = queue.Queue()

                def invoke(box, port):
                    results.put(measure(box, port, pass_number))

                while pending or active:
                    while len(active) < 4:
                        index = next((i for i, (box, _) in enumerate(pending)
                                      if active_by_provider[box.provider] < 3), None)
                        if index is None:
                            break
                        box, port = pending.pop(index)
                        thread = threading.Thread(target=invoke, args=(box, port), daemon=True)
                        active[box.binding_id] = thread
                        active_by_provider[box.provider] += 1
                        thread.start()
                        peak = max(peak, len(active))
                    entry = results.get()
                    active.pop(entry["binding_id"]).join()
                    active_by_provider[entry["provider"]] -= 1
                    cycle_entries.append(entry)
                    completed(entry)
            else:
                for box, port in ports:
                    entry = measure(box, port, pass_number)
                    cycle_entries.append(entry)
                    completed(entry)
            cycle_counts = Counter()
            for entry in cycle_entries:
                cycle_counts.update(entry["api_requests"])
            record["cycles"].append({"pass": pass_number, "cache": "cold" if pass_number == 1 else "warm",
                                      "elapsed_seconds": round(time.monotonic() - cycle_started, 4),
                                      "api_requests": dict(cycle_counts), "peak_concurrent": peak,
                                      "ports_ok": sum(row["status"] == "OK" for row in cycle_entries)})
        record["cycle_seconds"] = round(sum(row["elapsed_seconds"] for row in record["cycles"]), 4)
        record["status"] = "OK" if all(row["status"] == "OK" for row in record["ports"]) else "PARTIAL"
    except ProbeDeadline:
        record.update(status="TIMED_OUT", code="PROBE_180_SECOND_LIMIT")
    except Exception as error:
        code = getattr(error, "code", "PROBE_SETUP_FAILED")
        record.update(status="ERROR", code=code if isinstance(code, str)
                      and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "PROBE_SETUP_FAILED")
    finally:
        signal.alarm(0)
        record["total_seconds"] = round(time.monotonic() - started, 4)
        record["api_requests"] = dict(counts)
        record["source_unchanged"] = source_hashes() == record["source_sha256"]
        save()
    print(json.dumps({"status": record["status"], "ports_measured": len(record["ports"]),
                      "total_seconds": record["total_seconds"], "api_requests": dict(counts)}), flush=True)
    return 0 if record["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
