#!/usr/bin/env bash
# Fresh installation only; upgrades are refused before modifying installed files.
set -Eeuo pipefail
umask 077
if [ "$#" -ne 5 ] || [ "$(id -u)" -ne 0 ]; then
  echo 'Usage (root): install.sh APP_SOURCE PRIVATE_SYNC_DIR CREDENTIAL_DIR DOMAIN SSH_REPO' >&2
  exit 2
fi
APP_SOURCE=$(realpath "$1")
SYNC_SOURCE=$(realpath "$2")
CREDENTIAL_DIR=$(realpath "$3")
DOMAIN=$4
VAULT_REPO=$5
[[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] && [[ "$VAULT_REPO" =~ ^git@github.com:[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git$ ]] || exit 2
test -f "$APP_SOURCE/frontend/dist/index.html"
test -f "$APP_SOURCE/requirements.lock"
test -f "$SYNC_SOURCE/brain_sync.py"
test -f "$SYNC_SOURCE/brain_policy.py"
test -f "$CREDENTIAL_DIR/github"
test -f "$CREDENTIAL_DIR/known_hosts"
for command in uv docker python3 flock caddy systemctl runuser visudo curl; do
  command -v "$command" >/dev/null
done
exec 8>/run/lock/mwsb-install.lock
flock -n -x 8 || { echo 'Another installer is running.' >&2; exit 1; }
for existing in /opt/mwsecondbrain/current /etc/mwsecondbrain/environment \
  /etc/mwsecondbrain/github /etc/mwsecondbrain/known_hosts \
  /usr/local/libexec/mwsb-editor /etc/sudoers.d/mwsecondbrain \
  /etc/systemd/system/mwsecondbrain.service \
  /opt/mwsecondbrain/private-sync/brain_sync.py /opt/mwsecondbrain/private-sync/brain_policy.py \
  /var/lib/mwsecondbrain/state/state.sqlite3; do
  if [ -e "$existing" ] || [ -L "$existing" ]; then
    echo 'Upgrades are not supported by this installer; existing installation was not modified.' >&2
    exit 1
  fi
done
if systemctl cat mwsecondbrain.service >/dev/null 2>&1; then
  echo 'Upgrades are not supported by this installer; existing service was not modified.' >&2
  exit 1
fi
docker info >/dev/null
if docker container inspect mwsb-obsidian >/dev/null 2>&1; then
  echo 'Upgrades are not supported by this installer; existing editor container was not modified.' >&2
  exit 1
fi
test -f /etc/caddy/Caddyfile
WORK=$(mktemp -d /tmp/mwsb-install.XXXXXX)
CADDY_CANDIDATE=$(mktemp /etc/caddy/.mwsb-candidate.XXXXXX)
FILES_TOUCHED=false
SERVICE_STARTED=false
SERVICE_ENABLED=false
CONTAINER_CREATED=false
CADDY_CHANGED=false
HELPER_LOCKED=false
COMMITTED=false
SNAPSHOT_FILES=(
  /etc/mwsecondbrain/environment /etc/mwsecondbrain/github /etc/mwsecondbrain/known_hosts
  /opt/mwsecondbrain/private-sync/brain_sync.py /opt/mwsecondbrain/private-sync/brain_policy.py
  /usr/local/libexec/mwsb-editor /etc/sudoers.d/mwsecondbrain
  /etc/systemd/system/mwsecondbrain.service /opt/mwsecondbrain/current /etc/caddy/Caddyfile
  /var/lib/mwsecondbrain/obsidian/.config/obsidian/obsidian.json
)
mkdir -p "$WORK/files"
for index in "${!SNAPSHOT_FILES[@]}"; do
  path=${SNAPSHOT_FILES[$index]}
  if [ -e "$path" ] || [ -L "$path" ]; then cp -a -- "$path" "$WORK/files/$index"; fi
done
rollback() {
  result=$?
  trap - EXIT INT TERM
  set +e
  if ! "$COMMITTED" && "$FILES_TOUCHED"; then
    echo 'Installation failed; restoring managed files. Vault data and staged release are retained.' >&2
    if "$SERVICE_STARTED"; then systemctl stop mwsecondbrain.service || echo 'WARNING: service stop failed.' >&2; fi
    if "$SERVICE_ENABLED"; then systemctl disable mwsecondbrain.service || echo 'WARNING: service disable failed.' >&2; fi
    if ! "$HELPER_LOCKED"; then
      exec 9>/run/lock/mwsb-editor.lock
      flock -x 9
      HELPER_LOCKED=true
    fi
    for index in "${!SNAPSHOT_FILES[@]}"; do
      path=${SNAPSHOT_FILES[$index]}
      if [ -e "$WORK/files/$index" ] || [ -L "$WORK/files/$index" ]; then
        if cp -a -- "$WORK/files/$index" "$path.rollback.$$"; then
          mv -Tf -- "$path.rollback.$$" "$path" || echo "WARNING: restore failed for $path" >&2
        else echo "WARNING: restore failed for $path" >&2; fi
      else rm -f -- "$path" || echo "WARNING: removal failed for $path" >&2; fi
    done
    systemctl daemon-reload || echo 'WARNING: service unit reload failed.' >&2
    if "$CADDY_CHANGED"; then systemctl reload caddy || echo 'WARNING: Caddy rollback reload failed.' >&2; fi
    if "$CONTAINER_CREATED"; then
      if [ "$(docker inspect --format '{{.State.Running}}' mwsb-obsidian 2>/dev/null)" = false ]; then
        docker rm mwsb-obsidian >/dev/null || echo 'WARNING: stopped editor cleanup failed.' >&2
      else echo 'WARNING: editor state uncertain; container retained without force.' >&2; fi
    fi
  fi
  rm -f -- "$CADDY_CANDIDATE" "/opt/mwsecondbrain/current.next.$$" "/etc/caddy/Caddyfile.next.$$"
  rm -rf -- "$WORK"
  exit "$result"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -a /etc/caddy/Caddyfile "$CADDY_CANDIDATE"
python3 - "$CADDY_CANDIDATE" "$APP_SOURCE/deploy/Caddyfile.example" "$DOMAIN" <<'PY'
from pathlib import Path
import re
import sys
candidate, template, domain = sys.argv[1:]
path = Path(candidate)
original = path.read_text()
begin = f"# BEGIN MWSecondBrain managed site {domain}"
end = f"# END MWSecondBrain managed site {domain}"
starts = list(re.finditer(r"(?m)^" + re.escape(begin) + r"$", original))
ends = list(re.finditer(r"(?m)^" + re.escape(end) + r"$", original))
if len(starts) != len(ends) or len(starts) > 1:
    raise SystemExit("Malformed managed Caddy site; configuration was not modified")
block = begin + "\n" + Path(template).read_text().replace("brain.example.com", domain).rstrip() + "\n" + end + "\n"
if starts:
    first = starts[0].start()
    last = ends[0].end()
    if last <= first: raise SystemExit("Malformed managed Caddy site; configuration was not modified")
    remaining = original[:first] + original[last:]
else:
    first = last = len(original)
    remaining = original
if re.search(r"(?<![A-Za-z0-9.-])" + re.escape(domain) + r"(?![A-Za-z0-9.-])", remaining):
    raise SystemExit("Domain already occurs outside the managed Caddy site; configuration was not modified")
if starts: path.write_text(original[:first] + block + original[last:])
else: path.write_text(original.rstrip() + "\n\n" + block)
PY
caddy validate --adapter caddyfile --config "$CADDY_CANDIDATE"
id mwsb >/dev/null 2>&1 || useradd --system --home-dir /var/lib/mwsecondbrain --shell /usr/sbin/nologin mwsb
MWSB_UID=$(id -u mwsb)
MWSB_GID=$(id -g mwsb)
export MWSB_UID MWSB_GID
install -d -m 755 /opt/mwsecondbrain /opt/mwsecondbrain/releases /opt/mwsecondbrain/private-sync /usr/local/libexec
install -d -m 750 -o root -g mwsb /etc/mwsecondbrain
install -d -m 700 -o mwsb -g mwsb /var/lib/mwsecondbrain /var/lib/mwsecondbrain/state /var/lib/mwsecondbrain/backups /var/lib/mwsecondbrain/obsidian
RELEASE=/opt/mwsecondbrain/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 755 "$RELEASE" "$RELEASE/frontend"
cp -a "$APP_SOURCE/src" "$APP_SOURCE/pyproject.toml" "$APP_SOURCE/requirements.lock" "$RELEASE/"
cp -a "$APP_SOURCE/frontend/dist" "$RELEASE/frontend/"
chmod -R a+rX "$RELEASE"
uv venv --cache-dir /tmp/mwsb-install-cache --python /usr/bin/python3 "$RELEASE/.venv"
uv pip install --cache-dir /tmp/mwsb-install-cache --python "$RELEASE/.venv/bin/python" --require-hashes -r "$RELEASE/requirements.lock"
uv pip install --cache-dir /tmp/mwsb-install-cache --python "$RELEASE/.venv/bin/python" --no-deps "$RELEASE"
chmod -R a+rX "$RELEASE"
runuser -u mwsb -- "$RELEASE/.venv/bin/python" -c 'import mwsecondbrain.app, uvicorn'
# Exclude every helper verb through stopped-state verification and replacement.
if systemctl cat mwsecondbrain.service >/dev/null 2>&1; then systemctl stop mwsecondbrain.service; fi
exec 9>/run/lock/mwsb-editor.lock
flock -x 9
HELPER_LOCKED=true
if docker container inspect mwsb-obsidian >/dev/null 2>&1; then
  echo 'Editor container appeared during preparation; installation refused.' >&2
  exit 1
fi
FILES_TOUCHED=true
install -m 640 -o root -g mwsb "$CREDENTIAL_DIR/github" /etc/mwsecondbrain/github
install -m 644 "$CREDENTIAL_DIR/known_hosts" /etc/mwsecondbrain/known_hosts
install -m 644 "$SYNC_SOURCE/brain_sync.py" "$SYNC_SOURCE/brain_policy.py" /opt/mwsecondbrain/private-sync/
sed "s|https://brain.example.com|https://$DOMAIN|" "$APP_SOURCE/deploy/environment.example" > /etc/mwsecondbrain/environment
chmod 600 /etc/mwsecondbrain/environment
set -a
source /etc/mwsecondbrain/environment
set +a
if [ ! -d /var/lib/mwsecondbrain/vault ]; then
  runuser -u mwsb -- env "GIT_SSH_COMMAND=$GIT_SSH_COMMAND" git clone --branch main "$VAULT_REPO" /var/lib/mwsecondbrain/vault
  runuser -u mwsb -- git -C /var/lib/mwsecondbrain/vault config user.name MWSecondBrain
  runuser -u mwsb -- git -C /var/lib/mwsecondbrain/vault config user.email mwsecondbrain@users.noreply.github.com
fi
runuser -u mwsb -- mkdir -p /var/lib/mwsecondbrain/obsidian/.config/obsidian
if [ ! -f /var/lib/mwsecondbrain/obsidian/.config/obsidian/obsidian.json ]; then
  printf '%s\n' '{"vaults":{"7365636f6e646272":{"path":"/vault","ts":1,"open":true}}}' > /var/lib/mwsecondbrain/obsidian/.config/obsidian/obsidian.json
  chown mwsb:mwsb /var/lib/mwsecondbrain/obsidian/.config/obsidian/obsidian.json
fi
install -m 755 "$APP_SOURCE/deploy/mwsb-editor" /usr/local/libexec/mwsb-editor
printf '%s\n' 'mwsb ALL=(root) NOPASSWD: /usr/local/libexec/mwsb-editor start, /usr/local/libexec/mwsb-editor stop, /usr/local/libexec/mwsb-editor status' > /etc/sudoers.d/mwsecondbrain
chmod 440 /etc/sudoers.d/mwsecondbrain
visudo -cf /etc/sudoers.d/mwsecondbrain
CONTAINER_CREATED=true
docker compose -p mwsecondbrain -f "$APP_SOURCE/deploy/compose.yaml" create
if [ "$(docker inspect --format '{{.State.Running}}' mwsb-obsidian)" != false ]; then
  echo 'Editor stopped state was not confirmed; installation refused.' >&2
  exit 1
fi
if [ ! -e /var/lib/mwsecondbrain/state/state.sqlite3 ]; then
  echo 'Password initialization is a separate local step; no default password is installed.'
fi
ln -s "$RELEASE" "/opt/mwsecondbrain/current.next.$$"
mv -Tf "/opt/mwsecondbrain/current.next.$$" /opt/mwsecondbrain/current
install -m 644 "$APP_SOURCE/deploy/mwsecondbrain.service" /etc/systemd/system/mwsecondbrain.service
systemctl daemon-reload
cp -a "$CADDY_CANDIDATE" "/etc/caddy/Caddyfile.next.$$"
mv -Tf "/etc/caddy/Caddyfile.next.$$" /etc/caddy/Caddyfile
CADDY_CHANGED=true
flock -u 9
HELPER_LOCKED=false
SERVICE_ENABLED=true
systemctl enable mwsecondbrain.service
SERVICE_STARTED=true
systemctl start mwsecondbrain.service
curl --fail --silent --retry 5 --retry-delay 1 --retry-connrefused http://127.0.0.1:8765/healthz >/dev/null
systemctl reload caddy
COMMITTED=true
echo "Release installed: $RELEASE"
