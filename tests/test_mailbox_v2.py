from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from uuid import UUID, uuid5

import pytest

from runner_app.domain.commands import DomainError
from runner_app.domain.mailbox_v2 import (
    acknowledge_mailbox,
    build_receipt,
    parse_mailbox,
    require_ready,
)

INSTALLATION = "aa111111-1111-4111-8111-111111111111"
TOKEN = "bb222222-2222-4222-8222-222222222222"
REQUEST = "cc333333-3333-4333-8333-333333333333"
NEXT_TOKEN = "dd444444-4444-4444-8444-444444444444"


def envelope() -> dict:
    return {"protocol": "cdr-mailbox/2", "runner_id": "runner-1", "project_id": "project-1",
            "slot_token": TOKEN, "dispatch": "READY", "receipt": None,
            "request": {"request_id": None, "action": "RUN_TASK", "task": "test",
                        "source": {"kind": "github_branch", "branch": "feature/tests"},
                        "execution": {"mode": "native", "expected_os": "linux",
                                      "timeout_seconds": 300}}}


def parse(value: dict):
    return parse_mailbox(json.dumps(value).encode(), installation_id=INSTALLATION,
                         mailbox_id="github:repo:runner-1")


def test_identity_is_stable_and_scoped_to_enrolled_binding_and_consumed_token():
    value = envelope()
    frozen = parse(value)
    name = json.dumps(["github:repo:runner-1", TOKEN], separators=(",", ":"))
    assert frozen.request_id == str(uuid5(UUID(INSTALLATION), name))
    pretty = json.dumps(value, indent=4).encode()
    assert frozen == parse_mailbox(pretty, installation_id=INSTALLATION,
                                   mailbox_id="github:repo:runner-1")
    other_binding = parse_mailbox(pretty, installation_id=INSTALLATION, mailbox_id="docs:tab")
    assert other_binding.request_id != frozen.request_id
    value["slot_token"] = NEXT_TOKEN
    assert parse(value).request_id != frozen.request_id
    value["request"]["request_id"] = REQUEST
    assert parse(value).request_id == REQUEST
    assert parse_mailbox(json.dumps(value).encode(), installation_id=INSTALLATION,
                         mailbox_id="docs:tab").request_id == REQUEST


