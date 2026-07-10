# P6-2H Monitor -> Ingest -> Process Handoff 设计

> 状态：设计已按 required changes 修正
> 范围：MonitorRuntime 在 matched message 入库后，受控调用 RawMessageProcessingBoundary
> 不包含：Outbox、worker、retry queue、订阅模型、history backfill、媒体下载、生产可靠通知

## 阶段位置

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2E | Monitor -> RawMessage ingestion boundary | 已完成 |
| P6-2F | RawMessage -> Parser / Normalizer / Dedup 编排 | 已完成 |
| P6-2G | EventBus / Bot 查询通知接入 | 已完成 |
| P6-2H | Monitor -> ingest -> process handoff 编排 | 本文档定义 |

P6-2H 只能回答：

> Monitor 收到一条 matched Telegram 消息后，是否能通过受控 application boundary 完成入库，并在可处理时触发 RawMessage processing？

P6-2H 不能回答：

> 是否具备可靠队列、断点补偿、Outbox 投递、用户订阅、history backfill、媒体下载或多实例生产通知语义。

## 固定链路

P6-2H 接通的是 Monitor runtime 的 application handoff 编排：

```text
Telethon NewMessage
-> MonitorRuntime._handle_event()
-> IncomingMessage
-> watchlist filter
-> IncomingMessageIngestionBoundary.ingest_incoming()
-> IncomingIngestionResult
-> RawMessageProcessingBoundary.process_raw_message(raw_message_id)
-> RawMessageProcessingResult
-> EventBus / Bot notification (P6-2G)
```

Monitor 仍然不是业务处理层。Monitor 只持有两个 application boundary：

```text
MonitorRuntime
-> IncomingMessageIngestionBoundary protocol
-> RawMessageProcessingBoundary protocol
```

建议协议固定为：

```text
class IncomingIngestionBoundaryProtocol:
    async def ingest_incoming(
        self,
        message: IncomingMessage,
    ) -> IncomingIngestionResult:
        ...

class RawMessageProcessingBoundaryProtocol:
    async def process_raw_message(
        self,
        raw_message_id: int,
    ) -> RawMessageProcessingResult:
        ...
```

`MonitorRuntime` 构造器只依赖协议：

```text
MonitorRuntime(
  ingestion_boundary: IncomingIngestionBoundaryProtocol | None = None,
  processing_boundary: RawMessageProcessingBoundaryProtocol | None = None,
)
```

测试中注入 fake boundary。Monitor 不依赖具体实现类。

Monitor 不直接持有：

- `AsyncSession`
- Repository
- `RawMessageService`
- Parser
- Normalizer
- Dedup
- EventBus
- Bot handler / Bot transport

## 允许范围

- `MonitorRuntime` 增加可选 `processing_boundary`
- matched message 入库后按 ingestion result 决定是否触发 processing
- processing 结果进入脱敏 counter / heartbeat / summary
- processing 异常隔离，不导致 runtime 崩溃
- app assembly 可以把已创建的 `RawMessageProcessingBoundary` 注入 MonitorRuntime
- 测试使用 fake ingestion / processing boundary
- 不访问真实 Telegram API
- 不发送真实 Bot HTTP

## 禁止范围

- Monitor 直接访问 DB
- Monitor 直接调用 `RawMessageService`
- Monitor 直接调用 Parser / Normalizer / Dedup
- Monitor 直接发布 EventBus
- Monitor 直接发送 Bot 通知
- Monitor 直接读取 Resource / ResourceSource
- 引入后台 worker / queue / scheduler
- 引入 Outbox
- 自动扫描历史 `parse_pending` RawMessage
- 自动重试 `parse_failed`
- history backfill
- media download
- 修改 Parser / Normalizer / Dedup 业务规则
- 新增 Bot 命令或订阅模型

## Handoff 决策表

`IncomingIngestionResult` 必须提供：

```text
status
raw_message_id
error_code
```

固定契约：

```text
IncomingIngestionResult.raw_message_id
= canonical persisted RawMessage.id
```

对于：

```text
status in {"stored", "duplicate"}
```

都必须满足：

```text
raw_message_id is positive int
```

这个 ID 不是 Telegram source message id，也不是本次 duplicate 输入临时值，而是数据库中已存在的 `RawMessage` 主键。

P6-2H 固定处理策略：

