# Analytics Control Center Compose route

Owner authorization: 2026-10-23, after locating the Analytics Platform and its
isolated Control Center contract: “Действуй и не останавливайся, работай”.

- Target: a separate test branch/workspace on the existing Runner test repository,
  installed owner-user Runner, existing rootless daemon and hard-quota helper.
- Version identity: the platform's existing Control Center release manifest pins
  runtime/UI images and upstream source; record those identities with the frozen
  test adapter source and installed Runner version. No platform release/promotion.
- Nominal scenario: publish one fresh v2 RUN_TASK through the existing
  `cdr-runner-test-github` mailbox on `main`, selecting source branch
  `runner/analytics-cc-20261023`; admit, freeze source, start actual released application on an internal
  network using example catalog/temporary data, run its browser contract, retain
  reports/logs, clean exact owned resources, publish terminal result to GitHub.
- Changed boundary: real multi-image browser workload and project enrollment.
  Use existing lifecycle/source/publication logic unless the workload exposes a
  concrete missing behavior. No generic YAML implementation is presumed necessary.
- Prior evidence reused: audit-20261022 regression; GP08 timeout/cancel/readiness,
  immutable source/profile rejection, resource identity and uncertain cleanup
  cases. Do not replay unchanged adverse cases. New adapter behavior receives
  focused structural/contract checks and the one installed cloud roundtrip.
- Effects: two digest-pinned image downloads, new local test source/profile,
  one added Compose profile for the existing `cdr-runner-test` project, idle
  service restart, new test branch and reports. All ten channels remain unchanged;
  never overwrite READY intent.
- Evidence: request ID/digest, source commit, image IDs/digests, browser report,
  execution and delivery axes, exact resource cleanup and independent GitHub read.
- Budget: bounded request covering readiness, finite browser contract and cleanup;
  record actual elapsed timing. Observe long work through Process HUD.
- Safe stop: unavailable registry/GitHub access stops that dependency; ambiguous
  execution retains lane/evidence for exact reconciliation, never request replay.
  Preserve original runtime config backup; rollback only our added registration.
- Exclusions: analytics-server deployment, production catalogs/data/credentials,
  DWH jobs, production Control Center changes, arbitrary Compose files and full
  platform acceptance. Hosted CI is a distinct observation, not inferred locally.
