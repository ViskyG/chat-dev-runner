# Installed 0.2.7 upgrade checkpoint

The owner-user service was stopped and disabled after an exact-ID, read-only
preflight of 13 registered Drive mailboxes. None held an unexpired READY command
without terminal evidence. Its 18 operations and 177 effects had no RUNNING or
reconciliation-required entry. The stopped-state backup at
`/home/visky/Work/GPTRunner/.runner-backups/pre-0.2.7-20260929T0907Z` is owner-only;
an independent verification checked all 597 file hashes and sizes, zero
symlinks, SQLite schema 9 and `integrity_check=ok`. It excludes credentials and
tokens.

The frozen source passed 1406 tests with one marked skip, Ruff, mypy and JS
syntax checks. The wheel hash, 126 byte-identical source members, unchanged
production lock and 43 compatible installed packages are recorded in the
adjacent JSON and `source-freeze-0.2.7.json`. Hosted CI run 36547026925 passed
on commit `888b4aef78c445bf9f85377424bb9f6a35bdb1be`.

The exact wheel was installed into the private wheel environment, and
`install --dry-run` reported CURRENT. Reinstalling the managed owner unit
enabled and started it. Read-only post-start checks observed 0.2.7, active and
enabled service, `Linger=yes`, only `127.0.0.1:8765`, HTTP 200 health, armed
intake and the same epoch in the local ledger and existing Drive SYSTEM STATE.
The 18 operations/177 effects were unchanged before any new canary. Session,
Host and Origin rejection checks passed. No root, repository, project or
workspace was created as part of the upgrade.

This is a deployment checkpoint, not EP-07 release sign-off. New route
acceptance and the remaining real-provider/owner gates are tracked separately.