| ingestion status | raw_message_id | processing boundary | 行为 |
|------------------|----------------|---------------------|------|
| `stored` | positive int | present | 调用 processing |
| `duplicate` | positive int | present | 调用 processing |
| `stored` | missing / invalid | any | handoff failed，`PROCESSING_INVALID_RESULT` |
| `duplicate` | missing / invalid | any | handoff failed，`PROCESSING_INVALID_RESULT` |
| `stored` | positive int | absent | ingestion only |
| `duplicate` | positive int | absent | ingestion only |
| rejected status | any | any | 不调用 processing |
| `ingest_failed` | any | any | 不调用 processing |
| unknown status | any | any | invalid ingestion result，`INGESTION_INVALID_RESULT` |

为什么 `duplicate` 也允许 processing：

- 如果上次运行在 ingest 成功后、process 前中断，duplicate 可能仍是 `parse_pending / dedup_pending`
- `RawMessageProcessingBoundary` 已具备状态机保护
- 已处理完成的 duplicate 会返回 `already_processed`
- `already_processed` 不补发 EventBus，不重复通知

## Processing 触发规则

Monitor 调用 processing 的唯一条件：

```text
ingestion.status in {"stored", "duplicate"}
and ingestion.raw_message_id is positive int
and processing_boundary is not None
```

如果 `processing_boundary is None`：

- ingestion 仍正常执行
- processing 计数保持 0
- heartbeat / summary 标记 `processing_enabled=no`
- 不记 error

如果 `processing_boundary` 存在但 ingestion 不满足触发条件：

- 不调用 processing
- 对 rejected / failed ingestion 只记录 ingestion 结果
- 不用 processing 猜测修复 ingestion 失败

`process_attempt_total` 的增加时点固定为：

```text
immediately before calling processing_boundary.process_raw_message()
```

以下情况不得增加 `process_attempt_total`：

- `processing_boundary is None`
- ingestion rejected
- `ingest_failed`
- `stored / duplicate` 但 `raw_message_id` missing / invalid
- ingestion result status unknown
- ingestion result malformed

## Processing 结果映射

Monitor 只读取 `RawMessageProcessingResult` 的稳定字段：

```text
status
raw_message_id
error_code
parse_executed
dedup_executed
eventbus_enabled
```

P6-2H 必须校验 processing result：

```text
result is RawMessageProcessingResult-compatible
result.status in known_processing_statuses
result.raw_message_id == ingestion.raw_message_id
result.error_code is valid or None
result.parse_executed is bool
result.dedup_executed is bool
result.eventbus_enabled is bool
```

以下情况全部映射为：

```text
PROCESSING_INVALID_RESULT
```

- result is None
- unknown status
- raw_message_id mismatch
- non-bool execution flags
- missing required field
- malformed DTO

Monitor 不尝试修复非法结果。

Monitor 不读取：

- parsed payload
- raw_payload
- Resource / ResourceSource
- EventBus publish result
- Bot notification result

Processing status 映射：

| processing status | monitor 结果 |
|-------------------|-------------|
| `dedup_new` | process_success |
| `dedup_matched` | process_success |
| `dedup_skipped` | process_success |
| `already_processed` | process_already_done |
| `parse_failed` | process_failed |
| `dedup_failed` | process_failed |
| `processing_failed` | process_failed |
| `raw_message_not_found` | process_failed |
| `invalid_raw_message_id` | process_failed |

注意：

- `parse_failed` 是业务处理失败，不是 monitor handler fatal error
- processing failed 不应让 monitor 退出
- EventBus / Bot 通知结果不进入 monitor 判断

## Counter / Heartbeat / Summary

P6-2H 允许新增 monitor counters：

```text
processing_enabled
process_attempt_total
process_success_total
process_already_done_total
process_failed_total
parse_executed_total
dedup_executed_total
last_process_at
last_process_status
last_process_error_code
```

固定含义：

- `processing_enabled = processing_boundary is not None`
- `parse_executed_total` 只从 processing result 的 `parse_executed=true` 统计
- `dedup_executed_total` 只从 processing result 的 `dedup_executed=true` 统计
- Monitor 不输出 `parser_called / normalizer_called / dedup_called`
- Monitor 不输出 `database_accessed`
- Monitor 不输出 `notification_sent`
- Monitor 不通过 EventBus / Bot 副作用反推通知状态

原因：

