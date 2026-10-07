#!/usr/bin/env bash
# Promote application code on an existing installation; container/config are unchanged.
set -Eeuo pipefail
umask 077
test "$(id -u)" = 0
test "$#" = 1
SOURCE=$(realpath "$1")
test -f "$SOURCE/frontend/dist/index.html"
test -L /opt/mwsecondbrain/current
exec 8>/run/lock/mwsb-install.lock
flock -n -x 8
PREVIOUS=$(readlink -f /opt/mwsecondbrain/current)
RELEASE=/opt/mwsecondbrain/releases/$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 755 "$RELEASE/frontend"
cp -a "$SOURCE/src" "$SOURCE/pyproject.toml" "$SOURCE/requirements.lock" "$RELEASE/"
cp -a "$SOURCE/frontend/dist" "$RELEASE/frontend/"
if [ -d "$SOURCE/tools" ]; then cp -a "$SOURCE/tools" "$RELEASE/"; fi
uv venv --cache-dir /tmp/mwsb-install-cache --python /usr/bin/python3 "$RELEASE/.venv"
uv pip install --cache-dir /tmp/mwsb-install-cache --python "$RELEASE/.venv/bin/python" --require-hashes -r "$RELEASE/requirements.lock"
uv pip install --cache-dir /tmp/mwsb-install-cache --python "$RELEASE/.venv/bin/python" --no-deps "$RELEASE"
chmod -R a+rX "$RELEASE"
runuser -u mwsb -- "$RELEASE/.venv/bin/python" -c 'import mwsecondbrain.app, uvicorn'
test "$(docker inspect --format '{{.State.Running}}' mwsb-obsidian)" = false
SUCCESS=false
SWITCHED=false
HELPER_LOCKED=false
cleanup() {
  result=$?
  trap - EXIT INT TERM
  set +e
  if ! "$SUCCESS"; then
    systemctl stop mwsecondbrain
    if "$SWITCHED"; then
      ln -s "$PREVIOUS" "/opt/mwsecondbrain/current.rollback.$$"
      mv -Tf "/opt/mwsecondbrain/current.rollback.$$" /opt/mwsecondbrain/current
    fi
    if "$HELPER_LOCKED"; then flock -u 9; fi
    systemctl start mwsecondbrain
    echo 'Promotion failed; previous application release restored. Recovery backup retained.' >&2
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
systemctl stop mwsecondbrain
exec 9>/run/lock/mwsb-editor.lock
flock -x 9
HELPER_LOCKED=true
test "$(docker inspect --format '{{.State.Running}}' mwsb-obsidian)" = false
set -a
source /etc/mwsecondbrain/environment
set +a
# Use the tested new backup implementation before changing the active version.
# No editor or application process can mutate the sources during this snapshot.
runuser -u mwsb -- "$RELEASE/.venv/bin/python" -c 'from pathlib import Path; from mwsecondbrain.backup import backup; import os; backup(Path(os.environ["MWSB_VAULT_DIR"]),Path(os.environ["MWSB_STATE_DIR"]),Path(os.environ["MWSB_BACKUP_DIR"]))'
ln -s "$RELEASE" "/opt/mwsecondbrain/current.next.$$"
mv -Tf "/opt/mwsecondbrain/current.next.$$" /opt/mwsecondbrain/current
SWITCHED=true
flock -u 9
HELPER_LOCKED=false
systemctl start mwsecondbrain
curl --fail --silent --retry 5 --retry-delay 1 --retry-connrefused http://127.0.0.1:8765/healthz >/dev/null
SUCCESS=true
echo "Application release promoted: $RELEASE"
