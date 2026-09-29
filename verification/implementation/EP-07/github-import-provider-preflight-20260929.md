# EP-07 live GitHub import preflight — read only

On 2026-09-29 the owner runtime configuration was read through `load_owner_runtime_config`. It is present and names GitHub login `ViskyG` (user ID `12452296`), identity `viskyg`, but its `github_imports` list has **zero** entries. The configured GitHub token file was checked only by file metadata: it is a regular owner-owned `0600` file. Its contents were not printed or copied.

Using the production `GitHubREST` / `UrllibGitHubTransport` adapter with the existing `OwnerTokenFile`, read-only GitHub GETs confirmed the authenticated user, the private source `ViskyG/cdr-ep07-ui-20260928` (repository ID `1392469828`, owner ID `12452296`, node ID `R_kgDOUv9nRA`) and default branch `main` at `1eac4fb222d5ace7af33e086e0e19cde65c13bb1`. GitHub supplied the exact HTTPS clone URL `https://github.com/ViskyG/cdr-ep07-ui-20260928.git`. The in-memory `ApprovedGitHubImport` entry with those identities passed its shape validation; no grant was persisted. The import module SHA-256 is `1d1d2bd95203a7d72095f2e1eaac318507027426b03218bf3a1c51f9f7a50045`, matching the earlier host import evidence.

The import gate remains open. `LocalPolicy` refuses a GitHub source not named in the owner configuration, and `GitHubImportService` independently requires the exact approved repository and authenticated identity before the clone effect. HTTPS cloning or a Runner `CREATE_PROJECT` import now would bypass that owner policy. Neither was attempted. No owner configuration, installed service, Drive document, GitHub repository or source checkout was changed.

If the owner approves one import test using this exact source and a new private disposable destination project, add this one object to the owner-only `github_imports` array in `~/.config/chat-dev-runner/owner-runtime.json`, preserving all other fields and `0600` permissions:

```json
{
  "repository": "ViskyG/cdr-ep07-ui-20260928",
  "github_identity": "viskyg",
  "authenticated_login": "ViskyG",
  "repository_id": 1392469828,
  "owner_id": 12452296,
  "node_id": "R_kgDOUv9nRA"
}
```

Then restart the installed service to reload the local policy, verify the exact grant and source metadata again, choose an unused disposable project ID, and submit one finite `CREATE_PROJECT` with `source.kind=github` through the normal admitted route. That operation can create a **new** private GitHub destination and Drive project; those external effects require the separately requested owner approval. Compare the source repository identity/default head and existing checkout before and after, and retain the exact terminal receipt and managed clone/workspace evidence. If GitHub metadata changes before the attempt, refresh the grant deliberately instead of silently broadening it. Do not adopt or replay an uncertain partial import.
