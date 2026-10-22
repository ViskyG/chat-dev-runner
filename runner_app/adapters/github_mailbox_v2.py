"""Bounded GitHub v2 mailbox and same-branch result transports.

Construction is inert. Credentials stay behind an owner-process callable. All
mutable publication uses PATCH of an existing ref, force=false; no create-ref,
history polling, automatic branch, PR, release, LFS or write replay is provided.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from runner_app.adapters.github import (
    GitHubError,
    GitHubHTTPResponse,
    GitHubRepository,
    GitHubREST,
    GitHubTransport,
    _NoRedirect,
)
from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import parse_contract_json
from runner_app.domain.mailbox_v2 import validate_mailbox_shape
from runner_app.engine.intake_v2 import AckResult, Observation
from runner_app.git.argv import (
    _BASE_CONFIG,
    GitError,
    _environment,
    _terminate,
    run_git,
    run_git_bytes,
    validate_branch,
)
from runner_app.git.github_transport import (
    OwnerTokenFile,
    _reject_network_config,
    _run_network_git,
    _validate_token,
)
from runner_app.git.results_v2 import COMMIT_AUTHOR, ResultPushRejected, _managed_repo

_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z", re.ASCII)
_RESULT_PATH = re.compile(
    rb"\.runner/results/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/"
    rb"[A-Za-z0-9][A-Za-z0-9_-]{0,127}/.+\Z", re.ASCII,
)
_AUTHOR_NAME = "Runner Results"
_AUTHOR_EMAIL = "runner-results@localhost"
_STATE_PATH = re.compile(
    rb"\.runner/state/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/"
    rb"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json\Z", re.ASCII,
)


class GitHubV2Error(OSError):
    """A stable code without credentials, HTTP bodies or provider diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GitHubV2Limits:
    mailbox_bytes: int = 1024 * 1024
    blob_bytes: int = 10 * 1024 * 1024
    bundle_bytes: int = 25 * 1024 * 1024
    json_bytes: int = 16 * 1024 * 1024
    tree_entries: int = 8192
    objects: int = 8192


DEFAULT_LIMITS = GitHubV2Limits()
_OBJECT_CACHE_ENTRIES = 128
_OBJECT_CACHE_BYTES = 4 * 1024 * 1024
_BLOB_UPLOAD_WORKERS = 4
_CONDITIONAL_CACHE_ENTRIES = 64
_CONDITIONAL_CACHE_BYTES = 1024 * 1024
_CONDITIONAL_PATH = re.compile(r"/repos/[^/]+/[^/]+(?:/git/ref/heads/.+)?\Z")
_RESPONSE_HEADERS = ("etag", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                     "x-ratelimit-reset", "x-poll-interval")


def _sha(value: Any) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise GitHubV2Error("GITHUB_OBJECT_INVALID")
    return value


def _git_hash(kind: str, data: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data,
                        usedforsecurity=False).hexdigest()


def _json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def _command_path(path: str) -> str:
    if (not isinstance(path, str) or len(path.encode()) > 2048
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(c in path for c in "\\:\0\r\n\t")
            or any(ord(c) < 32 for c in path)):
        raise GitHubV2Error("INVALID_COMMAND_PATH")
    return path


