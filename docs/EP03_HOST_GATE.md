# EP-03 host execution gate

The offline broker and fake-backed tests do not authorize project-code execution. The
runner stays unarmed until this host passes the real rootless acceptance matrix in
`codex/packets/EP-03-execution-outputs.md`. Do not use the system/rootful Docker socket or
substitute an ordinary directory for the broker's hard-quota run volume.

## Current host finding (2026-09-27)

The user-owned `/run/user/1000/docker.sock` is absent. The default Docker context points to
`/var/run/docker.sock` and does not report `name=rootless`. The owner has subordinate UID/GID
ranges, but `newuidmap` and `newgidmap` are absent. The user manager delegates `memory` and
`pids`, not `cpu`. No dedicated, owner-private per-run quota mount or approved installed
image/profile exists. See `verification/implementation/EP-03/broker-host-audit.json` for the
recorded probe. A noninteractive `sudo -n true` check failed because an administrator password
is required; no system settings were changed.

## Operator sequence

1. Install the host `uidmap` package with administrator access and verify that both
   `newuidmap` and `newgidmap` are available. Keep the existing subordinate ID ranges.
2. Add `cpu` delegation for the user manager while retaining `memory` and `pids`. Apply this
   in a planned maintenance window: restarting `user@1000.service` can end the current VS Code
   and terminal session. Verify the active `cgroup.controllers`, not only the unit file.
3. Install/start a user-owned rootless Docker service. Keep the existing system daemon if other
   work uses it. Docker's installer may change the CLI context; the broker always addresses
   the explicit user socket. Check `docker --host unix:///run/user/1000/docker.sock info` for
   `name=rootless`, cgroup version `2` and driver `systemd`.
4. Provide a dedicated empty hard-quota filesystem mount for each run UUID. The broker requires
   an owner-owned `0700` mountpoint beneath an owner-only parent, a different device from that
   parent, 16 MiB to its profile-calculated budget, at most 100,000 inodes, and at least 1 GiB
   free on the host outside the run allocation. A plain directory does not satisfy this check.
   Select and test a mount provider before enabling any profile.
5. Install one disposable toy image by exact digest and locally approve its profile. Run the
   real passing, failing, timeout, cancellation, hostile access, fork/memory/log-volume,
   capture-fault and disk-full cases. Record command, exit code, container identity, observed
   limit and retained evidence under `verification/implementation/EP-03/`. Only then mark the
   packet VERIFIED and proceed to EP-05.

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
