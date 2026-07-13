#!/bin/sh
set -eu

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKEND=$(CDPATH= cd -- "$HERE/.." && pwd)
PYTHON="$BACKEND/.venv/bin/python"
ENV_FILE=${TG_HUB_ENV_FILE:-"$HOME/.tg-hub/production.env"}
LABEL="com.tghub.rotate-logs"
DOMAIN="gui/$UID/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$HERE/$LABEL.plist.template"
TMP="$PLIST.tmp.$$"
CREATED=0

fail() {
  printf '%s\n' "{\"status\":\"fail\",\"error_code\":\"$1\"}"
  exit "${2:-10}"
}

validate() {
  [ -x "$PYTHON" ] || fail "ROTATION_PYTHON_MISSING" 11
  [ -f "$ENV_FILE" ] || fail "ROTATION_ENV_FILE_MISSING" 12
  [ -r "$ENV_FILE" ] || fail "ROTATION_ENV_FILE_UNREADABLE" 12
  (
    cd "$BACKEND"
    "$PYTHON" -c 'import app.deploy.rotate_logs'
  ) >/dev/null 2>&1 || fail "ROTATION_MODULE_UNAVAILABLE" 13
  plutil -lint "$TEMPLATE" >/dev/null || fail "ROTATION_PLIST_INVALID" 14
}

rollback() {
  if [ "$CREATED" -ne 1 ]; then
    return 0
  fi
  if launchctl print "$DOMAIN" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN" >/dev/null 2>&1 || return 1
  fi
  launchctl print "$DOMAIN" >/dev/null 2>&1 && return 1
  rm -f "$PLIST"
}

validate

if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s\n' '{"status":"pass","action":"install_rotation","dry_run":true}'
  exit 0
fi

if [ -e "$PLIST" ] || [ -L "$PLIST" ] || launchctl print "$DOMAIN" >/dev/null 2>&1; then
  fail "ROTATION_AGENT_ALREADY_INSTALLED" 10
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed \
  -e "s|__PYTHON__|$PYTHON|g" \
  -e "s|__BACKEND_DIR__|$BACKEND|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  "$TEMPLATE" > "$TMP"
plutil -lint "$TMP" >/dev/null || {
  rm -f "$TMP"
  fail "ROTATION_PLIST_INVALID" 14
}
chmod 600 "$TMP"
mv "$TMP" "$PLIST"
CREATED=1

if ! launchctl bootstrap "gui/$UID" "$PLIST" >/dev/null 2>&1; then
  if rollback; then
    fail "ROTATION_AGENT_BOOTSTRAP_FAILED" 15
  fi
  fail "ROTATION_AGENT_ROLLBACK_FAILED" 16
fi

if ! launchctl print "$DOMAIN" >/dev/null 2>&1; then
  if rollback; then
    fail "ROTATION_AGENT_VERIFICATION_FAILED" 15
  fi
  fail "ROTATION_AGENT_ROLLBACK_FAILED" 16
fi

printf '%s\n' '{"status":"pass","action":"install_rotation"}'
