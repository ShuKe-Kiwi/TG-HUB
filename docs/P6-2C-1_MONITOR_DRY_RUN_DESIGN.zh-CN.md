# P6-2C-1 Monitor Dry-Run 设计

[English](P6-2C-1_MONITOR_DRY_RUN_DESIGN.md)

> 状态：设计已锁定
> 范围：短时 Telegram `NewMessage` dry-run 设计
> 不包含：生产 monitor、数据库入库、Parser、Dedup、Bot 通知

## 阶段链路

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2C-1 | 监听 dry-run 设计 | 设计已锁定 |
| P6-2C-2 | 短时真实监听 dry-run | 未开始 |
| P6-2D | 长期运行 monitor | 未开始 |

P6-2C 只能回答：

> enabled channels 上的真实 `NewMessage` 事件，是否能在受控短时窗口内被 handler 接收，转换为 `IncomingMessage`，经过 watchlist filter，并输出脱敏 dry-run 报告？

P6-2C 不能回答：

> monitor 已经可以长期稳定生产运行。

## 允许链路

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

只允许注册一个 handler：

```text
events.NewMessage(chats=resolved_channel_ids)
```

不要每个频道单独注册 handler。

## 禁止链路

P6-2C-2 必须继续禁止：

- 写数据库
- 使用 `AsyncSession`
- 调用 `RawMessage` 或 `RawMessageService`
- Parser
- Normalizer
- Dedup
- Bot 通知
- 媒体下载
- 消息转发
- 业务状态修改
- 无限循环
- `run_until_disconnected()`
- 后台驻留进程
- 长期自动重连
- 历史补采
- `iter_messages`
- `get_messages`
- 修改 watchlist
- 持久化 resolved channel id cache
- 保存完整 message text
- 保存 Telethon raw event

## 模块边界

不要把监听生命周期逻辑写进既有 resolver 模块。

建议边界：

| 模块 | 职责 |
|------|------|
| `resolver.py` | 只负责 `source_ref -> numeric channel id` |
| `telethon_resolver.py` | 只负责一次性 Telethon 身份解析 adapter |
| `listener_dry_run.py` | 只负责短时监听生命周期 |
| `schema.py` | 复用既有 `IncomingMessage` DTO |
| `filter.py` | 复用既有 watchlist filter |

listener 不得依赖数据库 service 或下游业务模块。

## 核心接口

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

`resolved_channel_ids` 必须和 resolver report 使用同一种 numeric id 规范。当前 resolver 输出 `-100...` peer id；listener DTO 的来源身份和 `events.NewMessage(chats=...)` 过滤必须保持一致。

## IncomingMessage 映射

优先复用现有 `IncomingMessage` DTO，不新增平行 DTO。

当前 DTO 字段：

```text
source_ref: str
source_message_id: str | int
text: str | null
caption: str | null
raw_payload: dict | null
published_at: datetime | null
```

P6-2C 映射规则：

| Telethon event 字段 | IncomingMessage 字段 |
|---------------------|-----------------------|
| peer/channel id | `source_ref`，使用 numeric string |
| message id | `source_message_id` |
| message text | `text` |
| message caption，如果存在 | `caption` |
| raw event | 不保存，保持 `raw_payload=None` |
| message date | `published_at` |

除非 watchlist filter 真正需要，否则 P6-2C 不扩展 DTO。

## Dry-Run 报告

顶层报告：

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

`traffic_observed=no` 且 `exit_reason=timeout` 不自动表示实现失败，只表示测试窗口内没有真实目标频道消息到达。

单条 event 报告：

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

禁止输出：

- 完整正文
- 完整 username
- 完整 channel title
- 完整 sender id
- 完整 URL query
- invite link
- token-like 字符串
- raw event
- raw payload

第一版实现应避免 text preview，只输出 `text_length` 和 `text_hash_prefix`。

## 退出条件

P6-2C-2 必须是有限运行。

默认值：

```text
timeout_seconds = 60
max_messages = 10
```

任一条件满足即退出 dry-run：

- 到达 timeout
- `events_received >= max_messages`

实现形态：

```python
try:
    await client.connect()
    handler = client.add_event_handler(...)
    await asyncio.wait_for(done_event.wait(), timeout=timeout_seconds)
finally:
    remove handler
    disconnect
```

即使出现转换错误、handler 错误、client 错误、timeout 或 interruption，也必须在 `finally` 中清理。

需要使用 `closing` 标志或 `asyncio.Event`，防止退出条件触发后的 event 继续修改最终报告，或与清理逻辑产生竞态。

## Handler 规则

handler 只做轻量内存工作：

```text
receive event
-> verify channel id
-> convert to IncomingMessage
-> call filter_message
-> append desensitized report item
-> update counters
-> set done_event if max_messages reached
```

handler 禁止：

- 发起网络请求
- 访问数据库
- 下载媒体
- 重试或 sleep
- 发送消息
- 调用 Parser
- 调用 Dedup

单条 event 异常必须捕获并计数：

```text
dto_conversion_error_count
handler_error_count
error_code
```

单条格式异常不能击穿整个 dry-run，除非是 client 级别故障。

## 测试矩阵

P6-2C-1 为 P6-2C-2 实现锁定以下测试预期：

| 场景 | 预期 |
|------|------|
| 无 enabled channel | 不注册 handler，返回 setup failure report |
| numeric id list 为空 | setup error |
| handler 注册成功 | `handler_registered=yes` |
| 目标频道消息到达 | 尝试 DTO 转换 |
| 非目标频道消息到达 | 丢弃并增加 out-of-scope count |
| DTO 转换失败 | 记录错误，不写 DB |
| Filter matched | `filter_pass_count + 1` |
| Filter rejected | `filter_reject_count + 1` |
| 达到 max messages | `exit_reason=max_messages_reached` |
| timeout 内无消息 | `exit_reason=timeout`，`traffic_observed=no` |
| client 提前断开 | `exit_reason=client_disconnected` |
| interrupted | 仍然执行清理 |
| handler error | 记录错误并清理 |
| 报告不包含完整正文 | pass |
| 报告不包含 username/title/raw event | pass |
| DB/Parser/Dedup 未调用 | pass |
| history backfill 未调用 | pass |
| handler 已移除 | pass |
| client 已正常 disconnect | pass |
| 退出条件触发后又有 event 到达 | 不再计数，不重复 finalize |

## P6-2D 设计准入门槛

`allow_P6_2D_design=yes` 要求：

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

流量观察单独判断：

- `traffic_observed=yes` 表示至少有一个真实目标频道 event 到达。
- `traffic_observed=no` 且 clean timeout，本身不代表实现失败。

## 完成标准

P6-2C-1 完成条件：

- 短时监听接口已固定。
- 生命周期和清理要求已固定。
- 退出条件已固定。
- 报告结构已固定。
- 脱敏规则已固定。
- 禁止调用边界已固定。
- 测试矩阵已固定。

之后才允许 P6-2C-2 实现短时真实监听 dry-run。它仍然不能宣称长期 monitor ready。
