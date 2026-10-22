from __future__ import annotations

import base64
import copy
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import runner_app.adapters.github_mailbox_v2 as github_v2
from runner_app.adapters.github import GitHubHTTPResponse, GitHubRepository
from runner_app.adapters.github_mailbox_v2 import (
    BoundedGitHubV2HTTP,
    GitHubMailboxPort,
    GitHubResultTransport,
    GitHubV2Client,
    GitHubV2Error,
    GitHubV2Limits,
)
from runner_app.domain.mailbox_v2 import acknowledge_mailbox, parse_mailbox
from runner_app.git.argv import GitError, run_git, run_git_bytes
from runner_app.git.results_v2 import (
    ResultPublishIntent,
    ResultPushRejected,
    _write_object,
    publish_results,
)
from runner_app.git.state_v2 import publish_state

INSTALLATION = "aa111111-1111-4111-8111-111111111111"
TOKEN = "bb222222-2222-4222-8222-222222222222"
NEXT_TOKEN = "dd444444-4444-4444-8444-444444444444"
COMMAND = ".runner/commands/node.json"
REPOSITORY = GitHubRepository(
    42, "R_42", "owner", 7, "project", "owner/project", "https://github.com/owner/project",
    "https://github.com/owner/project.git", "git@github.com:owner/project.git", True, "tested", None,
)


def git(repo: Path, *args: str) -> str:
    return run_git(repo, list(args)).strip()


def mailbox() -> bytes:
    return json.dumps({
        "protocol": "cdr-mailbox/2", "runner_id": "runner-1", "project_id": "project-1",
        "slot_token": TOKEN, "dispatch": "READY", "receipt": None,
        "request": {"request_id": None, "action": "RUN_TASK", "task": "test",
                    "source": {"kind": "github_branch", "branch": "tested"},
                    "execution": {"mode": "native", "expected_os": "linux", "timeout_seconds": 30}},
    }, indent=2).encode()


def desired(raw: bytes) -> bytes:
    return acknowledge_mailbox(parse_mailbox(raw, installation_id=INSTALLATION,
                                             mailbox_id="github:42:node"), slot_token=NEXT_TOKEN)


