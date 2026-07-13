#!/bin/sh
set -eu

LABEL="com.tghub.rotate-logs"
DOMAIN="gui/$UID/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  if ! launchctl bootout "$DOMAIN" >/dev/null 2>&1; then
    printf '%s\n' '{"status":"fail","error_code":"ROTATION_AGENT_BOOTOUT_FAILED"}'
    exit 10
  fi
fi

if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  printf '%s\n' '{"status":"fail","error_code":"ROTATION_AGENT_UNINSTALL_VERIFICATION_FAILED"}'
  exit 11
fi

rm -f "$PLIST"
printf '%s\n' '{"status":"pass","action":"uninstall_rotation"}'
