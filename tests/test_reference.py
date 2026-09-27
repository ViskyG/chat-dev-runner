from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from bridge_reference.changes import apply_changeset
from bridge_reference.demo import demo
from bridge_reference.execution import capture
from bridge_reference.ledger import Ledger
from bridge_reference.protocol import (
    Rejected,
    canonical_digest,
    safe_relative_path,
    validate_branch,
    validate_command,
    validate_schema,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 0, 5, tzinfo=timezone.utc)

@pytest.fixture
def command():
    value = json.loads((ROOT / "examples/verify.command.json").read_text())
    value["dispatch"] = "READY"
    return value

def valid(command, **kwargs):
    return validate_command(command, runner_id="local-runner", epoch=command["epoch"], now=NOW, **kwargs)

def test_command_accepted(command):
    assert len(valid(command)) == 64

@pytest.mark.parametrize("field,value,code", [
    ("dispatch", "DRAFT", "NOT_READY"),
    ("runner_id", "other", "WRONG_RUNNER_EPOCH"),
    ("expires_at", "2026-09-26T00:04:00Z", "EXPIRED"),
    ("expires_at", "2026-09-26T03:00:00Z", "INVALID_TTL"),
    ("action", "RUN_SHELL", "INVALID_SCHEMA"),
    ("expected_head", "main", "INVALID_SCHEMA"),
    ("workspace_id", None, "INVALID_SCHEMA"),
])
def test_command_refusals(command, field, value, code):
    command[field] = value
    with pytest.raises(Rejected) as e:
        valid(command)
    assert e.value.code == code

def test_wrong_epoch(command):
    with pytest.raises(Rejected, match="armed"):
        validate_command(command, runner_id="local-runner", epoch="new", now=NOW)

def test_unknown_top_level_field(command):
    command["shell"] = "rm -rf /"
    with pytest.raises(Rejected): valid(command)

def test_unknown_profile_param(command):
    command["params"]["argv"] = ["bash"]
    with pytest.raises(Rejected): valid(command)

def test_future_request(command):
    command["issued_at"] = "2026-09-26T00:06:00Z"
    command["expires_at"] = "2026-09-26T00:16:00Z"
    with pytest.raises(Rejected) as e: valid(command)
    assert e.value.code == "FUTURE_COMMAND"

def test_all_examples_validate():
    for path in (ROOT / "examples").glob("*.command.json"):
        validate_schema(json.loads(path.read_text()), "command")
    validate_schema(json.loads((ROOT / "examples/change-set.json").read_text()), "changeset")

def test_digest_key_order_and_unicode():
    assert canonical_digest({"b": "тест", "a": 1}) == canonical_digest({"a": 1, "b": "тест"})
    assert canonical_digest({"a": "é"}) != canonical_digest({"a": "e\u0301"})

@pytest.mark.parametrize("path", ["../secret", "/tmp/a", "x/../../a", "a//b", "./x", ".git/config",
    ".git/hooks/pre-commit", ".env", "x/.env.local", "secrets/key", "C:/x", "a\\b", "a\x00b", "x/\nq", ".github/workflows/build.yml"])
def test_reject_paths(path):
    with pytest.raises(Rejected): safe_relative_path(path)

@pytest.mark.parametrize("path", ["src/main.py", "docs/текст.md", "tests/test_x.py", "docs/environment.md"])
def test_normal_paths(path):
    assert str(safe_relative_path(path)) == path

@pytest.mark.parametrize("name", ["feature/x", "fix/issue-3", "main"])
def test_valid_branches(name): validate_branch(name)

@pytest.mark.parametrize("name", ["-b", "../main", "feature/../x", "a//b", "a.lock/x", "x~1", "x/", ".hidden", "a/.hidden"])
def test_invalid_branches(name):
    with pytest.raises(Rejected): validate_branch(name)

def test_idempotence_persisted(tmp_path):
    ledger = Ledger(tmp_path / "db")
    assert ledger.admit("id", "hash", "p/w").execute
    assert not ledger.admit("id", "hash", "p/w").execute
    ledger.finish("id", "COMPLETED", {"outcome": "FAILED"})
    restarted = Ledger(tmp_path / "db")
    assert not restarted.admit("id", "hash", "p/w").execute
    assert restarted.get("id")["status"] == "COMPLETED"

def test_same_id_different_payload(tmp_path):
    ledger = Ledger(tmp_path / "db")
    ledger.admit("id", "hash", "p/w")
    with pytest.raises(Rejected) as e: ledger.admit("id", "changed", "p/w")
    assert e.value.code == "REQUEST_ID_REUSED"

def test_no_queue(tmp_path):
    ledger = Ledger(tmp_path / "db")
    ledger.admit("one", "1", "p/w")
    refused = ledger.admit("two", "2", "p/w")
    assert refused.status == "REJECTED" and refused.code == "BUSY"
    ledger.finish("one", "COMPLETED")
    assert not ledger.admit("two", "2", "p/w").execute
    assert ledger.admit("three", "3", "p/w").execute

def test_global_capacity(tmp_path):
    ledger = Ledger(tmp_path / "db", max_parallel=2)
    assert ledger.admit("1", "a", "a/x").execute
    assert ledger.admit("2", "b", "b/y").execute
    assert ledger.admit("3", "c", "c/z").status == "REJECTED"

def test_concurrent_claims(tmp_path):
    ledger = Ledger(tmp_path / "db", max_parallel=10)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda i: ledger.admit(str(i), str(i), "one/workspace"), range(5)))
    assert sum(r.execute for r in results) == 1

