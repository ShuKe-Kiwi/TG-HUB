# tg-hub

[中文版本](README.zh-CN.md)

Telegram resource aggregation backend.

`tg-hub` ingests Telegram resource messages, parses and normalizes resource
metadata, deduplicates resources, exposes query/bot-facing read models, and is
currently adding a staged Telegram monitor.

## Current Status

The project is implemented in phase gates. Recent monitor work is intentionally
split so dry-run validation does not silently become a production monitor.

| Area | Status |
|------|--------|
| P1-P5 backend pipeline | Implemented |
| Parser / Normalizer / Dedup | Implemented |
| Telegram bot query/notification adapter | Implemented |
| P6-2B watchlist filter boundary | Implemented |
| P6-2C-0 source channel precheck | Implemented |
| P6-2C-0B controlled channel resolution | Implemented |
| P6-2C-1 listener dry-run design | Locked |
| P6-2C-2 short-window listener dry-run | Implemented |
| P6-2D long-running monitor | Design locked, not implemented |

See:

- [Architecture](docs/ARCHITECTURE.md)
- [Implementation status](docs/IMPLEMENTATION_STATUS.md)
- [P6-2C-1 monitor dry-run design](docs/P6-2C-1_MONITOR_DRY_RUN_DESIGN.md)
- [P6-2D monitor runtime design (ZH)](docs/P6-2D_MONITOR_RUNTIME_DESIGN.zh-CN.md)

## Repository Layout

```text
backend/
  app/
    modules/
      monitor/       # watchlist, source resolution, dry-run listener
      parser/        # resource parser pipeline
      normalizer/    # normalized resource model and fingerprints
      resource/      # registry, query service, dedup service
      rawmessage/    # raw Telegram message storage service
      bot/           # Telegram bot query/notification adapter
    infra/           # event bus and infrastructure helpers
  tests/
docs/
```

## Setup

Use the backend virtual environment from the `backend` directory.

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pip install -r requirements.txt
```

If the virtual environment does not exist:

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Configuration

Backend config is loaded from environment variables and `backend/.env`.

Core app/database variables:

```text
DATABASE_URL
TEST_DATABASE_URL
APP_NAME
APP_ENV
APP_HOST
APP_PORT
LOG_LEVEL
```

Telegram bot variables:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
TELEGRAM_ALLOWED_CHAT_IDS
TELEGRAM_NOTIFY_CHAT_IDS
```

Telegram monitor variables:

```text
WATCHLIST_PATH=~/.tg-hub/watchlist.json
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_NAME=~/.tg-hub/telethon
```

Monitor runtime files:

```text
~/.tg-hub/watchlist.json       # runtime watchlist config
~/.tg-hub/p6_2b_samples.json   # offline P6-2B acceptance samples
```

`watchlist.json` owns runtime configuration:

```text
source_channels
watch_titles
```

`p6_2b_samples.json` owns offline acceptance samples only.

## Tests

Run monitor tests:

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pytest tests/monitor
```

Run the current non-database regression set:

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pytest tests/bot/test_bot_p5d.py tests/infra/test_eventbus.py tests/monitor tests/normalizer tests/parser
```

Database-oriented tests require a configured PostgreSQL test database and are
not part of the monitor dry-run validation loop.

## Monitor Phase Boundaries

### P6-2B

Validates only:

```text
IncomingMessage -> content_text selection -> watchlist title filter
```

It does not validate Telegram collection, parser, provider detection, database
ingest, dedup, or notification.

### P6-2C-0 / P6-2C-0B

Validates source channel references and one-shot channel identity resolution:

```text
source_channels -> numeric channel ids
```

It does not register `NewMessage` handlers or listen for messages.

### P6-2C-2

Validates a short-window listener dry-run:

```text
resolved channel ids
-> one NewMessage handler
-> IncomingMessage
-> watchlist filter
-> desensitized report
-> cleanup and exit
```

It explicitly does not:

- write database rows
- call `RawMessageService`
- run Parser / Normalizer / Dedup
- send Bot notifications
- download media
- backfill history
- run indefinitely

### P6-2D

P6-2D is currently a design-locked production runtime contract for:

- reconnect
- heartbeat
- error recovery
- observability
- graceful shutdown
- production lifecycle

The long-running monitor implementation has not started.

## Useful Verification Commands

Runtime preflight:

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -c "from app.modules.monitor.runtime_preflight import run_runtime_preflight; import json; print(json.dumps(run_runtime_preflight().model_dump(), ensure_ascii=False, indent=2))"
```

Telethon dependency check:

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -c "import telethon; print(telethon.__version__)"
```

Session directory permission check:

```bash
test -w /Users/kiwishook/.tg-hub && echo writable
```

## Git Hygiene

`backend/uv.lock` is currently untracked in this workspace. Do not include it in
phase commits unless the phase explicitly approves lockfile changes.
