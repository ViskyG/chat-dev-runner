#!/usr/bin/env bash
# One-time, owner-authorized EP-03 host setup. Run only through a local admin prompt.
set -euo pipefail
umask 077

if [[ $(id -u) != 0 ]]; then
  printf '%s\n' 'EP-03 admin batch requires root' >&2
  exit 1
fi
if [[ $(id -u visky) != 1000 || $(id -g visky) != 1000 ]]; then
  printf '%s\n' 'EP-03 owner identity differs from the audited host' >&2
  exit 1
fi

pool_file=/home/visky/Work/GPTRunner/chat-dev-runner/verification/implementation/EP-03/toy-volume-pool.tsv
volume_base=/home/visky/.local/share/chat-dev-runner/volumes
delegate_file=/etc/systemd/system/user@.service.d/90-chat-dev-runner-cpu.conf
expected_delegate=$'[Service]\nDelegate=cpu memory pids\n'

# Validate the entire untrusted, owner-writable plan before any root mutation.
/usr/bin/python3 - "$pool_file" <<'PY'
import pathlib, re, sys, uuid
path=pathlib.Path(sys.argv[1])
rows=[]
for line in path.read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    fields=line.split('\t')
    if len(fields)!=2 or not all(re.fullmatch(r'[0-9a-f-]{36}', value) for value in fields):
        raise SystemExit('invalid toy volume plan row')
    request, preparation=fields
    if str(uuid.UUID(request))!=request or str(uuid.UUID(preparation))!=preparation:
        raise SystemExit('noncanonical toy volume UUID')
    if str(uuid.uuid5(uuid.UUID(request), 'preparation'))!=preparation:
        raise SystemExit('preparation UUID does not match request')
    rows.append((request,preparation))
if len(rows)!=12 or len({item for row in rows for item in row})!=24:
    raise SystemExit('toy volume plan count or uniqueness changed')
PY
if [[ -e "$delegate_file" ]] && [[ $(cat "$delegate_file") != "${expected_delegate%$'\n'}" ]]; then
  printf '%s\n' 'Existing EP-03 delegation override differs; refusing overwrite' >&2
  exit 1
fi

printf '%s\n' 'EP-03: installing the one missing uidmap package'
DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -qq install -y --no-install-recommends uidmap
command -v newuidmap >/dev/null
command -v newgidmap >/dev/null

printf '%s\n' 'EP-03: setting persistent CPU/memory/pids delegation without restarting the user manager'
/usr/bin/install -d -m 0755 /etc/systemd/system/user@.service.d
printf '%s' "$expected_delegate" > "$delegate_file"
/bin/chmod 0644 "$delegate_file"
/bin/systemctl daemon-reload
if /bin/systemctl set-property --runtime user@1000.service 'Delegate=cpu memory pids'; then
  printf '%s\n' 'EP-03: live delegation update was accepted; active controllers still require verification'
else
  printf '%s\n' 'EP-03: live delegation update was unavailable; persistent change awaits a later user-manager restart'
fi
/usr/bin/loginctl enable-linger visky

printf '%s\n' 'EP-03: mounting disposable, separate 64 MiB phase volumes'
/usr/bin/install -d -o visky -g visky -m 0700 "$volume_base"
while IFS=$'\t' read -r request_id preparation_id; do
  [[ -z "${request_id:-}" || "$request_id" == \#* ]] && continue
  for phase_id in "$preparation_id" "$request_id"; do
    phase_path="$volume_base/$phase_id"
    if [[ -e "$phase_path" ]]; then
      printf 'Existing toy volume path: %s\n' "$phase_path" >&2
      exit 1
    fi
    /usr/bin/install -d -o visky -g visky -m 0700 "$phase_path"
    /usr/bin/mount -t tmpfs -o size=64m,nr_inodes=16384,uid=1000,gid=1000,mode=0700,nosuid,nodev cdr-ep03-toy "$phase_path"
  done
done < "$pool_file"

printf '%s\n' 'EP-03: uidmap, delegation settings, linger and 24 toy mounts prepared'
