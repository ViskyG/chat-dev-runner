"""Offline bootstrap policy and Google API retry checks; no OAuth or live writes."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_drive", ROOT / "scripts/bootstrap_drive.py")
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class FakeResponse(dict):
    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__({"retry-after": retry_after} if retry_after is not None else {})
        self.status = status


class FakeHttpError(Exception):
    def __init__(self, status: int, *, reason: str | None = None,
                 retry_after: str | None = None):
        super().__init__("secret response body must not enter diagnostics")
        self.resp = FakeResponse(status, retry_after)
        self.content = json.dumps({
            "error": {"errors": [{"reason": reason}] if reason else []}
        }).encode()


class FakeRequest:
    def __init__(self, outcomes: list[dict | Exception]):
        self.outcomes = outcomes
        self.calls = 0

    def execute(self, *, num_retries: int) -> dict:
        assert num_retries == 0
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_default_cli_is_plan_only_without_auth() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bootstrap_drive.py")],
        capture_output=True, text=True, check=True, timeout=10)
    result = json.loads(completed.stdout)
    assert result["mode"] == "PLAN_ONLY"
    assert result["network_calls"] is False


def test_existing_installation_blocks_bootstrap_before_credentials_or_state(tmp_path: Path) -> None:
    with pytest.raises(bootstrap.BootstrapRefused) as caught:
        bootstrap.ensure_bootstrap_allowed()
    detail = caught.value.diagnostic()
    assert detail["code"] == "BOOTSTRAP_FORBIDDEN_EXISTING_INSTALLATION"
    assert detail["mode"] == "attach_existing"
    assert detail["bootstrap_allowed"] is False
    assert detail["existing_root_folder_id"] == "177IEHeBVezHIlbIijZsIvRDSbTJ42kxe"

    state = tmp_path / "would-create-state.json"
    token = tmp_path / "would-create-token.json"
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.apply({"entries": []}, tmp_path / "fake-credentials.json", token, state)
    assert not state.exists()
    assert not token.exists()


@pytest.mark.parametrize("policy", [
    {"mode": "attach_existing", "bootstrap_allowed": True},
    {"mode": "bootstrap_new", "bootstrap_allowed": False},
    {"mode": "bootstrap_new"},
    [],
])
def test_policy_requires_explicit_new_install_permission(tmp_path: Path, policy: object) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.ensure_bootstrap_allowed(path)


def test_invalid_policy_fails_closed_and_explicit_new_policy_allows(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text("{bad json")
    with pytest.raises(bootstrap.BootstrapRefused) as caught:
        bootstrap.ensure_bootstrap_allowed(path)
    assert caught.value.code == "BOOTSTRAP_POLICY_INVALID"

    path.write_text('{"mode":"bootstrap_new","bootstrap_allowed":true}')
    bootstrap.ensure_bootstrap_allowed(path)


def test_symlinked_policy_is_not_trusted(tmp_path: Path) -> None:
    target = tmp_path / "allow.json"
    target.write_text('{"mode":"bootstrap_new","bootstrap_allowed":true}')
    link = tmp_path / "installation.existing.json"
    link.symlink_to(target)
    with pytest.raises(bootstrap.BootstrapRefused) as caught:
        bootstrap.ensure_bootstrap_allowed(link)
    assert caught.value.code == "BOOTSTRAP_POLICY_INVALID"


def test_read_retries_rate_limit_with_bounded_backoff() -> None:
    request = FakeRequest([
        FakeHttpError(429, retry_after="3"),
        FakeHttpError(403, reason="userRateLimitExceeded"),
        {"id": "owned"},
    ])
    sleeps: list[float] = []
    result = bootstrap.execute_api(
        lambda: request, operation="drive.files.get", read_only=True,
        sleep=sleeps.append, random_fraction=lambda: 0.0)
    assert result == {"id": "owned"}
    assert request.calls == 3
    assert sleeps == [3.0, 2.0]


def test_read_retry_is_finite_and_retry_after_is_capped() -> None:
    request = FakeRequest([FakeHttpError(503, retry_after="999")] * 3)
    sleeps: list[float] = []
    with pytest.raises(bootstrap.BootstrapApiError) as caught:
        bootstrap.execute_api(
            lambda: request, operation="docs.documents.get", read_only=True,
            max_attempts=3, max_delay_seconds=2.0,
            sleep=sleeps.append, random_fraction=lambda: 0.0)
    assert request.calls == 3
    assert sleeps == [2.0, 2.0]
    assert caught.value.diagnostic() == {
        "stage": "google_api",
        "code": "GOOGLE_API_REQUEST_FAILED",
        "operation": "docs.documents.get",
        "http_status": 503,
        "attempts": 3,
        "retryable": True,
        "effect_uncertain": False,
        "next_step": "CHECK_AUTH_OR_RETRY_LATER",
    }


def test_write_error_is_not_replayed_and_reports_uncertain_effect() -> None:
    request = FakeRequest([FakeHttpError(503), {"id": "duplicate-if-replayed"}])
    with pytest.raises(bootstrap.BootstrapApiError) as caught:
        bootstrap.execute_api(
            lambda: request, operation="drive.files.create", read_only=False,
            sleep=lambda _: pytest.fail("write must not back off for a replay"))
    assert request.calls == 1
    assert caught.value.diagnostic()["effect_uncertain"] is True
    assert caught.value.diagnostic()["next_step"] == "RECONCILE_BEFORE_RETRY"
    assert "secret response body" not in str(caught.value)
    assert "secret response body" not in json.dumps(caught.value.diagnostic())


def test_malformed_403_body_still_yields_redacted_diagnostic() -> None:
    error = FakeHttpError(403)
    error.content = b'{"error":"unexpected"}'
    request = FakeRequest([error])
    with pytest.raises(bootstrap.BootstrapApiError) as caught:
        bootstrap.execute_api(
            lambda: request, operation="docs.documents.get", read_only=True,
            sleep=lambda _: pytest.fail("malformed permission denial must not back off"))
    assert caught.value.diagnostic()["http_status"] == 403
    assert request.calls == 1


def test_auth_error_is_not_retried() -> None:
    request = FakeRequest([FakeHttpError(403, reason="insufficientFilePermissions")])
    with pytest.raises(bootstrap.BootstrapApiError) as caught:
        bootstrap.execute_api(
            lambda: request, operation="drive.files.get", read_only=True,
            sleep=lambda _: pytest.fail("permission denial must not back off"))
    assert request.calls == 1
    assert caught.value.diagnostic()["retryable"] is False
