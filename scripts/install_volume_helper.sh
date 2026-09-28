#!/usr/bin/env bash
# One-time root installation of the narrow, root-controlled per-run tmpfs helper.
set -euo pipefail
umask 077

if [[ $(id -u) != 0 ]]; then
  printf '%s\n' 'Root authorization is required' >&2
  exit 1
fi
if [[ $(id -u visky) != 1000 || $(id -g visky) != 1000 ]]; then
  printf '%s\n' 'Approved owner identity changed' >&2
  exit 1
fi

source_file=/home/visky/Work/GPTRunner/chat-dev-runner/scripts/cdr_volume_helper.py
install_path=/usr/local/libexec/chat-dev-runner-volume-helper
sudoers_path=/etc/sudoers.d/chat-dev-runner-volumes
expected_sha256=c1e7c8e749dde40f49b644830525dbcf931ed1f3397ce943ed5542e0233f6daa
previous_sha256=d78abf99a53c790a76e74438d0b4270148eb4ec26d73ba89cae3dd0abfc7c567
expected_sudoers='visky ALL=(root) NOPASSWD: /usr/local/libexec/chat-dev-runner-volume-helper'

# Stage owner-writable source into a root-owned directory, then hash the staged
# bytes. This closes the check/install race for a privileged executable.
staging=$(/usr/bin/mktemp -d /tmp/cdr-volume-install.XXXXXX)
install_stage=
cleanup() {
  if [[ -n "$install_stage" ]]; then /bin/rm -f -- "$install_stage"; fi
  /bin/rm -f -- "$staging/helper" "$staging/sudoers"
  /bin/rmdir -- "$staging"
}
trap cleanup EXIT
# The caller stops the runner service before this one-time upgrade.
# Check both before staging and just before the rename to narrow a late mount race.
ensure_no_legacy_mounts() {
  local target mount_targets
  mount_targets=$(/usr/bin/findmnt --kernel -rn -o TARGET -t tmpfs)
  while IFS= read -r target; do
    if [[ "$target" == /run/chat-dev-runner/volumes/* ]]; then
      printf '%s\n' 'Old run-volume mounts remain; retire them before helper upgrade' >&2
      exit 1
    fi
  done <<< "$mount_targets"
}
/usr/bin/install -m 0600 "$source_file" "$staging/helper"
if [[ $(sha256sum "$staging/helper" | cut -d' ' -f1) != "$expected_sha256" ]]; then
  printf '%s\n' 'Reviewed helper source changed' >&2
  exit 1
fi
/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec
if [[ -e "$install_path" || -L "$install_path" ]]; then
  if [[ -L "$install_path" || $(stat -c '%u:%g:%a:%h' "$install_path") != '0:0:755:1' ]]; then
    printf '%s\n' 'Existing root helper identity differs; refusing overwrite' >&2
    exit 1
  fi
  installed_sha256=$(sha256sum "$install_path" | cut -d' ' -f1)
  if [[ "$installed_sha256" != "$expected_sha256" && "$installed_sha256" != "$previous_sha256" ]]; then
    printf '%s\n' 'Existing root helper bytes differ; refusing overwrite' >&2
    exit 1
  fi
  if [[ "$installed_sha256" == "$previous_sha256" ]]; then
    # The old executable is the only approved way to retire its /run mounts.
    # Replacing it first would strand those mounts after a failed run.
    ensure_no_legacy_mounts
    install_stage=$(/usr/bin/mktemp /usr/local/libexec/.chat-dev-runner-volume-helper.XXXXXX)
    /usr/bin/install -o root -g root -m 0755 "$staging/helper" "$install_stage"
    if [[ $(sha256sum "$install_stage" | cut -d' ' -f1) != "$expected_sha256" ]]; then
      printf '%s\n' 'Staged helper bytes changed' >&2
      exit 1
    fi
    ensure_no_legacy_mounts
    /bin/mv -f -- "$install_stage" "$install_path"
    install_stage=
  fi
else
  /usr/bin/install -o root -g root -m 0755 "$staging/helper" "$install_path"
fi
if [[ $(stat -c '%u:%g:%a' "$install_path") != '0:0:755'
      || $(sha256sum "$install_path" | cut -d' ' -f1) != "$expected_sha256" ]]; then
  printf '%s\n' 'Root helper ownership or mode differs' >&2
  exit 1
fi

printf '%s\n' "$expected_sudoers" > "$staging/sudoers"
/bin/chmod 0440 "$staging/sudoers"
/usr/sbin/visudo -c -f "$staging/sudoers" >/dev/null
if [[ -e "$sudoers_path" ]]; then
  if [[ $(cat "$sudoers_path") != "$expected_sudoers" ]]; then
    printf '%s\n' 'Existing sudoers policy differs; refusing overwrite' >&2
    exit 1
  fi
else
  /usr/bin/install -o root -g root -m 0440 "$staging/sudoers" "$sudoers_path"
fi
/usr/sbin/visudo -c -f "$sudoers_path" >/dev/null
printf '%s\n' 'Root-owned per-run volume helper and scoped sudo rule installed'