def test_digest_is_sorted_compact_utf8_semantic_intent_only():
    value = envelope()
    value["request"] = {"request_id": REQUEST, "action": "SHELL",
                        "source": {"kind": "local_workspace", "branch": "feature/tests",
                                   "workspace_id": "local-1"},
                        "execution": value["request"]["execution"],
                        "shell": {"dialect": "bash", "script": "printf 'Привет'\r\n"}}
    frozen = parse(value)
    expected = json.dumps({key: value[key] for key in ("request", "runner_id", "project_id")},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert frozen.canonical_payload == expected
    assert frozen.digest == hashlib.sha256(expected).hexdigest()
    assert b"\\u041f" not in expected
    value["dispatch"] = "DRAFT"
    value["slot_token"] = NEXT_TOKEN
    value["receipt"] = build_receipt(frozen, status="ACCEPTED", queue_sequence=1)
    assert parse(value).digest == frozen.digest
    value["request"]["shell"]["script"] = "printf 'Привет'\n"
    assert parse(value).digest != frozen.digest
    value["project_id"] = "project-2"
    assert parse(value).digest != frozen.digest


def test_capture_is_immutable_and_views_cannot_change_intent():
    frozen = parse(envelope())
    detached = frozen.envelope
    detached["request"]["task"] = "changed"
    assert frozen.envelope["request"]["task"] == "test"
    with pytest.raises(FrozenInstanceError):
        frozen.digest = "changed"


@pytest.mark.parametrize("dispatch", ["DRAFT", "ACCEPTED", "REJECTED", "NEEDS_RESUBMIT"])
def test_only_ready_can_initiate_an_action(dispatch):
    value = envelope()
    value["dispatch"] = dispatch
    frozen = parse(value)
    assert not frozen.ready
    with pytest.raises(DomainError, match="Only a READY") as exc:
        require_ready(frozen)
    assert exc.value.code == "NOT_READY"
    require_ready(parse(envelope()))


@pytest.mark.parametrize("raw,code", [
    (b'{"protocol":"cdr-mailbox/2","protocol":"cdr-mailbox/2"}', "DUPLICATE_JSON_KEY"),
    (b'{"nested":{"x":1,"x":2}}', "DUPLICATE_JSON_KEY"),
    (b'{"x":1.0}', "INVALID_JSON"),
    (b'{"x":1e0}', "INVALID_JSON"),
    (b'{"x":NaN}', "INVALID_JSON"),
    (b'{"x":Infinity}', "INVALID_JSON"),
    (b'{"x":-Infinity}', "INVALID_JSON"),
    (b'{"x":"\\ud800"}', "INVALID_UTF8"),
    (b'{"x":"\xff"}', "INVALID_UTF8"),
    (b'[]', "INVALID_JSON"),
    (b'{', "INVALID_JSON"),
    (b' ' * 262_145, "PAYLOAD_TOO_LARGE"),
])
def test_strict_json_refusals(raw, code):
    with pytest.raises(DomainError) as exc:
        parse_mailbox(raw, installation_id=INSTALLATION, mailbox_id="binding")
    assert exc.value.code == code


@pytest.mark.parametrize("branch", ["../main", "-main", "a//b", "a.lock/b", "main~1", "a b"])
def test_unsafe_branch_is_rejected(branch):
    value = envelope()
    value["request"]["source"]["branch"] = branch
    with pytest.raises(DomainError) as exc:
        parse(value)
    assert exc.value.code == "INVALID_BRANCH"


@pytest.mark.parametrize("change", [
    lambda e: e["request"].pop("execution"),
    lambda e: e["request"].update(shell={"dialect": "bash", "script": "true"}),
    lambda e: e["request"].update(target_request_id=REQUEST),
    lambda e: e["request"].update(action="UNKNOWN"),
    lambda e: e["request"]["execution"].update(mode="container"),
    lambda e: e["request"]["execution"].update(timeout_seconds=True),
    lambda e: e["request"]["source"].update(kind="local_workspace"),
    lambda e: e["request"]["source"].update(workspace_id="local-1"),
    lambda e: e.update(slot_token=TOKEN.upper()),
    lambda e: e["request"].update(request_id=REQUEST.upper()),
    lambda e: e.update(untrusted_authority="shell"),
])
def test_request_shape_cannot_mix_actions_or_broaden_authority(change):
    value = envelope()
    change(value)
    with pytest.raises(DomainError) as exc:
        parse(value)
    assert exc.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize("action", ["CANCEL", "STATUS", "REDELIVER"])
def test_control_actions_have_exact_target_shape(action):
    value = envelope()
    value["request"] = {"request_id": REQUEST, "action": action, "target_request_id": NEXT_TOKEN}
    assert parse(value).envelope["request"]["target_request_id"] == NEXT_TOKEN
    value["request"]["execution"] = {"mode": "native", "expected_os": "linux",
                                      "timeout_seconds": 1}
    with pytest.raises(DomainError):
        parse(value)


def test_ack_rotates_token_preserves_intent_and_defers_sequence():
    frozen = parse(envelope())
    ack = acknowledge_mailbox(frozen, slot_token=NEXT_TOKEN)
    value = json.loads(ack)
    assert value["slot_token"] == NEXT_TOKEN
    assert value["dispatch"] == "ACCEPTED"
    assert value["request"] == frozen.envelope["request"]
    assert value["receipt"] == {"request_id": frozen.request_id, "accepted_slot_token": TOKEN,
                                "payload_sha256": frozen.digest, "status": "ACCEPTED",
                                "queue_sequence": None, "code": None}
    assert parse(value).digest == frozen.digest
    assert frozen.envelope["slot_token"] == TOKEN
    with pytest.raises(DomainError):
        acknowledge_mailbox(frozen, slot_token=TOKEN)
    with pytest.raises(DomainError):
        build_receipt(frozen, status="ACCEPTED", queue_sequence=True)


def test_deep_nesting_is_bounded_before_schema_validation():
    raw = b'{"x":' * 66 + b'null' + b'}' * 66
    with pytest.raises(DomainError) as exc:
        parse_mailbox(raw, installation_id=INSTALLATION, mailbox_id="binding")
    assert exc.value.code == "INVALID_JSON"


def test_receipt_id_and_target_id_must_be_canonical():
    value = envelope()
    value["receipt"] = build_receipt(parse(value), status="ACK_UNKNOWN")
    value["receipt"]["accepted_slot_token"] = TOKEN.upper()
    with pytest.raises(DomainError):
        parse(value)
    value = copy.deepcopy(envelope())
    value["request"] = {"request_id": None, "action": "CANCEL",
                        "target_request_id": REQUEST.upper()}
    with pytest.raises(DomainError):
        parse(value)


def test_draft_cannot_receive_an_accepted_ack():
    value = envelope()
    value["dispatch"] = "DRAFT"
    with pytest.raises(DomainError) as exc:
        acknowledge_mailbox(parse(value))
    assert exc.value.code == "NOT_READY"
