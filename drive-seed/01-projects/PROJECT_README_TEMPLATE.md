# Project <project_id>

This template becomes project documentation during provisioning. Do not treat placeholders as state.

Purpose: <owner description>
Repository: <observed provider URL and canonical owner/repo ID>
Local project: <runner-controlled root; no arbitrary Drive-specified path>
Chat repository access: MANUAL_REQUIRED until verified
Approved template / test profiles: <local policy IDs>
Publication: draft PR, workspace branch only, no merge

## Workspaces
workspace_id | display_name | branch | source_commit | command_doc_id | context_doc_id | latest_result_id
Populated by runner from real created entities. The table is a view, not the permission registry.

## Sources and delivery
Git is the source history; each run snapshots exact bytes. Drive carries instructions, bounded
context and explicit change sets. It is not a blind bidirectional mirror of the whole repository.

## Current work
Owner goal, scope, acceptance criteria, expected outputs and external dependencies.
Do not store credentials, machine secrets or private production records in this document.
