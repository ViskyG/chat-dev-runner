"""Offline installed-wheel smoke; writes only disposable /tmp XDG state."""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[3]
evidence = Path(__file__).resolve().parent
wheel = Path(sys.argv[1]).resolve()
trial = Path(tempfile.mkdtemp(prefix="cdr-ep02-installed-", dir="/tmp"))
python = trial / "venv/bin/python"
runner = trial / "venv/bin/runner"
steps: list[dict[str, object]] = []
process: subprocess.Popen[bytes] | None = None
report = {"environment": {"python": platform.python_version(), "platform": platform.platform(),
                          "cwd": str(trial), "wheel": str(wheel), "network": "loopback only"},
          "steps": steps}


def step(name: str, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(argv, cwd=cwd or trial, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=90, check=False)
    log = evidence / f"wheel-{name}.log"
    log.write_bytes(result.stdout)
    steps.append({"name": name, "argv": argv, "exit_code": result.returncode,
                  "log": str(log.relative_to(root))})
    if result.returncode:
        raise RuntimeError(f"{name} exited {result.returncode}; see {log}")
    return result.stdout


try:
    step("venv", ["python3", "-m", "venv", str(trial / "venv")])
    step("dependencies", ["uv", "pip", "install", "--offline", "--python", str(python),
                          "--require-hashes", "-r", str(root / "requirements.lock")])
    step("package", ["uv", "pip", "install", "--offline", "--python", str(python),
                     "--no-deps", str(wheel)])
    script = """import json, pathlib, sys, runner_app, bridge_reference.protocol as p
from runner_app.resources import load_schema
from runner_app.storage import Database
assert pathlib.Path(runner_app.__file__).is_relative_to(pathlib.Path(sys.prefix))
assert pathlib.Path(p.SCHEMAS).is_relative_to(pathlib.Path(sys.prefix))
assert load_schema('command') == json.loads((p.SCHEMAS/'command.schema.json').read_text())
state = pathlib.Path.cwd()/'db'; state.mkdir(mode=0o700)
assert Database(state/'state.sqlite').migrate() == 3
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import create_repository, create_workspace
repo = create_repository(state/'managed', 'wheel-fixture')
head = run_git(repo, ['rev-parse', 'refs/heads/main']).strip()
workspace = create_workspace(state/'managed', 'wheel-fixture', repo, 'first', 'feature/wheel', head)
registry = SourceRegistry(Database(state/'state.sqlite'), state/'managed')
registry.register_project('wheel-fixture', 'Wheel Fixture', repo, origin_kind='new')
registry.register_workspace('wheel-fixture', 'first', 'First', workspace, branch='feature/wheel', head=head)
assert registry.get_workspace('wheel-fixture', 'first')['head'] == head
print(json.dumps({'installed_module': runner_app.__file__, 'schema_version': 3, 'git_head': head}))
"""
    step("isolated-import", [str(python), "-I", "-c", script])
    doctor = json.loads(step("doctor", [str(runner), "doctor", "--offline"]))
    assert doctor["armed"] is False and doctor["mode"] == "OFFLINE"

    home = trial / "home"
    home.mkdir(mode=0o700)
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(trial / "config"),
                "XDG_DATA_HOME": str(trial / "data"), "XDG_CACHE_HOME": str(trial / "cache")})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    log_path = evidence / "wheel-runtime.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([str(runner), "serve", "--port", str(port)], cwd=trial,
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        health = None
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(f"installed runner exited {process.returncode}; see {log_path}")
            try:
                with opener.open(origin + "/api/v1/health", timeout=0.3) as response:
                    health = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        if health is None:
            raise RuntimeError("installed runner health did not become available")
        assert health["armed"] is False and "local_api" in health["capabilities"]
        bootstrap_path = trial / "config/chat-dev-runner/tokens/session-bootstrap.json"
        bootstrap = json.loads(bootstrap_path.read_text())["bootstrap"]
        request = urllib.request.Request(origin + "/api/v1/local/session",
                                         data=json.dumps({"bootstrap": bootstrap}).encode(),
                                         headers={"Origin": origin, "Content-Type": "application/json"},
                                         method="POST")
        with opener.open(request, timeout=3) as response:
            assert response.status == 200
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        request = urllib.request.Request(origin + "/api/v1/system", headers={"Cookie": cookie})
        with opener.open(request, timeout=3) as response:
            state = json.load(response)
        assert state["armed"] is False
        assert state["integrations"]["google"]["bootstrap_allowed"] is False
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        assert not bootstrap_path.exists(), "private bootstrap file must be removed on shutdown"
    steps.append({"name": "installed-runtime", "argv": [str(runner), "serve", "--port", str(port)],
                  "exit_code": process.returncode, "log": str(log_path.relative_to(root)),
                  "health": "NOT_CONFIGURED/unarmed/local_api", "session": "exchange + system read",
                  "bootstrap_cleaned": True})
    report["result"] = "PASS"
except BaseException as error:
    report["result"] = "FAIL"
    report["error"] = f"{type(error).__name__}: {error}"
    raise
finally:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    (evidence / "wheel-acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    shutil.rmtree(trial, ignore_errors=True)
