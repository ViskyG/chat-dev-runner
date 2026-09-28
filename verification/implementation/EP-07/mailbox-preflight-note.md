# EP-07 exact-ID mailbox preflight, 2026-09-28

The installed `0.2.5` wheel performed an exact-ID, read-only Drive/Docs check at
`2026-09-28T17:16:15Z`, after the service had been stopped for host acceptance.
The command was run from `/tmp` so imports came from the installed wheel:

```bash
XDG_DATA_HOME=/home/visky/Work/GPTRunner/.runner-data \
  /home/visky/Work/GPTRunner/.runner-install/venv/bin/python \
  /home/visky/Work/GPTRunner/chat-dev-runner-ep07-mailbox/verification/implementation/EP-07/read_only_mailbox_preflight.py \
  --allow-ephemeral-refresh > /home/visky/Work/GPTRunner/chat-dev-runner-ep07-mailbox/verification/implementation/EP-07/live-mailbox-preflight-20260928.json
```

Exit code: `0`. The final JSON evidence reports `maintenance_safe=true`: exact
existing Installation Index attachment and six workspace registrations passed,
and root, five project, and six workspace mailboxes had zero unexpired READY
commands without a digest-matched terminal receipt. All older workspace READY
commands had expired; workspace delivery receipts were identified using their
native header and JSON payload but were not treated as digest-backed terminal
proof for a still-live READY. The root command had a matching COMPLETED receipt.
The script made no Drive/Docs writes and did not change the owner-private token
file; its optional OAuth refresh remained in memory. The evidence file contains
only IDs, revisions, hashes and command/receipt status metadata. Its local mode
was set to `0600` after capture.

An earlier diagnostic attempt used `drive_check._build_provider`, which shares
one httplib2 transport across concurrent registry reads. It produced inconsistent
`REGISTRATION_IDENTITY_CHANGED`/`REGISTRATION_ACCESS_MISSING` findings. That
provider is unsuitable for this concurrent check. A subsequent sequential
exact-ID read showed the first registration's ten resources had matching
metadata; the final preflight uses the installed runner's
`isolated_request_builder` for request-local HTTP transports and all six
registrations passed. No registry or remote document was changed.

The script has seven focused fake-backed tests, including a command-change race,
malformed content redaction, digestless workspace receipt behavior and transport
selection; Ruff passes. This preflight supports the stopped-service maintenance
gate but does not by itself prove same-path restore, uninstall, or power-cycle.
