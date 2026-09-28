"""The permanent quota port allocates only for an admitted exact profile."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

import pytest

from runner_app.executors.preparation_handoff import _scan, _tree_digest
from runner_app.executors.preparation_plan import PreparationGrant
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.volume_provider import (
    VOLUME_BASE,
    HardQuotaVolumeProvider,
    VolumeProviderError,
    load_python_quick_grant,
)
from runner_app.git.templates import starter_files
from runner_app.storage.run_evidence import EvidenceError

ROOT = Path(__file__).resolve().parents[1]
REQUEST = "00000000-0000-4000-8000-000000000001"


class _FakeHandoffConnection:
    def __init__(self, ledger: FakeLedger) -> None:
        self.ledger = ledger
        self.query = ""

    def execute(self, query: str, arguments: tuple[str, ...]) -> _FakeHandoffConnection:
        assert arguments == (REQUEST,)
        assert ("FROM preparation_handoffs h JOIN preparation_results p" in query
                or "FROM journal" in query)
        self.query = query
        return self

    def fetchone(self) -> dict[str, Any] | None:
        assert "FROM preparation_handoffs" in self.query
        return self.ledger.handoff_row

    def fetchall(self) -> list[dict[str, str]]:
        assert "FROM journal" in self.query
        return [{"detail_json": json.dumps(marker)} for marker in self.ledger.clear_markers]

    def close(self) -> None:
        pass


class FakeLedger:
    def __init__(self, status: str = "RUNNING", action: str = "VERIFY",
                 profile_id: str = "python-quick") -> None:
        self.database = SimpleNamespace(
            path=Path("/tmp/volume-policy-state.sqlite"),
            connect=lambda: _FakeHandoffConnection(self),
        )
        self.handoff_row: dict[str, Any] | None = None
        self.clear_markers: list[dict[str, str]] = []
        self.preparation_result: dict[str, str] | None = None
        self.run_manifest: dict[str, Any] | None = None
        self.operation: dict[str, Any] = {
            "status": status,
            "command": {"action": action, "params": {"profile_id": profile_id}},
        }

    def get(self, request_id: str) -> dict[str, Any] | None:
        assert request_id == REQUEST
        return self.operation

    def get_preparation_result(self, request_id: str) -> dict[str, str] | None:
        assert request_id == REQUEST
        return self.preparation_result

    def get_run(self, request_id: str) -> dict[str, Any] | None:
        assert request_id == REQUEST
        return self.run_manifest

    def journal(self, request_id: str, step_id: str, event_type: str,
                detail: dict[str, str]) -> None:
        assert request_id == REQUEST and step_id == "absent-test-build-clear"
        assert event_type == "ABSENT_TEST_BUILD_CLEAR_INTENDED"
        self.clear_markers.append(detail)


def _provider(tmp_path: Path, ledger: FakeLedger) -> HardQuotaVolumeProvider:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir(mode=0o700)
    hashes: dict[str, str] = {}
    for profile_id in ("python-prepare", "python-quick"):
        reference = json.loads((ROOT / "config" / f"profile.{profile_id}.json").read_text())
        reference.update(installed=True, approved=True)
        raw = (json.dumps(reference, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path = profile_dir / f"{profile_id}.json"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        hashes[profile_id] = hashlib.sha256(raw).hexdigest()
    profiles = ProfileRegistry(profile_dir, hashes)
    grant = PreparationGrant(
        template_id="python-basic", execution_profile_id="python-quick",
        execution_revision_sha256=hashes["python-quick"],
        preparation_profile_id="python-prepare",
        preparation_revision_sha256=hashes["python-prepare"],
        lockfile_path="requirements.lock",
    )
    preparation = SimpleNamespace(
        ledger=ledger, profiles=profiles, evidence_root=tmp_path / "prep-evidence",
    )
    return HardQuotaVolumeProvider(
        ledger, profiles, preparation, grant,
        preparation_runs_root=tmp_path / "preparation-runs",
    )  # type: ignore[arg-type]


def test_admitted_verify_allocates_distinct_bounded_phase_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(tmp_path, FakeLedger())
    calls: list[tuple[str, str, str]] = []

    def fake_helper(action: str, run_id: str, size_mb: str) -> dict[str, object]:
        calls.append((action, run_id, size_mb))
        return {"status": "ALLOCATED", "run_id": run_id,
                "size_mb": int(size_mb), "path": str(VOLUME_BASE / run_id)}

    verified: list[tuple[Path, str, int]] = []

    def fake_verify(path: Path, run_id: str, budget: int) -> Path:
        verified.append((path, run_id, budget))
        return path

    monkeypatch.setattr(HardQuotaVolumeProvider, "_helper", staticmethod(fake_helper))
    monkeypatch.setattr("runner_app.executors.volume_provider._verified_run_volume", fake_verify)
    assert provider.volume_for_run(REQUEST) == VOLUME_BASE / REQUEST
    prepared = provider.preparation_for_run(REQUEST, "python-quick")
    prep_id = str(uuid5(UUID(REQUEST), "preparation"))
    assert prepared.preparation_volume == VOLUME_BASE / prep_id
    assert prepared.grant is provider.grant
    assert prepared.preparation_run_root.name == prep_id
    assert calls == [("allocate", REQUEST, "256"), ("allocate", prep_id, "64")]
    assert verified == [(VOLUME_BASE / REQUEST, REQUEST, 256 * 1024 * 1024),
                        (VOLUME_BASE / prep_id, prep_id, 64 * 1024 * 1024)]


@pytest.mark.parametrize(
    ("status", "action", "profile_id", "expected"),
    [
        ("BLOCKED", "VERIFY", "python-quick", "OPERATION_NOT_RUNNING"),
        ("RUNNING", "CREATE_PROJECT", "python-quick", "PROFILE_NOT_GRANTED"),
        ("RUNNING", "VERIFY", "other-profile", "PROFILE_NOT_GRANTED"),
    ],
)
def test_no_quota_mount_before_admission_or_for_wrong_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    status: str, action: str, profile_id: str, expected: str,
) -> None:
    provider = _provider(tmp_path, FakeLedger(status, action, profile_id))
    monkeypatch.setattr(HardQuotaVolumeProvider, "_helper", staticmethod(
        lambda *_args: pytest.fail("helper called before admission"),
    ))
    with pytest.raises(VolumeProviderError) as raised:
        provider.volume_for_run(REQUEST)
    assert raised.value.code == expected


def test_owner_grant_loader_rechecks_private_receipt_and_profile_revisions(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path, FakeLedger())
    profile_dir = tmp_path / "profiles"
    hashes = {
        name: provider.profiles.require(name).revision_sha256
        for name in ("python-prepare", "python-quick")
    }
    receipt = {
        "schema_version": "1.0", "installed": True,
        "profile_dir": str(profile_dir),
        "profile_revision_sha256": hashes,
        "preparation_grant": asdict(provider.grant),
        "starter_lockfile_sha256": hashlib.sha256(
            starter_files("python-basic", "owner-grant-test")["requirements.lock"],
        ).hexdigest(),
        "image": provider.profiles.require("python-quick").image,
    }
    receipt_path = profile_dir / "python-quick-grant.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    os.chmod(receipt_path, 0o600)
    profiles, grant = load_python_quick_grant(profile_dir)
    assert profiles.available_ids() == frozenset({"python-prepare", "python-quick"})
    assert grant == provider.grant
    receipt["starter_lockfile_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(VolumeProviderError) as raised:
        load_python_quick_grant(profile_dir)
    assert raised.value.code == "OWNER_PROFILE_UNAVAILABLE"


class _FakeEvidence:
    def __init__(self, database: object, *, changed: bool = False) -> None:
        self.database = database
        self.changed = changed
        self.checked: list[str] = []

    def output_retention_index(self, run_id: str) -> list[dict[str, str]]:
        assert run_id == REQUEST
        return [{"output_id": "junit", "state": "AVAILABLE"}]

    def open_output(self, run_id: str, output_id: str) -> object:
        assert run_id == REQUEST
        if self.changed:
            raise EvidenceError("OUTPUT_CHANGED", "frozen bytes changed")
        self.checked.append(output_id)
        return SimpleNamespace(close=lambda: None)


class _FakePreparationEvidence:
    def __init__(self, _database: object, _root: Path) -> None:
        self.checked: list[str] = []

    def receipt(self, run_id: str) -> dict[str, str]:
        return {"request_id": REQUEST, "run_id": run_id, "outcome": "PASSED"}

    def retention_index(self, _run_id: str) -> list[dict[str, str]]:
        return [{"output_id": "stdout-raw", "state": "AVAILABLE"}]

    def open_output(self, _run_id: str, output_id: str) -> object:
        self.checked.append(output_id)
        return SimpleNamespace(close=lambda: None)


def _retirement_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                     ) -> tuple[HardQuotaVolumeProvider, FakeLedger, _FakeEvidence,
                                Path, Path, list[str]]:
    ledger = FakeLedger("COMPLETED")
    ledger.operation["reconcile_required"] = False
    prep_id = str(uuid5(UUID(REQUEST), "preparation"))
    ledger.preparation_result = {"run_id": prep_id, "outcome": "PASSED",
                                 "evidence_sha256": "a" * 64}
    ledger.run_manifest = {"run_id": REQUEST}
    provider = _provider(tmp_path, ledger)
    base = tmp_path / "volumes"
    base.mkdir()
    prep_mount = base / prep_id
    test_mount = base / REQUEST
    prep_mount.mkdir()
    test_mount.mkdir()
    (prep_mount / "out").mkdir()
    (prep_mount / "out" / "stdout.bin").write_bytes(b"prep")
    (test_mount / "out").mkdir()
    (test_mount / "out" / "junit.xml").write_bytes(b"test")
    monkeypatch.setattr("runner_app.executors.volume_provider.VOLUME_BASE", base)
    monkeypatch.setattr(
        "runner_app.executors.volume_provider._verified_run_volume",
        lambda path, run_id, _budget: path if path.name == run_id else pytest.fail("wrong slot"),
    )
    monkeypatch.setattr("runner_app.executors.volume_provider._verified_retired_volume",
                        lambda path, run_id, _budget: path if path.name == run_id else pytest.fail("wrong slot"))
    monkeypatch.setattr("runner_app.executors.volume_provider.PreparationEvidenceStore",
                        _FakePreparationEvidence)
    intents = {"PREPARATION": {"run_id": prep_id}, "TEST": {"run_id": REQUEST}}
    proofs = {phase: {"run_id": value["run_id"], "state": "REMOVED"}
              for phase, value in intents.items()}
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (intents, proofs))
    released: list[str] = []

    def fake_helper(action: str, run_id: str) -> dict[str, str]:
        assert action == "release"
        assert not any((base / run_id).iterdir())
        released.append(run_id)
        (base / run_id).rmdir()
        return {"status": "RELEASED", "run_id": run_id}

    monkeypatch.setattr(provider, "_helper", fake_helper)
    evidence = _FakeEvidence(ledger.database)
    return provider, ledger, evidence, prep_mount, test_mount, released


def test_retirement_verifies_both_phase_evidence_then_reuses_mount_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    outside = tmp_path / "outside"
    outside.write_bytes(b"retain")
    (test_mount / "out" / "outside-link").symlink_to(outside)
    assert provider.release_evidenced(REQUEST, evidence) == (prep_mount.name, test_mount.name)
    assert released == [prep_mount.name, test_mount.name]
    assert outside.read_bytes() == b"retain"
    assert not prep_mount.exists() and not test_mount.exists()
    assert evidence.checked == ["junit"]
    assert provider.release_evidenced(REQUEST, evidence) == ()


@pytest.mark.parametrize("uncertain", ["container", "preparation", "run", "changed-output"])
def test_retirement_preserves_uncertain_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: str,
) -> None:
    provider, ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    if uncertain == "container":
        prep_id = prep_mount.name
        monkeypatch.setattr(provider, "_phase_state", lambda _request: (
            {"PREPARATION": {"run_id": prep_id}}, {},
        ))
    elif uncertain == "preparation":
        ledger.preparation_result = None
    elif uncertain == "run":
        ledger.run_manifest = None
    else:
        evidence.changed = True
    with pytest.raises(VolumeProviderError):
        provider.release_evidenced(REQUEST, evidence)
    assert released == []
    assert (prep_mount / "out" / "stdout.bin").read_bytes() == b"prep"
    assert (test_mount / "out" / "junit.xml").read_bytes() == b"test"


def test_absent_preparation_retires_only_empty_phase_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    prep_id = prep_mount.name
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (
        {"PREPARATION": {"run_id": prep_id}},
        {"PREPARATION": {"run_id": prep_id, "state": "ABSENT"}},
    ))
    ledger.preparation_result = None
    ledger.run_manifest = None
    (test_mount / "out" / "junit.xml").unlink()
    (test_mount / "out").rmdir()
    with pytest.raises(VolumeProviderError) as has_data:
        provider.release_evidenced(REQUEST, evidence)
    assert has_data.value.code == "ABSENT_VOLUME_HAS_DATA"
    assert released == []
    (prep_mount / "out" / "stdout.bin").unlink()
    assert provider.release_evidenced(REQUEST, evidence) == (prep_id, REQUEST)
    assert released == [prep_id, REQUEST]


def test_unstarted_test_volume_retires_empty_build_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    prep_id = prep_mount.name
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (
        {"PREPARATION": {"run_id": prep_id}},
        {"PREPARATION": {"run_id": prep_id, "state": "REMOVED"}},
    ))
    (test_mount / "out" / "junit.xml").unlink()
    (test_mount / "out").rmdir()
    (test_mount / "build").mkdir(mode=0o700)
    assert provider.release_evidenced(REQUEST, evidence) == (prep_id, REQUEST)
    assert released == [prep_id, REQUEST]
    assert not prep_mount.exists() and not test_mount.exists()


@pytest.mark.parametrize("unsafe", ["nonempty", "unknown", "symlink", "wrong_mode"])
def test_unstarted_test_volume_preserves_unsafe_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str,
) -> None:
    provider, _ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    prep_id = prep_mount.name
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (
        {"PREPARATION": {"run_id": prep_id}},
        {"PREPARATION": {"run_id": prep_id, "state": "REMOVED"}},
    ))
    (test_mount / "out" / "junit.xml").unlink()
    (test_mount / "out").rmdir()
    build = test_mount / "build"
    outside = tmp_path / "outside"
    if unsafe == "nonempty":
        build.mkdir(mode=0o700)
        (build / "keep.txt").write_bytes(b"keep")
    elif unsafe == "unknown":
        build.mkdir(mode=0o700)
        (test_mount / "unknown.txt").write_bytes(b"keep")
    elif unsafe == "symlink":
        outside.mkdir()
        build.symlink_to(outside, target_is_directory=True)
    else:
        build.mkdir(mode=0o755)
        build.chmod(0o755)
    with pytest.raises(VolumeProviderError) as refused:
        provider.release_evidenced(REQUEST, evidence)
    assert refused.value.code == "UNSTARTED_VOLUME_NOT_EMPTY"
    assert released == [prep_id]
    assert test_mount.exists() and build.is_symlink() == (unsafe == "symlink")
    if unsafe == "nonempty":
        assert (build / "keep.txt").read_bytes() == b"keep"
    if unsafe == "unknown":
        assert (test_mount / "unknown.txt").read_bytes() == b"keep"
    if unsafe == "symlink":
        assert outside.is_dir()


def test_absent_phase_with_recorded_result_is_not_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    prep_id = prep_mount.name
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (
        {"PREPARATION": {"run_id": prep_id}},
        {"PREPARATION": {"run_id": prep_id, "state": "ABSENT"}},
    ))
    with pytest.raises(VolumeProviderError) as has_result:
        provider.release_evidenced(REQUEST, evidence)
    assert has_result.value.code == "ABSENT_PHASE_HAS_RESULT"
    assert released == []
    assert prep_mount.exists() and test_mount.exists()


def test_absent_test_phase_requires_immutable_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    prep_id = prep_mount.name
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (
        {"PREPARATION": {"run_id": prep_id}, "TEST": {"run_id": REQUEST}},
        {"PREPARATION": {"run_id": prep_id, "state": "REMOVED"},
         "TEST": {"run_id": REQUEST, "state": "ABSENT"}},
    ))
    ledger.run_manifest = None
    (test_mount / "out" / "junit.xml").unlink()
    (test_mount / "build").mkdir()
    (test_mount / "build" / "dependency.whl").write_bytes(b"staged")
    with pytest.raises(VolumeProviderError) as missing_handoff:
        provider.release_evidenced(REQUEST, evidence)
    assert missing_handoff.value.code == "ABSENT_TEST_HANDOFF_UNVERIFIED"
    assert released == [prep_id]
    assert (test_mount / "build" / "dependency.whl").read_bytes() == b"staged"


def _attested_absent_test_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[HardQuotaVolumeProvider, FakeLedger, _FakeEvidence, Path, Path, list[str]]:
    provider, ledger, evidence, prep_mount, test_mount, released = _retirement_case(
        tmp_path, monkeypatch,
    )
    prep_id = prep_mount.name
    monkeypatch.setattr(provider, "_phase_state", lambda _request: (
        {"PREPARATION": {"run_id": prep_id}, "TEST": {"run_id": REQUEST}},
        {"PREPARATION": {"run_id": prep_id, "state": "REMOVED"},
         "TEST": {"run_id": REQUEST, "state": "ABSENT"}},
    ))
    ledger.run_manifest = None
    (test_mount / "out" / "junit.xml").unlink()
    (test_mount / "out").chmod(0o700)
    (test_mount / "tmp").mkdir(mode=0o700)
    test_mount.chmod(0o700)
    build = test_mount / "build"
    build.mkdir(mode=0o700)
    package = build / "package"
    package.mkdir(mode=0o700)
    artifact = package / "dependency.whl"
    artifact.write_bytes(b"exact copied build")
    artifact.chmod(0o600)
    entries, _ = _scan(build)
    profile = provider.profiles.require(provider.grant.preparation_profile_id)
    snapshot_id = "00000000-0000-4000-8000-000000000002"
    ledger.handoff_row = {
        "preparation_run_id": prep_id,
        "snapshot_id": snapshot_id,
        "test_run_id": REQUEST,
        "plan_identity_sha256": "b" * 64,
        "preparation_profile_revision_sha256": provider.grant.preparation_revision_sha256,
        "image": profile.image,
        "build_tree_sha256": _tree_digest(entries),
        "evidence_sha256": ledger.preparation_result["evidence_sha256"],
        "test_volume_path": str(test_mount),
        "result_run_id": prep_id,
        "result_snapshot_id": snapshot_id,
        "result_plan_identity_sha256": "b" * 64,
        "result_profile_revision_sha256": provider.grant.preparation_revision_sha256,
        "result_image": profile.image,
        "outcome": "PASSED",
        "exit_code": 0,
        "capture_complete": 1,
        "cleanup_verified": 1,
        "result_evidence_sha256": ledger.preparation_result["evidence_sha256"],
    }
    return provider, ledger, evidence, prep_mount, test_mount, released


def test_absent_test_phase_retires_only_attested_copied_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ledger, evidence, prep_mount, test_mount, released = (
        _attested_absent_test_case(tmp_path, monkeypatch)
    )
    assert provider.release_evidenced(REQUEST, evidence) == (prep_mount.name, REQUEST)
    assert released == [prep_mount.name, REQUEST]
    assert not prep_mount.exists() and not test_mount.exists()


@pytest.mark.parametrize("unsafe", ["changed-build", "unexpected-output", "symlink", "wrong-mode"])
def test_absent_test_phase_preserves_unattested_or_unknown_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str,
) -> None:
    provider, ledger, evidence, prep_mount, test_mount, released = (
        _attested_absent_test_case(tmp_path, monkeypatch)
    )
    artifact = test_mount / "build" / "package" / "dependency.whl"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    if unsafe == "changed-build":
        artifact.write_bytes(b"changed copied build")
    elif unsafe == "unexpected-output":
        (test_mount / "out" / "unexpected.log").write_bytes(b"keep")
    elif unsafe == "wrong-mode":
        artifact.chmod(0o644)
    else:
        artifact.unlink()
        artifact.symlink_to(outside)
    with pytest.raises(VolumeProviderError) as refused:
        provider.release_evidenced(REQUEST, evidence)
    assert refused.value.code == (
        "ABSENT_VOLUME_HAS_DATA" if unsafe == "unexpected-output"
        else "ABSENT_TEST_BUILD_CHANGED"
    )
    assert released == [prep_mount.name]
    assert test_mount.is_dir()
    assert outside.read_bytes() == b"outside"
    assert ledger.handoff_row is not None


def test_absent_test_clear_retries_after_partial_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, ledger, evidence, prep_mount, test_mount, released = (
        _attested_absent_test_case(tmp_path, monkeypatch)
    )
    clear = HardQuotaVolumeProvider._clear_verified_mount
    artifact = test_mount / "build" / "package" / "dependency.whl"

    def interrupt_once(path: Path, run_id: str, budget: int) -> None:
        if run_id == REQUEST:
            artifact.unlink()
            raise VolumeProviderError("SIMULATED_INTERRUPTION")
        clear(path, run_id, budget)

    monkeypatch.setattr(HardQuotaVolumeProvider, "_clear_verified_mount",
                        staticmethod(interrupt_once))
    with pytest.raises(VolumeProviderError) as interrupted:
        provider.release_evidenced(REQUEST, evidence)
    assert interrupted.value.code == "SIMULATED_INTERRUPTION"
    assert released == [prep_mount.name]
    assert len(ledger.clear_markers) == 1
    assert not artifact.exists() and test_mount.exists()

    monkeypatch.setattr(HardQuotaVolumeProvider, "_clear_verified_mount",
                        staticmethod(clear))
    assert provider.release_evidenced(REQUEST, evidence) == (REQUEST,)
    assert released == [prep_mount.name, REQUEST]
    assert not test_mount.exists()


@pytest.mark.parametrize("changed", ["volume_path_sha256", "mount_device"])
def test_absent_test_clear_marker_cannot_authorize_another_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    provider, ledger, evidence, prep_mount, test_mount, released = (
        _attested_absent_test_case(tmp_path, monkeypatch)
    )
    info = test_mount.stat()
    marker = {
        "run_id": REQUEST,
        "preparation_run_id": prep_mount.name,
        "build_tree_sha256": ledger.handoff_row["build_tree_sha256"],
        "volume_path_sha256": hashlib.sha256(str(test_mount).encode("utf-8")).hexdigest(),
        "mount_device": str(info.st_dev),
        "mount_inode": str(info.st_ino),
    }
    marker[changed] = "0" * 64
    ledger.clear_markers.append(marker)
    with pytest.raises(VolumeProviderError) as wrong_volume:
        provider.release_evidenced(REQUEST, evidence)
    assert wrong_volume.value.code == "ABSENT_TEST_CLEAR_INTENT_INVALID"
    assert released == [prep_mount.name]
    assert (test_mount / "build" / "package" / "dependency.whl").is_file()
