"""Owner backup/restore CLI reaches the real stopped-state path with stable refusals."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from runner_app.storage import Database, Paths
from runner_app.storage.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]


def _cli(environment: dict[str, str], *arguments: str) -> tuple[int, dict[str, object]]:
    result = subprocess.run(
        [sys.executable, "-m", "runner_app.cli", *arguments],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=20, check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_cli_backup_restore_roundtrip_uses_exact_absent_state_path(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.update({
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    paths = Paths.discover(environment)
    paths.ensure_private()
    paths.state_dir.parent.chmod(0o700)
    ledger = Ledger(Database(paths.database_path))
    ledger.startup()
    marker = paths.evidence_dir / "sentinel.txt"
    marker.write_text("owned evidence")
    marker.chmod(0o600)
    archive_parent = tmp_path / "archives"
    archive_parent.mkdir(mode=0o700)
    archive_parent.chmod(0o700)
    archive = archive_parent / "snapshot"
    code, result = _cli(environment, "backup", "--output", str(archive))
    assert code == 0
    assert result["mode"] == "BACKUP"
    assert result["archive"] == str(archive)
    assert (archive / "manifest.json").is_file()
    assert not (archive / "state" / "tokens").exists()
    code, refused = _cli(environment, "restore", "--from", str(archive))
    assert code == 2 and refused == {"error": "RESTORE_TARGET_EXISTS"}
    moved = paths.state_dir.with_name("saved-original")
    paths.state_dir.rename(moved)
    code, restored = _cli(environment, "restore", "--from", str(archive))
    assert code == 0
    assert restored["mode"] == "RESTORE"
    assert restored["state_dir"] == str(paths.state_dir)
    assert (paths.evidence_dir / "sentinel.txt").read_text() == "owned evidence"
    code, refused = _cli(environment, "backup", "--output", str(archive))
    assert code == 2 and refused == {"error": "BACKUP_TARGET_EXISTS"}
    assert (moved / "outputs" / "sentinel.txt").read_text() == "owned evidence"
