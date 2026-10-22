"""One frozen regression for v2, handoffs and the touched v1 storage/source boundaries."""
from pathlib import Path
import json
import subprocess
import sys

root = Path(__file__).resolve().parents[3]
tests = root / 'tests'
selected = set(tests.glob('*v2*.py')) | set(tests.glob('test_handoff*.py'))
selected.update(tests / name for name in (
    'test_foundation_storage.py', 'test_domain_storage.py', 'test_backup_restore.py',
    'test_backup_cli.py', 'test_git_snapshots.py', 'test_drive_context.py'))
args = [sys.executable, '-m', 'pytest', '-q',
        *(str(path.relative_to(root)) for path in sorted(selected)),
        '--junitxml=verification/implementation/handoff/frozen-regression.xml']
print(json.dumps({'selected_files': len(selected), 'command': args}), flush=True)
raise SystemExit(subprocess.call(args, cwd=root))
