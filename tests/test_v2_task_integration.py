"""Real finite Python/Git boundary; no live providers or installed service changes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_intake_v2 import accept, setup

from runner_app.config_v2 import NativeProfile
from runner_app.engine.queue_v2 import QueueWorker
from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
from runner_app.git.argv import run_git
from runner_app.git.results_v2 import LocalBareResultTransport
from runner_app.git.source_v2 import SourceProject, SourceResolver


def git(path: Path, *args: str) -> str:
    return run_git(path, list(args)).strip()


def commit(path: Path, message: str) -> str:
    git(path, "add", "--all")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", message)
    return git(path, "rev-parse", "HEAD")


class Fixture:
    def __init__(self, root: Path, *, failing: bool = False, offline: bool = False) -> None:
        self.store, self.session, self.coordinator, self.port = setup(root)
        self.coordinator.poll_once("slot", self.port)
        self.source = root / "author"
        self.source.mkdir()
        git(self.source, "init", "--initial-branch=feature/test")
        (self.source / ".runner").mkdir()
        (self.source / "tests").mkdir()
        manifest = {"protocol": "cdr-project/2", "project_id": "project-1", "tasks": {"test": {
            "language": "python", "profile_id": "native-python", "steps": [{
                "id": "pytest", "phase": "test", "cwd": ".", "timeout_seconds": 30,
                "argv": ["{python}", "-m", "pytest", "-q", "tests", "--junitxml", "{output:tests}"]}],
            "outputs": [
                {"id": "tests", "source_root": "out", "path": "tests.xml", "kind": "test_result",
                 "format": "junit-xml", "required": True, "publish": "git_if_fits"},
                {"id": "calculation", "source_root": "out", "path": "calculation.json", "kind": "artifact",
                 "format": "json", "required": True, "publish": "git_if_fits"}],
            "quality_gate": {"test_required": True, "minimum_executed": 1,
                             "required_outputs": ["tests", "calculation"]}}}}
        (self.source / ".runner/project.json").write_text(json.dumps(manifest))
        (self.source / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        (self.source / "tests/test_calculator.py").write_text(
            "import json, os\nfrom pathlib import Path\nfrom calculator import add\n"
            "def test_add():\n"
            "    value = add(2, 3)\n"
            "    (Path(os.environ['CDR_OUTPUT_DIR']) / 'calculation.json').write_text(json.dumps({'value':value}))\n"
            f"    assert value == {6 if failing else 5}\n")
        # python -m pytest preserves this worktree as the import root.
        self.initial = commit(self.source, "Finite calculator fixture")
        self.receiver, self.repo = root / "receiver.git", root / "controller.git"
        git(root, "clone", "--bare", "--no-local", str(self.source), str(self.receiver))
        git(root, "clone", "--bare", "--no-local", str(self.receiver), str(self.repo))
        git(self.source, "remote", "add", "receiver", str(self.receiver))
        self.transport = LocalBareResultTransport(self.receiver)
        self.resolver = SourceResolver({"project-1": SourceProject(self.repo, root / "runs",
                                                                   fetch_head=self.transport.fetch_head)})
        self.results = root / "runner-results"

        class Offline:
            def fetch_head(self, *args):
                raise OSError("provider unavailable")

            def push_commit(self, *args):
                raise AssertionError("Must not push without a head")

        self.project = TaskProject(self.repo, self.results, ("native-python",),
                                   Offline() if offline else self.transport)
        self.runner = NativeTaskRunner(self.store, self.resolver, {"project-1": self.project}, {
            "native-python": NativeProfile({"python": (Path(sys.executable),)}, (),
                                            {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, 60, 1048576)})
        self.worker = QueueWorker(self.store, self.session, self.runner)
        self.port.value["request"]["execution"]["timeout_seconds"] = 60

    def run(self) -> dict:
        request_id = accept(self.coordinator, self.port)
        assert self.worker.run_once()
        return self.store.get(request_id)


@pytest.mark.parametrize("failing", [False, True])
def test_real_pytest_reports_and_program_output_publish_in_tested_branch(tmp_path, failing):
    fixture = Fixture(tmp_path, failing=failing)
    request_id = accept(fixture.coordinator, fixture.port)
    # A queued request resolves the branch at worker start, not admission time.
    (fixture.source / "queued.txt").write_text("arrived after ACK\n")
    current = commit(fixture.source, "Advance after admission")
    git(fixture.source, "push", "receiver", "HEAD:refs/heads/feature/test")
    index = (fixture.source / ".git/index").read_bytes()
    assert fixture.worker.run_once()
    operation = fixture.store.get(request_id)
    assert operation["status"] == "COMPLETED", operation
    result = operation["result"]
    manifest = result["manifest"]
    assert manifest["source"]["commit"] == current
    expected = "FAILED" if failing else "PASSED"
    assert manifest["execution"]["outcome"] == expected
    assert manifest["execution"]["quality_gate"] == expected
    assert result["delivery"]["status"] == "DELIVERED"
    assert (fixture.source / ".git/index").read_bytes() == index
    assert git(fixture.source, "rev-parse", "HEAD") == current
    head = git(fixture.receiver, "rev-parse", "feature/test")
    changed = git(fixture.receiver, "diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines()
    assert changed and all(path.startswith(f".runner/results/runner-1/{request_id}/") for path in changed)
    assert git(fixture.receiver, "show", "feature/test:queued.txt") == "arrived after ACK"
    bundle = Path(result["bundle_path"])
    facts = json.loads((bundle / "tests.json").read_text())
    assert facts["tests"]["status"] == "PARSED"
    artifacts = manifest["artifacts"]
    assert all(Path(item["local_path"]).is_file() for item in artifacts)
    assert json.loads(next(Path(item["local_path"]) for item in artifacts
                           if item["id"] == "calculation").read_text()) == {"value": 5}
    assert fixture.store.claim_next(fixture.session) is None


@pytest.mark.parametrize("change_bundle", [False, True])
def test_delivery_only_reuses_sealed_evidence_without_resolving_or_running(tmp_path, change_bundle):
    fixture = Fixture(tmp_path, offline=True)
    original = fixture.run()
    assert original["status"] == "COMPLETED", original
    assert original["result"]["delivery"]["status"] == "LOCAL_ONLY"
    if change_bundle:
        (Path(original["result"]["bundle_path"]) / "summary.md").write_text("Changed after seal")
    fixture.runner.projects["project-1"] = TaskProject(fixture.repo, fixture.results, ("native-python",),
                                                       fixture.transport)

    class NoNewSource:
        def resolve_current(self, *args):
            raise AssertionError("Redelivery must not rerun source or tests")

    fixture.runner.resolver = NoNewSource()
    fixture.port.ready(request={"request_id": None, "action": "REDELIVER",
                                "target_request_id": original["request_id"]})
    admitted = fixture.coordinator.poll_once("slot", fixture.port)
    assert admitted.kind == "ACCEPTED"
    assert fixture.worker.run_once()
    operation = fixture.store.get(admitted.request_id)
    if change_bundle:
        assert operation["status"] == "REJECTED"
        assert operation["code"] == "RESULT_BUNDLE_CHANGED"
        assert git(fixture.receiver, "rev-parse", "feature/test") == fixture.initial
    else:
        assert operation["status"] == "COMPLETED"
        assert operation["result"]["delivery"]["status"] == "DELIVERED"
        assert fixture.store.get(original["request_id"])["result"]["delivery"]["status"] == "DELIVERED"


def test_one_second_shell_budget_reaches_executor_and_keeps_raw_logs(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.runner.profiles["native-python"] = NativeProfile({}, ("sh",), {}, 1, 1024)
    fixture.port.value["request"] = {
        "request_id": None, "action": "SHELL", "shell": {"dialect": "sh", "script": "printf 'short run\\n'"},
        "source": {"kind": "github_branch", "branch": "feature/test"},
        "execution": {"mode": "native", "expected_os": "linux", "timeout_seconds": 1}}
    operation = fixture.run()
    assert operation["status"] == "COMPLETED", operation
    assert operation["result"]["manifest"]["execution"]["outcome"] == "PASSED"
    stdout = next(item for item in operation["result"]["manifest"]["artifacts"] if "stdout" in item["id"])
    assert Path(stdout["local_path"]).read_bytes() == b"short run\n"


def test_project_output_name_cannot_collide_with_controller_log_ids(tmp_path):
    fixture = Fixture(tmp_path)
    path = fixture.source / ".runner/project.json"
    manifest = json.loads(path.read_text())
    task = manifest["tasks"]["test"]
    task["outputs"].append({**task["outputs"][1], "id": "phase-0-stdout"})
    path.write_text(json.dumps(manifest))
    commit(fixture.source, "Output name also used by controller logs")
    git(fixture.source, "push", "receiver", "HEAD:refs/heads/feature/test")
    operation = fixture.run()
    assert operation["status"] == "COMPLETED", operation
    artifacts = operation["result"]["manifest"]["artifacts"]
    assert len({item["id"] for item in artifacts}) == len(artifacts)
    assert any(item["id"] == "phase-0-stdout" and item["kind"] == "artifact" for item in artifacts)
    assert any(item["id"] != "phase-0-stdout" and item["kind"] == "console" for item in artifacts)
