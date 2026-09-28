# EP-07 containment and resource-budget evidence bridge

Reviewer: Codex, 2026-09-28. Scope: the existing owner host and the approved
`python-quick` rootless execution route. This is a read-only synthesis of retained
real-host proof; it did not submit a Drive command, rerun an unchanged hostile toy,
read token bytes, or start the production service.

The broker installed in wheel `0.2.5` and current EP-07 source is byte-identical:
`runner_app/executors/broker.py` SHA-256
`45c6d01c88ed9a38bb54499c25832c8b60abd0581a812dd4dda8136b3c554bd0`.
There is no diff for that file from installed-source commit `3815d1d` to current
source. The two installed EP-07 publication routes in
`source-identity-readonly-20260928.json` recorded `rootless_docker`, verified
containment, approved `python-quick` revision
`b47160af8e5abd5ea7cc4b9624ee1a929216a5705c394fd4e287468590efd182`,
and pinned Python image digest
`4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111`.
They completed separate passing and failing verification/publication operations
through the installed service. The approved preparation and test profiles have
`network=none` and `secrets=[]`; the broker verifies mounts before start and
records cleanup after execution.

The EP-03 real-host rootless matrix in
`../EP-03/live-acceptance-summary.json` used the same pinned image and Docker
daemon ID `7ccec57e-4bca-4a3c-910a-acc924d2180f`. Its hostile case
`7fa65503-8531-4c49-a465-4231dc67fdbd` directly observed host home,
Docker socket and sibling workspace absent, external routing blocked, and
only loopback present. Other real cases directly measured timeout
(`e756cc0e-2f73-47b1-b0ef-48bb32ce3c10`), PID refusal at 32 with
`pids.events` increment (`da5907c5-d826-47d8-9c56-cfad4abb56c0`), OOM at
128 MiB with `memory.events` increment (`86882085-2377-4ab7-abd0-ecfad9c4efbf`),
10 MiB observed / 8 MiB retained stream truncation
(`8177323c-c66b-48ea-a04d-4fe4536eb780`), and a full 64 MiB hard-volume
limit (`a8487949-073f-4e82-a037-d508a8d12115`). Each case retained intent,
result and cleanup evidence. The later EP-07 real-host CPU case in
`cpu-containment-summary.json` used the same broker, daemon and image, with
`cpu.max=25000 100000`; over 3.001 seconds `nr_throttled` rose 3→33 and
`throttled_usec` rose 182241→2418811. Its exact container was absent and the
root-controlled quota volume was released after the run. This also exercises
the later quota-volume path used by the installed service.

Inference and limit: token-byte denial is established from the absent host home,
absent credential mounts, fixed mount policy and `secrets=[]`; no secret was
opened as a test fixture. The observed threshold exceedances used smaller toy
limits than the installed `python-quick` profile (600-second time, one CPU,
128 PIDs, 512 MiB memory, 64 MiB logs, 256 MiB volume). The installed routes
prove that the same enforcer applies that approved profile; this synthesis does
not claim a direct exceedance at every production threshold. It closes the
containment and actual-enforcement criteria for this unchanged rootless boundary,
while outage, cancellation, malicious output, reboot and final-wheel gates remain
separate.
