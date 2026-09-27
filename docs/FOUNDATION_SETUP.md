# EP-00 reproducible setup

Host selected interpreter: Python 3.10.12 on Ubuntu 22.04.5 x86_64. The host was inventoried;
no interpreter, Docker, Kubernetes, system service or kernel configuration was upgraded.
`verification/implementation/EP-00/host-inventory.json` contains actual probes.

`requirements.lock` pins application/test/build/lint/type/browser dependencies with hashes
for the selected Python/Linux environment. Optional legacy Google bootstrap dependencies are
not activated or authenticated. Application and build dependencies in `pyproject.toml` are
also pinned. To reproduce locally:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/runner --help
.venv/bin/runner doctor --offline
.venv/bin/runner install --dry-run
.venv/bin/python -m pytest -q
.venv/bin/ruff check runner_app tests/test_foundation_api.py tests/test_foundation_storage.py
.venv/bin/mypy runner_app
.venv/bin/python -m build --wheel --no-isolation
```

The existing host `uv` resolver generated the hash lock; it is not a runtime dependency.
To regenerate deliberately: `uv pip compile pyproject.toml --extra test --extra dev --extra
ui-test --python .venv/bin/python --generate-hashes --output-file requirements.lock`.
Changes require renewed acceptance evidence. Wheel checks must install into a fresh venv
and run outside the source checkout with isolated Python; `schemas/` is packaged from the
same authoritative source directory. Never rely on PYTHONPATH to repair a wheel.

The approved candidate base for linux/amd64 is:
`docker.io/library/python@sha256:54b4fc9408ea4f5d1b1b9c63c7ef1968d46d3b927e00df8ab1f09364593f979f`.
Registry metadata identified Python 3.10.21 from `python:3.10-slim-bookworm`; the immutable
manifest and config hashes were verified. See `sandbox-image.json` for request/digest evidence.
No image was pulled or executed. This is the selected base, not a qualified test environment:
pytest/dependencies still need an approved locked contained build in EP-03. The profile is
uninstalled and unapproved. No rootless runtime socket is configured on this host. Existing
rootful Docker is not an execution fallback. Runtime containment and release gates remain
blocked until the owner configures a compatible rootless execution broker.

Primary documentation checked during selection: [FastAPI manual deployment](https://fastapi.tiangolo.com/deployment/manually/),
[PyPA package metadata](https://packaging.python.org/en/latest/specifications/pyproject-toml/),
[PyPA wheel format](https://packaging.python.org/en/latest/specifications/binary-distribution-format/),
[official Python image](https://hub.docker.com/_/python),
[Docker rootless mode](https://docs.docker.com/engine/security/rootless/).
The project's mandatory loopback policy overrides examples that use public bind addresses.
