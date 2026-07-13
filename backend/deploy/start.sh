#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKEND=$(CDPATH= cd -- "$HERE/.." && pwd)
exec "$BACKEND/../.venv/bin/python" -m app.deploy.runtime
