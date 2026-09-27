# Run <run_id> / <project_id> / <workspace_id>
This file is a FORMAT TEMPLATE, not an actual successful run.

Request: <UUID>
Source: <base commit> → <tested snapshot commit + tree digest>
Environment: <approved profile ID + image digest>
Started/finished: <ISO timestamps>
Operation: <COMPLETED | FAILED | CANCELLED | INTERRUPTED>
Execution: <PASSED | FAILED | TIMED_OUT | CANCELLED | NOT_RUN | UNKNOWN>
Publication: <PUSHED / DRAFT_PR / NOT_REQUESTED / BLOCKED / UNKNOWN>
Delivery: <DELIVERED | PARTIAL | PENDING>
Current source changed after test: <true/false/unknown>

## Findings
Actual test totals and parser confidence, exit code, execution duration. Explain project errors
without labelling the infrastructure unhealthy. Never report tests as passed when no parser ran.

## Evidence
output_id | kind / schema | byte size | sha256 | complete? | privacy | actual Drive URL
List all registered outputs; use actual verified IDs/URLs, not invented links.

## External effects
Pushed commit/branch, PR URL/number, created resources. If partial/cancelled, retain completed effects.

## Limits
Truncated streams, missing collectors, parser errors, redacted exports, delivery gaps, unverified fields.

No next operation is automatically scheduled. Owner decides the next development step.
