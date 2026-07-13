#!/bin/sh
set -eu
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKEND=$(CDPATH= cd -- "$HERE/.." && pwd)
ENV_FILE=${TG_HUB_ENV_FILE:-"$HOME/.tg-hub/production.env"}
PLIST="$HOME/Library/LaunchAgents/com.tghub.service.plist"
LOG_DIR="$HOME/.tg-hub/logs"
TMP="$PLIST.tmp"
if [ "$DRY_RUN" -eq 1 ]; then
  plutil -lint "$HERE/com.tghub.service.plist.template" >/dev/null
  printf '%s\n' '{"status":"pass","action":"install","dry_run":true}'
  exit 0
fi
if launchctl print "gui/$UID/com.tghub.service" >/dev/null 2>&1 || [ -e "$PLIST" ]; then
  printf '%s\n' '{"status":"fail","error_code":"SERVICE_ALREADY_INSTALLED"}'
  exit 10
fi
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
chmod 700 "$LOG_DIR"
sed -e "s|__START_SCRIPT__|$HERE/start.sh|g" -e "s|__BACKEND_DIR__|$BACKEND|g" -e "s|__ENV_FILE__|$ENV_FILE|g" -e "s|__LOG_DIR__|$LOG_DIR|g" "$HERE/com.tghub.service.plist.template" > "$TMP"
plutil -lint "$TMP" >/dev/null
chmod 600 "$TMP"
mv "$TMP" "$PLIST"
launchctl bootstrap "gui/$UID" "$PLIST"
printf '%s\n' '{"status":"pass","action":"install"}'
