"""Exact-identity GitHub REST operations for EP-05 provisioning and draft PRs.

The caller must persist a locally derived provision marker before invoking repository
creation. This adapter never retries a write after a lost response. It reads the exact
remote resource and returns a reconciled result only when identity is proved.
Construction and imports perform no network I/O or credential lookup.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from runner_app.git.argv import GitError, validate_branch

_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z", re.ASCII)
_REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z", re.ASCII)
_MARKER = re.compile(r"cdr:v1:[0-9a-f]{64}\Z", re.ASCII)
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RESPONSE = 1_048_576
_API_ROOT = "https://api.github.com"


class GitHubError(RuntimeError):
    """Stable provider finding; raw HTTP bodies and credentials are never included."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GitHubHTTPResponse:
    status: int
    data: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class GitHubTransport(Protocol):
    def request(self, method: str, path: str, *,
                params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class UrllibGitHubTransport:
    """Inert owner-process transport; the token callback must use owner-only storage.

    Requests are restricted to api.github.com. Redirects and environment proxies are
    disabled so an Authorization header cannot be forwarded to another destination.
    """

    def __init__(self, token_provider: Callable[[], str], *, timeout: float = 10.0) -> None:
        if not callable(token_provider) or not 1 <= timeout <= 30:
            raise ValueError("A token provider and bounded timeout are required")
        self._token_provider = token_provider
        self._timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, *,
                params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        if method not in {"GET", "POST", "PATCH"} or not path.startswith("/") or "//" in path:
            raise ValueError("Unsupported GitHub request")
        if any(ch in path for ch in "?#\r\n\x00"):
            raise ValueError("Invalid GitHub request path")
        token = self._token_provider()
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)):
            raise GitHubError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub credential is unavailable")
        query = "?" + urllib.parse.urlencode(params) if params else ""
        payload = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode() if body else None
        if payload is not None and len(payload) > 131_072:
            raise ValueError("GitHub request exceeds local size limit")
        request = urllib.request.Request(
            _API_ROOT + path + query, data=payload, method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "chat-dev-runner",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub response exceeds local limit")
            try:
                data = json.loads(raw) if raw else {}
            except (UnicodeError, ValueError):
                data = None
            headers = {
                "x-ratelimit-remaining": response.headers.get("X-RateLimit-Remaining", ""),
            }
            return GitHubHTTPResponse(int(response.status), data, headers)


@dataclass(frozen=True)
class GitHubIdentity:
    login: str
    id: int


@dataclass(frozen=True)
class GitHubRepository:
    id: int
    node_id: str
    owner: str
    owner_id: int
    name: str
    full_name: str
    html_url: str
    clone_url: str
    ssh_url: str
    private: bool
    default_branch: str | None
    description: str | None


@dataclass(frozen=True)
class RepositoryResult:
    repository: GitHubRepository
    disposition: Literal["created", "attached", "reconciled"]


@dataclass(frozen=True)
class GitHubBranch:
    name: str
    sha: str
    protected: bool


@dataclass(frozen=True)
class GitHubPullRequest:
    id: int
    node_id: str
    number: int
    html_url: str
    head_branch: str
    head_sha: str
    base_branch: str
    draft: bool
    title: str
    body: str


@dataclass(frozen=True)
class PullRequestResult:
    pull_request: GitHubPullRequest
    disposition: Literal["created", "attached", "updated", "reconciled"]


def make_provision_marker(installation_id: str, project_id: str, operation_id: str) -> str:
    """Produce a stable, non-secret marker for a *persisted* provisioning intent."""
    parts = (installation_id, project_id, operation_id)
    if any(not isinstance(part, str) or not 1 <= len(part) <= 256
           or any(ord(ch) < 33 or ord(ch) > 126 for ch in part) for part in parts):
        raise ValueError("Canonical installation, project and operation IDs are required")
    digest = hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()
    return "cdr:v1:" + digest


def _positive_id(value: Any) -> bool:
    return type(value) is int and value > 0


def _same_name(actual: Any, expected: str) -> bool:
    return isinstance(actual, str) and actual.casefold() == expected.casefold()


def _web_url(value: Any, owner: str, repo: str, suffix: str = "") -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlsplit(value)
    return (parsed.scheme == "https" and parsed.netloc == "github.com"
            and parsed.path.casefold() == f"/{owner}/{repo}{suffix}".casefold()
            and not parsed.query and not parsed.fragment)


def _valid_branch(value: str) -> str:
    try:
        return validate_branch(value)
    except GitError:
        raise GitHubError("INVALID_BRANCH", "GitHub branch name is invalid") from None


def _error_for_response(response: GitHubHTTPResponse, *, read: bool) -> GitHubError:
    status = response.status
    if status == 401:
        return GitHubError("GITHUB_AUTHENTICATION_FAILED", "GitHub credential was rejected")
    if status == 403:
        if response.headers.get("x-ratelimit-remaining") == "0":
            return GitHubError("GITHUB_QUOTA_EXCEEDED", "GitHub rate limit is exhausted")
        return GitHubError("GITHUB_AUTHORIZATION_DENIED", "GitHub operation is not authorized")
    if status == 429:
        return GitHubError("GITHUB_QUOTA_EXCEEDED", "GitHub rate limit is exhausted")
    if status in _TRANSIENT:
        return GitHubError("GITHUB_PROVIDER_UNAVAILABLE", "GitHub did not complete the request")
    if 300 <= status < 400:
        return GitHubError("GITHUB_IDENTITY_CHANGED", "GitHub redirected the exact resource")
    return GitHubError(
        "GITHUB_READ_REJECTED" if read else "GITHUB_WRITE_REJECTED",
        "GitHub rejected the operation",
    )


def _validation_code(response: GitHubHTTPResponse) -> str:
    if response.status not in {403, 422} or not isinstance(response.data, dict):
        return "GITHUB_WRITE_REJECTED"
    messages: list[str] = []
    value = response.data.get("message")
    if isinstance(value, str):
        messages.append(value.casefold())
    errors = response.data.get("errors")
    if isinstance(errors, list):
        for item in errors[:16]:
            if isinstance(item, dict) and isinstance(item.get("message"), str):
                messages.append(item["message"].casefold())
    if any("no commits between" in value for value in messages):
        return "NO_DIFF"
    if any("draft" in value and ("not supported" in value or "not available" in value)
           for value in messages):
        return "PUBLICATION_CAPABILITY_MISSING"
    return "GITHUB_WRITE_REJECTED"


class GitHubREST:
    """Narrow GitHub provider interface; no Git subprocess or project-code execution."""

    def __init__(self, transport: GitHubTransport, *, expected_login: str,
                 expected_user_id: int | None = None) -> None:
        if not isinstance(expected_login, str) or not _LOGIN.fullmatch(expected_login):
            raise ValueError("Exact authenticated GitHub login is required")
        if expected_user_id is not None and not _positive_id(expected_user_id):
            raise ValueError("Expected GitHub user ID must be positive")
        self._transport = transport
        self.expected_login = expected_login
        self.expected_user_id = expected_user_id

    def _request(self, method: str, path: str, *, params: Mapping[str, str] | None = None,
                 body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        try:
            response = self._transport.request(method, path, params=params, body=body)
        except GitHubError:
            raise
        except Exception:  # noqa: BLE001 - never expose provider/credential exception text
            raise GitHubError("GITHUB_PROVIDER_UNAVAILABLE", "GitHub request outcome is unknown") from None
        if not isinstance(response, GitHubHTTPResponse) or not 100 <= response.status <= 599:
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub returned an invalid HTTP response")
        return response

    def _read(self, path: str, *, params: Mapping[str, str] | None = None,
              allow_missing: bool = False) -> Any:
        response = self._request("GET", path, params=params)
        if response.status == 404 and allow_missing:
            return None
        if response.status != 200:
            raise _error_for_response(response, read=True)
        return response.data

    def authenticated_identity(self) -> GitHubIdentity:
        data = self._read("/user")
        if not isinstance(data, dict) or not _same_name(data.get("login"), self.expected_login):
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Authenticated GitHub login differs")
        identifier = data.get("id")
        if not _positive_id(identifier):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "Authenticated GitHub ID is missing")
        if self.expected_user_id is not None and identifier != self.expected_user_id:
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Authenticated GitHub ID differs")
        return GitHubIdentity(str(data["login"]), cast(int, identifier))

    def _owner(self, owner: str, identity: GitHubIdentity, *,
               personal_create: bool = False) -> tuple[int, str]:
        if not isinstance(owner, str) or not _LOGIN.fullmatch(owner):
            raise GitHubError("INVALID_REPOSITORY", "GitHub owner is invalid")
        data = self._read(f"/users/{owner}")
        if not isinstance(data, dict) or not _same_name(data.get("login"), owner):
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Target GitHub owner differs")
        if not _positive_id(data.get("id")) or data.get("type") not in {"User", "Organization"}:
            raise GitHubError("GITHUB_RESPONSE_INVALID", "Target GitHub owner identity is incomplete")
        if personal_create and data["type"] == "User" and data["id"] != identity.id:
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Personal repository owner differs")
        return int(data["id"]), str(data["type"])

    @staticmethod
    def _repo_path(owner: str, name: str) -> str:
        if not isinstance(owner, str) or not _LOGIN.fullmatch(owner):
            raise GitHubError("INVALID_REPOSITORY", "GitHub owner is invalid")
        if not isinstance(name, str) or not _REPO_NAME.fullmatch(name):
            raise GitHubError("INVALID_REPOSITORY", "GitHub repository name is invalid")
        return f"/repos/{owner}/{name}"

    @staticmethod
    def _parse_repository(data: Any, owner: str, name: str, *,
                          owner_id: int | None, require_private: bool) -> GitHubRepository:
        if not isinstance(data, dict) or not _positive_id(data.get("id")):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub repository ID is missing")
        remote_owner = data.get("owner")
        if not isinstance(remote_owner, dict) or not _positive_id(remote_owner.get("id")):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub repository owner is missing")
        if (not _same_name(remote_owner.get("login"), owner)
                or not _same_name(data.get("name"), name)
                or not _same_name(data.get("full_name"), f"{owner}/{name}")
                or owner_id is not None and remote_owner["id"] != owner_id
                or not _web_url(data.get("html_url"), owner, name)
                or not _web_url(data.get("clone_url"), owner, name, ".git")
                or not isinstance(data.get("ssh_url"), str)
                or data["ssh_url"].casefold() != f"git@github.com:{owner}/{name}.git".casefold()):
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "GitHub returned another repository")
        if type(data.get("private")) is not bool:
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub repository visibility is missing")
        if require_private and data["private"] is not True:
            raise GitHubError("REPOSITORY_NOT_PRIVATE", "Target repository is not private")
        node_id = data.get("node_id")
        if not isinstance(node_id, str) or not 1 <= len(node_id) <= 256:
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub repository node ID is missing")
        default = data.get("default_branch")
        if default is not None:
            if not isinstance(default, str):
                raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub default branch is invalid")
            default = _valid_branch(default)
        description = data.get("description")
        if description is not None and not isinstance(description, str):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub description is invalid")
        return GitHubRepository(
            id=data["id"], node_id=node_id, owner=str(remote_owner["login"]),
            owner_id=remote_owner["id"], name=str(data["name"]),
            full_name=str(data["full_name"]), html_url=str(data["html_url"]),
            clone_url=str(data["clone_url"]), ssh_url=str(data["ssh_url"]),
            private=data["private"], default_branch=default, description=description,
        )

    def _get_repository(self, owner: str, name: str, *, owner_id: int | None,
                        require_private: bool, allow_missing: bool = False) -> GitHubRepository | None:
        data = self._read(self._repo_path(owner, name), allow_missing=allow_missing)
        if data is None:
            return None
        return self._parse_repository(data, owner, name, owner_id=owner_id,
                                      require_private=require_private)

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        """Inspect an owner-approved existing repository without adopting it."""
        if expected_repo_id is not None and not _positive_id(expected_repo_id):
            raise ValueError("Expected repository ID must be positive")
        identity = self.authenticated_identity()
        owner_id, _ = self._owner(owner, identity)
        repo = self._get_repository(owner, name, owner_id=owner_id, require_private=False)
        if repo is None:
            raise GitHubError("REPOSITORY_MISSING", "Approved repository is unavailable")
        if expected_repo_id is not None and repo.id != expected_repo_id:
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Repository numeric ID differs")
        return repo

    def reconcile_private_repository(self, owner: str, name: str, provision_marker: str, *,
                                     expected_repo_id: int | None = None) -> RepositoryResult:
        """Read a prior create intent without issuing another repository POST."""
        self._repo_path(owner, name)
        if not isinstance(provision_marker, str) or not _MARKER.fullmatch(provision_marker):
            raise ValueError("Persisted canonical provision marker is required")
        if expected_repo_id is not None and not _positive_id(expected_repo_id):
            raise ValueError("Expected repository ID must be positive")
        identity = self.authenticated_identity()
        owner_id, _ = self._owner(owner, identity, personal_create=True)
        repo = self._get_repository(owner, name, owner_id=owner_id, require_private=True,
                                    allow_missing=True)
        if repo is None:
            raise GitHubError("REPOSITORY_CREATE_UNKNOWN", "Repository creation needs reconciliation")
        if expected_repo_id is not None and repo.id != expected_repo_id:
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Repository numeric ID differs")
        if expected_repo_id is None and repo.description != provision_marker:
            raise GitHubError("REPOSITORY_COLLISION", "Repository name belongs to another intent")
        return RepositoryResult(repo, "reconciled")

    def create_or_attach_private(self, owner: str, name: str, provision_marker: str, *,
                                 expected_repo_id: int | None = None) -> RepositoryResult:
        """Create private repo once, or prove an existing repo matches persisted intent.

        The caller must journal ``provision_marker`` before this method. A colliding name
        without that marker or a previously confirmed numeric ID is never adopted.
        """
        self._repo_path(owner, name)
        if not isinstance(provision_marker, str) or not _MARKER.fullmatch(provision_marker):
            raise ValueError("Persisted canonical provision marker is required")
        if expected_repo_id is not None and not _positive_id(expected_repo_id):
            raise ValueError("Expected repository ID must be positive")
        identity = self.authenticated_identity()
        owner_id, owner_type = self._owner(owner, identity, personal_create=True)
        repo = self._get_repository(owner, name, owner_id=owner_id, require_private=True,
                                    allow_missing=True)
        if repo is not None:
            if expected_repo_id is not None and repo.id != expected_repo_id:
                raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Repository numeric ID differs")
            if expected_repo_id is None and repo.description != provision_marker:
                raise GitHubError("REPOSITORY_COLLISION", "Repository name belongs to another intent")
            return RepositoryResult(repo, "attached")
        if expected_repo_id is not None:
            raise GitHubError("REPOSITORY_MISSING", "Previously confirmed repository is missing")
        path = "/user/repos" if owner_type == "User" else f"/orgs/{owner}/repos"
        payload = {"name": name, "description": provision_marker, "private": True,
                   "auto_init": False}
        uncertain = False
        try:
            response = self._request("POST", path, body=payload)
        except GitHubError:
            uncertain = True
            response = None
        if response is not None and response.status == 201:
            try:
                created = self._parse_repository(response.data, owner, name,
                                                 owner_id=owner_id, require_private=True)
                if created.description != provision_marker:
                    raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Provision marker differs")
                return RepositoryResult(created, "created")
            except GitHubError:
                uncertain = True
        elif response is not None:
            uncertain = response.status in _TRANSIENT or response.status == 422
        if uncertain:
            try:
                repo = self._get_repository(owner, name, owner_id=owner_id,
                                            require_private=True, allow_missing=True)
            except GitHubError:
                raise GitHubError("REPOSITORY_CREATE_UNKNOWN", "Repository creation needs reconciliation") from None
            if repo is not None:
                if repo.description != provision_marker:
                    raise GitHubError("REPOSITORY_COLLISION", "Repository name belongs to another intent")
                return RepositoryResult(repo, "reconciled")
            if response is None or response.status in _TRANSIENT or response.status == 201:
                raise GitHubError("REPOSITORY_CREATE_UNKNOWN", "Repository creation needs reconciliation")
        assert response is not None
        raise _error_for_response(response, read=False)

    def _ensure_repository(self, repository: GitHubRepository) -> GitHubRepository:
        identity = self.authenticated_identity()
        owner_id, _ = self._owner(repository.owner, identity)
        current = self._get_repository(repository.owner, repository.name,
                                       owner_id=owner_id, require_private=repository.private)
        if (current is None or current.id != repository.id
                or current.node_id != repository.node_id
                or current.owner_id != repository.owner_id):
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Repository changed since registration")
        return current

    def _branch_head(self, repository: GitHubRepository, branch: str) -> GitHubBranch | None:
        _valid_branch(branch)
        encoded = urllib.parse.quote(branch, safe="")
        data = self._read(self._repo_path(repository.owner, repository.name)
                          + f"/branches/{encoded}", allow_missing=True)
        if data is None:
            return None
        commit = data.get("commit") if isinstance(data, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if (not isinstance(data, dict) or data.get("name") != branch
                or not isinstance(sha, str) or not _SHA.fullmatch(sha)
                or type(data.get("protected")) is not bool):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub branch response is invalid")
        return GitHubBranch(branch, sha, data["protected"])

    def branch_head(self, repository: GitHubRepository, branch: str) -> GitHubBranch | None:
        self._ensure_repository(repository)
        return self._branch_head(repository, branch)

    @staticmethod
    def _parse_pull_request(data: Any, repository: GitHubRepository,
                            head_branch: str, base_branch: str) -> GitHubPullRequest:
        if not isinstance(data, dict) or not _positive_id(data.get("id")):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub PR ID is missing")
        number = data.get("number")
        head = data.get("head")
        base = data.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        base_repo = base.get("repo") if isinstance(base, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        if (not _positive_id(number) or data.get("state") != "open"
                or not isinstance(head_repo, dict)
                or not isinstance(base_repo, dict)
                or head_repo.get("id") != repository.id
                or base_repo.get("id") != repository.id
                or not isinstance(head, dict) or head.get("ref") != head_branch
                or not isinstance(base, dict) or base.get("ref") != base_branch
                or not isinstance(sha, str) or not _SHA.fullmatch(sha)
                or not _web_url(data.get("html_url"), repository.owner,
                                repository.name, f"/pull/{number}")):
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "GitHub returned another pull request")
        node_id = data.get("node_id")
        if (not isinstance(node_id, str) or not 1 <= len(node_id) <= 256
                or type(data.get("draft")) is not bool
                or not isinstance(data.get("title"), str)
                or not isinstance(data.get("body"), (str, type(None)))):
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub PR metadata is incomplete")
        return GitHubPullRequest(
            id=data["id"], node_id=node_id, number=cast(int, number),
            html_url=data["html_url"], head_branch=head_branch, head_sha=sha,
            base_branch=base_branch, draft=data["draft"], title=data["title"],
            body=data["body"] or "",
        )

    def _find_open_pull_request(self, repository: GitHubRepository, head_branch: str,
                                base_branch: str) -> GitHubPullRequest | None:
        data = self._read(self._repo_path(repository.owner, repository.name) + "/pulls",
                          params={"state": "open", "head": f"{repository.owner}:{head_branch}",
                                  "base": base_branch, "per_page": "100"})
        if not isinstance(data, list) or len(data) >= 100:
            raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub PR query is incomplete")
        matches: list[GitHubPullRequest] = []
        for item in data:
            if not isinstance(item, dict):
                raise GitHubError("GITHUB_RESPONSE_INVALID", "GitHub PR list is invalid")
            head = item.get("head")
            base = item.get("base")
            if (isinstance(head, dict) and isinstance(base, dict)
                    and head.get("ref") == head_branch and base.get("ref") == base_branch):
                matches.append(self._parse_pull_request(item, repository, head_branch, base_branch))
        if len(matches) > 1:
            raise GitHubError("PULL_REQUEST_CONFLICT", "Several open PRs match the branch and base")
        return matches[0] if matches else None

    def find_open_pull_request(self, repository: GitHubRepository, head_branch: str,
                               base_branch: str) -> GitHubPullRequest | None:
        _valid_branch(head_branch)
        _valid_branch(base_branch)
        self._ensure_repository(repository)
        return self._find_open_pull_request(repository, head_branch, base_branch)

    def _read_pull_request(self, repository: GitHubRepository, number: int,
                           head_branch: str, base_branch: str) -> GitHubPullRequest:
        data = self._read(self._repo_path(repository.owner, repository.name) + f"/pulls/{number}")
        pr = self._parse_pull_request(data, repository, head_branch, base_branch)
        if pr.number != number:
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "GitHub PR number changed")
        return pr

    def reconcile_draft_pr(self, repository: GitHubRepository, *,
                           head_branch: str, base_branch: str,
                           expected_head_sha: str,
                           expected_pr_id: int | None = None) -> PullRequestResult:
        """Read a prior PR intent without issuing another create or update write."""
        _valid_branch(head_branch)
        _valid_branch(base_branch)
        if not isinstance(expected_head_sha, str) or not _SHA.fullmatch(expected_head_sha):
            raise ValueError("Expected tested head SHA is invalid")
        if expected_pr_id is not None and not _positive_id(expected_pr_id):
            raise ValueError("Expected PR ID must be positive")
        self._ensure_repository(repository)
        branch = self._branch_head(repository, head_branch)
        if branch is None or branch.sha != expected_head_sha:
            raise GitHubError("BRANCH_CHANGED", "GitHub head differs from tested snapshot")
        observed = self._find_open_pull_request(repository, head_branch, base_branch)
        if observed is None:
            raise GitHubError("PULL_REQUEST_CREATE_UNKNOWN", "PR creation needs reconciliation")
        if expected_pr_id is not None and observed.id != expected_pr_id:
            raise GitHubError("GITHUB_IDENTITY_MISMATCH", "PR numeric ID differs")
        if not observed.draft:
            raise GitHubError("PULL_REQUEST_NOT_DRAFT", "Matching PR is not a draft")
        if observed.head_sha != expected_head_sha:
            raise GitHubError("BRANCH_CHANGED", "Matching PR head differs from tested snapshot")
        return PullRequestResult(observed, "reconciled")

    def update_draft_pr_body(self, repository: GitHubRepository, *,
                             head_branch: str, base_branch: str,
                             expected_head_sha: str, expected_pr_id: int,
                             expected_title: str, previous_body: str,
                             body: str) -> PullRequestResult:
        """PATCH only one proven draft PR; never create a replacement PR.

        A lost PATCH response is reconciled by reading the same numbered PR.
        The caller journals the intended body before invoking this method.
        """
        _valid_branch(head_branch)
        _valid_branch(base_branch)
        if (head_branch == base_branch
                or not isinstance(expected_head_sha, str)
                or _SHA.fullmatch(expected_head_sha) is None
                or not _positive_id(expected_pr_id)
                or not isinstance(expected_title, str)
                or not 1 <= len(expected_title) <= 256
                or not isinstance(previous_body, str)
                or not isinstance(body, str)
                or len(body.encode("utf-8")) > 65_536):
            raise ValueError("Exact draft PR identity and bounded body are required")
        matched = self.reconcile_draft_pr(
            repository, head_branch=head_branch, base_branch=base_branch,
            expected_head_sha=expected_head_sha, expected_pr_id=expected_pr_id,
        ).pull_request
        if matched.title != expected_title:
            raise GitHubError("PULL_REQUEST_CHANGED", "Draft PR title changed")
        if matched.body == body:
            return PullRequestResult(matched, "attached")
        if matched.body != previous_body:
            raise GitHubError("PULL_REQUEST_CHANGED", "Draft PR body changed")
        path = self._repo_path(repository.owner, repository.name) + f"/pulls/{matched.number}"
        try:
            response = self._request("PATCH", path, body={"body": body})
        except GitHubError:
            response = None
        if response is not None and response.status == 200:
            try:
                updated = self._parse_pull_request(response.data, repository,
                                                   head_branch, base_branch)
                if (updated.id == expected_pr_id and updated.number == matched.number
                        and updated.draft and updated.head_sha == expected_head_sha
                        and updated.title == expected_title and updated.body == body):
                    return PullRequestResult(updated, "updated")
            except GitHubError:
                pass
        try:
            observed = self._read_pull_request(repository, matched.number,
                                               head_branch, base_branch)
        except GitHubError:
            raise GitHubError("PULL_REQUEST_UPDATE_UNKNOWN",
                              "Draft PR body update needs reconciliation") from None
        if (observed.id == expected_pr_id and observed.draft
                and observed.head_sha == expected_head_sha
                and observed.title == expected_title and observed.body == body):
            return PullRequestResult(observed, "reconciled")
        if response is not None and response.status not in _TRANSIENT:
            raise _error_for_response(response, read=False)
        raise GitHubError("PULL_REQUEST_UPDATE_UNKNOWN",
                          "Draft PR body update needs reconciliation")

    def create_or_update_draft_pr(self, repository: GitHubRepository, *,
                                  head_branch: str, base_branch: str,
                                  expected_head_sha: str, title: str,
                                  body: str) -> PullRequestResult:
        """Create or update one matching draft; writes are never blindly replayed."""
        _valid_branch(head_branch)
        _valid_branch(base_branch)
        if (not isinstance(expected_head_sha, str) or not _SHA.fullmatch(expected_head_sha)
                or not isinstance(title, str) or not 1 <= len(title) <= 256
                or not isinstance(body, str) or len(body.encode("utf-8")) > 65_536):
            raise ValueError("Exact head SHA and bounded PR text are required")
        if head_branch == base_branch:
            raise GitHubError("PROTECTED_BRANCH", "A working branch distinct from base is required")
        current = self._ensure_repository(repository)
        if current.default_branch is None or head_branch == current.default_branch:
            raise GitHubError("PROTECTED_BRANCH", "The current default branch cannot be a PR head")
        head = self._branch_head(repository, head_branch)
        base = self._branch_head(repository, base_branch)
        if head is None or base is None:
            raise GitHubError("BRANCH_MISSING", "PR head or base branch is missing")
        if head.sha != expected_head_sha:
            raise GitHubError("BRANCH_CHANGED", "GitHub head differs from tested snapshot")
        existing = self._find_open_pull_request(repository, head_branch, base_branch)
        if existing is not None:
            if not existing.draft:
                raise GitHubError("PULL_REQUEST_NOT_DRAFT", "Matching PR is not a draft")
            if existing.head_sha != expected_head_sha:
                raise GitHubError("BRANCH_CHANGED", "Matching PR head differs from tested snapshot")
            if existing.title == title and existing.body == body:
                return PullRequestResult(existing, "attached")
            path = self._repo_path(repository.owner, repository.name) + f"/pulls/{existing.number}"
            try:
                response = self._request("PATCH", path, body={"title": title, "body": body})
            except GitHubError:
                response = None
            if response is not None and response.status == 200:
                try:
                    updated = self._parse_pull_request(response.data, repository,
                                                       head_branch, base_branch)
                    if (updated.id == existing.id and updated.number == existing.number
                            and updated.draft and updated.head_sha == expected_head_sha
                            and updated.title == title and updated.body == body):
                        return PullRequestResult(updated, "updated")
                except GitHubError:
                    pass
            try:
                observed = self._read_pull_request(repository, existing.number,
                                                   head_branch, base_branch)
            except GitHubError:
                raise GitHubError("PULL_REQUEST_UPDATE_UNKNOWN", "PR update needs reconciliation") from None
            if (observed.id == existing.id and observed.draft
                    and observed.head_sha == expected_head_sha
                    and observed.title == title and observed.body == body):
                return PullRequestResult(observed, "reconciled")
            if response is not None and response.status not in _TRANSIENT:
                raise _error_for_response(response, read=False)
            raise GitHubError("PULL_REQUEST_UPDATE_UNKNOWN", "PR update needs reconciliation")
        path = self._repo_path(repository.owner, repository.name) + "/pulls"
        payload = {"title": title, "body": body, "head": f"{repository.owner}:{head_branch}",
                   "base": base_branch, "draft": True}
        try:
            response = self._request("POST", path, body=payload)
        except GitHubError:
            response = None
        if response is not None and response.status == 201:
            try:
                created = self._parse_pull_request(response.data, repository,
                                                   head_branch, base_branch)
                if created.draft and created.head_sha == expected_head_sha:
                    return PullRequestResult(created, "created")
            except GitHubError:
                pass
        if (response is None or response.status == 201
                or response.status in _TRANSIENT or response.status in {403, 422}):
            try:
                matching = self._find_open_pull_request(repository, head_branch, base_branch)
            except GitHubError:
                raise GitHubError("PULL_REQUEST_CREATE_UNKNOWN", "PR creation needs reconciliation") from None
            if matching is not None:
                if not matching.draft:
                    raise GitHubError("PULL_REQUEST_NOT_DRAFT", "Matching PR is not a draft")
                if matching.head_sha != expected_head_sha:
                    raise GitHubError("BRANCH_CHANGED", "Matching PR head differs from tested snapshot")
                return PullRequestResult(matching, "reconciled")
            if response is not None and response.status == 429:
                raise _error_for_response(response, read=False)
            if response is None or response.status == 201 or response.status in _TRANSIENT:
                raise GitHubError("PULL_REQUEST_CREATE_UNKNOWN", "PR creation needs reconciliation")
        assert response is not None
        if response.status in {403, 422}:
            code = _validation_code(response)
            if code != "GITHUB_WRITE_REJECTED":
                raise GitHubError(code, "GitHub rejected draft publication")
        raise _error_for_response(response, read=False)
