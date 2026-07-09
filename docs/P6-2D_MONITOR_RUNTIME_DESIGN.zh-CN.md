# P6-2D Monitor Runtime 设计

> 状态：设计已锁定；第一版 runtime 生命周期已实现
> 范围：长期运行 Telegram monitor 的运行时契约
> 不包含：实现代码、数据库入库、Parser、Dedup、Bot 通知

## 阶段位置

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2C-1 | 监听 dry-run 设计 | 已完成 |
| P6-2C-2 | 短时真实监听 dry-run | 已完成 |
| P6-2D | 长期 monitor runtime 设计 | 设计已锁定 |
| P6-2D implementation | 长期 monitor runtime 生命周期代码实现 | 第一版已完成 |

P6-2D 设计只能回答：

> 长期运行的 monitor 应该如何启动、重连、上报心跳、从错误中恢复、暴露可观测性，并安全停机？

本文档本身不能回答：

> 长期运行的 monitor 已经具备生产入库能力，或已经接通 Parser / Dedup / Bot。

## 固定边界

P6-2D 把 P6-2C-2 的短时监听 dry-run 扩展为生产运行时契约，但 monitor 生命周期仍必须和下游业务处理分离。

允许的设计范围：

- 启动生命周期
- 运行时状态
- 重连策略
- 心跳策略
- 错误分类
- 健康与就绪语义
- metrics / log / event 形态
- 优雅停机
- 面向运维的报告
- 实现阶段测试矩阵

本设计阶段禁止：

- 创建长期 monitor 实现代码
- 注册真实长期运行 handler
- 写数据库
- 使用 `AsyncSession`
- 调用 `RawMessageService`
- 调用 Parser
- 调用 Normalizer
- 调用 Dedup
- 发送 Bot 通知
- 下载媒体
- 回溯历史消息
- 调用 `iter_messages`
- 调用 `get_messages`
- 转发消息
- 修改 watchlist
- 持久化 resolved id cache

## 运行时状态机

长期 monitor 必须建模为有限状态机。

```text
created
-> starting
-> preflight
-> resolving_channels
-> connecting
-> registering_handler
-> listening
-> draining
-> stopped
```

失败状态：

```text
degraded
failed
```

状态含义：

| 状态 | 含义 |
|------|------|
| `created` | Runtime 对象已创建，尚未开始 I/O |
| `starting` | 收到启动请求 |
| `preflight` | 本地配置、session、依赖检查 |
| `resolving_channels` | 将 `source_channels` 解析为 numeric id |
| `connecting` | Telethon client 正在连接 |
| `registering_handler` | 正在注册唯一的 `NewMessage` handler |
| `listening` | handler 已激活，runtime 正在接收事件 |
| `degraded` | runtime 仍存活，但部分能力受损 |
| `draining` | 收到停止请求，阻止新事件处理 |
| `stopped` | handler 已移除，client 已断开 |
| `failed` | 致命启动或运行错误，需要人工处理 |

禁止的状态捷径：

- `created -> listening`
- `failed -> listening`，除非完整重启
- `draining -> listening`
- `stopped -> listening`，除非重新执行启动序列

## 启动生命周期

启动顺序：

```text
load settings
-> load watchlist
-> runtime preflight
-> resolve enabled source_channels
-> create Telethon client
-> connect
-> register exactly one NewMessage handler
-> start heartbeat loop
-> enter listening
```

启动失败策略：

| 失败 | 分类 | 动作 |
|------|------|------|
| watchlist 不可读 | 致命 | `failed`，不注册 handler |
| watchlist schema 非法 | 致命 | `failed`，不注册 handler |
| 没有 enabled channel | 致命 | `failed`，不注册 handler |
| 部分频道解析失败 | P6-2D 第一版视为致命 | 不注册 handler |
| 缺少 Telethon 依赖 | 致命 | 不创建 client |
| 缺少 API 凭据 | 致命 | 不创建 client |
| session 不可用 | 致命 | 需要人工处理 |
| 网络连接失败 | 瞬时 | 按 backoff 重试 |
| handler 注册失败 | 致命 | disconnect 后进入 failed |

P6-2D 第一版实现应要求所有 enabled channel 都能解析成功。后续阶段可以设计 partial-channel degraded mode，但那必须是单独设计变更。

## Handler 契约

runtime 仍只注册一个 handler：

```text
events.NewMessage(chats=resolved_channel_ids)
```

