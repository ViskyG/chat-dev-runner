"""New composition seams only; Docker/quota here are controlled test doubles."""
from __future__ import annotations

import hashlib
import json

import pytest
from test_intake_v2 import setup
from test_rootless_v2 import Fixture

from runner_app.engine.queue_v2 import QueueWorker
from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
from runner_app.git.argv import run_git
from runner_app.git.results_v2 import ResultLimits
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.outputs.bundle_v2 import collect_bundle_outputs


def test_large_original_retained_while_link_and_incomplete_required_output_stay_invalid(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    payload = b"large relative to publication policy\n" * 20
    (out / "large.bin").write_bytes(payload)
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"must not collect")
    (out / "linked.txt").symlink_to(outside)
    declarations = [{"id": name, "path": path, "source_root": "out", "kind": "artifact",
                     "format": "binary", "required": True, "publish": "git_if_fits"}
                    for name, path in (("large", "large.bin"), ("linked", "linked.txt"))]
    outputs = collect_bundle_outputs({"out": out}, declarations, tmp_path / "results",
                                     publication_limits=ResultLimits(per_file_bytes=100))
    assert outputs.originals["large"]["large.bin"].read_bytes() == payload
    assert outputs.artifacts[0]["reason"] == "POLICY_SIZE_LIMIT"
    assert outputs.artifacts[0]["complete"]
    assert outputs.available_outputs == {"large"}
    assert "linked" in outputs.invalid_outputs
    assert not tuple((tmp_path / "results/bundle/artifacts").iterdir())


def test_directory_outputs_with_same_truncated_collector_name_do_not_overwrite(tmp_path):
    roots = {name: tmp_path / name for name in ("workspace", "out")}
    declarations = []
    for index, (name, root) in enumerate(roots.items()):
        (root / "reports").mkdir(parents=True)
        (root / "reports/result.txt").write_text(f"different original {index}")
        declarations.append({"id": "long-output-identifier-that-shares-prefix-" + str(index),
                             "source_root": name, "path": "reports", "kind": "artifact", "format": "text",
                             "required": True, "publish": "git_if_fits"})
    outputs = collect_bundle_outputs(roots, declarations, tmp_path / "results")
    assert len(outputs.artifacts) == 2
    assert len({item["id"] for item in outputs.artifacts}) == 2
    assert len({item["remote_path"] for item in outputs.artifacts}) == 2
    for item in outputs.artifacts:
        frozen = tmp_path / "results/bundle" / item["remote_path"]
        assert hashlib.sha256(frozen.read_bytes()).hexdigest() == item["sha256"]


@pytest.mark.parametrize("override", [False, True])
def test_fixed_rootless_route_uses_same_v2_source_and_result_contract(tmp_path, monkeypatch, override):
    fixture = Fixture(tmp_path, monkeypatch)
    profile = fixture.backend.profile("python-quick")
    source = tmp_path / "original"
    (source / ".runner").mkdir()
    outputs = [{"id": item.id, "source_root": "out", "path": item.path, "kind": item.kind,
                "format": item.format, "required": item.required, "publish": "git_if_fits"}
               for item in profile.collect]
    argv = list(profile.argv)
    if override:
        argv.append("unapproved-extra-test-argument")
    task = {"language": "python", "profile_id": "python-quick", "steps": [{
        "id": "pytest", "phase": "test", "argv": argv, "cwd": ".",
        "timeout_seconds": profile.timeout_seconds}], "outputs": outputs,
        "quality_gate": {"test_required": True, "minimum_executed": 1, "required_outputs": ["junit"]}}
    (source / ".runner/project.json").write_text(json.dumps({
        "protocol": "cdr-project/2", "project_id": "project-1", "tasks": {"test": task}}))
    run_git(source, ["add", ".runner/project.json"])
    run_git(source, ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                     "commit", "-m", "Declare exact approved container task"])
    resolver = SourceResolver({"project-1": SourceProject(tmp_path / "controller.git", tmp_path / "v2-runs",
                                                           {"editor": LocalWorkspace(source)})})

    class CaptureResolver:
        def resolve_current(self, *args):
            fixture.snapshot = resolver.resolve_current(*args)
            return fixture.snapshot

    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.ready(request={"request_id": fixture.request, "action": "RUN_TASK", "task": "test",
                        "source": {"kind": "local_workspace", "branch": "tested", "workspace_id": "editor"},
                        "execution": {"mode": "container", "backend_id": "rootless-docker", "expected_os": "linux",
                                      "timeout_seconds": fixture.backend.timeout_seconds("python-quick")}})
    assert coordinator.poll_once("slot", port).kind == "ACCEPTED"
    runner = NativeTaskRunner(store, CaptureResolver(), {"project-1": TaskProject(
        tmp_path / "controller.git", tmp_path / "runner-results", (), None, ("python-quick",))}, {},
        container_backends={"rootless-docker": fixture.backend})
    assert QueueWorker(store, session, runner).run_once()
    operation = store.get(fixture.request)
    if override:
        assert operation["code"] == "FIXED_CONTAINER_TASK_REQUIRED"
        assert fixture.calls == []
        assert fixture.started == []
    else:
        assert operation["status"] == "COMPLETED", operation
        manifest = operation["result"]["manifest"]
        assert manifest["execution"]["quality_gate"] == "PASSED"
        assert manifest["environment"]["mode"] == "container"
        assert manifest["environment"]["execution_os"] == "linux"
        assert manifest["environment"]["execution_arch"] == "amd64"
        assert fixture.started == ["PREPARATION", "TEST"]
        assert not tuple(fixture.base.iterdir())
        assert all(item["sha256"] for item in manifest["artifacts"])
