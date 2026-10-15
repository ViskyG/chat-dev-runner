---
name: install-runner-v2
description: Prepare an authorized installation or upgrade of this repository's existing Runner service using its v2 configuration, preserved v1 bindings and a matching stopped-state rollback.
metadata:
  version: "2.0.0-source-preview"
  state: source-preview
---
# Install or upgrade Runner v2 — source preview

Use on an installation/upgrade request for this project. This is repo-local
source, not an installed skill or deployed capability. Read the project's AGENTS,
current installed version/evidence and [V2 operations](../../../docs/V2_OPERATIONS.md)
sections “Owner configuration and service” and “Upgrade and rollback boundary”.
The [Russian companion](SKILL.ru.md) carries the same contract.

Establish ordinary account, actual service/XDG paths, durable `runner_id`, DB
schema, exact existing Drive attachment, selected GitHub repository grants and
installed tool/profile paths without exposing credentials. Keep the existing
user service and loopback boundary. GitHub-only needs no Google setup; selecting
repositories manually remains the owner's choice.

Reuse existing authorization. Prepare concrete source/config/schema/service
items and rollback for any newly required deployment change; ask only for a
consequential item outside the authorized scope. Source-stage approval does not
install a wheel, register global skills or authorize system SDK installation.
Do not repeat settled trust, transport or ordinary recipe approvals.

Before schema 10, stop the service and use the preserved old schema-9 binary's
`runner backup --output ...`; verify its recovery identity. Preserve the old
binary and owner-private config binding. Require verified v2-inclusive backup
support before relying on post-upgrade archives. Never roll old code onto a
schema-10 database or silently replay work after restore.

Use a reviewed owner-local `runtime-v2.json`, mode 0600, matching the ledger's
runner ID; preserve `enable_v1` deliberately. Keep credentials outside repos and
exact Drive root/index IDs unchanged. Apply an approved wheel/service plan with
the existing installer, not a second daemon. Normal configured startup arms
intake; `--start-unarmed` is for a controlled stopped-intake process.

Report source/wheel identity, applied scope, actual channel/backend observations,
rollback evidence and remaining target gates. Only real installed provider/OS
acceptance can promote deployment status. Reading these files does not register
a skill or authorize canonical library/navigation changes.

Provenance: [unchanged design source](../../../design/github-native-v2/skills/install-runner/SKILL.md),
[hash index](../PROVENANCE.json). Current source APIs take precedence over the
historical design's not-yet-implemented wording.