handler 职责：

```text
receive event
-> verify runtime is listening
-> convert to IncomingMessage
-> run watchlist filter
-> enqueue or hand off according to the next approved phase
-> update counters and last_event_at
```

P6-2D 实现可以定义 handoff 点，但除非后续阶段明确批准 pipeline，否则不得直接调用数据库、Parser、Dedup 或 Bot 通知。

handler 必须保持轻量：

- 不做除 Telethon 接收路径之外的网络请求
- 不访问数据库
- 不下载媒体
- 不回溯历史
- 不在 handler 内做重试循环
- 不在 handler 内 sleep
- 不发送消息
- 不持久化 raw event

## 重连策略

重连必须由 runtime 生命周期拥有，不属于 handler 职责。

可重连的瞬时错误：

- 网络断开
- 临时连接超时
- Telegram server migration / retryable RPC error
- connect 或 channel refresh 阶段出现的短 `FloodWait`

致命错误：

- API id/hash 无效
- Auth key unregistered
- 需要 session password
- session 文件不可读或损坏
- watchlist schema 非法
- 所有 source channel 都无法解析
- handler 无法注册

Backoff 策略：

```text
initial_delay_seconds = 1
max_delay_seconds = 60
multiplier = 2
jitter = yes
reset_after_stable_seconds = 300
```

重试序列示例：

```text
1s -> 2s -> 4s -> 8s -> 16s -> 32s -> 60s -> 60s ...
```

重连循环约束：

- 同一时间只能有一个 active connect attempt
- 不允许并行 Telethon client
- 重连前先移除旧 handler
- 成功重连后才注册新 handler
- 重连期间阻止事件处理
- heartbeat 持续输出，状态为 `degraded` 或 `connecting`
- backoff 必须可观测
- 致命错误必须退出循环并进入 `failed`

P6-2D 不允许在重连后自动 history backfill。漏掉的消息属于后续阶段，因为 backfill 会引入持久化和去重语义。

## 心跳策略

Heartbeat 是运行时健康信号，不是业务通知。

默认间隔：

```text
heartbeat_interval_seconds = 30
```

Heartbeat payload：

```text
monitor_state
uptime_seconds
connected
handler_registered
enabled_source_channels
resolved_channel_count
events_seen_total
events_matched_total
events_rejected_total
conversion_error_total
handler_error_total
reconnect_attempt_total
last_event_at
last_successful_event_at
last_error_at
last_error_code
backoff_seconds_current
traffic_observed
database_accessed=no
parser_called=no
dedup_called=no
notification_sent=no
history_backfill_called=no
raw_event_persisted=no
```

Heartbeat 禁止包含：

- 完整消息正文
- 完整频道 username
- 完整频道 title
- sender id
- URL query
- invite link
- token
- raw Telethon event

第一版 heartbeat 输出目标：

- 结构化日志行

后续阶段可以增加：

- health endpoint
- metrics exporter
- external alerting

这些扩展都必须从同一份 heartbeat state 派生。

## 健康与就绪

必须区分 liveness 和 readiness。

Liveness：

> monitor 进程是否存活，并且生命周期是否还能推进？

Readiness：

> monitor 当前是否已连接、handler 已注册，并且能够接收 resolved channels 的新事件？

建议状态：

| Runtime state | Liveness | Readiness |
|---------------|----------|-----------|
| `created` | yes | no |
| `starting` | yes | no |
| `preflight` | yes | no |
| `resolving_channels` | yes | no |
| `connecting` | yes | no |
| `registering_handler` | yes | no |
| `listening` | yes | yes |
| `degraded` | yes | no |
| `draining` | yes | no |
| `stopped` | no | no |
| `failed` | no | no |

重连和 drain 期间 readiness 必须为 false。

## 错误分类

使用稳定 error code，不使用自由文本作为判断依据。

启动错误：

```text
WATCHLIST_UNREADABLE
WATCHLIST_SCHEMA_INVALID
NO_ENABLED_CHANNELS
CHANNEL_RESOLUTION_FAILED
TELETHON_DEPENDENCY_MISSING
TELEGRAM_CREDENTIALS_MISSING
SESSION_UNAVAILABLE
CONNECT_FAILED
HANDLER_REGISTRATION_FAILED
```

运行时错误：

