---
id: CDR-SOURCES
checked: 2026-09-26
---
# Primary references and verification boundaries

References support API/platform facts, not a claim that this design is endorsed by providers.
The component boundaries, default budgets, UI choices and user interaction limits are our design.

- **S1 — Git worktree manual.** Multiple working trees, branch/worktree lifecycle and constraints.
  https://git-scm.com/docs/git-worktree
- **S2 — Google Drive API scopes.** `drive.file` is scoped file access, not an assumed recursive
  permission over every future document placed in a folder.
  https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- **S3 — Docs documents.batchUpdate.** Atomic document batch and revision write control.
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

## Direct tool inspection in this session

Available Google Drive actions exposed get_document/get_document_text, revision-guarded
batch_update_document, metadata reads, folder operations and raw file replacement. This is
schema discovery, NOT a successful real-account code round-trip. No credential-bearing
provider writes, project execution or repo creation were performed in the user's account.

## Unverified until deployment

Google OAuth per-file access across runner-created/chat-edited Docs; real connector write
availability in the chosen chat mode; private draft PR entitlement; GitHub new-repo token
coverage; actual rootless runtime and isolation; real project test profiles; OS editor-open
handler; API call quotas and polling performance. EP-00 inventories these gates; EP-04/EP-07 collect authorized live evidence before the
release assumes availability. Do not fabricate API permission conclusions from one successful login.
