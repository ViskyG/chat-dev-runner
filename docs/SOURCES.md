---
id: CDR-SOURCES
checked: 2026-09-27
---
# Primary references and verification boundaries

References support API/platform facts, not a claim that this design is endorsed by providers.
The component boundaries, default budgets, UI choices and user interaction limits are our design.

- **S1 — Git worktree manual.** Multiple working trees, branch/worktree lifecycle and constraints.
  https://git-scm.com/docs/git-worktree
- **S2 — Google Drive API scopes.** `drive.file` is scoped file access, not an assumed recursive
  permission over every future document placed in a folder.
  https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- **S3 — Docs documents.batchUpdate.** Atomic document batch and revision write control;
  requiredRevisionId rejects a stale base.
  https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate
- **S4 — Drive changes.** Start/page tokens, chronological changes and durable cursor handling.
  https://developers.google.com/workspace/drive/api/guides/manage-changes
- **S5 — GitHub repository endpoints.** Repository creation, explicit private flag, per-endpoint
  authorization. Capabilities must be checked for the user's actual identity and account.
  https://docs.github.com/en/rest/repos/repos#create-a-repository-for-the-authenticated-user
- **S6 — GitHub pull-request endpoints.** PR operations and access/capability constraints.
  https://docs.github.com/en/rest/pulls/pulls
- **S7 — Docker rootless mode.** Non-root daemon/container mode, not a universal security guarantee.
  https://docs.docker.com/engine/security/rootless/
- **S8 — MDN CSRF.** Browser request credentials and CSRF mitigations; localhost still needs
  deliberate mutation protection in this design.
  https://developer.mozilla.org/en-US/docs/Web/Security/Attacks/CSRF
- **S9 — MDN prefers-reduced-motion.** Respect the operating-system accessibility preference.
  https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion
- **S10 — OpenAI connected apps.** Supported app integration is a product feature whose
  availability must be checked in the actual chat. This is not individual policy approval.
  https://help.openai.com/en/articles/11487775-connected-apps-in-chatgpt
- **S11 — Drive files.create.** Create a native Doc or folder with an explicit parent ID.
  https://developers.google.com/workspace/drive/api/reference/rest/v3/files/create
- **S12 — Drive file metadata.** `appProperties` are private to the requesting app, while
  `parents` identify the actual containing folder.
  https://developers.google.com/workspace/drive/api/reference/rest/v3/files

## Direct tool inspection in this session

Available Google Drive actions exposed get_document/get_document_text, revision-guarded
batch_update_document, metadata reads, folder operations and raw file replacement. On the
owner's connected shared experiments account, a dedicated local-OAuth-created canary Doc
was read and edited through the connector, then read back through local OAuth. Its exact
IDs and content digest are in `verification/implementation/EP-04/live-existing-installation-canary.json`.
No pre-existing Drive file was edited or deleted, and no project execution or runner-created
project repository was performed.

## Deployment status after the original source inspection

The cross-app Doc canary established one account/session boundary, not universal access.
EP-07 later verified installed root COMMAND/RECEIPT routes, private draft PRs, rootless
project runs and a fixed-argument VS Code open request; see
`codex/RELEASE_ACCEPTANCE.md` for their exact evidence and limitations. A visible editor
window was not asserted. Fresh-chat access, newly created private repository selection in
the chat GitHub connector, provider quotas and the full release scenario matrix remain open.
