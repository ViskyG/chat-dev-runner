"""Build and qualify one frozen wheel offline, without touching installed state."""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
FREEZE = "a26fe8ca64d25442ce80f2f80f1f0c6bb5302974"
DEVELOPMENT_PYTHON = Path("/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python")
WHEEL_DIRECTORY = Path("/home/visky/Work/GPTRunner/.runner-install/wheels/gp07-a26fe8c")
VERSION = "0.3.0.dev1"
SMOKE = r'''
import hashlib,importlib,importlib.metadata,json,sys
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI
import runner_app,schemas,bridge_reference.protocol
from runner_app.config_v2 import parse_v2_config
from runner_app.domain.mailbox_v2 import parse_mailbox
from runner_app.engine.runtime_v2 import V2Assembly
from runner_app.resources import SCHEMA_NAMES,load_schema
from runner_app.storage.database import Database,SCHEMA_VERSION
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths
root,installed=map(Path,sys.argv[1:3])
assert runner_app.__version__==importlib.metadata.version('chat-dev-runner')=='0.3.0.dev1'
assert SCHEMA_VERSION==10
for name in SCHEMA_NAMES: assert isinstance(load_schema(name),dict)
assert len([name for name in SCHEMA_NAMES if name.startswith('v2-')])==5
ui=files('runner_app.web').joinpath('v2.js').read_bytes()
assert ui and files('runner_app.web').joinpath('index.html').read_bytes()
raw=json.dumps({'protocol':'cdr-mailbox/2','runner_id':'wheel-runner','project_id':'wheel-project',
 'slot_token':'6d5c4c5c-1001-4d87-a337-ad4de343aa8a','dispatch':'READY','receipt':None,
 'request':{'request_id':None,'action':'RUN_TASK','task':'test',
 'source':{'kind':'local_workspace','branch':'main','workspace_id':'local'},
 'execution':{'mode':'native','expected_os':'linux','timeout_seconds':10}}}).encode()
frozen=parse_mailbox(raw,installation_id='f2226b09-3aee-4b38-bb9e-22ad7517404f',mailbox_id='wheel-slot')
assert frozen.ready and UUID(frozen.request_id).version==5
assert hashlib.sha256(frozen.canonical_payload).hexdigest()==frozen.digest
paths=Paths(root/'config',root/'state',root/'cache')
paths.ensure_private()
original=root/'original';original.mkdir(mode=0o700)
config=parse_v2_config(json.dumps({'protocol':'cdr-runtime/2','runner_id':'wheel-runner',
 'projects':[{'project_id':'wheel-project','workspaces':{'local':{'path':str(original)}}}],
 'mailboxes':[]}).encode())
ledger=Ledger(Database(paths.database_path),runner_id='wheel-runner',single_execution_lane=True)
ledger.startup()
assembly=V2Assembly(config,paths,ledger)
try:
 app=FastAPI();assembly.attach_api(app)
 status=assembly.status()
 assert status['protocol']=='cdr-runtime/2' and status['projects']==['wheel-project']
 assert status['capability_status']=='IMPLEMENTED_NOT_QUALIFIED'
 assert not status['configured'] and status['channels']=={}
 assert assembly.store.list_requests()==[]
 assert (paths.state_dir/'projects-v2/wheel-project/source.git').is_dir()
 routes=sorted(route.path for route in app.routes if route.path.startswith('/api/v2/'))
 assert routes==['/api/v2/requests','/api/v2/requests/{request_id}','/api/v2/status']
 with closing(Database(paths.database_path).connect()) as connection:
  assert connection.execute('PRAGMA integrity_check').fetchone()==('ok',)
  assert connection.execute('PRAGMA foreign_key_check').fetchall()==[]
finally: assembly.close()
loaded={}
for name,module in sorted(sys.modules.items()):
 if name.split('.')[0] in {'runner_app','schemas','bridge_reference'} and getattr(module,'__file__',None):
  path=Path(module.__file__).resolve()
  assert path.is_relative_to(installed),name
  loaded[name]=str(path.relative_to(installed))
print(json.dumps({'version':runner_app.__version__,'schema_version':SCHEMA_VERSION,
 'loaded_modules':loaded,'schema_count':len(SCHEMA_NAMES),'v2_schema_count':5,
 'v2_ui_sha256':hashlib.sha256(ui).hexdigest(),'request_id':frozen.request_id,
 'semantic_digest':frozen.digest,'composition_status':status,'attached_routes':routes,
 'provider_calls':0,'pollers_started':False,'service_started':False,
 'execution_requests':0,'scratch_database_integrity':'ok'},sort_keys=True))
'''


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run_audit() -> dict[str, Any]:
    os.umask(0o077)
    started = datetime.now(timezone.utc).isoformat()
    scratch = Path(tempfile.mkdtemp(prefix="cdr-gp07-wheel-", dir="/tmp"))
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(scratch), "LANG": "C.UTF-8",
                   "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    commands: list[dict[str, Any]] = []

    def command(argv: list[str], *, cwd: Path = REPO, timeout: int = 60) -> bytes:
        result = subprocess.run(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout, check=False)
        number = len(commands) + 1
        log = scratch / f"command-{number:02d}.log"
        log.write_bytes(result.stdout + result.stderr)
        commands.append({"argv": argv, "cwd": str(cwd), "exit_code": result.returncode,
                         "log": str(log), "log_sha256": sha(log.read_bytes())})
        assert result.returncode == 0, f"COMMAND_{number}_FAILED"
        return result.stdout

    command(["/usr/bin/git", "diff", "--quiet", FREEZE, "--", "pyproject.toml", "runner_app", "bridge_reference", "schemas"])
    paths = command(["/usr/bin/git", "ls-tree", "-r", "--name-only", FREEZE, "--", "runner_app", "bridge_reference", "schemas"]).decode().splitlines()
    expected = {path: (REPO / path).read_bytes() for path in paths if "__pycache__" not in Path(path).parts}
    WHEEL_DIRECTORY.mkdir(mode=0o700)
    command([str(DEVELOPMENT_PYTHON), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(WHEEL_DIRECTORY)])
    wheels = list(WHEEL_DIRECTORY.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    assert wheel.name == f"chat_dev_runner-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert not any(stat.S_ISLNK(entry.external_attr >> 16) for entry in archive.infolist())
        payload = {name: archive.read(name) for name in names if ".dist-info/" not in name and not name.endswith("/")}
        missing = sorted(expected.keys() - payload.keys())
        unexpected = sorted(payload.keys() - expected.keys())
        differences = sorted(name for name in expected.keys() & payload.keys() if expected[name] != payload[name])
        assert not missing and not unexpected and not differences
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        assert metadata["Name"] == "chat-dev-runner" and metadata["Version"] == VERSION
        project = (REPO / "pyproject.toml").read_text()
        dependencies = ast.literal_eval(next(line.split("=", 1)[1].strip() for line in project.splitlines() if line.startswith("dependencies =")))
        assert set(dependencies) <= set(metadata.get_all("Requires-Dist"))
        entries = archive.read(next(name for name in names if name.endswith(".dist-info/entry_points.txt"))).decode()
        assert "runner = runner_app.cli:main" in entries
        record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
        record = list(csv.reader(io.StringIO(archive.read(record_name).decode())))
        assert {row[0] for row in record} == set(names)
        for name, encoded, size in record:
            if name == record_name:
                assert encoded == size == ""
            else:
                data = archive.read(name)
                assert encoded == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                assert int(size) == len(data)
    virtualenv = scratch / "venv"
    command([str(DEVELOPMENT_PYTHON), "-m", "venv", "--system-site-packages", "--copies", str(virtualenv)])
    python = virtualenv / "bin/python"
    installed = Path(command([str(python), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], cwd=scratch).decode().strip())
    dependencies_path = Path(command([str(DEVELOPMENT_PYTHON), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], cwd=scratch).decode().strip())
    # A plain .pth path exposes existing dependency files, without processing the
    # shared environment's editable-package finder .pth files. The wheel's own
    # site-packages is first, and all imported project paths are checked below.
    (installed / "existing-development-dependencies.pth").write_text(str(dependencies_path) + "\n")
    command([str(python), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--no-compile", "--disable-pip-version-check", str(wheel)], cwd=scratch)
    pip_check = command([str(python), "-I", "-m", "pip", "--isolated", "check"], cwd=scratch).decode().strip()
    cli_version = command([str(virtualenv / "bin/runner"), "--version"], cwd=scratch).decode().strip()
    assert cli_version == VERSION
    help_text = command([str(virtualenv / "bin/runner"), "--help"], cwd=scratch).decode()
    assert all(name in help_text for name in ("serve", "backup", "restore"))
    installed_differences = sorted(name for name, data in payload.items()
                                   if (installed / name).read_bytes() != data)
    assert not installed_differences
    smoke_script = scratch / "installed-smoke.py"
    smoke_script.write_text(SMOKE)
    smoke_root = scratch / "smoke"
    smoke_root.mkdir(mode=0o700)
    smoke = json.loads(command([str(python), "-I", str(smoke_script), str(smoke_root), str(installed)], cwd=scratch))
    counts = {"all_payload_files": len(payload), "python_files": sum(name.endswith(".py") for name in payload),
              "schema_files": sum(name.startswith("schemas/") and name.endswith(".schema.json") for name in payload),
              "v2_schema_files": sum(name.startswith("schemas/v2-") for name in payload),
              "web_resources": sum(name.startswith("runner_app/web/") and not name.endswith(".py") for name in payload),
              "starter_wheels": sum(name.startswith("runner_app/git/python_quick_wheels/") for name in payload),
              "record_entries": len(record)}
    assert counts["v2_schema_files"] == 5 and "runner_app/web/v2.js" in payload
    provenance = {name: {"sha256": sha(data), "bytes": len(data)} for name, data in sorted(payload.items())}
    return {"schema": "cdr-frozen-wheel-audit/1", "status": "PASSED", "started_at": started,
            "ended_at": datetime.now(timezone.utc).isoformat(), "source_freeze": FREEZE,
            "wheel_path": str(wheel), "wheel_sha256": sha(wheel.read_bytes()), "wheel_bytes": wheel.stat().st_size,
            "package_name": metadata["Name"], "package_version": metadata["Version"],
            "production_requirements": dependencies, "cli_version": cli_version, "pip_check": pip_check,
            "counts": counts, "missing_paths": missing, "unexpected_paths": unexpected,
            "source_difference_paths": differences, "installed_difference_paths": installed_differences,
            "payload_manifest_sha256": sha(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()),
            "payload": provenance, "installed_smoke": smoke, "scratch": str(scratch),
            "temporary_site_packages": str(installed), "existing_dependency_path": str(dependencies_path),
            "dependency_mode": "Existing development dependencies referenced by plain temporary .pth; none downloaded or changed",
            "commands": commands, "command_environment": environment,
            "audit_script_sha256": sha(Path(__file__).read_bytes()), "smoke_script_sha256": sha(SMOKE.encode()),
            "not_performed": ["service startup/shutdown", "actual installed runtime replacement", "live configuration/database changes", "provider/network calls", "global installation", "dependency changes", "broad regression"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    record = run_audit()
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in ("status", "source_freeze", "wheel_path", "wheel_sha256", "counts", "pip_check")}, sort_keys=True))


if __name__ == "__main__":
    main()
