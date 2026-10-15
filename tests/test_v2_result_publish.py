"""Focused real-Git checks; receivers and all source work are local scratch."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from runner_app.git.argv import GitError, run_git, run_git_bytes
from runner_app.git.results_v2 import (
    COMMIT_AUTHOR,
    LocalBareResultTransport,
    ResultLimits,
    ResultPublication,
    ResultPublishIntent,
    ResultPushRejected,
    publish_results,
)


def git(path: Path, *arguments: str) -> str:
    return run_git(path, list(arguments)).strip()


def commit(source: Path, message: str) -> str:
    git(source, "add", "--all")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
        "commit", "--quiet", "-m", message)
    return git(source, "rev-parse", "HEAD")


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "source"
        self.source.mkdir()
        git(self.source, "init", "--initial-branch=tested")
        (self.source / "app.txt").write_bytes(b"tested source\n")
        (self.source / "keep.bin").write_bytes(b"\x00\xffuntouched\n")
        (self.source / ".runner").mkdir()
        (self.source / ".runner" / "settings.json").write_bytes(b'{"keep":true}\n')
        self.tested = commit(self.source, "Initial tested source")
        self.receiver = root / "receiver.git"
        git(root, "clone", "--bare", "--no-local", "--no-hardlinks",
            str(self.source), str(self.receiver))
        self.repo = root / "controller.git"
        git(root, "clone", "--bare", "--no-local", "--no-hardlinks",
            str(self.receiver), str(self.repo))
        git(self.source, "remote", "add", "receiver", str(self.receiver))
        self.results = root / "runner-results" / "node" / "request"
        self.results.mkdir(parents=True)
        (self.results / "result.json").write_bytes(b'{"source":"tested","outcome":"FAILED"}\n')
        (self.results / "summary.md").write_bytes(b"A failed test still has deliverable results.\n")
        (self.results / "delivery.json").write_bytes(b'{"status":"PENDING"}\n')
        self.intents: list[ResultPublishIntent] = []

    def advance(self, value: bytes) -> str:
        # Represent an independent developer advancing the remote tested branch.
        git(self.source, "fetch", "--no-tags", "receiver", "refs/heads/tested")
        git(self.source, "reset", "--hard", "FETCH_HEAD")
        (self.source / "app.txt").write_bytes(value)
        head = commit(self.source, "Independent source advancement")
        git(self.source, "push", "receiver", "HEAD:refs/heads/tested")
        return head

    def publish(self, transport: LocalBareResultTransport | None = None,
                **options: object) -> ResultPublication:
        arguments = {
            "tested_branch": "tested", "runner_id": "node", "request_id": "request",
            "result_dir": self.results, "record_intent": self.intents.append,
            "commit_timestamp": 1_700_000_000, **options,
        }
        return publish_results(self.repo, transport or LocalBareResultTransport(self.receiver),
                               **arguments)  # type: ignore[arg-type]

    def remote_head(self) -> str:
        return git(self.receiver, "rev-parse", "refs/heads/tested")


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_real_nonforce_conflict_rebuilds_on_fresh_head_without_touching_source(
    fixture: Fixture,
) -> None:
    refs_before = git(fixture.repo, "for-each-ref", "--format=%(refname) %(objectname)")

    class Racing(LocalBareResultTransport):
        pushes = 0
        advanced: str | None = None

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            self.pushes += 1
            if self.pushes == 1:
                self.advanced = fixture.advance(b"new developer source\n")
            super().push_commit(repo, candidate, branch)

    transport = Racing(fixture.receiver)
    result = fixture.publish(transport)
    assert result.status == "DELIVERED"
    assert result.code == "DELIVERED"
    assert transport.pushes == 2
    assert result.commit == fixture.remote_head()
    assert git(fixture.receiver, "rev-parse", f"{result.commit}^") == transport.advanced
    assert git(fixture.receiver, "show", f"{result.commit}:app.txt") == "new developer source"
    assert run_git_bytes(fixture.receiver, ["show", f"{result.commit}:keep.bin"]) == b"\x00\xffuntouched\n"
    assert git(fixture.receiver, "show", f"{result.commit}:.runner/settings.json") == '{"keep":true}'
    changed = git(fixture.receiver, "diff-tree", "--no-commit-id", "--name-only", "-r",
                  transport.advanced or "", result.commit or "").splitlines()
    assert set(changed) == {
        ".runner/results/node/request/result.json",
        ".runner/results/node/request/summary.md",
        ".runner/results/node/request/.runner-bundle.json",
    }
    assert len(fixture.intents) == 2
    assert fixture.intents[0].base_commit == fixture.tested
    assert fixture.intents[1].base_commit == transport.advanced
    assert fixture.intents[0].commit != fixture.intents[1].commit
    assert fixture.intents[0].marker == fixture.intents[1].marker
    assert fixture.intents[0].bundle_sha256 == fixture.intents[1].bundle_sha256
    raw_commit = git(fixture.repo, "cat-file", "commit", result.commit or "")
    assert f"author {COMMIT_AUTHOR} 1700000000 +0000\n" in raw_commit
    assert f"committer {COMMIT_AUTHOR} 1700000000 +0000\n" in raw_commit
    assert raw_commit.endswith(fixture.intents[-1].commit_message.rstrip("\n"))
    assert git(fixture.repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert not (fixture.repo / "index").exists()
    assert (fixture.results / "summary.md").read_bytes().startswith(b"A failed test")


def test_exact_repeat_is_idempotent_and_collision_is_local_without_new_branch(
    fixture: Fixture,
) -> None:
    first = fixture.publish()
    assert first.status == "DELIVERED"
    first_head = fixture.remote_head()
    (fixture.results / "delivery.json").write_bytes(b'{"status":"DELIVERED"}\n')
    second = fixture.publish()
    assert second.status == "DELIVERED"
    assert second.code == "ALREADY_DELIVERED"
    assert second.commit == first.commit
    assert second.bundle_sha256 == first.bundle_sha256
    assert len(fixture.intents) == 1
    (fixture.results / "summary.md").write_bytes(b"Different immutable bytes\n")
    conflict = fixture.publish()
    assert conflict.status == "LOCAL_ONLY"
    assert conflict.code == "RESULT_PATH_CONFLICT"
    assert conflict.local_available
    assert conflict.local_locator == str(fixture.results)
    assert fixture.remote_head() == first_head
    assert git(fixture.receiver, "for-each-ref", "--format=%(refname)") == "refs/heads/tested"
    git(fixture.receiver, "update-ref", "-d", "refs/heads/tested")
    missing = fixture.publish()
    assert missing.status == "LOCAL_ONLY"
    assert missing.code == "TARGET_BRANCH_MISSING"
    assert git(fixture.receiver, "for-each-ref", "--format=%(refname)") == ""
    assert (fixture.results / "summary.md").read_bytes() == b"Different immutable bytes\n"


@pytest.mark.parametrize("advance_after_push", [False, True])
def test_lost_push_response_requires_exact_remote_proof(
    fixture: Fixture, advance_after_push: bool,
) -> None:
    class LostResponse(LocalBareResultTransport):
        pushes = 0

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            self.pushes += 1
            super().push_commit(repo, candidate, branch)
            if advance_after_push:
                fixture.advance(b"source advanced after accepted results\n")
            raise GitError("RESPONSE_LOST", "Response lost after sending")

    transport = LostResponse(fixture.receiver)
    result = fixture.publish(transport)
    assert result.status == "DELIVERED"
    assert result.code == "DELIVERED_RECONCILED"
    assert result.commit == fixture.intents[0].commit
    assert result.remote_head == fixture.remote_head()
    assert transport.pushes == 1


def test_lost_response_and_unavailable_readback_stay_unknown_without_retry(fixture: Fixture) -> None:
    class UnreadableAfterPush(LocalBareResultTransport):
        pushes = 0

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            self.pushes += 1
            super().push_commit(repo, candidate, branch)
            raise GitError("RESPONSE_LOST", "Private diagnostics are suppressed")

        def fetch_head(self, repo: Path, branch: str) -> str | None:
            if self.pushes:
                raise GitError("UNAVAILABLE", "Readback unavailable")
            return super().fetch_head(repo, branch)

    transport = UnreadableAfterPush(fixture.receiver)
    result = fixture.publish(transport)
    assert result.status == "UNKNOWN"
    assert result.code == "DELIVERY_UNKNOWN"
    assert result.commit == fixture.intents[0].commit == fixture.remote_head()
    assert result.local_available
    assert transport.pushes == 1


def test_ambiguous_failure_with_moved_head_does_not_blindly_retry(fixture: Fixture) -> None:
    class Ambiguous(LocalBareResultTransport):
        pushes = 0

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            self.pushes += 1
            fixture.advance(b"concurrent source but no result proof\n")
            raise GitError("RESPONSE_LOST", "Push outcome cannot be inferred")

    transport = Ambiguous(fixture.receiver)
    result = fixture.publish(transport)
    assert result.status == "UNKNOWN"
    assert result.code == "DELIVERY_UNKNOWN"
    assert transport.pushes == 1
    assert len(fixture.intents) == 1
    assert ".runner/results" not in git(fixture.receiver, "ls-tree", "-r", "--name-only", "tested")


def test_definite_protection_rejection_uses_local_fallback(fixture: Fixture) -> None:
    class Protected(LocalBareResultTransport):
        pushes = 0

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            self.pushes += 1
            raise ResultPushRejected("BRANCH_PROTECTED")

    transport = Protected(fixture.receiver)
    result = fixture.publish(transport)
    assert result.status == "LOCAL_ONLY"
    assert result.code == "BRANCH_PROTECTED"
    assert result.local_available
    assert fixture.remote_head() == fixture.tested
    assert transport.pushes == 1


def test_real_conflict_retry_budget_is_finite(fixture: Fixture) -> None:
    class AlwaysRacing(LocalBareResultTransport):
        pushes = 0

        def push_commit(self, repo: Path, candidate: str, branch: str) -> None:
            self.pushes += 1
            fixture.advance(f"source change {self.pushes}\n".encode())
            super().push_commit(repo, candidate, branch)

    transport = AlwaysRacing(fixture.receiver)
    result = fixture.publish(transport)
    assert result.status == "LOCAL_ONLY"
    assert result.code == "RESULT_CONFLICT_RETRY_LIMIT"
    assert transport.pushes == len(fixture.intents) == 4
    assert len({intent.commit for intent in fixture.intents}) == 4
    assert result.local_available


@pytest.mark.parametrize("unsafe", [
    "symlink", "directory_symlink", "hardlink", "traversal_id", "backslash",
    "case_collision", "root_symlink", "source_overlap", "delivery_symlink",
])
def test_unsafe_bundle_paths_never_write_remote(fixture: Fixture, unsafe: str) -> None:
    options: dict[str, object] = {}
    if unsafe == "symlink":
        (fixture.results / "linked").symlink_to(fixture.source / "app.txt")
    elif unsafe == "directory_symlink":
        (fixture.results / "linked").symlink_to(fixture.source, target_is_directory=True)
    elif unsafe == "hardlink":
        os.link(fixture.source / "app.txt", fixture.results / "linked")
    elif unsafe == "traversal_id":
        options["request_id"] = "../escape"
    elif unsafe == "backslash":
        (fixture.results / "..\\escape").write_bytes(b"bad")
    elif unsafe == "case_collision":
        (fixture.results / "SUMMARY.md").write_bytes(b"bad")
    elif unsafe == "root_symlink":
        alias = fixture.root / "alias"
        alias.symlink_to(fixture.results, target_is_directory=True)
        options["result_dir"] = alias
    elif unsafe == "source_overlap":
        options["result_dir"] = fixture.repo
    elif unsafe == "delivery_symlink":
        (fixture.results / "delivery.json").unlink()
        (fixture.results / "delivery.json").symlink_to(fixture.source / "app.txt")
    result = fixture.publish(**options)
    assert result.status == "LOCAL_ONLY"
    assert result.code in {
        "UNSAFE_RESULT_PATH", "INVALID_RESULT_ID", "RESULT_PATH_COLLISION",
        "RESULT_SOURCE_PATH_OVERLAP",
    }
    assert fixture.remote_head() == fixture.tested
    assert fixture.intents == []


@pytest.mark.parametrize("limits", [
    ResultLimits(per_file_bytes=32),
    ResultLimits(bundle_bytes=100),
    ResultLimits(max_files=2),
])
def test_budgets_keep_entire_original_bundle_locally(
    fixture: Fixture, limits: ResultLimits,
) -> None:
    before = {path.name: path.read_bytes() for path in fixture.results.iterdir()}
    result = fixture.publish(limits=limits)
    assert result.status == "LOCAL_ONLY"
    assert result.code in {"POLICY_SIZE_LIMIT", "RESULT_FILE_COUNT_LIMIT"}
    assert result.local_available
    assert result.local_locator == str(fixture.results)
    assert {path.name: path.read_bytes() for path in fixture.results.iterdir()} == before
    assert fixture.remote_head() == fixture.tested
    assert fixture.intents == []


def test_intent_persistence_must_succeed_before_push(fixture: Fixture) -> None:
    def refuse(intent: ResultPublishIntent) -> None:
        raise OSError("Journal unavailable")

    result = fixture.publish(record_intent=refuse)
    assert result.status == "LOCAL_ONLY"
    assert result.code == "INTENT_PERSISTENCE_FAILED"
    assert fixture.remote_head() == fixture.tested
    assert result.local_available


def test_blob_larger_than_git_stdin_limit_stays_policy_bounded(fixture: Fixture) -> None:
    data = b"bounded binary\x00" * 180_000
    (fixture.results / "binary.dat").write_bytes(data)
    result = fixture.publish()
    assert result.status == "DELIVERED"
    remote = run_git_bytes(fixture.receiver, [
        "show", f"{result.commit}:.runner/results/node/request/binary.dat",
    ])
    assert hashlib.sha256(remote).digest() == hashlib.sha256(data).digest()
    assert (fixture.results / "binary.dat").read_bytes() == data
