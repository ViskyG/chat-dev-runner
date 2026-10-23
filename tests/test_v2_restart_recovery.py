"""Startup/result integration with real scratch Git and a fake Compose boundary."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_compose_v2 import Fixture as ComposeFixture
from test_intake_v2 import setup
from test_v2_result_publish import Fixture as PublicationFixture

from runner_app.engine.recovery_v2 import recover_interrupted
from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
from runner_app.executors.rootless_v2 import RootlessError, RootlessTaskResult
from runner_app.git.argv import run_git
from runner_app.git.results_v2 import LocalBareResultTransport
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.storage.paths import ensure_private_directory


class Fixture:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *, running: bool = True):
        f = self.compose = ComposeFixture(root, monkeypatch)
        self.store, self.session, coordinator, port = setup(root)
        coordinator.poll_once("slot", port)
        self.profile = f.backend.profile("compose-python")
        self.task = {"language": "python", "profile_id": self.profile.profile_id, "steps": [{
            "id": "ui", "phase": "test", "argv": list(self.profile.argv), "cwd": ".",
            "timeout_seconds": self.profile.timeout_seconds}], "outputs": [{
                "id": "junit", "source_root": "out", "path": "junit.xml", "kind": "test_result",
                "format": "junit-xml", "required": True, "publish": "git_if_fits"}],
            "quality_gate": {"test_required": True, "minimum_executed": 1, "required_outputs": ["junit"]}}
        source = root / "original"
        (source / ".runner").mkdir()
        (source / ".runner/project.json").write_text(json.dumps({
            "protocol": "cdr-project/2", "project_id": "project-1", "tasks": {"test": self.task}}))
        run_git(source, ["add", ".runner/project.json"])
        run_git(source, ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                         "commit", "-m", "Enrolled task"])
        self.receiver = root / "receiver.git"
        run_git(root, ["clone", "--bare", "--no-local", str(source), str(self.receiver)])
        run_git(self.receiver, ["branch", "commands", "tested"])
        self.transport = LocalBareResultTransport(self.receiver)
        self.resolver = SourceResolver({"project-1": SourceProject(root / "controller.git", root / "v2-runs",
                                                                    {"editor": LocalWorkspace(source)})})
        self.project = TaskProject(root / "controller.git", root / "runner-results", (),
                                   self.transport, (self.profile.profile_id,))
        self.runner = NativeTaskRunner(self.store, self.resolver, {"project-1": self.project}, {},
                                      container_backends={"rootless-compose": f.backend})
        port.ready(request={"request_id": f.request, "action": "RUN_TASK", "task": "test",
                            "source": {"kind": "local_workspace", "workspace_id": "editor", "branch": "tested"},
                            "execution": {"mode": "container", "backend_id": "rootless-compose",
                                          "expected_os": "linux", "timeout_seconds": 800}})
        assert coordinator.poll_once("slot", port).kind == "ACCEPTED"
        assert self.store.claim_next(self.session)["request_id"] == f.request
        self.snapshot = self.resolver.resolve_current("project-1", f.request, port.value["request"]["source"])
        self.task_digest = hashlib.sha256(json.dumps(self.task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if running:
            self.store.mark_running(f.request, {"source_commit": self.snapshot.source_commit,
                                               "workload_sha256": self.snapshot.workload_sha256,
                                               "source_manifest_sha256": self.snapshot.manifest_sha256,
                                               "task_digest": self.task_digest, "mode": "container",
                                               "profile_revision": self.profile.revision_sha256})
        out = root / "captured"
        out.mkdir(mode=0o700)
        (out / "junit.xml").write_text('<testsuite><testcase name="complete-before-controller-loss"/></testsuite>')
        self.result = RootlessTaskResult("UNKNOWN", None, True, out, None, None, True, 1,
                                        self.profile.profile_id, self.profile.argv, self.profile.image,
                                        "linux", "amd64", {}, "COMPOSE_RESTART_INTERRUPTED")
        self.recovered = 0
        def recover(*_args, **_kwargs):
            self.recovered += 1
            return self.result
        monkeypatch.setattr(f.backend, "recover", recover, raising=False)
        monkeypatch.setattr(f.backend, "run", lambda *_a, **_kw: pytest.fail("Workload replay"))
        self.operation = self.store.get(f.request)

    def restart(self):
        new_session = self.store.start_session("runner-1")
        recover_interrupted(self.runner, "runner-1", new_session)
        return self.store.get(self.compose.request), new_session


def test_starting_request_is_released_without_fetch_or_container_action(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch, running=False)
    monkeypatch.setattr(f.resolver, "load_existing", lambda *_a: pytest.fail("Source must not be needed"))
    operation, session = f.restart()
    assert operation["status"] == "INTERRUPTED" and operation["code"] == "RESTART_BEFORE_EXECUTION"
    assert operation["result"]["manifest"]["execution"]["outcome"] == "NOT_RUN"
    assert f.recovered == 0 and f.store.recovery_candidates("runner-1", session) == []


def test_interrupted_execution_keeps_real_reports_without_claiming_pass_or_rerunning(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch)
    operation, session = f.restart()
    assert operation["status"] == "INTERRUPTED"
    result = operation["result"]
    assert result["manifest"]["execution"] == {"outcome": "UNKNOWN", "exit_code": None,
                                                "cleanup": "CONFIRMED", "quality_gate": "INSUFFICIENT"}
    assert result["delivery"]["status"] == "DELIVERED"
    assert (Path(result["bundle_path"]) / "artifacts/junit/junit.xml").is_file()
    assert f.store.recovery_candidates("runner-1", session) == []
    recover_interrupted(f.runner, "runner-1", session)
    assert f.recovered == 1
    state_path = f".runner/state/runner-1/{f.compose.request}.json"
    state = json.loads(run_git(f.receiver, ["show", f"commands:{state_path}"]))
    assert state["status"] == "INTERRUPTED" and state["execution"]["cleanup"] == "CONFIRMED"


def test_recovery_preserves_partial_unsealed_capture_before_new_bundle(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch)
    partial = f.project.results_root / "runner-1" / f.compose.request
    ensure_private_directory(partial.parent)
    partial.mkdir(mode=0o700)
    (partial / "original-partial.log").write_text("keep exact diagnostics")
    operation, _ = f.restart()
    assert operation["status"] == "INTERRUPTED"
    retained = list(partial.parent.glob(partial.name + ".unsealed-*"))
    assert len(retained) == 1 and (retained[0] / "original-partial.log").read_text() == "keep exact diagnostics"


def test_uncertain_identity_retains_lane_and_publishes_reason(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch)
    def refuse(*_a, **_kw):
        raise RootlessError("COMPOSE_DAEMON_CHANGED")
    monkeypatch.setattr(f.compose.backend, "recover", refuse)
    operation, session = f.restart()
    assert operation["status"] == "NEEDS_RECONCILIATION" and operation["code"] == "COMPOSE_DAEMON_CHANGED"
    assert len(f.store.recovery_candidates("runner-1", session)) == 1
    assert not (f.project.results_root / "runner-1" / f.compose.request).exists()


def test_finished_bundle_and_lost_local_finish_are_reconciled_without_second_push(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch)
    f.result = replace(f.result, outcome="PASSED", exit_code=0, error_code=None)
    outcome = f.runner._seal_container(f.operation, f.snapshot, f.compose.backend, f.profile.profile_id,
                                       f.task, f.task_digest, f.operation["created_at"], "linux", f.result)
    bundle = Path(outcome.result["bundle_path"])
    original = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    real_push = f.transport.push_commit
    pushes = []
    def state_only(repo, commit, branch):
        pushes.append(branch)
        assert branch == "commands", "Must read back the already published result"
        return real_push(repo, commit, branch)
    monkeypatch.setattr(f.transport, "push_commit", state_only)
    operation, _ = f.restart()
    assert operation["status"] == "COMPLETED"
    assert operation["result"]["delivery"]["code"] == "ALREADY_DELIVERED"
    assert {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()} == original
    assert pushes == ["commands"]


def test_prior_unknown_publication_is_readback_only_when_remote_result_is_absent(tmp_path, monkeypatch):
    f = PublicationFixture(tmp_path)
    before = f.remote_head()
    transport = LocalBareResultTransport(f.receiver)
    monkeypatch.setattr(transport, "push_commit", lambda *_a: pytest.fail("Ambiguous write replay"))
    result = f.publish(transport, reconcile_only=True)
    assert result.status == "UNKNOWN" and result.code == "DELIVERY_UNKNOWN"
    assert f.intents == [] and f.remote_head() == before


def test_recovery_pins_original_source_manifest_before_backend_effects(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(f.resolver, "load_existing", lambda *_a: replace(f.snapshot, manifest_sha256="0" * 64))
    operation, session = f.restart()
    assert operation["code"] == "RECOVERY_EXECUTION_IDENTITY_MISMATCH"
    assert f.recovered == 0 and f.store.recovery_candidates("runner-1", session)


def test_reconciled_cleanup_updates_terminal_view_without_rewriting_sealed_manifest(tmp_path, monkeypatch):
    f = Fixture(tmp_path, monkeypatch)
    outcome = f.runner._seal_container(f.operation, f.snapshot, f.compose.backend, f.profile.profile_id,
                                       f.task, f.task_digest, f.operation["created_at"], "linux",
                                       replace(f.result, cleanup_verified=False))
    bundle = Path(outcome.result["bundle_path"])
    original = (bundle / "result.json").read_bytes()
    operation, session = f.restart()
    assert operation["status"] == "INTERRUPTED"
    assert operation["result"]["manifest"]["execution"]["cleanup"] == "UNCERTAIN"
    state_path = f".runner/state/runner-1/{f.compose.request}.json"
    state = json.loads(run_git(f.receiver, ["show", f"commands:{state_path}"]))
    assert state["execution"]["cleanup"] == "CONFIRMED"
    assert (bundle / "result.json").read_bytes() == original
    assert f.store.recovery_candidates("runner-1", session) == []
