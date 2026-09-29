---
id: CDR-DRIVE
status: accepted-design-with-live-gate
---
# Google Drive and Docs protocol

> The owner has already provisioned this installation. Attach the runner to the exact
> root and Installation Index IDs in `config/installation.existing.json`; validate the
> indexed files under the owner-authorized full Drive OAuth scope. `drive-seed/` is the reference specification.
> Do not run bootstrap `--apply` for this installation or create a duplicate root.

## Why native Docs, not a global editable spreadsheet

The command channel uses dedicated runner-created native Docs. A new chat reads plain
instructions and edits one structured JSON body. No spreadsheet formula interpretation,
row sorting, shared status column or ambiguous `edited=1` flag. A mailbox is a single current
command slot, not a waiting queue. Its historical requests are frozen in SQLite and receipts.

On the owner's connected account, an EP-04 canary under the existing root passed local OAuth
write/readback → connector revision-guarded edit → local OAuth readback. The connector's
capabilities can differ in a new chat or after reauthorization, so the runner still
checks the actual connection state instead of assuming access from this one result.
The dedicated test document and exact IDs are in
`verification/implementation/EP-04/live-existing-installation-canary.json`.

## Ownership and OAuth scope

For this dedicated Google account, the owner explicitly selected the full Drive OAuth
scope `https://www.googleapis.com/auth/drive` instead of `drive.file`. This is the only
scope requested by the local Desktop OAuth flow. A cached token with the former scope is
rejected. The wider scope does not replace exact-ID registration, root/Index validation or
revision checks. The runner first reads the existing root and Installation Index by their
owner-provided IDs, then validates each indexed file before any write. Future
CREATE_PROJECT/CREATE_WORKSPACE provisioning uses that same local client and stores exact
IDs in private local registrations. ChatGPT edits only those registered command/change Docs. A generic document
copied by the chat is not automatically a valid watched command. For pre-existing files
created by the earlier installer, a private local exact-ID manifest pins the human-readable
Index hash and validates that every registered ID appears in the Index text. It accepts
missing current-app private metadata only for those explicitly mapped legacy IDs, with
unchanged type, parent, title, capabilities and Index revision; newly created files keep
current-app ownership checks. The manifest is owner-only and outside the source repository.

The chatbot connector and local runner are independently authorized. Never save bearer tokens,
refresh tokens or OAuth client secrets in Drive instructions. The owner reports ChatGPT's Drive and GitHub connections configured and the runner's
private GitHub identity was verified in EP-05. Those are separate grants. EP-07 must
still confirm a fresh chat can read the published instructions and issue one valid
command; owner-reported access alone does not prove that round trip.

## Structure and writers

```text
START HERE                       bootstrap-created owner guide
INSTALLATION INDEX               provider-returned resource IDs
00 Control/
  CHAT PROTOCOL                  runner writes versioned instructions; chat reads
  COMMAND                        root command; chat writes CREATE_PROJECT
  RECEIPT                        root receipt; runner writes
  SYSTEM STATE                   runner writes IDs, epoch, versions and capabilities
  COMMAND RECIPES                 inert examples, never watched as commands
01 Projects/<project_id>/         created by the live provisioning workflow
  PROJECT STATE                  runner writes branch/workspace/profile index
  PROJECT COMMAND                chat writes CREATE_WORKSPACE / FETCH etc.
  PROJECT RECEIPT                 runner writes
  <workspace_id>/
    COMMAND                      chat writes one scoped operation
    CHANGESET                    chat writes explicit UTF-8 file changes
    RECEIPT                      runner writes acceptance/final status
    LATEST RESULT                runner writes summary and output links
02 Results/<request_id>/
  operation-manifest.json        also exists for operations with no program run
  frozen-command.json
  <run_id>/
    input-manifest.json
    run-manifest.json
    report.md and/or native readable report
    sanitized, locally approved output files
    bundle-index-<delivery_request_id>.json
    delivery-index-<delivery_request_id>.json  exact Drive file IDs and hashes; written last
03 Contracts/                    schema versions and formats
04 Templates/                    inert source templates, not active mailboxes
```

Use IDs stored in the installation/project index, not title search, for dispatch. A moved,
missing or duplicated document is a configuration problem; do not guess a replacement by name.
Do not derive destination paths from arbitrary document text. Validate every blob reference
against the runner-created index and output allowlist. No arbitrary URL fetching.

