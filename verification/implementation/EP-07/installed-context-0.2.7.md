# Installed 0.2.7 owner-local context gate

Result: **PASS** for one `EXPORT_CONTEXT` on the existing disposable
`cdr-ep07-ui-20260928/chat-route-20260929` workspace after its connected-chat
retarget. The installed service was armed at epoch
`6e9f5dde-2374-4155-a760-344a30b47539`. The owner-local API accepted request
`5bd281a1-4357-4eb0-a74a-a4470ce80a44` and reached terminal `COMPLETED`.

The route pinned generation 1, branch
`feature/ep07-retarget-acceptance-20260929`, and commit
`1eac4fb222d5ace7af33e086e0e19cde65c13bb1`. The selected committed
`README.md` excerpt was 215 bytes; its SHA-256 matched an independent read of
that exact Git blob. The bounded inventory had 12 files, with 9 hashed under
the route budget. The private operation result marked the selected excerpt
`LOCAL_ONLY`, required privacy review, and had `include_diff=false`. The local
terminal receipt matched the request digest and contained no source excerpt.
The operation recorded zero external effects. The registered and observed Git
head, branch, generation, and clean state matched before and after; an
independent Git check still showed the same clean branch and head.

The probe performed exactly one authenticated local operation POST, wrote no
Drive command, and made no provider write. This proves the installed local
context path and its local privacy boundary; it does not claim chat delivery of
source bytes or a live Drive-origin context request. The separately recorded
installed UI readback proves the context control is available. No excerpt or
bootstrap secret is stored in the committed evidence.

Reproduction: `installed_context_gate.py` is inert unless passed `--apply`
and a new absolute `--output` path. Syntax parse, inert default, and focused
Ruff check passed. The redacted machine record is
`installed-context-0.2.7.json`.
