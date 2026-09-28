# EP-03 host execution gate

The real rootless acceptance matrix in `codex/packets/EP-03-execution-outputs.md` passed
on this host; see `codex/checkpoints/EP-03.md`. EP-05 then exercised the installed
per-run hard-quota provider with passing and failing disposable projects. The runner
remains disarmed pending EP-07 durable service/release acceptance. Never substitute the
system/rootful Docker socket or an ordinary directory for the broker's hard-quota volume.

## Historical host finding (2026-09-27)

The owner authorized and completed a one-time local administrator setup. The user-owned
`/run/user/1000/docker.sock` served Docker 29.1.5 in rootless mode; the broker's
read-only probe returns `READY` with cgroup v2, systemd, and active `cpu memory pids`
delegation. `uidmap` and linger are installed. A disposable pool of 12 paired,
owner-private, 64 MiB tmpfs mounts is available for toy PREPARATION/TEST checks, and
an approved Python image was pulled by exact digest into this rootless daemon.
`verification/implementation/EP-03/live-host-setup-20260927.json` records the
read-only checks. The default Docker context remains the pre-existing `default`;
the broker always names the user socket explicitly.

The volume pool was finite acceptance infrastructure, not the automatic per-project
quota provisioner. Live toy results are recorded in `codex/checkpoints/EP-03.md`.

## Later acceptance and product volume path (2026-09-28)

After reboot, the finite EP-03 tmpfs pool was absent. The same 24 existing empty
slots were remounted through one authorized local administrator interaction and
verified as separate 64 MiB/16,384-inode filesystems. One pair then passed a
real `python-basic` PREPARATION→TEST/JUnit run through the broker; see
`verification/implementation/EP-05/live-python-quick/`. This pool is retained
only as acceptance infrastructure. The product-side per-run volume provider and
fixed root-owned helper were implemented and installed in EP-05, with the scoped
sudo rule. The installed helper SHA-256 and
`READY` probe are recorded in `verification/implementation/EP-05/live-volume-relocation-probe.json`.
It accepts only the reviewed 64/256 MiB sizes and proof-releases exact empty tmpfs
mounts. `scripts/install_volume_helper.sh` is the reviewed installer used with the
owner's administrator authorization; it is not a remaining setup step on this host.
The owner can retire terminal, evidence-verified volumes with
`scripts/release_run_volumes.py`; uncertain mounts stay available for review.

## Earlier host finding (before setup, 2026-09-27)

The user-owned `/run/user/1000/docker.sock` is absent. The default Docker context points to
`/var/run/docker.sock` and does not report `name=rootless`. The owner has subordinate UID/GID
ranges, but `newuidmap` and `newgidmap` are absent. The user manager delegates `memory` and
`pids`, not `cpu`. No dedicated, owner-private per-run quota mount or approved installed
image/profile exists. See `verification/implementation/EP-03/broker-host-audit.json` for the
recorded probe. A noninteractive `sudo -n true` check failed because an administrator password
is required; no system settings were changed.

## Acceptance sequence (completed in EP-03/05)

1. Install the host `uidmap` package with administrator access and verify that both
   `newuidmap` and `newgidmap` are available. Keep the existing subordinate ID ranges.
2. Add `cpu` delegation for the user manager while retaining `memory` and `pids`. Apply this
   in a planned maintenance window: restarting `user@1000.service` can end the current VS Code
   and terminal session. Verify the active `cgroup.controllers`, not only the unit file.
3. Install/start a user-owned rootless Docker service. Keep the existing system daemon if other
   work uses it. Docker's installer may change the CLI context; the broker always addresses
   the explicit user socket. Check `docker --host unix:///run/user/1000/docker.sock info` for
   `name=rootless`, cgroup version `2` and driver `systemd`.
4. Provide two separate dedicated hard-quota filesystem mounts for each prepared VERIFY:
   one empty PREPARATION mount named by its phase UUID and one initially empty TEST mount
   named by the request UUID. The broker requires
   an owner-owned `0700` mountpoint beneath an owner-only parent, a different device from that
   parent, 16 MiB to its profile-calculated budget, at most 100,000 inodes, and at least 1 GiB
   free on the host outside the run allocation. A plain directory does not satisfy this check.
   The preparation and test devices must also differ from each other. Select and test a
   mount provider before enabling any profile.
5. Install one disposable toy image by exact digest and locally approve its offline
   preparation and test profiles. Select a disposable lockfile/dependency source and a TEST
   command that actually consumes staged `/build`. Run the
   real passing, failing, timeout, cancellation, hostile access, fork/memory/log-volume
   and disk-full cases. The disk-full run must expose a real capture resource fault;
   deterministic injected storage-write/metadata-write faults and delivery/redaction paths
   remain in the offline integration suite so their exact failure points are reproducible.
   The EP-03 checks and observed limits are recorded under
   `verification/implementation/EP-03/`; EP-05's permanent-volume and passing/failing
   project evidence is under `verification/implementation/EP-05/`.

Read-only checks:

```bash
command -v newuidmap newgidmap
systemctl show user@1000.service -p Delegate -p DelegateControllers
cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/cgroup.controllers
test -S /run/user/1000/docker.sock
docker --host unix:///run/user/1000/docker.sock info --format '{{json .SecurityOptions}} {{.CgroupVersion}} {{.CgroupDriver}}'
```

Docker's [rootless prerequisites](https://docs.docker.com/engine/security/rootless/) require
`newuidmap`/`newgidmap`; its [resource-limit guidance](https://docs.docker.com/engine/security/rootless/tips/)
requires cgroup v2 with systemd and explains CPU controller delegation. The profile, quota,
source-mount and cleanup checks are enforced by `runner_app/executors/broker.py`.