The original accepted command remains in private SQLite. The delivered
`frozen-command.json` is a non-executable view: it carries the canonical request digest
and replaces the free-form `intent` text with a redaction marker. A delivery attempt declares
each Drive/Docs write in the local effect journal before making one provider call, verifies
the returned ID, parent and content hash, and leaves an uncertain write for reconciliation.
An explicit `PUBLISH_REPORT` can deliver the same saved run without executing it again. A
new attempt writes a new immutable index; it does not overwrite the old run evidence.

## Mailbox lifecycle

1. Read COMMAND, the current receipt and current source manifest. A previous READY command must
   have a **matching request_id** terminal receipt before replacement. An older terminal receipt
   does not prove the current READY command was observed. Do not overwrite unacknowledged intent;
   expired unseen commands require explicit review/abandonment, not silent overwrite.
2. Stage `CHANGESET` as ordinary JSON. Its full document text is a JSON object with explicit paths,
   expected old SHA-256 values and literal new UTF-8 contents. No rich-text code interpretation.
3. Read the changeset's actual Docs revision; write a new command with a new UUID, the current
   runner epoch, expected full Git HEAD and a short expiry. The initial dispatch is DRAFT.
4. Re-read. Publish READY as the final revision-guarded change. Never edit an already-submitted
   request; create a fresh ID for new intent. `intent` is audit text, not authorization.
5. Runner fetches the exact known command and changeset. It validates schema and ownership,
   checks expected Docs revision, captures immutable bytes, and verifies revisions again
   before admission. Revision drift yields REJECTED/INPUT_CHANGED, not a guessed merge.
6. SQLite freezes the accepted request digest. The runner does not rewrite the chat-owned
   command body; it writes a separate receipt. Same ID + same digest is a lookup. Same ID +
   changed content is REQUEST_ID_REUSED. No second execution.
7. After the requested plan, publish report and terminal receipt. These writes never trigger
   code execution because only command Docs are watched. The next chat iteration needs a new
   user instruction; no active-turn polling loop belongs in CHAT PROTOCOL.

Docs batchUpdate supports atomic batches within one document and revision controls [S3].
This is NOT atomicity across command, changeset, Git and SQLite. The captured revision IDs,
payload hash, admission transaction and staging snapshot provide the cross-system protocol.

## Timing

Poll only known command IDs, initially every 15 seconds (a local policy default, not a provider
promise). Back off on throttling/offline errors; surface stale observation time. Do not update
a “last successful check” timestamp on a failed read. Use pagination and a saved cursor if
later switching to Drive changes feed [S4]; checkpoint the cursor only after durable handling.
No push/webhook listener is needed. No public inbound address is created.

READY requests expire by default after 15 minutes. The user may adjust local TTL policy; the
model cannot increase it through command text. Inactive/offline runner status must not imply
that an old command is guaranteed to execute later.

## Code transport and context export

GitHub is source history. Drive transports changesets and reviewed source excerpts where
a delivery route explicitly supports them, not a blind bidirectional mirror. The current
`EXPORT_CONTEXT` route computes a bounded committed-tree index with path, size, content hash
and source snapshot in the private owner-local operation result. Selected excerpts stay local
for privacy review. Its workspace Drive RECEIPT conveys only terminal status and request
digest; it does not update SOURCE CONTEXT or deliver source bytes. Chat can read exact source
files through a separately authorized GitHub connector grant. Publishing reviewed excerpts
or an index into Drive needs its own writer-coordination and privacy contract. Context
snapshots are read-only views. Local manual edits are not silently replaced; expected base
and per-file hashes must match.

Large or binary edits use an explicitly reviewed blob extension later. v1 JSON changesets
have bounded counts and byte size. Never force binary data through Docs rich text. Artifacts
may still be binary; that is an output-transport feature, not a source-edit feature.

## Bootstrap safety

`bootstrap_drive.py` defaults to PLAN_ONLY. For this installation,
`config/installation.existing.json` permanently disables `--apply`; the seed is a reference,
not a migration target. Any future bootstrap for a different, explicitly selected installation
must preserve app-tagged IDs, user edits and no-overwrite behavior. It never changes sharing,
issues READY commands, creates GitHub repos or arms execution. After a partial API failure,
re-read local state and reconcile resources rather than starting a new install ID.

Cloud behavior is not certified by unit tests; EP-04 requires account-level evidence.
References [S2–S4] are listed in SOURCES.md.