- Monitor 不直接观察 Parser
- Normalizer 在 Dedup service 内部执行，Monitor 不可靠观察
- `dedup_executed=true` 只表示 processing boundary 报告了 dedup 尝试
- boundary 存在不等于实际访问数据库

计数恒等式必须成立：

```text
process_attempt_total
= process_success_total
+ process_already_done_total
+ process_failed_total
```

若不成立，说明 counter 更新路径有漏项。

不得新增：

- `database_accessed`
- `parser_called`
- `normalizer_called`
- `dedup_called`
- `eventbus_published`
- `notification_sent`
- `notification_count`
- `resource_created_count`
- `resource_merged_count`
- `bot_sent_total`

这些不属于 Monitor 的事实来源。

## Error Code

P6-2H 可以新增 runtime error code：

```text
INGESTION_INVALID_RESULT
PROCESSING_BOUNDARY_EXCEPTION
PROCESSING_INVALID_RESULT
```

错误隔离规则：

- ingestion boundary 返回未知或 malformed result：不调用 processing，记录 `INGESTION_INVALID_RESULT`
- processing boundary 抛异常：计入 `process_failed_total`
- processing 返回非法结果：计入 `process_failed_total`
- processing 返回业务失败：只计入 `process_failed_total`
- processing 失败不回滚 ingestion
- processing 失败不停止 monitor
- processing 失败不触发 reconnect
- processing boundary 抛异常不增加 `handler_error_total`
- 只有异常逃逸出 `_handle_event()` 才增加 `handler_error_total`

`last_process_error_code` 来源优先级：

```text
processing_result.error_code
-> PROCESSING_BOUNDARY_EXCEPTION
-> PROCESSING_INVALID_RESULT
```

异常隔离固定为：

| 异常来源 | 处理 |
|----------|------|
| watchlist / filter failure | handler failure |
| ingestion exception | ingest_failed counter，runtime continues，不调用 processing |
| ingestion invalid result | process_failed counter 不增加，记录 `INGESTION_INVALID_RESULT` |
| processing exception | process_failed counter，runtime continues，handler_error_total unchanged |
| processing invalid result | process_failed counter，runtime continues |
| EventBus / Bot failure | 由 P6-2G 隔离，Monitor 不感知 |

## App Assembly

FastAPI app 已在 P6-2G 创建：

```text
app.state.event_bus
app.state.raw_message_processing_boundary
```

P6-2H 的生产装配应保持：

```text
MonitorRuntime(
  ingestion_boundary=app.state.incoming_ingestion_boundary,
  processing_boundary=app.state.raw_message_processing_boundary,
)
```

若当前 app 尚未集中创建 monitor runtime，可在 P6-2H 仅完成 runtime constructor 与 tests，不强行新增长期启动入口。

不得新增：

- 自动启动 monitor 的 FastAPI lifespan task
- 后台 worker task
- scheduler
- queue consumer

长期启动方式应另行由 CLI / supervisor / deployment 层设计。

## 并发与顺序

P6-2H 第一版固定为单事件 handler 内顺序执行：

```text
filter
-> ingest
-> process
```

不新增队列、不并行拆分 ingest 与 process。

P6-2H v1 使用同步 in-handler handoff：

- process 完成前，该 handler 不结束
- Bot notification 可能作为 EventBus handler 在 process 内完成
- 慢通知可能延长 handler 生命周期
- 本阶段接受这种同步耦合
- 不提供异步持久化、独立重试或 durable delivery

现有 `max_inflight_events` 仍限制同时处理的 handler 数量。P6-2H 不新增跨事件锁。

并发重复的幂等由已有层负责：

- RawMessage ingest 唯一约束判定 `stored / duplicate`
- RawMessageProcessingBoundary 状态机判定 `already_processed`
- DedupService 判定 Resource merge

## 测试验收

建议新增测试集中在 `tests/monitor/test_runtime.py`，使用 fake boundaries。

必测：

