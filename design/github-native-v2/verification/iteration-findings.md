# Findings while validating this design package

1. Initial 57 model tests passed. Review then added restart-cleanup and candidate-cancellation cases.
2. During that extension, an overly broad text edit accidentally made the invariant checker demand ACK proof from non-runnable ACK_PENDING/ACK_UNKNOWN candidates. Three checks correctly failed at teardown. `intermediate-failure.txt` preserves this run. The correction confines that requirement to QUEUED/STARTING/RUNNING; pending candidates remain non-runnable.
3. Review exposed a usability gap in the model: a duplicate could return a receipt yet leave the reused slot READY. The model and protocol now clear it conditionally without adding another job; a duplicate of an unresolved ACK stays pending, not falsely ACCEPTED.
4. Restart now explicitly holds the execution lane when prior processes might remain. This is a modelled guard; actual OS cleanup proof remains a target test.

These are offline package/model changes, not repairs applied to the user's installed runner. The final verification report records the latest complete test run.
