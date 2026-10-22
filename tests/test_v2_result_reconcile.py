"""Restart publication readback must preserve prior ref uncertainty without writes."""
from __future__ import annotations

from pathlib import Path

import pytest
from test_v2_result_publish import Fixture, git

from runner_app.git.argv import GitError
from runner_app.git.results_v2 import LocalBareResultTransport


@pytest.mark.parametrize("boundary", [
    "absent", "missing_branch", "conflicting_result", "os_error", "git_error",
    "unexpected_error", "invalid_head", "unfetched_head", "changed_local_bundle",
])
def test_inconclusive_prior_publication_stays_unknown_without_writes(
    tmp_path: Path, boundary: str,
) -> None:
    fixture = Fixture(tmp_path)
    expected_digest = None
    if boundary == "missing_branch":
        git(fixture.receiver, "update-ref", "-d", "refs/heads/tested")
    elif boundary in {"conflicting_result", "changed_local_bundle"}:
        prior = fixture.publish()
        assert prior.status == "DELIVERED"
        if boundary == "changed_local_bundle":
            expected_digest = prior.bundle_sha256
        (fixture.results / "summary.md").write_bytes(b"different retained bytes\n")
    refs_before = git(fixture.receiver, "for-each-ref", "--format=%(refname) %(objectname)")
    intents_before = list(fixture.intents)
    files_before = {path.name: path.read_bytes() for path in fixture.results.iterdir()}

    class ReadOnly(LocalBareResultTransport):
        def fetch_head(self, repo: Path, branch: str) -> str | None:
            if boundary == "os_error":
                raise OSError("Provider temporarily unavailable")
            if boundary == "git_error":
                raise GitError("GITHUB_FETCH_FAILED", "Readback unavailable")
            if boundary == "unexpected_error":
                raise RuntimeError("Provider failure")
            if boundary == "invalid_head":
                return "not-a-git-object"
            if boundary == "unfetched_head":
                return "a" * 40
            return super().fetch_head(repo, branch)

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            pytest.fail("Prior publication must never be replayed by reconciliation")

    result = fixture.publish(ReadOnly(fixture.receiver), reconcile_only=True,
                             expected_bundle_sha256=expected_digest)
    assert result.status == "UNKNOWN"
    assert result.local_available
    assert fixture.intents == intents_before
    assert git(fixture.receiver, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert {path.name: path.read_bytes() for path in fixture.results.iterdir()} == files_before


def test_exact_prior_publication_is_proved_without_another_write(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    prior = fixture.publish()
    assert prior.status == "DELIVERED"
    fixture.advance(b"source advanced after result delivery\n")
    head = fixture.remote_head()
    intents_before = list(fixture.intents)

    class ReadOnly(LocalBareResultTransport):
        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            pytest.fail("Exact prior bundle requires readback only")

    result = fixture.publish(ReadOnly(fixture.receiver), reconcile_only=True,
                             expected_bundle_sha256=prior.bundle_sha256)
    assert result.status == "DELIVERED" and result.code == "ALREADY_DELIVERED"
    assert result.commit == prior.commit and result.remote_head == head
    assert result.bundle_sha256 == prior.bundle_sha256
    assert fixture.intents == intents_before and fixture.remote_head() == head


def test_first_publication_read_failure_remains_local_only(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)

    class Unavailable(LocalBareResultTransport):
        def fetch_head(self, repo: Path, branch: str) -> str | None:
            raise OSError("No publication has been attempted")

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            pytest.fail("Read failure must not write")

    result = fixture.publish(Unavailable(fixture.receiver))
    assert result.status == "LOCAL_ONLY" and fixture.intents == []
    assert fixture.remote_head() == fixture.tested