class BoundedGitHubV2HTTP:
    """HTTPS api.github.com only, no redirects/proxies, bounded large-blob JSON."""

    # GitHub accounts share quota across repositories and transports. Keys are
    # credential fingerprints kept only in memory, never diagnostics or evidence.
    _cooldowns: ClassVar[OrderedDict[str, tuple[float, int]]] = OrderedDict()
    _cooldown_lock = threading.Lock()

    def __init__(self, token_provider: Callable[[], str], *, timeout: float = 20,
                 limits: GitHubV2Limits = DEFAULT_LIMITS) -> None:
        if not callable(token_provider) or not 1 <= timeout <= 30:
            raise ValueError("An owner token callable and bounded timeout are required")
        self._token_provider, self._timeout, self._limits = token_provider, timeout, limits
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._conditional: OrderedDict[tuple[str, str], tuple[str, bytes]] = OrderedDict()
        self._conditional_bytes = 0
        self._conditional_lock = threading.Lock()

    def _cached_response(self, key: tuple[str, str]) -> tuple[str, bytes] | None:
        with self._conditional_lock:
            value = self._conditional.get(key)
            if value is not None:
                self._conditional.move_to_end(key)
            return value

    def _remember_response(self, key: tuple[str, str], etag: str, payload: bytes) -> None:
        budget = min(_CONDITIONAL_CACHE_BYTES, self._limits.json_bytes)

        def size(entry_key: tuple[str, str], entry: tuple[str, bytes]) -> int:
            return len(entry_key[0]) + len(entry_key[1].encode()) + len(entry[0]) + len(entry[1])

        with self._conditional_lock:
            previous = self._conditional.pop(key, None)
            if previous is not None:
                self._conditional_bytes -= size(key, previous)
            if (not etag or len(etag) > 512 or any(ord(char) < 32 or ord(char) > 126 for char in etag)
                    or size(key, (etag, payload)) > budget):
                return
            self._conditional[key] = (etag, payload)
            self._conditional_bytes += size(key, (etag, payload))
            while (len(self._conditional) > _CONDITIONAL_CACHE_ENTRIES
                   or self._conditional_bytes > budget):
                removed_key, removed = self._conditional.popitem(last=False)
                self._conditional_bytes -= size(removed_key, removed)

    @classmethod
    def _check_cooldown(cls, auth: str) -> None:
        with cls._cooldown_lock:
            state = cls._cooldowns.get(auth)
            if state is not None and state[0] > time.monotonic():
                raise GitHubV2Error("GITHUB_RATE_LIMITED")

    @classmethod
    def _update_cooldown(cls, auth: str, status: int, headers: Mapping[str, str],
                         data: Any = None) -> None:
        now = time.monotonic()
        message = data.get("message") if isinstance(data, dict) else None
        rate_message = isinstance(message, str) and message.lower().startswith((
            "api rate limit exceeded", "you have exceeded a secondary rate limit",
            "you have exceeded a rate limit"))
        limited = (status == 429 or headers.get("x-ratelimit-remaining") == "0"
                   or status == 403 and (bool(headers.get("retry-after")) or rate_message))
        with cls._cooldown_lock:
            previous = cls._cooldowns.get(auth, (0.0, 0))
            if not limited:
                if previous[0] <= now:
                    cls._cooldowns.pop(auth, None)
                return
            failures = min(previous[1] + 1, 7)
            delay = float(min(3600, 60 * 2 ** (failures - 1)))
            advised: list[float] = []
            for name in ("retry-after", "x-ratelimit-reset"):
                if name == "x-ratelimit-reset" and headers.get("x-ratelimit-remaining") != "0":
                    continue
                try:
                    value = float(headers[name])
                except (KeyError, ValueError):
                    continue
                if name == "x-ratelimit-reset":
                    value -= time.time()
                if math.isfinite(value) and value > 0:
                    advised.append(value)
            if advised:
                delay = max(advised)
            cls._cooldowns[auth] = (max(previous[0], now + delay), failures)
            cls._cooldowns.move_to_end(auth)
            while len(cls._cooldowns) > 128:
                cls._cooldowns.popitem(last=False)

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        if (method not in {"GET", "POST", "PATCH"} or not path.startswith("/repos/")
                or "//" in path or any(c in path for c in "?#\r\n\0")
                or any(p in {".", ".."} for p in urllib.parse.unquote(path).split("/"))):
            raise GitHubV2Error("GITHUB_REQUEST_INVALID")
        raw = _json(body) if body is not None else None
        if raw is not None and len(raw) > self._limits.json_bytes:
            raise GitHubV2Error("GITHUB_JSON_LIMIT")
        try:
            token = _validate_token(self._token_provider()).decode("ascii")
        except Exception:  # noqa: BLE001 - a credential provider may include secret exception text
            raise GitHubV2Error("GITHUB_AUTH_UNAVAILABLE") from None
        auth = hashlib.sha256(token.encode("ascii")).hexdigest()
        self._check_cooldown(auth)
        cache_key = ((auth, path) if method == "GET" and body is None and not params
                     and _CONDITIONAL_PATH.fullmatch(path) else None)
        cached = self._cached_response(cache_key) if cache_key is not None else None
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(
            "https://api.github.com" + path + query, data=raw, method=method,
            headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "chat-dev-runner-v2",
                     **({"If-None-Match": cached[0]} if cached is not None else {}),
                     **({"Content-Type": "application/json"} if raw is not None else {})},
        )
        try:
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                headers = {name: str(response.headers.get(name, ""))[:1024]
                           for name in _RESPONSE_HEADERS}
                status = int(response.status)
                data = response.read(self._limits.json_bytes + 1)
                if len(data) > self._limits.json_bytes:
                    self._update_cooldown(auth, status, headers)
                    raise GitHubV2Error("GITHUB_JSON_LIMIT")
                if status == 304:
                    self._update_cooldown(auth, status, headers)
                    if cached is None:
                        raise GitHubV2Error("GITHUB_CONDITIONAL_CACHE_MISS")
                    # This response is freshly authorized by GitHub. Parsing the
                    # stored bytes also gives each caller an independent body.
                    return GitHubHTTPResponse(200, json.loads(cached[1]), headers)
                parsed = None
                try:
                    parsed = json.loads(data) if data else {}
                finally:
                    self._update_cooldown(auth, status, headers, parsed)
                if cache_key is not None:
                    if status == 200:
                        self._remember_response(cache_key, headers["etag"], data or b"{}")
                    elif status in {401, 403, 404}:
                        self._remember_response(cache_key, "", b"")
                return GitHubHTTPResponse(status, parsed, headers)
        except GitHubV2Error:
            raise
        except Exception:  # noqa: BLE001 - urllib exceptions may contain private request details
            raise GitHubV2Error("GITHUB_REQUEST_UNKNOWN") from None