```text
CLIENT_DISCONNECTED
RECONNECT_EXHAUSTED
DTO_CONVERSION_ERROR
HANDLER_ERROR
FILTER_ERROR
FLOOD_WAIT
ACCESS_FORBIDDEN
CHANNEL_PRIVATE
UNEXPECTED_EXCEPTION
```

停机错误：

```text
HANDLER_REMOVE_FAILED
DISCONNECT_FAILED
DRAIN_TIMEOUT
```

错误报告字段：

```text
error_code
error_phase
recoverability: transient/fatal
retry_scheduled: yes/no
backoff_seconds
occurred_at
message_desensitized: yes
```

如果 raw exception string 可能包含敏感路径、token、username 或消息正文，不得直接输出。已知异常应映射为稳定 code。

## 优雅停机

停机触发：

- SIGTERM
- SIGINT
- App lifespan shutdown
- operator stop command
- 致命 runtime error

停机顺序：

```text
state = draining
-> stop accepting new events
-> wait for in-flight handler tasks up to drain_timeout_seconds
-> remove handler
-> disconnect client
-> stop heartbeat loop
-> emit final summary
-> state = stopped
```

默认 drain timeout：

```text
drain_timeout_seconds = 10
```

规则：

- 条件允许时，先 remove handler，再 disconnect。
- 即使 handler removal 失败，也必须尝试 disconnect。
- final summary 只能输出一次。
- 进入 `draining` 后，新事件不得再修改状态。
- 停机不得触发 history backfill 或下游业务模块。

## 可观测性

最小结构化日志：

```text
monitor.starting
monitor.preflight_passed
monitor.channels_resolved
monitor.connected
monitor.handler_registered
monitor.heartbeat
monitor.event_processed
monitor.event_rejected
monitor.handler_error
monitor.reconnect_scheduled
monitor.reconnected
monitor.degraded
monitor.shutdown_started
monitor.shutdown_completed
monitor.failed
```

Counters：

```text
events_seen_total
events_matched_total
events_rejected_total
dto_conversion_error_total
handler_error_total
filter_error_total
reconnect_attempt_total
reconnect_success_total
heartbeat_total
shutdown_total
```

Gauges：

```text
monitor_state
connected
handler_registered
resolved_channel_count
backoff_seconds_current
inflight_handler_tasks
uptime_seconds
```

Timestamps：

```text
started_at
last_event_at
last_matched_event_at
last_error_at
last_reconnect_at
last_heartbeat_at
stopped_at
```

脱敏规则：

- human log 中 channel id 必须 mask。
- 不记录完整 source ref。
- 不记录 message text。
- 需要 message-level 诊断时，只使用 `text_length` 和 `text_hash_prefix`。
- 不记录 raw Telethon event。

## 配置

建议新增运行时配置：

```text
MONITOR_HEARTBEAT_INTERVAL_SECONDS = 30
MONITOR_RECONNECT_INITIAL_DELAY_SECONDS = 1
MONITOR_RECONNECT_MAX_DELAY_SECONDS = 60
MONITOR_RECONNECT_STABLE_RESET_SECONDS = 300
MONITOR_DRAIN_TIMEOUT_SECONDS = 10
MONITOR_MAX_INFLIGHT_EVENTS = 100
```

既有配置保持：

```text
WATCHLIST_PATH
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION_NAME
```

P6-2D 第一版不支持 watchlist hot reload。修改 `watchlist.json` 需要重启进程。Hot reload 会改变 handler 注册和 channel resolution 语义，必须后续单独设计。

## Backpressure

handler 不得 inline 执行重任务。

第一版实现可选：

1. 只 inline 执行轻量 watchlist filter。
2. 使用 bounded in-memory queue，为后续已批准 pipeline 提供 handoff。

如果使用 queue：

```text
max_queue_size = MONITOR_MAX_INFLIGHT_EVENTS
on_full = reject_new_event_and_count
```

Queue full error code：

```text
EVENT_QUEUE_FULL
```

不允许无界队列。

## 数据处理边界

P6-2D 可以保持 P6-2C 行为：

```text
event -> IncomingMessage -> watchlist filter -> runtime counters/logs
```

但不得宣称 full production ingest：

```text
event -> RawMessage -> Parser -> Normalizer -> Dedup -> Notification
```

该 pipeline 需要后续单独阶段，因为它会引入数据库事务、幂等、parse failure 行为、dedup 状态流转和 Bot 通知副作用。

## Runtime Report

建议 final summary：