def test_interruption_never_reexecutes(tmp_path):
    ledger = Ledger(tmp_path / "db")
    ledger.admit("id", "h", "p/w")
    with pytest.raises(Rejected): ledger.confirm_interrupted("id", processes_confirmed_stopped=False)
    assert len(ledger.list_running()) == 1
    ledger.confirm_interrupted("id", processes_confirmed_stopped=True)
    assert ledger.get("id")["status"] == "INTERRUPTED"
    assert not ledger.admit("id", "h", "p/w").execute

def test_terminal_cannot_be_finished_again(tmp_path):
    ledger=Ledger(tmp_path/'db')
    ledger.admit('x','h','p/w'); ledger.finish('x','COMPLETED')
    with pytest.raises(Rejected): ledger.finish('x','COMPLETED')

def changes(tmp_path):
    return json.loads((ROOT / "examples/change-set.json").read_text())

def test_apply_and_conflict(tmp_path):
    c=changes(tmp_path)
    out=apply_changeset(tmp_path,c,expected_head='a'*40)
    assert (tmp_path/'notes.txt').read_text()=='New project note.\n'
    assert out[0]['sha256']==hashlib.sha256(b'New project note.\n').hexdigest()
    with pytest.raises(Rejected) as e: apply_changeset(tmp_path,c,expected_head='a'*40)
    assert e.value.code=='CONTENT_CONFLICT'

def test_stale_base(tmp_path):
    with pytest.raises(Rejected) as e: apply_changeset(tmp_path,changes(tmp_path),expected_head='b'*40)
    assert e.value.code=='STALE_BASE'

def test_all_checks_before_first_write(tmp_path):
    c=changes(tmp_path)
    c['files'].append({'op':'upsert','path':'../escape.txt','expected_sha256':None,'content_utf8':'x'})
    with pytest.raises(Rejected): apply_changeset(tmp_path,c,expected_head='a'*40)
    assert not (tmp_path/'notes.txt').exists()

def test_duplicate_paths(tmp_path):
    c=changes(tmp_path); c['files'].append(dict(c['files'][0]))
    with pytest.raises(Rejected) as e: apply_changeset(tmp_path,c,expected_head='a'*40)
    assert e.value.code=='DUPLICATE_PATH'

def test_no_symlink_targets(tmp_path):
    external=tmp_path/'external'; external.mkdir()
    (tmp_path/'linked').symlink_to(external,target_is_directory=True)
    c=changes(tmp_path);c['files'][0]['path']='linked/data.txt'
    with pytest.raises(Rejected) as e: apply_changeset(tmp_path,c,expected_head='a'*40)
    assert e.value.code=='SYMLINK'

def test_delete_and_byte_preservation(tmp_path):
    (tmp_path/'notes.txt').write_bytes(b'a\r\nb')
    c=changes(tmp_path); c['files']=[{'op':'delete','path':'notes.txt','expected_sha256':hashlib.sha256(b'a\r\nb').hexdigest()}]
    apply_changeset(tmp_path,c,expected_head='a'*40)
    assert not (tmp_path/'notes.txt').exists()

def test_change_budget(tmp_path):
    with pytest.raises(Rejected) as e: apply_changeset(tmp_path,changes(tmp_path),expected_head='a'*40,max_bytes=2)
    assert e.value.code=='CHANGESET_TOO_LARGE'

def test_console_separation_and_invalid_utf8(tmp_path):
    out=tmp_path/'out'
    result=capture([sys.executable,'-c','import os;os.write(1,b"a\\xff");os.write(2,b"error")'],tmp_path,out)
    assert result['outcome']=='PASSED'
    assert (out/'stdout.bin').read_bytes()==b'a\xff'
    assert (out/'stderr.bin').read_bytes()==b'error'
    assert '\ufffd' in (out/'stdout.txt').read_text()

def test_failed_test_is_not_runner_fault(tmp_path):
    result=capture([sys.executable,'-c','raise SystemExit(1)'],tmp_path,tmp_path/'out')
    assert result['outcome']=='FAILED' and result['exit_code']==1

def test_timeout(tmp_path):
    result=capture([sys.executable,'-c','import time;time.sleep(5)'],tmp_path,tmp_path/'out',timeout=.15)
    assert result['outcome']=='TIMED_OUT' and result['duration_seconds']<3

def test_cancel(tmp_path):
    cancel=Event();cancel.set()
    result=capture([sys.executable,'-c','import time;time.sleep(5)'],tmp_path,tmp_path/'out',cancel=cancel)
    assert result['outcome']=='CANCELLED'

def test_bounded_output_is_reported(tmp_path):
    result=capture([sys.executable,'-c','print("x"*10000)'],tmp_path,tmp_path/'out',max_stream_bytes=20)
    assert not result['streams']['stdout']['complete']
    assert result['streams']['stdout']['stored_bytes']==20
    assert result['streams']['stdout']['observed_bytes']==10001

def test_real_git_fixture(tmp_path):
    result=demo(tmp_path/'brand-new-demo')
    assert [r['outcome'] for r in result['runs']]==['PASSED','FAILED']
    assert all(not r['duplicate_executed'] for r in result['runs'])
    assert not result['cloud_calls'] and not result['user_projects_touched']
    for run in result['runs']:
        validate_schema(json.loads(Path(run['manifest']).read_text()),'run-manifest')

def test_demo_refuses_existing_destination(tmp_path):
    with pytest.raises(FileExistsError): demo(tmp_path)
