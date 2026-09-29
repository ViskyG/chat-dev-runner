# EP-07 observed boot startup — 2026-09-29

The host's previous boot stopped the owner-user Runner cleanly at 22:16:26 +04 on 2026-09-28. A different kernel boot ID began at about 11:18 +04 on 2026-09-29. The enabled user unit with Linger=yes started automatically. Its first attempt failed one second later with `OAUTH_REFRESH_FAILED`; the configured `Restart=on-failure` restarted it five seconds later. The installed 0.2.6 application completed startup at 11:18:31, listening only on `127.0.0.1:8765`.

The restarted process created a fresh armed local epoch. Connected Docs read the same epoch and ARMED state from the exact existing Drive SYSTEM STATE; a later independent project CREATE_WORKSPACE command using that epoch completed. This is a real host-boot observation, including its transient first-start error. It does not assert that the first process attempt succeeded or diagnose a power outage. Machine facts and sources: `host-boot-auto-intake-20260929.json`.
