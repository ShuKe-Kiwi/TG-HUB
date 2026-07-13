#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKEND=$(CDPATH= cd -- "$HERE/.." && pwd)
PYTHON="$BACKEND/.venv/bin/python"
ENV_FILE=${TG_HUB_ENV_FILE:-"$HOME/.tg-hub/production.env"}
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

if ! (
  cd "$BACKEND"
  TG_HUB_ENV_FILE="$ENV_FILE" "$PYTHON" -m app.deploy.rotation_agent remove-install
) >/dev/null 2>&1; then
  printf '%s\n' '{"status":"fail","error_code":"ROTATION_AGENT_METADATA_REMOVE_FAILED"}'
  exit 12
fi

rm -f "$PLIST"
printf '%s\n' '{"status":"pass","action":"uninstall_rotation"}'