@dataclass(frozen=True)
class _File:
    head: str
    tree: str
    blob: str
    mode: str
    payload: bytes


class GitHubV2Client:
    """Git Data client pinned to the enrolled repository and owner numeric IDs."""

    def __init__(self, repository: GitHubRepository, token_provider: Callable[[], str], *,
                 transport: GitHubTransport | None = None,
                 limits: GitHubV2Limits = DEFAULT_LIMITS) -> None:
        if (not callable(token_provider) or not _LOGIN.fullmatch(repository.owner)
                or not _NAME.fullmatch(repository.name)
                or type(repository.id) is not int or repository.id <= 0
                or type(repository.owner_id) is not int or repository.owner_id <= 0
                or type(repository.private) is not bool
                or not isinstance(repository.node_id, str) or not 1 <= len(repository.node_id) <= 256
                or repository.full_name != f"{repository.owner}/{repository.name}"
                or repository.clone_url != f"https://github.com/{repository.full_name}.git"
                or repository.html_url != f"https://github.com/{repository.full_name}"):
            raise GitHubV2Error("GITHUB_ENROLLMENT_INVALID")
        if any(type(n) is not int or n < 1 for n in (
            limits.mailbox_bytes, limits.blob_bytes, limits.bundle_bytes,
            limits.json_bytes, limits.tree_entries, limits.objects,
        )):
            raise GitHubV2Error("GITHUB_BUDGET_INVALID")
        self.repository, self.limits = repository, limits
        self._token_provider = token_provider
        self._transport = transport or BoundedGitHubV2HTTP(token_provider, limits=limits)
        self._prefix = f"/repos/{repository.owner}/{repository.name}"
        # Only validated, content-addressed objects are reusable. Identity and
        # branch HEAD must still be read from the provider for every observation.
        self._object_cache: OrderedDict[tuple[str, str], tuple[Any, int]] = OrderedDict()
        self._object_cache_bytes = 0
        self._object_cache_lock = threading.Lock()

    def _cached_object(self, kind: str, oid: str) -> Any:
        with self._object_cache_lock:
            key = (kind, oid)
            entry = self._object_cache.get(key)
            if entry is None:
                return None
            self._object_cache.move_to_end(key)
            # A caller must not be able to mutate a cached tree response.
            return copy.deepcopy(entry[0])

    def _cache_object(self, kind: str, oid: str, value: Any, size: int) -> None:
        budget = min(_OBJECT_CACHE_BYTES, self.limits.json_bytes)
        if size > budget:
            return
        with self._object_cache_lock:
            key = (kind, oid)
            prior = self._object_cache.pop(key, None)
            if prior is not None:
                self._object_cache_bytes -= prior[1]
            self._object_cache[key] = (copy.deepcopy(value), size)
            self._object_cache_bytes += size
            while (len(self._object_cache) > min(_OBJECT_CACHE_ENTRIES, self.limits.objects)
                   or self._object_cache_bytes > budget):
                _, (_, removed_size) = self._object_cache.popitem(last=False)
                self._object_cache_bytes -= removed_size

    def request(self, method: str, suffix: str, *,
                body: Mapping[str, Any] | None = None) -> GitHubHTTPResponse:
        if body is not None and len(_json(body)) > self.limits.json_bytes:
            raise GitHubV2Error("GITHUB_JSON_LIMIT")
        try:
            response = self._transport.request(method, self._prefix + suffix, body=body)
            if (not isinstance(response, GitHubHTTPResponse)
                    or type(response.status) is not int or not 100 <= response.status <= 599
                    or len(_json(response.data)) > self.limits.json_bytes):
                raise GitHubV2Error("GITHUB_RESPONSE_INVALID")
            return response
        except GitHubV2Error:
            raise
        except Exception:  # noqa: BLE001 - transport diagnostics/credentials must remain private
            raise GitHubV2Error("GITHUB_REQUEST_UNKNOWN") from None

    def read(self, suffix: str, *, missing: bool = False) -> Any:
        response = self.request("GET", suffix)
        if missing and response.status == 404:
            return None
        if response.status != 200:
            raise GitHubV2Error("GITHUB_READ_FAILED")
        return response.data

    def ensure_identity(self) -> None:
        data = self.read("")
        try:
            actual = GitHubREST._parse_repository(
                data, self.repository.owner, self.repository.name,
                owner_id=self.repository.owner_id, require_private=self.repository.private,
            )
        except GitHubError:
            raise GitHubV2Error("GITHUB_IDENTITY_MISMATCH") from None
        if (actual.id != self.repository.id or actual.node_id != self.repository.node_id
                or actual.owner_id != self.repository.owner_id
                or actual.private != self.repository.private):
            raise GitHubV2Error("GITHUB_IDENTITY_MISMATCH")

    def head(self, branch: str) -> str | None:
        ref = f"refs/heads/{validate_branch(branch)}"
        data = self.read("/git/ref/heads/" + urllib.parse.quote(branch, safe="/"), missing=True)
        if data is None:
            return None
        if (not isinstance(data, dict) or data.get("ref") != ref
                or not isinstance(data.get("object"), dict)
                or data["object"].get("type") != "commit"):
            raise GitHubV2Error("GITHUB_REF_INVALID")
        return _sha(data["object"].get("sha"))

    def commit_tree(self, commit: str) -> str:
        commit = _sha(commit)
        cached = self._cached_object("commit", commit)
        if isinstance(cached, str):
            return cached
        data = self.read(f"/git/commits/{commit}")
        if (not isinstance(data, dict) or data.get("sha") != commit
                or not isinstance(data.get("tree"), dict)):
            raise GitHubV2Error("GITHUB_OBJECT_INVALID")
        tree = _sha(data["tree"].get("sha"))
        self._cache_object("commit", commit, tree, len(tree))
        return tree

    def tree(self, tree: str) -> list[dict[str, Any]]:
        tree = _sha(tree)
        cached = self._cached_object("tree", tree)
        if isinstance(cached, list):
            return cached
        data = self.read(f"/git/trees/{tree}")
        if (not isinstance(data, dict) or data.get("sha") != tree
                or data.get("truncated") is not False or not isinstance(data.get("tree"), list)
                or len(data["tree"]) > self.limits.tree_entries):
            raise GitHubV2Error("GITHUB_TREE_INVALID")
        entries: list[dict[str, Any]] = data["tree"]
        names: set[str] = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or not entry["path"] or "/" in entry["path"] or "\0" in entry["path"]
                    or entry["path"] in names):
                raise GitHubV2Error("GITHUB_TREE_INVALID")
            _sha(entry.get("sha"))
            names.add(entry["path"])
        self._cache_object("tree", tree, entries, len(_json(entries)))
        return entries

    def blob(self, blob: str, budget: int) -> bytes:
        blob = _sha(blob)
        cached = self._cached_object("blob", blob)
        if isinstance(cached, bytes):
            if len(cached) > budget:
                raise GitHubV2Error("GITHUB_BLOB_INVALID")
            return cached
        data = self.read(f"/git/blobs/{blob}")
        if (not isinstance(data, dict) or data.get("sha") != blob
                or data.get("encoding") != "base64" or not isinstance(data.get("content"), str)
                or type(data.get("size")) is not int or not 0 <= data["size"] <= budget):
            raise GitHubV2Error("GITHUB_BLOB_INVALID")
        try:
            encoded = "".join(data["content"].split()).encode("ascii")
            if len(encoded) > ((budget + 2) // 3) * 4:
                raise GitHubV2Error("GITHUB_BLOB_INVALID")
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError):
            raise GitHubV2Error("GITHUB_BLOB_INVALID") from None
        if len(raw) != data["size"] or _git_hash("blob", raw) != blob:
            raise GitHubV2Error("GITHUB_BLOB_INVALID")
        self._cache_object("blob", blob, raw, len(raw))
        return raw

    def current_file(self, branch: str, path: str) -> _File:
        self.ensure_identity()
        head = self.head(branch)
        if head is None:
            raise GitHubV2Error("TARGET_BRANCH_MISSING")
        root = tree = self.commit_tree(head)
        parts = _command_path(path).split("/")
        for index, part in enumerate(parts):
            entry = next((entry for entry in self.tree(tree) if entry["path"] == part), None)
            if entry is None:
                raise GitHubV2Error("COMMAND_BLOB_MISSING")
            if index != len(parts) - 1:
                if (entry.get("mode"), entry.get("type")) != ("040000", "tree"):
                    raise GitHubV2Error("COMMAND_PATH_UNSAFE")
                tree = entry["sha"]
            else:
                if entry.get("mode") not in {"100644", "100755"} or entry.get("type") != "blob":
                    raise GitHubV2Error("COMMAND_PATH_UNSAFE")
                return _File(head, root, entry["sha"], entry["mode"],
                             self.blob(entry["sha"], self.limits.mailbox_bytes))
        raise GitHubV2Error("COMMAND_BLOB_MISSING")

    def create_blob(self, raw: bytes) -> str:
        if len(raw) > self.limits.blob_bytes:
            raise GitHubV2Error("GITHUB_BLOB_LIMIT")
        expected = _git_hash("blob", raw)
        response = self.request("POST", "/git/blobs", body={
            "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64",
        })
        if response.status != 201 or not isinstance(response.data, dict) or response.data.get("sha") != expected:
            raise GitHubV2Error("GITHUB_OBJECT_UPLOAD_FAILED")
        return expected

    def create_tree(self, entries: Sequence[Mapping[str, str]], *, base: str | None = None,
                    expected: str | None = None) -> str:
        if len(entries) > self.limits.tree_entries:
            raise GitHubV2Error("GITHUB_TREE_LIMIT")
        body: dict[str, Any] = {"tree": list(entries)}
        if base is not None:
            body["base_tree"] = _sha(base)
        response = self.request("POST", "/git/trees", body=body)
        if response.status != 201 or not isinstance(response.data, dict):
            raise GitHubV2Error("GITHUB_OBJECT_UPLOAD_FAILED")
        tree = _sha(response.data.get("sha"))
        if expected is not None and expected != tree:
            raise GitHubV2Error("GITHUB_OBJECT_SHA_MISMATCH")
        return tree

    def create_commit(self, tree: str, parent: str, message: str, timestamp: int, *,
                      expected: str | None = None) -> str:
        identity = f"{COMMIT_AUTHOR} {timestamp} +0000"
        raw = (f"tree {_sha(tree)}\nparent {_sha(parent)}\nauthor {identity}\n"
               f"committer {identity}\n\n{message}").encode()
        intended = _git_hash("commit", raw)
        if expected is not None and intended != expected:
            raise GitHubV2Error("GITHUB_OBJECT_SHA_MISMATCH")
        author = {"name": _AUTHOR_NAME, "email": _AUTHOR_EMAIL,
                  "date": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}
        response = self.request("POST", "/git/commits", body={
            "tree": tree, "parents": [parent], "message": message,
            "author": author, "committer": author,
        })
        if response.status != 201 or not isinstance(response.data, dict) or response.data.get("sha") != intended:
            raise GitHubV2Error("GITHUB_OBJECT_SHA_MISMATCH")
        return intended

    def patch_existing(self, branch: str, commit: str) -> GitHubHTTPResponse:
        self.ensure_identity()  # Recheck numeric identity immediately before ref mutation.
        return self.request("PATCH", "/git/refs/heads/" + urllib.parse.quote(
            validate_branch(branch), safe="/"), body={"sha": _sha(commit), "force": False})

    def fetch_objects(self, repo: Path, branch: str, commit: str) -> None:
        """Authenticated exact-SHA fetch; no source ref, index or FETCH_HEAD write."""
        validate_branch(branch)
        repo = _managed_repo(repo)
        _reject_network_config(repo)
        if isinstance(self._token_provider, OwnerTokenFile):
            credential = self._token_provider.path.resolve()
            for forbidden in (repo.parent.resolve(), Path(__file__).resolve().parents[2]):
                if credential == forbidden or forbidden in credential.parents:
                    raise GitHubV2Error("GITHUB_AUTH_UNSAFE")
        try:
            token = _validate_token(self._token_provider())
            _run_network_git(repo, [
                "fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-gc",
                "--no-write-fetch-head", "--no-progress", self.repository.clone_url, _sha(commit),
            ], token, timeout=120)
        except Exception:  # noqa: BLE001 - network-Git/token exceptions may include private details
            raise GitHubV2Error("GITHUB_FETCH_FAILED") from None


