"""A real but deliberately small local scenario. It creates ONLY a new toy repo."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from .changes import apply_changeset
from .execution import capture
from .ledger import Ledger, utcnow
from .protocol import canonical_digest

GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
       "-c", "user.name=Runner Demonstration", "-c", "user.email=runner@example.invalid"]

def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.check_output([*GIT, "-C", str(cwd), *args], env=env,
                                   stderr=subprocess.STDOUT, text=True).strip()

def demo(destination: Path | None = None) -> dict:
    root = destination or Path(tempfile.mkdtemp(prefix="cdr-reference-"))
    if destination:
        # Never run in an existing repository or silently adopt a directory.
        destination.mkdir(parents=True, exist_ok=False)
    root = root.resolve()
    repo = root / "toy-repository"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "compute.py").write_text("def total(values):\n    return sum(values)\n")
    (repo / "test_compute.py").write_text('''import unittest
from compute import total
class TotalTests(unittest.TestCase):
    def test_sum(self): self.assertEqual(total([1, 2]), 3)
    def test_empty(self): self.assertEqual(total([]), 0)
''')
    git(repo, "add", "compute.py", "test_compute.py")
    git(repo, "commit", "-m", "Initialize isolated runner fixture")
    base = git(repo, "rev-parse", "HEAD")
    ledger = Ledger(root / "state.sqlite")
    evidence = []
    for label, broken in (("correct", False), ("failing", True)):
        workspace = root / f"workspace-{label}"
        git(repo, "worktree", "add", "-b", f"feature/{label}", str(workspace), base)
        request_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        old = (workspace / "compute.py").read_bytes()
        content = "def total(values):\n    return sum(values)" + (" + 1\n" if broken else "\n")
        change = {"schema_version": "1.0", "change_id": str(uuid.uuid4()),
                  "project_id": "toy-project", "workspace_id": label, "base_head": base,
                  "files": [{"op": "upsert", "path": "compute.py",
                             "expected_sha256": hashlib.sha256(old).hexdigest(), "content_utf8": content}]}
        admission = ledger.admit(request_id, canonical_digest(change), f"toy-project/{label}")
        assert admission.execute
        apply_changeset(workspace, change, expected_head=base)
        git(workspace, "add", "compute.py")
        git(workspace, "commit", "--allow-empty", "-m", f"Freeze {label} demonstration snapshot")
        commit, tree = git(workspace, "rev-parse", "HEAD"), git(workspace, "rev-parse", "HEAD^{tree}")
        run_dir = root / "results" / run_id
        result = capture([sys.executable, "-m", "unittest", "discover", "-v"], workspace, run_dir)
        outputs = []
        for name in ("stdout", "stderr"):
            info = result["streams"][name]
            outputs.append({"output_id": name, "kind": "console", "format": "raw-bytes/v1",
                            "media_type": "application/octet-stream", "relative_path": info["raw_path"],
                            "size_bytes": info["stored_bytes"], "sha256": info["sha256"],
                            "complete": info["complete"], "truncated_reason": None if info["complete"] else "CAPTURE_LIMIT",
                            "publication": "local_only"})
        manifest = {"schema_version": "1.0", "run_id": run_id, "request_id": request_id,
                    "project_id": "toy-project", "workspace_id": label,
                    "snapshot": {"commit": commit, "tree": tree, "source_dirty_at_capture": False},
                    "operation_status": "COMPLETED",
                    "execution": {k: result[k] for k in ("outcome", "exit_code", "duration_seconds")},
                    "delivery": {"status": "NOT_ATTEMPTED", "reason": "No cloud connection in reference demo"},
                    "outputs": outputs, "created_at": utcnow()}
        (run_dir / "run-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        ledger.finish(request_id, "COMPLETED", manifest)
        duplicate = ledger.admit(request_id, canonical_digest(change), f"toy-project/{label}")
        evidence.append({"workspace": label, "manifest": str(run_dir / "run-manifest.json"),
                         "outcome": result["outcome"], "duplicate_executed": duplicate.execute})
    summary = {"root": str(root), "real_local_git": True, "real_local_tests": True,
               "cloud_calls": False, "user_projects_touched": False, "runs": evidence}
    (root / "demo-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, help="A NEW directory only; omitted means a unique temp directory")
    args = parser.parse_args()
    print(json.dumps(demo(args.destination), indent=2))

if __name__ == "__main__":
    main()