class FakeGitAPI:
    """Fake HTTP operations backed by actual Git object storage and FF checks."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls: list[tuple[str, str, Any]] = []
        self.before_patch: Callable[[], None] | None = None
        self.lose_response = False
        self.unreadable_after_patch = False
        self.reads_unavailable = False
        self.patch_status: int | None = None
        self.identity_id = 42
        self.owner_id = 7

    def head(self) -> str | None:
        try:
            return git(self.repo, "rev-parse", "--verify", "refs/heads/tested")
        except GitError:
            return None

    def entries(self, tree: str) -> list[dict[str, str]]:
        result = []
        for entry in run_git_bytes(self.repo, ["ls-tree", "-z", tree]).split(b"\0"):
            if entry:
                header, name = entry.split(b"\t", 1)
                mode, kind, oid = header.decode().split(" ")
                result.append({"path": name.decode(), "mode": mode, "type": kind, "sha": oid})
        return result

    def tree(self, additions: list[dict[str, str]], base: str | None = None) -> str:
        entries = {entry["path"]: entry for entry in self.entries(base)} if base else {}
        for entry in additions:
            first, separator, tail = entry["path"].partition("/")
            if separator:
                child = self.tree([{**entry, "path": tail}], entries.get(first, {}).get("sha"))
                entries[first] = {"path": first, "mode": "040000", "type": "tree", "sha": child}
            else:
                entries[first] = entry
        data = b"".join(f"{entry['mode']} {entry['type']} {entry['sha']}\t{entry['path']}\0".encode()
                        for entry in entries.values())
        return run_git_bytes(self.repo, ["mktree", "-z"], input_data=data).decode().strip()

    def advance(self, changes: dict[str, bytes]) -> str:
        parent = self.head()
        assert parent is not None
        tree = git(self.repo, "rev-parse", f"{parent}^{{tree}}")
        new_tree = self.tree([{"path": path, "mode": "100644", "type": "blob",
                               "sha": _write_object(self.repo, "blob", data)}
                              for path, data in changes.items()], tree)
        raw = (f"tree {new_tree}\nparent {parent}\nauthor Fixture <fixture@localhost> 1700000000 +0000\n"
               "committer Fixture <fixture@localhost> 1700000000 +0000\n\nConcurrent source\n").encode()
        candidate = _write_object(self.repo, "commit", raw)
        git(self.repo, "update-ref", "refs/heads/tested", candidate, parent)
        return candidate

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        self.calls.append((method, path, copy.deepcopy(body)))
        assert params is None
        assert path.startswith("/repos/owner/project")
        suffix = path[len("/repos/owner/project"):]
        if method == "GET" and self.reads_unavailable:
            raise OSError("Fake unavailable response with private provider details")
        if method == "GET" and suffix == "":
            return GitHubHTTPResponse(200, {
                "id": self.identity_id, "node_id": "R_42", "owner": {"id": self.owner_id, "login": "owner"},
                "name": "project", "full_name": "owner/project", "html_url": REPOSITORY.html_url,
                "clone_url": REPOSITORY.clone_url, "ssh_url": REPOSITORY.ssh_url,
                "private": True, "default_branch": "tested", "description": None,
            })
        if method == "GET" and suffix.startswith("/git/ref/heads/"):
            head = self.head() if suffix == "/git/ref/heads/tested" else None
            return GitHubHTTPResponse(404, {}) if head is None else GitHubHTTPResponse(200, {
                "ref": "refs/heads/tested", "object": {"sha": head, "type": "commit"},
            })
        if method == "GET" and suffix.startswith("/git/commits/"):
            oid = suffix.rsplit("/", 1)[-1]
            return GitHubHTTPResponse(200, {"sha": oid,
                                          "tree": {"sha": git(self.repo, "rev-parse", f"{oid}^{{tree}}")}})
        if method == "GET" and suffix.startswith("/git/trees/"):
            oid = suffix.rsplit("/", 1)[-1]
            return GitHubHTTPResponse(200, {"sha": oid, "truncated": False, "tree": self.entries(oid)})
        if method == "GET" and suffix.startswith("/git/blobs/"):
            oid = suffix.rsplit("/", 1)[-1]
            raw = run_git_bytes(self.repo, ["cat-file", "blob", oid])
            return GitHubHTTPResponse(200, {"sha": oid, "size": len(raw), "encoding": "base64",
                                          "content": base64.b64encode(raw).decode()})
        assert body is not None
        if method == "POST" and suffix == "/git/blobs":
            assert body["encoding"] == "base64"
            oid = _write_object(self.repo, "blob", base64.b64decode(body["content"], validate=True))
            return GitHubHTTPResponse(201, {"sha": oid})
        if method == "POST" and suffix == "/git/trees":
            oid = self.tree(body["tree"], body.get("base_tree"))
            return GitHubHTTPResponse(201, {"sha": oid})
        if method == "POST" and suffix == "/git/commits":
            def identity(person: dict[str, str]) -> str:
                timestamp = int(datetime.fromisoformat(person["date"]).timestamp())
                return f"{person['name']} <{person['email']}> {timestamp} +0000"

            raw = (f"tree {body['tree']}\nparent {body['parents'][0]}\nauthor {identity(body['author'])}\n"
                   f"committer {identity(body['committer'])}\n\n{body['message']}").encode()
            oid = _write_object(self.repo, "commit", raw)
            return GitHubHTTPResponse(201, {"sha": oid})
        if method == "PATCH" and suffix == "/git/refs/heads/tested":
            assert set(body) == {"sha", "force"} and body["force"] is False
            if self.before_patch is not None:
                hook, self.before_patch = self.before_patch, None
                hook()
            old = self.head()
            if old is None:
                return GitHubHTTPResponse(404, {})
            if self.patch_status is not None:
                return GitHubHTTPResponse(self.patch_status, {})
            try:
                git(self.repo, "merge-base", "--is-ancestor", old, body["sha"])
            except GitError:
                return GitHubHTTPResponse(422, {"message": "Update is not a fast forward"})
            git(self.repo, "update-ref", "refs/heads/tested", body["sha"], old)
            if self.unreadable_after_patch:
                self.reads_unavailable = True
            if self.lose_response:
                raise OSError("Fake response lost after accepted PATCH")
            return GitHubHTTPResponse(200, {"ref": "refs/heads/tested",
                                           "object": {"sha": body["sha"], "type": "commit"}})
        raise AssertionError(f"Unexpected fake API operation {method} {suffix}")


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root, self.remote = root, root / "remote.git"
        git(root, "init", "--bare", "--initial-branch=tested", str(self.remote))
        self.api = FakeGitAPI(self.remote)
        tree = self.api.tree([
            {"path": COMMAND, "mode": "100644", "type": "blob", "sha": _write_object(self.remote, "blob", mailbox())},
            {"path": "app.txt", "mode": "100644", "type": "blob", "sha": _write_object(self.remote, "blob", b"tested source\n")},
        ])
        raw = (f"tree {tree}\nauthor Fixture <fixture@localhost> 1700000000 +0000\n"
               "committer Fixture <fixture@localhost> 1700000000 +0000\n\nInitial source\n").encode()
        self.initial = _write_object(self.remote, "commit", raw)
        git(self.remote, "update-ref", "refs/heads/tested", self.initial)
        self.repo = root / "repo.git"
        git(root, "clone", "--bare", "--no-local", "--no-hardlinks", str(self.remote), str(self.repo))
        self.token_calls = 0

        def no_real_credentials() -> str:
            self.token_calls += 1
            raise AssertionError("Fake HTTP must not read a credential")

        self.client = GitHubV2Client(REPOSITORY, no_real_credentials, transport=self.api)
        self.port = GitHubMailboxPort(self.client, branch="tested", command_path=COMMAND)

    def fetch(self, repo: Path, branch: str, oid: str) -> None:
        assert branch == "tested"
        git(repo, "fetch", "--no-tags", "--no-write-fetch-head", str(self.remote), oid)

    def publish(self, *, limits: GitHubV2Limits | None = None) -> tuple[Any, list[ResultPublishIntent]]:
        result_dir = self.root / "sealed-results"
        result_dir.mkdir(exist_ok=True)
        (result_dir / "result.json").write_bytes(b'{"tested":"historical source"}\n')
        (result_dir / "summary.md").write_bytes(b"Results for the frozen source.\n")
        client = self.client if limits is None else GitHubV2Client(
            REPOSITORY, lambda: "never-read", transport=self.api, limits=limits)
        transport = GitHubResultTransport(client, fetch_objects=self.fetch)
        intents: list[ResultPublishIntent] = []
        result = publish_results(self.repo, transport, tested_branch="tested", runner_id="node",
                                 request_id="request", result_dir=result_dir, record_intent=intents.append,
                                 commit_timestamp=1_700_000_000)
        return result, intents


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_construction_is_inert_and_read_is_current_head_only(fixture: Fixture) -> None:
    assert fixture.api.calls == [] and fixture.token_calls == 0
    old = fixture.initial
    current = fixture.api.advance({"app.txt": b"new source\n"})
    observed = fixture.port.read_current()
    assert observed.payload == mailbox()
    revision = json.loads(base64.urlsafe_b64decode(observed.revision[10:]))
    assert revision["head"] == current and revision["blob"]
    commit_reads = [path for method, path, _ in fixture.api.calls if "/git/commits/" in path]
    assert commit_reads == [f"/repos/owner/project/git/commits/{current}"]
    assert all(old not in path for _, path, _ in fixture.api.calls)
    assert all(method == "GET" for method, _, _ in fixture.api.calls)
    assert fixture.token_calls == 0


def test_unchanged_poll_reuses_objects_but_reads_identity_and_head(fixture: Fixture) -> None:
    original = fixture.port.read_current()
    assert len(fixture.api.calls) == 7
    fixture.api.calls.clear()
    assert fixture.port.read_current() == original
    assert [(method, path) for method, path, _ in fixture.api.calls] == [
        ("GET", "/repos/owner/project"),
        ("GET", "/repos/owner/project/git/ref/heads/tested"),
    ]

    advanced = fixture.api.advance({"app.txt": b"new source\n"})
    fixture.api.calls.clear()
    observed = fixture.port.read_current()
    assert observed.payload == original.payload
    revision = json.loads(base64.urlsafe_b64decode(observed.revision[10:]))
    assert revision["head"] == advanced
    # Only the new commit/root tree need reads; command subtree and blob are immutable.
    assert len(fixture.api.calls) == 4
    assert not any("/git/blobs/" in path for _, path, _ in fixture.api.calls)

    replacement = mailbox().replace(b'"test"', b'"another-task"')
    fixture.api.advance({COMMAND: replacement})
    fixture.api.calls.clear()
    assert fixture.port.read_current().payload == replacement
    assert len(fixture.api.calls) == 7


@pytest.mark.parametrize("failure", ["identity", "unavailable", "missing_branch"])
def test_cached_objects_do_not_mask_new_observation_failures(
    fixture: Fixture, failure: str,
) -> None:
    fixture.port.read_current()
    if failure == "identity":
        fixture.api.identity_id = 999
    elif failure == "unavailable":
        fixture.api.reads_unavailable = True
    else:
        git(fixture.remote, "update-ref", "-d", "refs/heads/tested")
    with pytest.raises(GitHubV2Error):
        fixture.port.read_current()


def test_cached_tree_cannot_be_mutated_by_a_caller(fixture: Fixture) -> None:
    root = fixture.client.commit_tree(fixture.initial)
    original = copy.deepcopy(fixture.client.tree(root))
    entries = fixture.client.tree(root)
    entries[0]["sha"] = "0" * 40
    entries.clear()
    before = len(fixture.api.calls)
    assert fixture.client.tree(root) == original
    assert len(fixture.api.calls) == before


def test_cached_blob_still_obeys_the_requested_size_budget(fixture: Fixture) -> None:
    observed = fixture.port.read_current()
    revision = json.loads(base64.urlsafe_b64decode(observed.revision[10:]))
    before = len(fixture.api.calls)
    with pytest.raises(GitHubV2Error, match="GITHUB_BLOB_INVALID"):
        fixture.client.blob(revision["blob"], len(observed.payload) - 1)
    assert len(fixture.api.calls) == before


@pytest.mark.parametrize("bound", ["entries", "bytes"])
def test_immutable_cache_evicts_within_bounded_memory(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, bound: str,
) -> None:
    monkeypatch.setattr(github_v2, "_OBJECT_CACHE_ENTRIES", 1 if bound == "entries" else 128)
    monkeypatch.setattr(github_v2, "_OBJECT_CACHE_BYTES", 1024 if bound == "entries" else 192)
    first, second = b"a" * 128, b"b" * 128
    first_oid = _write_object(fixture.remote, "blob", first)
    second_oid = _write_object(fixture.remote, "blob", second)
    assert fixture.client.blob(first_oid, 128) == first
    assert fixture.client.blob(second_oid, 128) == second
    assert fixture.client.blob(first_oid, 128) == first
    assert len(fixture.api.calls) == 3


def test_rejected_object_response_is_not_cached(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = fixture.client.commit_tree(fixture.initial)
    request = fixture.api.request
    corrupt = True

    def response(method: str, path: str, **kwargs: Any) -> GitHubHTTPResponse:
        result = request(method, path, **kwargs)
        if corrupt and path.endswith("/git/trees/" + root):
            result.data["truncated"] = True
        return result

    monkeypatch.setattr(fixture.api, "request", response)
    with pytest.raises(GitHubV2Error, match="GITHUB_TREE_INVALID"):
        fixture.client.tree(root)
    corrupt = False
    assert fixture.client.tree(root)
    assert len([path for _, path, _ in fixture.api.calls if "/git/trees/" in path]) == 2


def test_ack_preserves_payload_and_retries_only_unchanged_blob_on_new_head(fixture: Fixture) -> None:
    observed = fixture.port.read_current()
    advanced: list[str] = []
    fixture.api.before_patch = lambda: advanced.append(fixture.api.advance({"app.txt": b"concurrent source\n"}))
    ack = desired(observed.payload)
    result = fixture.port.ack_if_current(observed, ack)
    assert result.state == "APPLIED"
    assert result.payload_sha256 == hashlib.sha256(ack).hexdigest()
    assert fixture.port.read_current().payload == ack
    head = fixture.api.head()
    assert git(fixture.remote, "rev-parse", f"{head}^") == advanced[0]
    assert git(fixture.remote, "show", f"{head}:app.txt") == "concurrent source"
    assert json.loads(ack)["request"] == json.loads(observed.payload)["request"]
    assert json.loads(ack)["slot_token"] == NEXT_TOKEN
    patches = [(path, body) for method, path, body in fixture.api.calls if method == "PATCH"]
    assert len(patches) == 2
    assert all(body["force"] is False for _, body in patches)
    assert not any(method == "POST" and path.endswith("/git/refs") for method, path, _ in fixture.api.calls)


def test_ack_conflict_preserves_new_user_command(fixture: Fixture) -> None:
    observed = fixture.port.read_current()
    changed = json.loads(observed.payload)
    changed["request"]["task"] = "another-task"
    replacement = json.dumps(changed).encode()
    fixture.api.before_patch = lambda: fixture.api.advance({COMMAND: replacement})
    result = fixture.port.ack_if_current(observed, desired(observed.payload))
    assert result.state == "CONFLICT"
    assert fixture.port.read_current().payload == replacement
    assert len([call for call in fixture.api.calls if call[0] == "PATCH"]) == 1


@pytest.mark.parametrize("unavailable", [False, True])
def test_ack_lost_response_only_reads_proof_and_never_retries(fixture: Fixture, unavailable: bool) -> None:
    observed = fixture.port.read_current()
    fixture.api.lose_response = True
    fixture.api.unreadable_after_patch = unavailable
    result = fixture.port.ack_if_current(observed, desired(observed.payload))
    assert result.state == ("UNKNOWN" if unavailable else "APPLIED")
    assert len([call for call in fixture.api.calls if call[0] == "PATCH"]) == 1


def test_ack_refuses_mutated_user_payload_or_unrotated_token_before_writes(fixture: Fixture) -> None:
    observed = fixture.port.read_current()
    for change in ("payload", "token"):
        ack = json.loads(desired(observed.payload))
        if change == "payload":
            ack["request"]["task"] = "unasked-change"
        else:
            ack["slot_token"] = TOKEN
        result = fixture.port.ack_if_current(observed, json.dumps(ack).encode())
        assert result.state == "FAILED"
    assert all(method == "GET" for method, _, _ in fixture.api.calls)


@pytest.mark.parametrize("field", ["identity_id", "owner_id"])
def test_exact_repository_and_owner_numeric_identity_are_required(fixture: Fixture, field: str) -> None:
    setattr(fixture.api, field, 999)
    with pytest.raises(GitHubV2Error, match="GITHUB_IDENTITY_MISMATCH"):
        fixture.port.read_current()
    assert all(method == "GET" for method, _, _ in fixture.api.calls)


@pytest.mark.parametrize("path", ["../command.json", "/command.json", "a//b", "a\\b", "a:stream"])
def test_unenrolled_paths_are_refused_before_io(fixture: Fixture, path: str) -> None:
    with pytest.raises(GitHubV2Error):
        GitHubMailboxPort(fixture.client, branch="tested", command_path=path)
    assert fixture.api.calls == []


def test_existing_ref_result_upload_reproduces_exact_commit_and_preserves_source(fixture: Fixture) -> None:
    refs = git(fixture.repo, "for-each-ref", "--format=%(refname) %(objectname)")
    advanced: list[str] = []
    fixture.api.before_patch = lambda: advanced.append(fixture.api.advance({"app.txt": b"new source at PATCH\n"}))
    result, intents = fixture.publish()
    assert result.status == "DELIVERED"
    assert len(intents) == 2
    assert result.commit == intents[-1].commit == fixture.api.head()
    assert git(fixture.remote, "rev-parse", f"{result.commit}^") == advanced[0]
    assert run_git_bytes(fixture.remote, ["cat-file", "commit", result.commit]) == run_git_bytes(
        fixture.repo, ["cat-file", "commit", result.commit])
    assert git(fixture.remote, "show", f"{result.commit}:app.txt") == "new source at PATCH"
    assert run_git_bytes(fixture.remote, ["show", f"{result.commit}:{COMMAND}"]) == mailbox()
    changed = git(fixture.remote, "diff-tree", "--name-only", "--no-commit-id", "-r", advanced[0], result.commit).splitlines()
    assert all(path.startswith(".runner/results/node/request/") for path in changed)
    assert git(fixture.repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert not (fixture.repo / "FETCH_HEAD").exists() and not (fixture.repo / "index").exists()
    assert not any(method == "POST" and "refs" in path for method, path, _ in fixture.api.calls)
    assert fixture.token_calls == 0


@pytest.mark.parametrize("unavailable", [False, True])
def test_result_patch_response_loss_is_proved_or_unknown(fixture: Fixture, unavailable: bool) -> None:
    fixture.api.lose_response = True
    fixture.api.unreadable_after_patch = unavailable
    result, intents = fixture.publish()
    assert result.status == ("UNKNOWN" if unavailable else "DELIVERED")
    assert len(intents) == len([call for call in fixture.api.calls if call[0] == "PATCH"]) == 1
    assert result.commit == fixture.api.head()


def test_deleted_result_branch_is_never_recreated(fixture: Fixture) -> None:
    fixture.api.before_patch = lambda: git(fixture.remote, "update-ref", "-d", "refs/heads/tested")
    result, _ = fixture.publish()
    assert result.status == "LOCAL_ONLY" and result.code == "TARGET_BRANCH_MISSING"
    assert fixture.api.head() is None
    assert not any(method == "POST" and "refs" in path for method, path, _ in fixture.api.calls)


def test_result_object_budget_refusal_never_updates_ref(fixture: Fixture) -> None:
    result, _ = fixture.publish(limits=GitHubV2Limits(blob_bytes=8))
    assert result.status == "LOCAL_ONLY"
    assert fixture.api.head() == fixture.initial
    assert all(method != "PATCH" for method, _, _ in fixture.api.calls)


def test_late_result_object_limit_is_rejected_before_any_upload(fixture: Fixture) -> None:
    # Five changed trees, three blobs and the commit require nine objects.
    result, _ = fixture.publish(limits=GitHubV2Limits(objects=8))
    assert result.status == "LOCAL_ONLY"
    assert fixture.api.head() == fixture.initial
    assert all(method == "GET" for method, _, _ in fixture.api.calls)


def test_result_tree_is_uploaded_once_with_all_paths_and_unique_blobs(fixture: Fixture) -> None:
    directory = fixture.root / "sealed-results"
    for name in ("one", "two"):
        child = directory / name
        child.mkdir(parents=True)
        (child / "same.bin").write_bytes(b"same binary\0contents")
    result, intents = fixture.publish()
    assert result.status == "DELIVERED" and result.commit == intents[0].commit
    trees = [body for method, path, body in fixture.api.calls
             if method == "POST" and path.endswith("/git/trees")]
    blobs = [body for method, path, body in fixture.api.calls
             if method == "POST" and path.endswith("/git/blobs")]
    assert len(trees) == 1 and len(blobs) == 4
    assert trees[0]["base_tree"] == git(fixture.repo, "rev-parse", f"{fixture.initial}^{{tree}}")
    assert {entry["path"] for entry in trees[0]["tree"]} == {
        f".runner/results/node/request/{name}" for name in (
            ".runner-bundle.json", "result.json", "summary.md", "one/same.bin", "two/same.bin")}
    for name in ("one", "two"):
        assert run_git_bytes(fixture.remote, [
            "show", f"{result.commit}:.runner/results/node/request/{name}/same.bin",
        ]) == b"same binary\0contents"
    assert git(fixture.remote, "show", f"{result.commit}:app.txt") == "tested source"


def test_small_custom_tree_limit_retains_exact_dependency_ordered_upload(fixture: Fixture) -> None:
    directory = fixture.root / "sealed-results" / "artifacts"
    directory.mkdir(parents=True)
    (directory / "one.txt").write_text("one")
    (directory / "two.txt").write_text("two")
    result, intents = fixture.publish(limits=GitHubV2Limits(tree_entries=4))
    assert result.status == "DELIVERED" and result.commit == intents[0].commit
    trees = [body for method, path, body in fixture.api.calls
             if method == "POST" and path.endswith("/git/trees")]
    assert len(trees) > 1 and all(len(body["tree"]) <= 4 for body in trees)
    assert git(fixture.remote, "show", f"{result.commit}:app.txt") == "tested source"


@pytest.mark.parametrize("failed", [False, True])
def test_blob_upload_groups_are_bounded_and_fail_before_trees_or_ref(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    directory = fixture.root / "sealed-results"
    directory.mkdir()
    for index in range(8):
        (directory / f"artifact-{index}.bin").write_bytes(bytes([index]))
    original = fixture.api.request
    lock, ready = threading.Lock(), threading.Event()
    active = started = peak = 0

    def request(method: str, path: str, *, params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        nonlocal active, started, peak
        if method != "POST" or not path.endswith("/git/blobs"):
            with lock:
                assert active == 0
            return original(method, path, params=params, body=body)
        with lock:
            if started % 4 == 0:
                assert active == 0  # A later group never overlaps the prior one.
            active += 1
            started += 1
            sequence = started
            peak = max(peak, active)
            if active == 4:
                ready.set()
        try:
            if sequence <= 4:
                assert ready.wait(5), "First four blob uploads must overlap"
            if failed and sequence == 1:
                raise OSError("Lost immutable-object response")
            return original(method, path, params=params, body=body)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(fixture.api, "request", request)
    result, _ = fixture.publish()
    assert active == 0 and peak == 4
    if failed:
        assert result.status == "LOCAL_ONLY" and started == 4
        assert fixture.api.head() == fixture.initial
        assert all(method == "GET" or path.endswith("/git/blobs")
                   for method, path, _ in fixture.api.calls)
    else:
        assert result.status == "DELIVERED" and started == 11


@pytest.mark.parametrize(("path", "mode"), [
    ("app.txt", "100644"), (".runner/results/node/request/run.sh", "100755"),
])
def test_result_source_or_mode_change_is_rejected_before_upload(
    fixture: Fixture, path: str, mode: str,
) -> None:
    base = git(fixture.repo, "rev-parse", f"{fixture.initial}^{{tree}}")
    local = FakeGitAPI(fixture.repo)
    tree = local.tree([{"path": path, "mode": mode, "type": "blob",
                       "sha": _write_object(fixture.repo, "blob", b"changed\n")}], base)
    identity = f"{github_v2.COMMIT_AUTHOR} 1700000000 +0000"
    commit = _write_object(fixture.repo, "commit", (
        f"tree {tree}\nparent {fixture.initial}\nauthor {identity}\ncommitter {identity}\n\n"
        "Publish runner results: node/request\n").encode())
    with pytest.raises(ResultPushRejected, match="RESULT_OBJECT_UPLOAD_FAILED"):
        GitHubResultTransport(fixture.client, fetch_objects=fixture.fetch).push_commit(
            fixture.repo, commit, "tested")
    assert all(method == "GET" for method, _, _ in fixture.api.calls)


def test_terminal_state_addition_and_update_use_one_exact_tree_each(fixture: Fixture) -> None:
    state = {"runner_id": "node", "request_id": "request", "project_id": "project",
             "request_digest": "a" * 64, "updated_at": "2026-10-23T00:00:00Z", "status": "FAILED"}
    transport = GitHubResultTransport(fixture.client, fetch_objects=fixture.fetch)
    for updated in (False, True):
        if updated:
            state.update(updated_at="2026-10-23T00:00:01Z", status="COMPLETED")
        fixture.api.calls.clear()
        result = publish_state(fixture.repo, transport, branch="tested", state=state,
                               record_intent=lambda _: None)
        assert result.status == "DELIVERED"
        assert json.loads(run_git_bytes(fixture.remote, [
            "show", f"{result.commit}:.runner/state/node/request.json"])) == state
        assert len([1 for method, path, _ in fixture.api.calls
                    if method == "POST" and path.endswith("/git/trees")]) == 1
        assert git(fixture.remote, "show", f"{result.commit}:app.txt") == "tested source"


def test_large_result_blob_uses_separate_json_budget_above_legacy_git_limit(fixture: Fixture) -> None:
    directory = fixture.root / "sealed-results"
    directory.mkdir()
    data = b"large-local-test\0" * 550_000  # 8.8 MB; exceeds legacy Git stdout cap.
    (directory / "binary.dat").write_bytes(data)
    result, _ = fixture.publish()
    assert result.status == "DELIVERED"
    uploaded = [body for method, path, body in fixture.api.calls
                if method == "POST" and path.endswith("/git/blobs")
                and len(body["content"]) > 8 * 1024 * 1024]
    assert len(uploaded) == 1
    assert base64.b64decode(uploaded[0]["content"]) == data


@pytest.mark.parametrize("path", ["https://attacker.invalid/repos/x", "//evil/repos/x", "/repos/a/../b"])
def test_http_origin_and_traversal_refusals_do_not_read_token(path: str) -> None:
    def forbidden() -> str:
        raise AssertionError("Credential must not be requested")

    transport = BoundedGitHubV2HTTP(forbidden)
    with pytest.raises(GitHubV2Error, match="GITHUB_REQUEST_INVALID"):
        transport.request("GET", path)


def test_http_json_budget_is_enforced_before_token_lookup() -> None:
    def forbidden() -> str:
        raise AssertionError("Credential must not be requested")

    transport = BoundedGitHubV2HTTP(forbidden, limits=GitHubV2Limits(json_bytes=16))
    with pytest.raises(GitHubV2Error, match="GITHUB_JSON_LIMIT"):
        transport.request("POST", "/repos/owner/project/git/blobs", body={"content": "x" * 20})


def test_enrollment_disallows_alternate_clone_origin_without_io() -> None:
    with pytest.raises(GitHubV2Error, match="GITHUB_ENROLLMENT_INVALID"):
        GitHubV2Client(replace(REPOSITORY, clone_url="https://attacker.invalid/repo.git"), lambda: "unused")
