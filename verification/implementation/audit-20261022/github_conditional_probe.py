"""Two authorized current reads; retain only HTTP statuses and quota metadata."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import time
import urllib.error
from pathlib import Path

import runner_app.adapters.github_mailbox_v2 as module
from runner_app.adapters.github_mailbox_v2 import GitHubMailboxPort, GitHubV2Client
from runner_app.config_v2 import parse_v2_config
from runner_app.git.github_transport import OwnerTokenFile
from runner_app.storage.paths import Paths


class ProbeDeadline(BaseException):
    pass


def main() -> int:
    os.umask(0o077)
    output = Path(__file__).with_name("github-conditional-live.json")
    source = Path(module.__file__).resolve()
    assert source.is_relative_to(Path(__file__).parents[3] / "runner_app")
    config = parse_v2_config((Paths.discover().config_dir / "runtime-v2.json").read_bytes())
    binding = next(box for box in config.mailboxes if box.provider == "github")
    client = GitHubV2Client(config.projects[binding.project_id].repository,
                            OwnerTokenFile(config.github_token_file))
    port = GitHubMailboxPort(client, branch=binding.coordinates["branch"],
                             command_path=binding.coordinates["path"])
    transport = client._transport
    original_open = transport._opener.open
    record = {"scope": "two real current reads of one registered GitHub port; no ACK or writes",
              "binding_id": binding.binding_id, "cloud_writes": 0, "service_state_writes": 0,
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "passes": [], "http": []}
    pass_number = 0

    def save():
        output.write_text(json.dumps(record, indent=2) + "\n")

    def observed(request, response):
        path = request.full_url
        endpoint = ("reference" if "/git/ref/heads/" in path else "git_object"
                    if "/git/" in path else "repository_identity")
        values = {}
        for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                     "retry-after", "x-poll-interval"):
            value = response.headers.get(name)
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
                values[name] = value
        record["http"].append({"pass": pass_number, "endpoint": endpoint,
                               "status": int(response.status),
                               "conditional_sent": bool(request.get_header("If-none-match")),
                               "etag_present": bool(response.headers.get("etag")),
                               "quota_headers": values})
        save()

    def open_read(request, **kwargs):
        assert request.method == "GET", "PROBE_REFUSED_WRITE"
        try:
            response = original_open(request, **kwargs)
        except urllib.error.HTTPError as error:
            observed(request, error)
            raise
        observed(request, response)
        return response

    def deadline(_signum, _frame):
        raise ProbeDeadline

    transport._opener.open = open_read
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(90)
    started = time.monotonic()
    try:
        for pass_number in (1, 2):
            beginning = time.monotonic()
            observation = port.read_current()
            record["passes"].append({"pass": pass_number, "status": "OK",
                                      "elapsed_seconds": round(time.monotonic() - beginning, 4),
                                      "payload_bytes": len(observation.payload),
                                      "revision_present": bool(observation.revision)})
            save()
            print("@@HUD " + json.dumps({"phase": "GitHub conditional read proof", "current": pass_number,
                                         "total": 2, "unit": "reads"}), flush=True)
        record["status"] = "OK"
    except ProbeDeadline:
        record.update(status="TIMED_OUT", code="PROBE_90_SECOND_LIMIT")
    except Exception as error:
        code = getattr(error, "code", "PROBE_FAILED")
        record.update(status="ERROR", code=code if isinstance(code, str)
                      and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "PROBE_FAILED")
    finally:
        signal.alarm(0)
        record["elapsed_seconds"] = round(time.monotonic() - started, 4)
        record["source_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == record["source_sha256"]
        save()
    print(json.dumps({"status": record["status"], "http_responses": len(record["http"]),
                      "not_modified": sum(row["status"] == 304 for row in record["http"])}), flush=True)
    return 0 if record["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
