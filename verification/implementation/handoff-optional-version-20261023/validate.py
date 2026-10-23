"""One release boundary: static checks, full regression and wheel build."""
from pathlib import Path
import json
import subprocess
import sys

root = Path(__file__).resolve().parents[3]
evidence = Path(__file__).parent
steps = [
    ('ruff', [sys.executable, '-m', 'ruff', 'check', 'runner_app', 'tests', 'scripts/bootstrap_drive.py']),
    ('mypy', [sys.executable, '-m', 'mypy']),
    ('regression', [sys.executable, '-m', 'pytest', '-q',
                    '--junitxml=' + str(evidence / 'regression.xml')]),
    ('wheel', [sys.executable, '-m', 'build', '--wheel', '--no-isolation']),
]
results = {}
for name, command in steps:
    print('@@HUD ' + json.dumps({'phase': name}), flush=True)
    with (evidence / (name + '.log')).open('w') as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    results[name] = {'command': command, 'exit_code': result.returncode}
    (evidence / 'validation.json').write_text(json.dumps(results, indent=2) + '\n')
    if result.returncode:
        print(name + ' failed; see ' + str(evidence / (name + '.log')), flush=True)
        raise SystemExit(result.returncode)
print(json.dumps({'status': 'RELEASE_CHECKS_PASSED', 'steps': list(results)}), flush=True)
