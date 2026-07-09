# P6-2C-1 Monitor Dry-Run Design

> Status: design locked
> Scope: short-window Telegram `NewMessage` dry-run design only
> Not in scope: production monitor, database ingest, parser, dedup, bot notification

## Phase Chain

| Phase | Goal | Status |
|-------|------|--------|
| P6-2C-1 | Listener dry-run design | Design locked |
| P6-2C-2 | Short-window real listener dry-run | Not started |
| P6-2D | Long-running monitor | Not started |

P6-2C can only answer:

> Can real `NewMessage` events from enabled channels be received by a handler
> within a controlled short window, converted into `IncomingMessage`, filtered
> by the watchlist, and reported as a desensitized dry-run result?

P6-2C cannot answer:

> The monitor is ready for stable long-term production operation.

## Allowed Flow

```text
watchlist.json
-> enabled source_channels
-> numeric channel ids
-> Telethon client connect
-> register exactly one NewMessage handler
-> receive real messages
-> convert to IncomingMessage
-> run watchlist filter
-> build desensitized dry-run report in memory
-> hit exit condition
-> remove handler
-> disconnect
-> exit
```

Only one handler should be registered:

```text
events.NewMessage(chats=resolved_channel_ids)
```

Do not register one handler per channel.

## Forbidden Flow

P6-2C-2 must not do any of the following:

- Database write
- `AsyncSession`
- `RawMessage` or `RawMessageService`
- Parser
- Normalizer
- Dedup
- Bot notification
- Media download
- Message forwarding
- Business state mutation
- Infinite loop
- `run_until_disconnected()`
- Background daemon process
- Long-term reconnect loop
- History backfill
- `iter_messages`
- `get_messages`
- Watchlist mutation
- Persistent resolved channel id cache
- Full message text persistence
- Telethon raw event persistence

## Module Boundaries

Do not put listener lifecycle logic into the existing resolver module.

Suggested boundaries:

| Module | Responsibility |
|--------|----------------|
| `resolver.py` | `source_ref -> numeric channel id` only |
| `telethon_resolver.py` | one-shot Telethon identity adapter only |
| `listener_dry_run.py` | short-window listener lifecycle only |
| `schema.py` | existing `IncomingMessage` DTO |
| `filter.py` | existing watchlist filter |

The listener must not depend on database services or downstream business
modules.

## Core Interfaces

```python
class IncomingMessageAdapter(Protocol):
    def from_telethon_event(self, event: object) -> IncomingMessage:
        ...
```

```python
async def run_monitor_dry_run(
    watchlist: WatchlistConfig,
    client: TelegramClientLike,
    *,
    resolved_channel_ids: tuple[int, ...],
    timeout_seconds: int,
    max_messages: int,
) -> MonitorDryRunReport:
    ...
```

`resolved_channel_ids` must use the same numeric id convention as the resolver
report. Current resolver output uses `-100...` peer ids; listener DTO source
identity and `events.NewMessage(chats=...)` filtering must stay consistent with
that convention.

## IncomingMessage Mapping

Prefer the existing `IncomingMessage` DTO instead of introducing a parallel DTO.

Current DTO fields:

```text
source_ref: str
source_message_id: str | int
text: str | null
caption: str | null
raw_payload: dict | null
published_at: datetime | null
```

P6-2C mapping rules:

| Telethon event field | IncomingMessage field |
|----------------------|-----------------------|
| peer/channel id | `source_ref` as numeric string |
| message id | `source_message_id` |
| message text | `text` |
| message caption if available | `caption` |
| raw event | do not store; keep `raw_payload=None` |
| message date | `published_at` |

P6-2C must not expand the DTO unless the watchlist filter requires it.

## Dry-Run Report

Top-level report:

```text
P6-2C_DRY_RUN_RESULT:
- watchlist_schema: pass/fail
- enabled_source_channels:
- resolved_channel_ids:
- handler_registered: yes/no
- listener_started: yes/no
- timeout_seconds:
- max_messages:
- exit_reason:
  - timeout
  - max_messages_reached
  - client_disconnected
  - setup_error
  - handler_error
  - interrupted
- implementation_pass: yes/no
- traffic_observed: yes/no
- events_received:
- dto_conversion_success_count:
- dto_conversion_error_count:
- handler_error_count:
- filter_pass_count:
- filter_reject_count:
- out_of_scope_channel_count:
- report_desensitized: yes/no
- telegram_api_accessed: yes
- database_accessed: no
- parser_called: no
- normalizer_called: no
- dedup_called: no
- notification_sent: no
- media_downloaded: no
- history_backfill_called: no
- raw_event_persisted: no
- handler_removed: yes/no
- client_disconnected_cleanly: yes/no
- long_running_process: no
- allow_P6_2D_design: yes/no
- blockers:
  - ...
- events:
  - ...
```

