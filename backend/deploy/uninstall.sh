#!/bin/sh
set -eu
PLIST="$HOME/Library/LaunchAgents/com.tghub.service.plist"
launchctl bootout "gui/$UID/com.tghub.service" >/dev/null 2>&1 || true
rm -f "$PLIST"
printf '%s\n' '{"status":"pass","action":"uninstall"}'