| 场景 | 预期 |
|------|------|
| no processing boundary | matched 后只 ingest，不 process |
| ingest stored + raw_message_id | 调用 process |
| ingest duplicate + raw_message_id | 调用 process |
| duplicate 返回 canonical RawMessage ID | 调用正确 ID |
| stored 但 missing raw_message_id | 不 process，process_failed |
| duplicate 但 missing raw_message_id | 不 process，process_failed |
| duplicate 返回非正整数 ID | `PROCESSING_INVALID_RESULT` |
| rejected ingestion | 不 process |
| ingest_failed | 不 process |
| ingestion 返回未知 status | `INGESTION_INVALID_RESULT` |
| processing dedup_new | success counter +1 |
| processing already_processed | already_done counter +1 |
| processing parse_failed | failed counter +1 |
| processing result ID 与 ingestion ID 不一致 | `PROCESSING_INVALID_RESULT` |
| processing 返回未知 status | `PROCESSING_INVALID_RESULT` |
| processing boundary raises | runtime 继续，记录 PROCESSING_BOUNDARY_EXCEPTION |
| processing invalid result | runtime 继续，记录 PROCESSING_INVALID_RESULT |
| processing exception | `handler_error_total` 不增加 |
| counter 恒等式 | attempt = success + already + failed |
| EventBus enabled / disabled | Monitor 只记录 result 字段，不推断通知 |
| process 后 notification 失败 | Monitor process 状态仍按 processing result |
| two concurrent duplicates | 依赖下层幂等，不在 Monitor 加锁 |
| heartbeat | 输出 processing counters 和 enabled flag |
| summary | 输出 processing counters 和 enabled flag |
| monitor direct DB guard | runtime 不创建 AsyncSession / RawMessageService |
| no direct notification | Monitor 不调用 BotTransport |

禁止测试：

- 不访问真实 Telegram API
- 不调用 Telethon
- 不发送真实 Bot HTTP
- 不启动后台 worker
- 不做 history backfill

## 建议验收报告

```text
P6-2H_MONITOR_PROCESS_HANDOFF_RESULT:
- monitor_ingestion_boundary_protocol: pass/fail
- monitor_processing_boundary_optional: pass/fail
- stored_triggers_processing: pass/fail
- duplicate_triggers_processing: pass/fail
- duplicate_uses_canonical_raw_message_id: pass/fail
- rejected_ingestion_skips_processing: pass/fail
- ingest_failed_skips_processing: pass/fail
- missing_raw_message_id_fails_handoff: pass/fail
- invalid_ingestion_result_isolated: pass/fail
- invalid_processing_result_isolated: pass/fail
- processing_result_id_mismatch_rejected: pass/fail
- processing_success_counted: pass/fail
- processing_already_done_counted: pass/fail
- processing_failure_counted: pass/fail
- processing_exception_isolated: pass/fail
- processing_exception_does_not_increment_handler_error: pass/fail
- processing_counter_invariant: pass/fail
- heartbeat_processing_fields: pass/fail
- summary_processing_fields: pass/fail
- monitor_direct_db_access: no
- monitor_direct_parser_call: no
- monitor_direct_normalizer_call: no
- monitor_direct_dedup_call: no
- monitor_direct_eventbus_publish: no
- monitor_direct_bot_notification: no
- telethon_accessed_in_tests: no
- telegram_api_accessed_in_tests: no
- outbox_used: no
- worker_started: no
- retry_queue_enabled: no
- blockers:
  - ...
```

## 完成标准

P6-2H 通过时，可以得出：

- Monitor matched message 可受控完成 ingest -> process handoff
- ingestion boundary 返回 canonical `RawMessage.id`
- stored / duplicate RawMessage 可触发 processing
- duplicate 不会因 replay 自动重复通知
- ingestion / processing 错误不会使 monitor 崩溃
- Monitor 仍不直接访问 DB / Parser / Dedup / EventBus / Bot
- processing 异常不误计为 handler crash

P6-2H 通过时，不能得出：

- 处理链具备可靠异步语义
- 有可靠队列
- 有 Outbox
- 有失败重试
- 有历史补偿扫描
- 有生产多实例通知保证
- 有 media download
- 有用户订阅
- 长期运行入口已经完成

## 下一阶段

P6-2H 后续应单独设计：

```text
P6-2I:
runtime start command / supervisor integration

P6-Reconcile:
扫描 parse_pending / dedup_pending 的历史补偿

P6-Outbox:
持久化事件与可靠通知

P6-Subscription:
用户订阅、过滤与定向通知
```

不得把这些内容混入 P6-2H。

## 阶段状态

```text
P6-2E: completed
P6-2F: completed
P6-2G: completed
P6-2H design: approved_with_required_changes_applied
P6-2H implementation: allowed
P6-2I: not allowed
P6-Reconcile: not allowed
P6-Outbox: not allowed
production runtime: not allowed
```