class GitHubMailboxPort:
    """Current-HEAD mailbox with byte-conditional ACK and exact content readback."""

    def __init__(self, client: GitHubV2Client, *, branch: str, command_path: str,
                 max_conflict_retries: int = 3) -> None:
        if type(max_conflict_retries) is not int or not 0 <= max_conflict_retries <= 3:
            raise ValueError("A finite conflict budget is required")
        self.client, self.branch = client, validate_branch(branch)
        self.command_path = _command_path(command_path)
        self.max_conflict_retries = max_conflict_retries

    def _observation(self, file: _File) -> Observation:
        revision = {"repo": self.client.repository.id, "owner": self.client.repository.owner_id,
                    "branch": self.branch, "path": self.command_path,
                    "head": file.head, "blob": file.blob, "tree": file.tree, "mode": file.mode}
        return Observation(file.payload, "github-v2:" + base64.urlsafe_b64encode(_json(revision)).decode())

    def read_current(self) -> Observation:
        try:
            return self._observation(self.client.current_file(self.branch, self.command_path))
        except (GitError, ValueError, TypeError):
            raise GitHubV2Error("GITHUB_OBSERVATION_FAILED") from None

    def _validate_ack(self, observed: Observation, desired: bytes) -> None:
        if (not isinstance(observed.revision, str) or not observed.revision.startswith("github-v2:")
                or len(observed.revision) > 8192 or len(desired) > self.client.limits.mailbox_bytes):
            raise GitHubV2Error("INVALID_ACK")
        revision = json.loads(base64.b64decode(observed.revision[10:], altchars=b"-_", validate=True))
        if (revision.get("repo") != self.client.repository.id
                or revision.get("owner") != self.client.repository.owner_id
                or revision.get("branch") != self.branch or revision.get("path") != self.command_path
                or revision.get("blob") != _git_hash("blob", observed.payload)):
            raise GitHubV2Error("INVALID_ACK_OBSERVATION")
        for field in ("head", "blob", "tree"):
            _sha(revision.get(field))
        before, after = parse_contract_json(observed.payload), parse_contract_json(desired)
        validate_mailbox_shape(before)
        validate_mailbox_shape(after)
        mutable = {"dispatch", "receipt", "slot_token"}
        if ({k: v for k, v in before.items() if k not in mutable}
                != {k: v for k, v in after.items() if k not in mutable}
                or UUID(before["slot_token"]) == UUID(after["slot_token"])
                or after["dispatch"] == "READY"):
            raise GitHubV2Error("ACK_MUTATES_USER_PAYLOAD")
        if after["dispatch"] == "DRAFT" and before["dispatch"] != "DRAFT":
            raise GitHubV2Error("INVALID_ACK")

    def ack_if_current(self, observed: Observation, payload: bytes) -> AckResult:
        try:
            self._validate_ack(observed, payload)
        except (GitHubV2Error, DomainError, ValueError, TypeError, KeyError, AttributeError):
            return AckResult("FAILED")
        digest = hashlib.sha256(payload).hexdigest()
        timestamp = int(time.time())

        def proof(current: Observation) -> AckResult:
            return AckResult("APPLIED", current.revision, digest)

        for attempt in range(self.max_conflict_retries + 1):
            try:
                current = self.client.current_file(self.branch, self.command_path)
                if current.payload == payload:
                    return proof(self._observation(current))
                if current.payload != observed.payload:
                    return AckResult("CONFLICT")
                blob = self.client.create_blob(payload)
                tree = self.client.create_tree([{
                    "path": self.command_path, "mode": current.mode, "type": "blob", "sha": blob,
                }], base=current.tree)
                candidate = self.client.create_commit(
                    tree, current.head, f"Acknowledge runner mailbox: {self.command_path}\n\n"
                    f"Ack-SHA256: {digest}\n", timestamp,
                )
            except Exception:  # noqa: BLE001 - no ref write attempted; sanitize provider failures
                return AckResult("FAILED")  # No ref write has been attempted.
            response: GitHubHTTPResponse | None = None
            try:
                response = self.client.patch_existing(self.branch, candidate)
            except Exception:  # noqa: BLE001 - a sent PATCH may have lost its response
                response = None  # Read only below; provider details must not be logged.
            uncertain = response is None or response.status not in {400, 401, 403, 404, 409, 422, 429}
            try:
                after_file = self.client.current_file(self.branch, self.command_path)
                after = self._observation(after_file)
            except OSError:
                return AckResult("UNKNOWN" if uncertain else "FAILED")
            if after.payload == payload:
                return proof(after)
            if uncertain:
                return AckResult("UNKNOWN")
            assert response is not None
            if response.status not in {409, 422}:
                return AckResult("FAILED")
            if after.payload != observed.payload:
                return AckResult("CONFLICT")
            if after_file.head == current.head:
                return AckResult("FAILED")
            if attempt == self.max_conflict_retries:
                return AckResult("FAILED")
        return AckResult("FAILED")