```text
P6-2D_RUNTIME_SUMMARY:
- startup_status: pass/fail
- final_state:
- uptime_seconds:
- enabled_source_channels:
- resolved_channel_count:
- handler_registered_final: yes/no
- connected_final: yes/no
- events_seen_total:
- events_matched_total:
- events_rejected_total:
- dto_conversion_error_total:
- handler_error_total:
- reconnect_attempt_total:
- reconnect_success_total:
- shutdown_reason:
- handler_removed: yes/no
- client_disconnected_cleanly: yes/no
- heartbeat_emitted: yes/no
- report_desensitized: yes
- database_accessed: no
- parser_called: no
- normalizer_called: no
- dedup_called: no
- notification_sent: no
- media_downloaded: no
- history_backfill_called: no
- raw_event_persisted: no
- production_ingest_enabled: no
- blockers:
  - ...
```

## 实现阶段测试矩阵

Startup：

| 场景 | 预期 |
|------|------|
| Watchlist 不可读 | `failed`，不注册 handler |
| 无 enabled channels | `failed`，不注册 handler |
| Channel resolution 失败 | `failed`，不注册 handler |
| Connect 成功 | 状态进入 `registering_handler` |
| Handler 注册成功 | 状态进入 `listening` |
| Handler 注册失败 | disconnect 并进入 `failed` |

Reconnect：

| 场景 | 预期 |
|------|------|
| Client disconnect | 状态进入 `degraded` 或 `connecting` |
| Reconnect 成功 | handler 重新注册且只注册一次 |
| Reconnect 遇到致命 auth error | 状态进入 `failed` |
| Backoff 增长 | 不超过 max delay |
| 稳定运行期结束 | backoff reset |
| Handler active 时重连 | 先移除旧 handler |

Heartbeat：

| 场景 | 预期 |
|------|------|
| Listening 状态 | 输出 heartbeat |
| Reconnecting 状态 | 输出 heartbeat，readiness=false |
| 发生错误 | heartbeat 包含稳定 error code |
| 无流量 | 仍输出 heartbeat |
| 检查日志 | 无完整正文、raw event、username、title |

Handler：

| 场景 | 预期 |
|------|------|
| 目标频道有效事件 | counter 更新 |
| DTO 转换失败 | error counter 更新，runtime 继续 |
| Filter reject | reject counter 更新 |
| Filter match | match counter 更新 |
| Queue full，如果使用 queue | event rejected，runtime 继续 |
| Draining 期间收到事件 | 忽略 |

Shutdown：

| 场景 | 预期 |
|------|------|
| SIGTERM | drain、remove handler、disconnect |
| SIGINT | drain、remove handler、disconnect |
| Handler removal 失败 | 仍尝试 disconnect |
| Disconnect 失败 | final summary 记录错误 |
| In-flight task 超过 drain timeout | 记录 `DRAIN_TIMEOUT` |
| Stop 调用两次 | final summary 只输出一次 |

Boundary：

| 场景 | 预期 |
|------|------|
| Runtime 在测试窗口运行 | 不访问 DB |
| Runtime 收到消息 | 不调用 Parser/Normalizer/Dedup |
| Runtime 观察到媒体消息 | 不下载媒体 |
| Runtime 重连 | 不 history backfill |
| Runtime 输出日志和报告 | 不持久化 raw event |

## P6-2D 实现准入门槛

只有当本设计被接受，并且第一版实现保持以下约束时，才允许开始 P6-2D 实现：

- 一个 runtime owner
- 一个 Telethon client
- 一个 `NewMessage` handler
- 显式有限状态机
- 有界 reconnect backoff
- heartbeat loop
- graceful shutdown
- structured counters
- stable error codes
- 脱敏 logs/reports
- 不调用 DB/Parser/Dedup/Notification
- 不 history backfill
- 不下载媒体
- 不持久化 raw event

## P6-2D 设计完成标准

P6-2D 设计完成条件：

- Runtime 状态机已定义。
- Startup lifecycle 已定义。
- Reconnect 策略已定义。
- Heartbeat 策略已定义。
- Health/readiness 语义已定义。
- Error recovery 策略已定义。
- Observability 字段已定义。
- Graceful shutdown 顺序已定义。
- 测试矩阵已定义。
- Non-goals 保持明确。

本文档是长期运行 monitor runtime 的设计基线；第一版实现位于 `backend/app/modules/monitor/runtime.py`。实现仍不接入数据库、Parser、Normalizer、Dedup 或 Bot。
