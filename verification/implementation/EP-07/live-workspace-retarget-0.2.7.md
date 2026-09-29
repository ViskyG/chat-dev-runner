# Installed workspace retarget

The installed 0.2.7 service completed one connected-chat
`RETARGET_WORKSPACE` command for the existing disposable
`cdr-ep07-ui-20260928/chat-route-20260929` workspace. Its registered
COMMAND, RECEIPT, PROJECT STATE and SOURCE CONTEXT IDs are in the adjacent
JSON. Before the command, the target worktree and the `primary` and `parallel`
worktrees were clean at the declared commit. The new local target branch
already pointed to that commit and was not checked out elsewhere.

The first request used a 20-minute TTL and was correctly rejected as
`INVALID_TTL`, with a matching terminal workspace receipt and no Git or
PROJECT STATE change. After reading that receipt, a fresh request with the
permitted 10-minute TTL was written DRAFT then READY using revision guards;
the full native body matched after each stage. Installed admission completed
the new request with matching ledger digest and terminal Drive receipt.

Independent readback found the selected local worktree and Drive PROJECT
STATE on `feature/ep07-retarget-acceptance-20260929`, at the same commit with
source generation 1. SOURCE CONTEXT names the same branch and snapshot. Git,
ledger and Drive all retained the original clean branch/head/generation for
`primary` and `parallel`. Four effects for the successful operation are
CONFIRMED, no operation remains RUNNING or requires reconciliation, and no
execution, push, PR, new project or second Drive root was caused by this route.
An independent GitHub read still listed only the two existing draft PRs (#1
and #2) on their original branches.

The route's failure cases and crash recovery are supported by focused source
and disposable real-Git evidence. This live check proves the positive
installed provider path; it did not deliberately corrupt this registered
workspace to reproduce those cases.