def _large_blob(repo: Path, oid: str, budget: int) -> bytes:
    """Read up to the configured blob budget, above the legacy 8 MiB Git cap."""
    size_text = run_git(repo, ["cat-file", "-s", _sha(oid)]).strip()
    if not size_text.isdecimal() or int(size_text) > budget:
        raise GitHubV2Error("GITHUB_BLOB_LIMIT")
    command = ["git"]
    for setting in _BASE_CONFIG:
        command.extend(("-c", setting))
    command.extend(("cat-file", "blob", oid))
    process = subprocess.Popen(command, cwd=repo, env=_environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, umask=0o077)
    output, diagnostics = bytearray(), bytearray()
    deadline = time.monotonic() + 30
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, diagnostics)
            while selector.get_map():
                if deadline <= time.monotonic():
                    raise GitHubV2Error("GITHUB_OBJECT_READ_TIMEOUT")
                for key, _ in selector.select(min(0.25, deadline - time.monotonic())):
                    raw = os.read(key.fd, 65536)
                    if not raw:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(raw)
                    if len(output) > budget or len(diagnostics) > 1024 * 1024:
                        raise GitHubV2Error("GITHUB_BLOB_LIMIT")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise GitHubV2Error("GITHUB_OBJECT_READ_FAILED")
        result = bytes(output)
        if len(result) != int(size_text) or _git_hash("blob", result) != oid:
            raise GitHubV2Error("GITHUB_OBJECT_INVALID")
        return result
    finally:
        if process.poll() is None:
            _terminate(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


class GitHubResultTransport:
    """Upload exact local result objects, then non-force PATCH an existing ref."""

    def __init__(self, client: GitHubV2Client, *,
                 fetch_objects: Callable[[Path, str, str], None] | None = None) -> None:
        self.client = client
        self._fetch_objects = fetch_objects or client.fetch_objects

    def fetch_head(self, repo: Path, branch: str) -> str | None:
        repo = _managed_repo(repo)
        branch = validate_branch(branch)
        self.client.ensure_identity()
        head = self.client.head(branch)
        if head is not None:
            self._fetch_objects(repo, branch, head)
            if run_git(repo, ["rev-parse", "--verify", f"{head}^{{commit}}"] ).strip() != head:
                raise GitHubV2Error("GITHUB_FETCH_FAILED")
        return head

    def _upload(self, repo: Path, commit: str) -> tuple[str, str]:
        raw = run_git_bytes(repo, ["cat-file", "commit", _sha(commit)])
        header, separator, message_bytes = raw.partition(b"\n\n")
        lines = header.decode("utf-8").splitlines()
        if not separator or len(lines) != 4 or not lines[0].startswith("tree ") or not lines[1].startswith("parent "):
            raise GitHubV2Error("RESULT_COMMIT_INVALID")
        tree, parent = _sha(lines[0][5:]), _sha(lines[1][7:])
        match = re.fullmatch(re.escape(f"author {COMMIT_AUTHOR} ") + r"([0-9]+) \+0000", lines[2])
        if match is None or lines[3] != lines[2].replace("author ", "committer ", 1):
            raise GitHubV2Error("RESULT_COMMIT_INVALID")
        timestamp, message = int(match[1]), message_bytes.decode("utf-8")
        state_commit = message.startswith("Publish runner state: ")
        if (_git_hash("commit", raw) != commit
                or not (state_commit or message.startswith("Publish runner results: "))):
            raise GitHubV2Error("RESULT_COMMIT_INVALID")
        changes = run_git_bytes(repo, ["diff-tree", "--no-commit-id", "--raw", "--no-abbrev", "-r", "-z",
                                       "--no-renames", parent, commit]).split(b"\0")
        changes = changes[:-1] if changes[-1] == b"" else changes
        if not changes or len(changes) % 2:
            raise GitHubV2Error("RESULT_SOURCE_CHANGE_REFUSED")
        prefixes: set[bytes] = set()
        updates: list[dict[str, str]] = []
        for metadata, path in zip(changes[::2], changes[1::2], strict=True):
            _, mode, _, object_id, status = metadata.split(b" ")
            allowed = (status in {b"A", b"M"} and _STATE_PATH.fullmatch(path) is not None
                       if state_commit else status == b"A" and _RESULT_PATH.fullmatch(path) is not None)
            if not allowed or mode != b"100644":
                raise GitHubV2Error("RESULT_SOURCE_CHANGE_REFUSED")
            prefixes.add(b"/".join(path.split(b"/")[:4]))
            updates.append({"path": path.decode("utf-8"), "mode": "100644", "type": "blob",
                            "sha": _sha(object_id.decode("ascii"))})
        if (len(prefixes) != 1 or len(changes) // 2 > self.client.limits.objects
                or state_commit and len(changes) != 2):
            raise GitHubV2Error("RESULT_SOURCE_CHANGE_REFUSED")
        total, count = len(raw), 1
        planned: set[str] = set()
        blobs: dict[str, bytes] = {}
        trees: list[tuple[str, list[dict[str, str]]]] = []

        def plan_tree(oid: str, base_oid: str | None) -> None:
            nonlocal total, count
            if oid == base_oid or oid in planned:
                return
            data = run_git_bytes(repo, ["ls-tree", "-z", oid])
            total += len(data)
            count += 1
            if total > self.client.limits.bundle_bytes or count > self.client.limits.objects:
                raise GitHubV2Error("GITHUB_BUNDLE_LIMIT")
            base_entries: dict[bytes, str] = {}
            if base_oid:
                for entry in run_git_bytes(repo, ["ls-tree", "-z", base_oid]).split(b"\0"):
                    if entry:
                        metadata, name = entry.split(b"\t", 1)
                        base_entries[name] = metadata.split(b" ")[2].decode("ascii")
            entries: list[dict[str, str]] = []
            for entry in data.split(b"\0"):
                if not entry:
                    continue
                metadata, name = entry.split(b"\t", 1)
                mode, kind, object_id = metadata.decode("ascii").split(" ")
                object_id = _sha(object_id)
                if kind == "tree":
                    plan_tree(object_id, base_entries.get(name))
                elif object_id != base_entries.get(name) and object_id not in planned:
                    if kind != "blob" or mode != "100644":
                        raise GitHubV2Error("RESULT_SOURCE_CHANGE_REFUSED")
                    blob = _large_blob(repo, object_id, self.client.limits.blob_bytes)
                    total += len(blob)
                    count += 1
                    if total > self.client.limits.bundle_bytes or count > self.client.limits.objects:
                        raise GitHubV2Error("GITHUB_BUNDLE_LIMIT")
                    encoded_size = ((len(blob) + 2) // 3) * 4
                    if encoded_size + len(_json({"content": "", "encoding": "base64"})) > self.client.limits.json_bytes:
                        raise GitHubV2Error("GITHUB_JSON_LIMIT")
                    blobs[object_id] = blob
                    planned.add(object_id)
                entries.append({"path": name.decode("utf-8"), "mode": mode,
                                "type": kind, "sha": object_id})
            if len(entries) > self.client.limits.tree_entries:
                raise GitHubV2Error("GITHUB_TREE_LIMIT")
            if len(_json({"tree": entries})) > self.client.limits.json_bytes:
                raise GitHubV2Error("GITHUB_JSON_LIMIT")
            trees.append((oid, entries))
            planned.add(oid)

        base_tree = run_git(repo, ["rev-parse", "--verify", f"{parent}^{{tree}}"] ).strip()
        # Validate the entire immutable object graph and its existing budgets
        # before writes. Retained blob bytes are bounded by bundle_bytes.
        plan_tree(tree, base_tree)
        payloads = list(blobs.values())
        if len(payloads) == 1:
            self.client.create_blob(payloads[0])
        elif payloads:
            with ThreadPoolExecutor(max_workers=_BLOB_UPLOAD_WORKERS,
                                    thread_name_prefix="cdr-github-blob") as executor:
                for offset in range(0, len(payloads), _BLOB_UPLOAD_WORKERS):
                    # Do not enqueue later groups until every current upload is
                    # proved. On failure only this bounded group may be in flight;
                    # the executor drains it before returning without a ref write.
                    futures = [executor.submit(self.client.create_blob, blob)
                               for blob in payloads[offset:offset + _BLOB_UPLOAD_WORKERS]]
                    for future in futures:
                        future.result()
        # GitHub builds intermediate trees for full paths, just as mailbox ACKs
        # do. Checking the root SHA proves the exact local tree, including source.
        # Small custom request limits retain the dependency-ordered tree path.
        if (len(updates) <= self.client.limits.tree_entries
                and len(_json({"tree": updates, "base_tree": base_tree})) <= self.client.limits.json_bytes):
            self.client.create_tree(updates, base=base_tree, expected=tree)
        else:
            for oid, entries in trees:
                self.client.create_tree(entries, expected=oid)
        self.client.create_commit(tree, parent, message, timestamp, expected=commit)
        return parent, tree

    def push_commit(self, repo: Path, commit: str, branch: str) -> None:
        branch = validate_branch(branch)
        repo = _managed_repo(repo)
        try:
            self.client.ensure_identity()
            head = self.client.head(branch)
            if head is None:
                raise ResultPushRejected("TARGET_BRANCH_MISSING")
            parent = run_git(repo, ["rev-parse", "--verify", f"{_sha(commit)}^"] ).strip()
            if head != parent:
                raise ResultPushRejected("REMOTE_CHANGED", retryable=True)
            self._upload(repo, commit)
        except ResultPushRejected:
            raise
        except Exception:  # noqa: BLE001 - no ref write attempted; sanitize object/token failures
            raise ResultPushRejected("RESULT_OBJECT_UPLOAD_FAILED") from None
        # From this boundary onwards, any unavailable response is ambiguous.
        response = self.client.patch_existing(branch, commit)
        if response.status == 200:
            if self.client.head(branch) != commit:
                raise GitHubV2Error("GITHUB_REF_WRITE_UNKNOWN")
            return
        if response.status in {409, 422}:
            current = self.client.head(branch)
            raise ResultPushRejected("REMOTE_CHANGED" if current != parent else "PUSH_REJECTED",
                                     retryable=current is not None and current != parent)
        if response.status == 404:
            raise ResultPushRejected("TARGET_BRANCH_MISSING")
        if response.status in {400, 401, 403}:
            raise ResultPushRejected("PUSH_REJECTED")
        raise GitHubV2Error("GITHUB_REF_WRITE_UNKNOWN")
