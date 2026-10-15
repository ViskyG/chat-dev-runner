# Proposed v2 contracts (not current runner schemas)

The schemas define structural envelopes. Semantic checks (session token, branch validity, runner binding, duplicate IDs, safe paths, task references and approvals) are also mandatory. Never equate JSON-schema validity with permission to execute. Existing v1 schemas/readers remain separate.

All command examples are DRAFT and inert. The example result is explicitly EXAMPLE, not measured evidence. Generated timestamps, UUIDs and digest fixtures are test data; they do not refer to real resources. Do not copy examples into a live mailbox as READY.

Task argv permits documented whole tokens, including `{output:<id>}` resolving to a specific declared output path under out. `{vitest}` and `{tsc}` resolve project-local executables through the installed Node toolchain; `{npm}`/`{maven}` may expand to an audited executable+prefix argv sequence (Windows shims require testing). Expansion rules must be typed, no implicit shell. Output declarations sourced from workspace (e.g. Maven) are copied to out after validating containment; no arbitrary traversal/symlink/reparse collection. Wildcard output IDs cannot be used as scalar `{output:<id>}` tokens.

Examples are minimal integration fixtures requiring already prepared compatible projects and installed tools. Python example assumes the declared venv is prepared. .NET example is a **VSTest** variant; MTP requires its own probed recipe. These are not universal copy-paste fixes for any existing repository. User approval is required before installing/reporting/configuration changes.

Run `python verification/check_contracts.py` from package root when jsonschema is available. The stdlib-only protocol model has no jsonschema dependency. Never install dependencies globally to run this design check.
