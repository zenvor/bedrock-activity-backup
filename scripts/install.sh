#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: sudo scripts/install.sh --config PATH [--restart]

Installs a versioned local release and systemd integration. Without --restart,
the running BDS process is not changed and the new watcher is not started.
EOF
  exit 2
}

config_source=""
restart=0
while (($#)); do
  case "$1" in
    --config)
      (($# >= 2)) || usage
      config_source="$2"
      shift 2
      ;;
    --restart)
      restart=1
      shift
      ;;
    *) usage ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run this installer as root" >&2; exit 1; }
[[ -n "$config_source" && -f "$config_source" ]] || usage
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'
rsync --help | grep -q -- '--link-dest'
rsync --help | grep -q -- '--chown'
rsync --help | grep -q -- '--fsync'

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
install_root="/opt/bedrock-activity-backup"
release="$install_root/releases/$timestamp"
backup="$install_root/install-backups/$timestamp"
current_link="$install_root/current"
bds_was_active=0
watcher_was_enabled=0
watcher_was_active=0
restart_attempted=0

if systemctl is-active --quiet minecraft-bedrock.service; then
  bds_was_active=1
fi
if systemctl is-enabled --quiet bedrock-activity-backup.service 2>/dev/null; then
  watcher_was_enabled=1
fi
if systemctl is-active --quiet bedrock-activity-backup.service 2>/dev/null; then
  watcher_was_active=1
fi

targets=(
  /usr/local/sbin/bedrock-activity-backup
  /usr/local/sbin/minecraft-bedrock-run
  /etc/bedrock-activity-backup/config.json
  /etc/systemd/system/bedrock-activity-backup.service
  /etc/systemd/system/minecraft-bedrock.service.d/10-activity-backup-console.conf
)
backup_names=(
  bedrock-activity-backup
  minecraft-bedrock-run
  config.json
  bedrock-activity-backup.service
  10-activity-backup-console.conf
)

install -d -m 0750 -o root -g root "$release" "$backup"
install -d -m 0755 -o root -g root "$release/src"
rsync -a --delete --exclude='__pycache__/' "$project_root/src/" "$release/src/"
chown -R root:root "$release"
find "$release" -type d -exec chmod 0755 {} +
find "$release" -type f -exec chmod 0644 {} +

for index in "${!targets[@]}"; do
  target="${targets[$index]}"
  backup_name="${backup_names[$index]}"
  if [[ -e "$target" || -L "$target" ]]; then
    cp -a "$target" "$backup/$backup_name"
  else
    : >"$backup/.absent-$backup_name"
  fi
done

if [[ -L "$current_link" ]]; then
  readlink "$current_link" >"$backup/current-link-target"
elif [[ -e "$current_link" ]]; then
  echo "Refusing to replace a non-symlink current path" >&2
  exit 1
else
  : >"$backup/.absent-current-link"
fi

rollback() {
  status=$?
  trap - ERR
  rollback_ok=1
  echo "Installation failed; restoring the previous integration files" >&2
  if ((restart_attempted == 1)); then
    if ! systemctl stop bedrock-activity-backup.service; then rollback_ok=0; fi
    if ! systemctl stop minecraft-bedrock.service; then rollback_ok=0; fi
  fi
  for index in "${!targets[@]}"; do
    target="${targets[$index]}"
    backup_name="${backup_names[$index]}"
    if [[ -e "$backup/$backup_name" || -L "$backup/$backup_name" ]]; then
      if ! rm -f -- "$target"; then rollback_ok=0; fi
      if ! cp -a "$backup/$backup_name" "$target"; then rollback_ok=0; fi
    elif [[ -f "$backup/.absent-$backup_name" ]]; then
      if ! rm -f -- "$target"; then rollback_ok=0; fi
    fi
  done
  if [[ -f "$backup/current-link-target" ]]; then
    previous_current="$(<"$backup/current-link-target")"
    if ! ln -sfn "$previous_current" "$current_link"; then rollback_ok=0; fi
  elif [[ -f "$backup/.absent-current-link" ]]; then
    if ! rm -f -- "$current_link"; then rollback_ok=0; fi
  fi
  if ! systemctl daemon-reload; then rollback_ok=0; fi
  if ((watcher_was_enabled == 1)); then
    if ! systemctl enable bedrock-activity-backup.service; then rollback_ok=0; fi
  else
    systemctl disable bedrock-activity-backup.service >/dev/null 2>&1 || :
  fi
  if ((bds_was_active == 1)); then
    if ! systemctl start minecraft-bedrock.service; then rollback_ok=0; fi
  elif systemctl is-active --quiet minecraft-bedrock.service; then
    if ! systemctl stop minecraft-bedrock.service; then rollback_ok=0; fi
  fi
  if ((watcher_was_active == 1)); then
    if ! systemctl start bedrock-activity-backup.service; then rollback_ok=0; fi
  elif systemctl is-active --quiet bedrock-activity-backup.service; then
    if ! systemctl stop bedrock-activity-backup.service; then rollback_ok=0; fi
  fi
  actual_bds_active=0
  actual_watcher_active=0
  actual_watcher_enabled=0
  if systemctl is-active --quiet minecraft-bedrock.service; then actual_bds_active=1; fi
  if systemctl is-active --quiet bedrock-activity-backup.service; then actual_watcher_active=1; fi
  if systemctl is-enabled --quiet bedrock-activity-backup.service 2>/dev/null; then actual_watcher_enabled=1; fi
  if ((
    actual_bds_active != bds_was_active
    || actual_watcher_active != watcher_was_active
    || actual_watcher_enabled != watcher_was_enabled
  )); then
    rollback_ok=0
  fi
  if ((rollback_ok == 0)); then
    echo "Rollback verification failed; manual service recovery is required" >&2
  fi
  exit "$status"
}
trap rollback ERR

install -d -m 0750 -o root -g root /etc/bedrock-activity-backup
install -d -m 0700 -o root -g root /var/lib/bedrock-activity-backup
install -d -m 0700 -o root -g root /var/lib/bedrock-activity-backup/rehearsals
install -d -m 0755 -o root -g root /etc/systemd/system/minecraft-bedrock.service.d
install -m 0755 -o root -g root "$project_root/bin/bedrock-activity-backup" /usr/local/sbin/bedrock-activity-backup
install -m 0755 -o root -g root "$project_root/scripts/minecraft-bedrock-run" /usr/local/sbin/minecraft-bedrock-run
install -m 0640 -o root -g root "$config_source" /etc/bedrock-activity-backup/config.json
install -m 0644 -o root -g root "$project_root/systemd/bedrock-activity-backup.service" /etc/systemd/system/bedrock-activity-backup.service
install -m 0644 -o root -g root "$project_root/systemd/minecraft-bedrock.service.d/10-activity-backup-console.conf" /etc/systemd/system/minecraft-bedrock.service.d/10-activity-backup-console.conf

ln -s "$release" "$install_root/.current-$timestamp"
mv -Tf "$install_root/.current-$timestamp" "$current_link"

/usr/bin/python3 -m compileall -q "$release/src"
/usr/local/sbin/bedrock-activity-backup --config /etc/bedrock-activity-backup/config.json status >/dev/null
systemctl daemon-reload
systemd-analyze verify minecraft-bedrock.service bedrock-activity-backup.service
systemctl enable bedrock-activity-backup.service

if ((restart == 1)); then
  restart_attempted=1
  restart_cursor="$(journalctl -u minecraft-bedrock.service -n 0 --show-cursor --no-pager \
    | sed -n 's/^-- cursor: //p' | tail -n 1)"
  [[ -n "$restart_cursor" ]]
  systemctl restart minecraft-bedrock.service
  server_started=0
  for _ in $(seq 1 60); do
    if journalctl -u minecraft-bedrock.service --after-cursor "$restart_cursor" --no-pager \
        | grep -Fq "Server started."; then
      server_started=1
      break
    fi
    sleep 1
  done
  ((server_started == 1))
  [[ "$(systemctl is-active minecraft-bedrock.service)" == "active" ]]
  [[ "$(systemctl is-active bedrock-activity-backup.service)" == "active" ]]
  [[ -p /run/minecraft-bedrock/console ]]
fi

trap - ERR

echo "Installed release: $timestamp"
echo "Previous files: $backup"
if ((restart == 0)); then
  echo "BDS was not restarted; activate later with a controlled service restart"
fi
