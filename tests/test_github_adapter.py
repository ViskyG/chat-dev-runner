"""EP-05 GitHub REST tests use a stateful fake; no credential or cloud access."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

from runner_app.adapters.github import (
    GitHubError,
    GitHubHTTPResponse,
    GitHubREST,
    UrllibGitHubTransport,
    make_provision_marker,
)

OWNER = "alice"
REPO = "notes-lab"
MARKER = make_provision_marker("installation", "notes-lab", "operation")
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def repository(owner: str = OWNER, name: str = REPO, *, identifier: int = 101,
               private: bool = True, description: str | None = MARKER) -> dict[str, Any]:
    return {
        "id": identifier, "node_id": f"R_{identifier}", "name": name,
        "full_name": f"{owner}/{name}", "owner": {"login": owner, "id": 1 if owner == OWNER else 2},
        "private": private, "description": description, "default_branch": "main",
        "html_url": f"https://github.com/{owner}/{name}",
        "clone_url": f"https://github.com/{owner}/{name}.git",
        "ssh_url": f"git@github.com:{owner}/{name}.git",
    }


def pull_request(number: int = 7, *, draft: bool = True, sha: str = HEAD_SHA,
                 title: str = "Tested change", body: str = "Report") -> dict[str, Any]:
    return {
        "id": 800 + number, "node_id": f"PR_{number}", "number": number,
        "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
        "state": "open", "draft": draft, "title": title, "body": body,
        "head": {"ref": "work/item", "sha": sha, "repo": {"id": 101}},
        "base": {"ref": "main", "repo": {"id": 101}},
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.login = OWNER
        self.current_repo: dict[str, Any] | None = None
        self.branches = {"main": BASE_SHA, "work/item": HEAD_SHA}
        self.prs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []
        self.repo_create_timeout = False
        self.repo_readback_failure = False
        self.pr_create_timeout = False
        self.pr_readback_failure = False
        self.pr_update_timeout = False
        self.post_status: int | None = None
        self.post_data: Any = None
        self.repo_read_failure = False
        self.pr_read_failure = False

    def request(self, method: str, path: str, *, params: Any = None,
                body: Any = None) -> GitHubHTTPResponse:
        self.calls.append((method, path, dict(params or {}), dict(body or {})))
        if path == "/user" and method == "GET":
            return GitHubHTTPResponse(200, {"login": self.login, "id": 1})
        if path == "/users/alice" and method == "GET":
            return GitHubHTTPResponse(200, {"login": OWNER, "id": 1, "type": "User"})
        if path == "/users/org" and method == "GET":
            return GitHubHTTPResponse(200, {"login": "org", "id": 2, "type": "Organization"})
        if path == "/users/other" and method == "GET":
            return GitHubHTTPResponse(200, {"login": "other", "id": 2, "type": "User"})
        if path in {f"/repos/{OWNER}/{REPO}", "/repos/org/notes-lab",
                    "/repos/other/notes-lab"} and method == "GET":
            if self.repo_read_failure:
                raise TimeoutError("secret-provider-error")
            return GitHubHTTPResponse(200, self.current_repo) if self.current_repo else GitHubHTTPResponse(404, {})
        if path in {"/user/repos", "/orgs/org/repos"} and method == "POST":
            if self.post_status is not None:
                return GitHubHTTPResponse(self.post_status, self.post_data or {})
            owner = OWNER if path == "/user/repos" else "org"
            self.current_repo = repository(owner=owner, description=body["description"])
            if self.repo_create_timeout:
                self.repo_read_failure = self.repo_readback_failure
                raise TimeoutError("secret-provider-error")
            return GitHubHTTPResponse(201, self.current_repo)
        prefix = f"/repos/{OWNER}/{REPO}"
        if method == "GET" and path.startswith(prefix + "/branches/"):
            branch = urllib.parse.unquote(path[len(prefix + "/branches/"):])
            sha = self.branches.get(branch)
            return (GitHubHTTPResponse(200, {"name": branch, "commit": {"sha": sha},
                                             "protected": branch == "main"}) if sha
                    else GitHubHTTPResponse(404, {}))
        if path == prefix + "/pulls" and method == "GET":
            if self.pr_read_failure:
                raise TimeoutError("secret-provider-error")
            return GitHubHTTPResponse(200, [dict(item) for item in self.prs])
        if path == prefix + "/pulls" and method == "POST":
            if self.post_status is not None:
                return GitHubHTTPResponse(self.post_status, self.post_data or {})
            pr = pull_request(number=7 + len(self.prs), title=body["title"], body=body["body"])
            self.prs.append(pr)
            if self.pr_create_timeout:
                self.pr_read_failure = self.pr_readback_failure
                raise TimeoutError("secret-provider-error")
            return GitHubHTTPResponse(201, pr)
        if path.startswith(prefix + "/pulls/") and method == "PATCH":
            number = int(path.rsplit("/", 1)[1])
            pr = next(item for item in self.prs if item["number"] == number)
            pr.update(title=body["title"], body=body["body"])
            if self.pr_update_timeout:
                raise TimeoutError("secret-provider-error")
            return GitHubHTTPResponse(200, pr)
        if path.startswith(prefix + "/pulls/") and method == "GET":
            number = int(path.rsplit("/", 1)[1])
            return GitHubHTTPResponse(200, next(item for item in self.prs if item["number"] == number))
        raise AssertionError((method, path, params, body))


def client(fake: FakeGitHub) -> GitHubREST:
    return GitHubREST(fake, expected_login=OWNER, expected_user_id=1)


def registered(fake: FakeGitHub):
    fake.current_repo = repository()
    return client(fake).get_repository(OWNER, REPO, expected_repo_id=101)


def test_construction_is_inert_and_marker_is_stable() -> None:
    calls: list[str] = []
    transport = UrllibGitHubTransport(lambda: calls.append("token") or "secret")
    GitHubREST(transport, expected_login=OWNER)
    assert calls == []
    assert MARKER == make_provision_marker("installation", "notes-lab", "operation")
    assert MARKER != make_provision_marker("installation", "other", "operation")


def test_private_repo_create_uses_exact_owner_and_empty_remote() -> None:
    fake = FakeGitHub()
    result = client(fake).create_or_attach_private(OWNER, REPO, MARKER)
    assert result.disposition == "created"
    assert result.repository.id == 101 and result.repository.node_id == "R_101"
    assert result.repository.clone_url == f"https://github.com/{OWNER}/{REPO}.git"
    assert result.repository.ssh_url == f"git@github.com:{OWNER}/{REPO}.git"
    posts = [call for call in fake.calls if call[0] == "POST"]
    assert posts == [("POST", "/user/repos", {}, {
        "name": REPO, "description": MARKER, "private": True, "auto_init": False,
    })]
    assert fake.calls[0][:2] == ("GET", "/user")
    assert fake.calls[1][:2] == ("GET", "/users/alice")


def test_repo_create_timeout_reconciles_marker_without_second_post() -> None:
    fake = FakeGitHub()
    fake.repo_create_timeout = True
    result = client(fake).create_or_attach_private(OWNER, REPO, MARKER)
    assert result.disposition == "reconciled"
    assert len([call for call in fake.calls if call[0] == "POST"]) == 1
    assert client(fake).create_or_attach_private(OWNER, REPO, MARKER).disposition == "attached"


def test_name_collision_and_missing_confirmed_id_refuse_adoption() -> None:
    fake = FakeGitHub()
    fake.current_repo = repository(description="Another owner's project")
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private(OWNER, REPO, MARKER)
    assert error.value.code == "REPOSITORY_COLLISION"
    assert not any(call[0] == "POST" for call in fake.calls)
    assert client(fake).create_or_attach_private(OWNER, REPO, MARKER,
                                                 expected_repo_id=101).disposition == "attached"
    fake.current_repo = None
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private(OWNER, REPO, MARKER, expected_repo_id=101)
    assert error.value.code == "REPOSITORY_MISSING"
    assert not any(call[0] == "POST" for call in fake.calls)


def test_auth_and_target_owner_are_verified_before_create() -> None:
    fake = FakeGitHub()
    fake.login = "wrong-account"
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private(OWNER, REPO, MARKER)
    assert error.value.code == "GITHUB_IDENTITY_MISMATCH"
    assert len(fake.calls) == 1

    fake = FakeGitHub()
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private("other", REPO, MARKER)
    assert error.value.code == "GITHUB_IDENTITY_MISMATCH"
    assert not any(call[0] == "POST" for call in fake.calls)


def test_org_creation_is_explicit_and_approved_import_may_have_other_user_owner() -> None:
    fake = FakeGitHub()
    result = client(fake).create_or_attach_private("org", REPO, MARKER)
    assert result.repository.owner == "org"
    assert [call[1] for call in fake.calls if call[0] == "POST"] == ["/orgs/org/repos"]
    fake = FakeGitHub()
    fake.current_repo = repository(owner="other", private=False, description=None)
    imported = client(fake).get_repository("other", REPO, expected_repo_id=101)
    assert imported.owner == "other" and imported.private is False


@pytest.mark.parametrize("change", [
    {"private": False},
    {"html_url": "https://evil.example/alice/notes-lab"},
    {"clone_url": "https://evil.example/alice/notes-lab.git"},
    {"ssh_url": "git@evil.example:alice/notes-lab.git"},
    {"id": 999},
])
def test_create_refuses_wrong_visibility_origin_or_confirmed_id(change: dict[str, Any]) -> None:
    fake = FakeGitHub()
    fake.current_repo = repository()
    fake.current_repo.update(change)
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private(OWNER, REPO, MARKER, expected_repo_id=101)
    assert error.value.code in {"REPOSITORY_NOT_PRIVATE", "GITHUB_IDENTITY_MISMATCH"}


def test_draft_pr_create_checks_exact_branch_and_sends_draft_true() -> None:
    fake = FakeGitHub()
    repo = registered(fake)
    result = client(fake).create_or_update_draft_pr(
        repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
        title="Tested change", body="Report",
    )
    assert result.disposition == "created"
    assert result.pull_request.id == 807 and result.pull_request.node_id == "PR_7"
    assert result.pull_request.draft is True
    posts = [call for call in fake.calls if call[0] == "POST"]
    assert posts == [("POST", f"/repos/{OWNER}/{REPO}/pulls", {}, {
        "title": "Tested change", "body": "Report", "head": "alice:work/item",
        "base": "main", "draft": True,
    })]
    listed = [call for call in fake.calls if call[1].endswith("/pulls") and call[0] == "GET"]
    assert listed[0][2] == {"state": "open", "head": "alice:work/item",
                            "base": "main", "per_page": "100"}


def test_pr_create_timeout_reconciles_and_never_duplicates() -> None:
    fake = FakeGitHub()
    repo = registered(fake)
    fake.pr_create_timeout = True
    result = client(fake).create_or_update_draft_pr(
        repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
        title="Tested change", body="Report",
    )
    assert result.disposition == "reconciled"
    assert len(fake.prs) == 1
    assert len([call for call in fake.calls if call[0] == "POST"]) == 1


def test_existing_draft_updates_only_text_and_timeout_reconciles() -> None:
    fake = FakeGitHub()
    repo = registered(fake)
    fake.prs = [pull_request(title="Old", body="Old")]
    fake.pr_update_timeout = True
    result = client(fake).create_or_update_draft_pr(
        repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
        title="New", body="Known failures",
    )
    assert result.disposition == "reconciled"
    assert fake.prs[0]["title"] == "New" and fake.prs[0]["body"] == "Known failures"
    assert [call[0] for call in fake.calls if call[0] in {"POST", "PATCH"}] == ["PATCH"]


def test_ready_or_duplicate_pr_and_changed_head_are_refused() -> None:
    fake = FakeGitHub()
    repo = registered(fake)
    fake.prs = [pull_request(draft=False)]
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_update_draft_pr(
            repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
            title="Tested change", body="Report",
        )
    assert error.value.code == "PULL_REQUEST_NOT_DRAFT"
    assert not any(call[0] in {"POST", "PATCH"} for call in fake.calls)

    fake.prs = [pull_request(7), pull_request(8)]
    with pytest.raises(GitHubError) as error:
        client(fake).find_open_pull_request(repo, "work/item", "main")
    assert error.value.code == "PULL_REQUEST_CONFLICT"

    fake.prs = []
    fake.branches["work/item"] = "c" * 40
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_update_draft_pr(
            repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
            title="Tested change", body="Report",
        )
    assert error.value.code == "BRANCH_CHANGED"


@pytest.mark.parametrize(("status", "message", "code"), [
    (422, "No commits between main and work/item", "NO_DIFF"),
    (422, "Draft pull requests are not supported", "PUBLICATION_CAPABILITY_MISSING"),
    (403, "Draft pull requests are not available", "PUBLICATION_CAPABILITY_MISSING"),
    (403, "Resource not accessible by integration", "GITHUB_AUTHORIZATION_DENIED"),
])
def test_pr_rejection_is_typed_without_ready_fallback(status: int, message: str,
                                                      code: str) -> None:
    fake = FakeGitHub()
    repo = registered(fake)
    fake.post_status = status
    fake.post_data = {"message": message}
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_update_draft_pr(
            repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
            title="Tested change", body="Report",
        )
    assert error.value.code == code
    assert not fake.prs


def test_ambiguous_writes_return_unknown_when_readback_cannot_prove_identity() -> None:
    fake = FakeGitHub()
    fake.repo_create_timeout = True
    fake.repo_readback_failure = True
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private(OWNER, REPO, MARKER)
    assert error.value.code == "REPOSITORY_CREATE_UNKNOWN"
    assert len([call for call in fake.calls if call[0] == "POST"]) == 1

    fake = FakeGitHub()
    repo = registered(fake)
    fake.pr_create_timeout = True
    fake.pr_readback_failure = True
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_update_draft_pr(
            repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
            title="Tested change", body="Report",
        )
    assert error.value.code == "PULL_REQUEST_CREATE_UNKNOWN"
    assert len([call for call in fake.calls if call[0] == "POST"]) == 1


def test_provider_exception_text_never_reaches_error() -> None:
    fake = FakeGitHub()
    fake.repo_read_failure = True
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_attach_private(OWNER, REPO, MARKER)
    assert "secret-provider-error" not in str(error.value)



def test_read_only_reconciliation_never_replays_repo_or_pr_write() -> None:
    fake = FakeGitHub()
    api = client(fake)
    with pytest.raises(GitHubError) as error:
        api.reconcile_private_repository(OWNER, REPO, MARKER)
    assert error.value.code == "REPOSITORY_CREATE_UNKNOWN"
    assert not any(call[0] == "POST" for call in fake.calls)

    fake.current_repo = repository()
    repo = api.reconcile_private_repository(OWNER, REPO, MARKER).repository
    assert repo.id == 101
    with pytest.raises(GitHubError) as error:
        api.reconcile_draft_pr(
            repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
        )
    assert error.value.code == "PULL_REQUEST_CREATE_UNKNOWN"
    fake.prs = [pull_request()]
    pr = api.reconcile_draft_pr(
        repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
        expected_pr_id=807,
    )
    assert pr.disposition == "reconciled" and pr.pull_request.id == 807
    assert not any(call[0] in {"POST", "PATCH"} for call in fake.calls)


def test_remote_default_branch_change_is_rechecked_before_pr_write() -> None:
    fake = FakeGitHub()
    repo = registered(fake)
    assert fake.current_repo is not None
    fake.current_repo["default_branch"] = "work/item"
    with pytest.raises(GitHubError) as error:
        client(fake).create_or_update_draft_pr(
            repo, head_branch="work/item", base_branch="main", expected_head_sha=HEAD_SHA,
            title="Tested change", body="Report",
        )
    assert error.value.code == "PROTECTED_BRANCH"
    assert not any(call[0] in {"POST", "PATCH"} for call in fake.calls)