`traffic_observed=no` with `exit_reason=timeout` is not automatically an
implementation failure. It only means no real target-channel message arrived
inside the test window.

Per-event report:

```text
- channel_id_masked
- message_id_masked
- received_at
- has_text
- text_length
- text_hash_prefix
- has_media
- media_type
- filter_result
- filter_reason
- conversion_status
- error_code
```

Do not include:

- Full text
- Full username
- Full channel title
- Full sender id
- Full URL query
- Invite link
- Token-like string
- Raw event
- Raw payload

First implementation should avoid text previews. Use `text_length` and
`text_hash_prefix` only.

## Exit Conditions

P6-2C-2 must be finite.

Defaults:

```text
timeout_seconds = 60
max_messages = 10
```

Either condition exits the dry-run:

- Timeout reached
- `events_received >= max_messages`

Implementation shape:

```python
try:
    await client.connect()
    handler = client.add_event_handler(...)
    await asyncio.wait_for(done_event.wait(), timeout=timeout_seconds)
finally:
    remove handler
    disconnect
```

`finally` cleanup is mandatory even on conversion errors, handler errors, client
errors, timeout, or interruption.

Use a `closing` flag or `asyncio.Event` so events delivered after the exit
condition do not mutate the final report or race with cleanup.

## Handler Rules

Handler should do only lightweight in-memory work:

```text
receive event
-> verify channel id
-> convert to IncomingMessage
-> call filter_message
-> append desensitized report item
-> update counters
-> set done_event if max_messages reached
```

Handler must not:

- Make network requests
- Access database
- Download media
- Retry or sleep
- Send messages
- Call Parser
- Call Dedup

Per-event exceptions must be caught and counted:

```text
dto_conversion_error_count
handler_error_count
error_code
```

A single malformed event must not crash the whole dry-run unless it is a
client-level failure.

## Test Matrix

P6-2C-1 locks these expected tests for P6-2C-2 implementation:

| Scenario | Expected |
|----------|----------|
| No enabled channel | No handler registered; setup failure report |
| Empty numeric id list | Setup error |
| Handler registration succeeds | `handler_registered=yes` |
| Target-channel message arrives | DTO conversion attempted |
| Out-of-scope channel message arrives | Drop and increment out-of-scope count |
| DTO conversion fails | Record error; no DB write |
| Filter matched | `filter_pass_count + 1` |
| Filter rejected | `filter_reject_count + 1` |
| Max messages reached | `exit_reason=max_messages_reached` |
| No messages during timeout | `exit_reason=timeout`, `traffic_observed=no` |
| Client disconnects early | `exit_reason=client_disconnected` |
| Interrupted | Cleanup still happens |
| Handler error | Record error and cleanup |
| Report contains no full text | Pass |
| Report contains no username/title/raw event | Pass |
| DB/Parser/Dedup not called | Pass |
| History backfill not called | Pass |
| Handler removed | Pass |
| Client disconnected cleanly | Pass |
| Event arrives after exit condition | Not counted; no double-finalize race |

## P6-2D Design Gate

`allow_P6_2D_design=yes` requires:

- `watchlist_schema=pass`
- `implementation_pass=yes`
- `handler_registered=yes`
- `handler_removed=yes`
- `client_disconnected_cleanly=yes`
- `report_desensitized=yes`
- `database_accessed=no`
- `parser_called=no`
- `normalizer_called=no`
- `dedup_called=no`
- `notification_sent=no`
- `media_downloaded=no`
- `history_backfill_called=no`
- `raw_event_persisted=no`
- `long_running_process=no`

Traffic requirements are separate:

- `traffic_observed=yes` proves at least one real target-channel event arrived.
- `traffic_observed=no` with clean timeout does not by itself fail the
  implementation.

## Completion Criteria

P6-2C-1 is complete when:

- Short-window listener interface is fixed.
- Lifecycle and cleanup requirements are fixed.
- Exit conditions are fixed.
- Report structure is fixed.
- Desensitization rules are fixed.
- Forbidden call boundaries are fixed.
- Test matrix is fixed.

P6-2C-2 may then implement the short-window real listener dry-run. It still
must not claim long-running monitor readiness.
