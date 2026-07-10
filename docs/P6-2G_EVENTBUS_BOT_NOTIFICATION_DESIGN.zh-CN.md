# P6-2G EventBus / Bot 查询通知接入设计

> 状态：设计已按 required changes 修正
> 范围：RawMessage processing boundary 接入 EventBus，并复用既有 Bot 查询/通知能力
> 不包含：Monitor 改造、队列 worker、Outbox、Bot 新命令、订阅系统、媒体下载、history backfill

## 阶段位置

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2E | Monitor -> RawMessage ingestion boundary | 已完成 |
| P6-2F | RawMessage -> Parser / Normalizer / Dedup 编排 | 已完成 |
| P6-2G | EventBus / Bot 查询通知接入 | 本文档定义 |

P6-2G 只能回答：

> 一个已入库的 `RawMessage` 完成 processing 后，是否能通过受控 EventBus 发布资源事件，并由既有 Bot 通知链路 best-effort 消费？

P6-2G 不能回答：

> 长期 monitor 是否自动处理所有消息、是否具备队列消费、是否具备 Outbox 可靠投递、是否支持用户订阅、是否完成生产级通知审计。

## 固定边界

P6-2G 接通的是 processing application boundary 到 EventBus 的最小链路：

```text
RawMessage.id
-> RawMessageProcessingBoundary.process_raw_message(raw_message_id)
-> application-owned AsyncSession lifecycle
-> RawMessageService.parse_and_persist(raw_message_id)
-> RawMessageService.dedup_and_persist(raw_message_id, event_bus=event_bus)
-> RawMessageProcessingResult
-> EventBus best-effort publish ResourceCreated / ResourceMerged
-> ResourceNotifyHandler
-> ResourceQueryService ViewModel
-> BotTransport.send_message()
```

Monitor 仍然只负责传输适配与 handoff，不直接发布 EventBus，也不直接发送 Bot 通知。

允许的范围：

- 为 `RawMessageProcessingBoundary` 增加可选 `event_bus`
- P6-2G 启用时，将该 `event_bus` 传给 `RawMessageService.dedup_and_persist()`
- 继续复用 P5 的 `ResourceCreated` / `ResourceMerged`
- 继续复用 P5 的 `InMemoryEventBus`
- 继续复用 P5 的 `ResourceNotifyHandler`
- 保持 P5 的 `BotCommandHandler` 查询链路回归，不让它参与通知链路
- processing result 增加受控观测字段，用于说明事件链路是否启用
- 通知失败不得影响 RawMessage / Resource 已提交状态

禁止的范围：

- `monitor/runtime.py` 直接发布 EventBus
- `monitor/runtime.py` 直接发送 Bot 通知
- `monitor/runtime.py` 直接创建 Bot handler
- Bot handler 反向调用 `RawMessageProcessingBoundary`
- Bot handler 反向访问 RawMessage processing 状态机
- EventBus handler 反向修改 Parser / Normalizer / Dedup 结果
- 引入 Outbox / durable queue
- 引入后台 worker / scheduler
- 自动重试历史消息
- 新增 Bot 命令
- 新增用户订阅模型
- 新增 media download / history backfill
- 改写 Parser / Normalizer / Dedup 业务规则

## 现有接口事实

当前 `RawMessageService.dedup_and_persist()` 接口已经支持：

```text
dedup_and_persist(raw_msg_id, event_bus=None) -> RawMessage
```

现有行为：

- dedup 成功并 commit 后才发布事件
- 新 Resource 发布 `ResourceCreated`
- 已有 Resource 新增来源或链接时发布 `ResourceMerged`
- `event_bus is None` 时不发布事件
- `RawMessageService` 捕获 `event_bus.publish()` 异常，只记录日志，不重新抛出
- 单个 `publish()` 调用失败后，`RawMessageService` 继续处理同批后续待发布事件
- dedup 失败时保持 `dedup_status=dedup_pending`，不发布事件

当前 P6-2F 固定传：

```text
event_bus=None
```

P6-2G 才允许将该值替换为应用注入的 EventBus。

当前 FastAPI 应用装配已经具备：

```text
create_app()
-> app.state.event_bus = InMemoryEventBus()
-> subscribe(ResourceCreated, notify_created)
-> subscribe(ResourceMerged, notify_merged)
-> ResourceNotifyHandler
-> ResourceQueryService
-> BotTransport
```

因此 P6-2G 不需要重做 Bot 通知 handler，也不需要新增通知 formatter。

## EventBus 异常隔离责任

P6-2G 固定业务隔离保证由调用方 `RawMessageService` 承担：

```text
RawMessageService
-> commit dedup / resource changes
-> call event_bus.publish(event)
-> catch publish exception
-> log only
-> continue later events
-> return committed RawMessage
```

`InMemoryEventBus.publish()` 可以继续隔离 handler 异常，但 P6-2G 的业务保证不得依赖具体 EventBus 实现。即使测试使用会抛错的 `FailingEventBus`，也必须满足：

```text
handler or publish throws
-> dedup_and_persist returns success state
-> dedup_status remains new / matched
-> boundary returns dedup_new / dedup_matched
-> database commit remains valid
```

不能把异常隔离责任模糊分摊到两层。

## EventBus 协议

实现时沿用项目已有抽象：

```text
class EventBus:
    async def publish(self, event: DomainEvent) -> None:
        ...
```

依赖规则：

- `RawMessageProcessingBoundary` 只依赖 `EventBus` 抽象
- `RawMessageService` 只依赖 `EventBus` 抽象
- 生产装配可使用 `InMemoryEventBus`
- 测试应使用 `RecordingEventBus` / `FailingEventBus`
- 不得通过日志或 Bot 消息反推 EventBus 发布行为

建议固定依赖方向：

```text
FastAPI assembly
├── InMemoryEventBus
├── ResourceQueryService
├── BotTransport
├── ResourceNotifyHandler
└── RawMessageProcessingBoundary(event_bus)
```

禁止反向依赖：

```text
EventBus -> RawMessageProcessingBoundary
BotTransport -> RawMessageProcessingBoundary
ResourceNotifyHandler -> RawMessageService
BotCommandHandler -> RawMessageProcessingBoundary
```

## Application Boundary 调整

正式边界仍然是：

```text
class RawMessageProcessingBoundary:
    async def process_raw_message(
        self,
        raw_message_id: int | str,
    ) -> RawMessageProcessingResult:
        ...
```

P6-2G 允许在构造时增加可选依赖：

```text
RawMessageProcessingBoundary(
  session_factory=...,
  raw_message_service_factory=...,
  event_bus: EventBus | None = None,
)
```

固定规则：

- `event_bus=None` 是默认值
- 默认行为必须保持 P6-2F 完全兼容
- 构造器保持框架无关，不接收 FastAPI `app` / `app.state` / `request`
- 只有 dedup 路径允许使用 `event_bus`
- parse-only 失败路径不得发布事件
- already_processed 不得补发事件
- invalid raw_message_id 不得打开 DB session，也不得触碰 EventBus
- raw_message_not_found 不得发布事件

允许调用：

```text
RawMessageService.dedup_and_persist(
  raw_message_id,
  event_bus=self._event_bus,
)
```

不得新增：

```text
RawMessageProcessingBoundary
-> ResourceNotifyHandler
RawMessageProcessingBoundary
-> BotTransport
RawMessageProcessingBoundary
-> ResourceQueryService
RawMessageProcessingBoundary(app)
RawMessageProcessingBoundary(app.state)
RawMessageProcessingBoundary(request)
```

processing boundary 只知道 EventBus 抽象，不知道 Bot。

应用装配层负责：

```text
create_app()
-> create EventBus
-> register ResourceNotifyHandler handlers
-> construct RawMessageProcessingBoundary(event_bus=event_bus)
```

application 层测试不得依赖 FastAPI lifespan 才能验证 processing boundary 行为。

## 结果 DTO 调整

P6-2F 结果 DTO 已有字段：

```text
RawMessageProcessingResult
- status
- raw_message_id
- parse_status
- dedup_status
- parse_attempts
- parsed_resource_count
- error_code
- parse_executed
- dedup_executed
```

P6-2G 第一版只允许追加最小观测字段：

```text
- eventbus_enabled: bool
```

固定含义：

- `true`：`self._event_bus is not None`
- `false`：`self._event_bus is None`

`eventbus_enabled` 不能读取或推断 FastAPI `app.state`。

以下情况都必须返回 `false`：

- `app.state.event_bus` 存在，但 boundary 未注入
- 应用创建了 EventBus，但当前测试使用默认 boundary
- Bot handler 已注册，但 processing boundary 未持有 EventBus

不允许追加以下字段到生产 DTO：

- `eventbus_published`
- `event_count`
- `notification_sent`
- `notification_count`
- `notification_failed_count`
- `created_resource_count`
- `merged_resource_count`

原因：

- 当前 `EventBus.publish()` 无返回值
- 当前 `RawMessageService.dedup_and_persist()` 不返回 per-event publish 结果
- Bot 通知是 EventBus consumer 副作用，不属于 processing result 的事实来源
- 用 DTO 反推事件或通知数量会造成误导

如需统计通知结果，应在后续 P6-2G-observability 或 Outbox 阶段单独设计。

## 状态映射

P6-2G 不改变 P6-2F 状态机。

合法状态仍为：

```text
invalid_raw_message_id
raw_message_not_found
already_processed
parse_failed
dedup_skipped
dedup_new
dedup_matched
dedup_failed
processing_failed
```

事件发布只可能发生在 dedup 成功路径：

| processing status | eventbus_enabled | 事件可能性 |
|-------------------|------------------|------------|
| `dedup_new` | `true` | 可能发布一个或多个 `ResourceCreated` |
| `dedup_matched` | `true` | 仅有新增来源或链接时可能发布 `ResourceMerged` |
| `dedup_skipped` | `true` | 不应发布事件 |
| `already_processed` | `true` | 不补发事件 |
| `parse_failed` | `true` | 不发布事件 |
| `dedup_failed` | `true` | 不发布事件 |
| `processing_failed` | `true` | 不发布事件 |
| 任意状态 | `false` | 不发布事件 |

注意：

`dedup_matched` 不等于一定发布 `ResourceMerged`。只有 `created_source` 或 `created_link_count > 0` 时，现有 service 才会发布 merge 事件。

一个 `RawMessage` 可能解析出多个 `ParsedResource`，因此一次 processing 可能发布多个事件。设计和测试都不得隐含：

```text
one RawMessage == one event
```

matched 场景必须拆开：

| 场景 | 预期 |
|------|------|
| matched，新增 source 或 link | 发布 `ResourceMerged` |
| matched，但没有新增 source / link | 不发布事件 |

## Bot 查询与通知职责

Bot 查询继续使用既有命令：

```text
/latest
/search
/resource
/help
```

P6-2G 不新增命令。

P6-2G 的新增目标是事件驱动通知接入，不是新增 Bot 命令查询能力。验收必须拆成两条链路：

```text
existing_bot_query_regression
event_driven_notification_integration
```

Bot 查询固定边界：

```text
Telegram webhook
-> BotCommandHandler
-> ResourceQueryService
-> ViewModel
-> BotTransport
```

Bot 通知固定边界：

```text
ResourceCreated / ResourceMerged
-> EventBus
-> ResourceNotifyHandler
-> ResourceQueryService.get_detail(resource_id)
-> formatter
-> BotTransport.send_message()
```

Bot 层不得：

- 直接暴露 ORM model
- 直接查询 RawMessage processing 内部状态
- 触发 Parser / Dedup
- 修改 Resource / ResourceSource
- 反向调用 Monitor

`BotCommandHandler` 不参与通知链路；`ResourceNotifyHandler` 不参与命令查询链路。

## 配置边界

P6-2G 可以复用 P5 已有配置：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
TELEGRAM_ALLOWED_CHAT_IDS
TELEGRAM_NOTIFY_CHAT_IDS
```

不新增配置项：

- 不新增 notification retry 配置
- 不新增 subscriber 配置
- 不新增 Outbox 配置
- 不新增 queue 配置

应用装配层负责决定是否传入 EventBus：

```text
eventbus_enabled = self._event_bus is not None
```

如果 Telegram Bot 配置缺失：

- `event_bus` 仍可存在
- Bot notification handler 可以不注册
- processing 仍可正常运行
- `eventbus_enabled` 只表示 boundary 实际持有 EventBus，不表示 Bot 通知可用

## 错误隔离

P6-2G 固定 best-effort 语义：

```text
DB commit 是真实状态源
EventBus / Bot notification 是提交后的副作用
```

错误隔离规则：

- Parser 失败不发布事件
- Dedup 失败不发布事件
- EventBus `publish()` 或 handler 失败不回滚 DB
- 单个 EventBus `publish()` 调用失败不阻断同批后续事件发布尝试
- Bot query 失败只返回查询错误消息
- Bot notify 查询失败则跳过该通知
- Bot formatter 失败由事件 handler / EventBus 隔离，不回滚 DB
- Bot transport 失败不阻断后续 chat_id
- 通知失败不改变 `RawMessage.dedup_status`
- 通知失败不改变 `Resource` / `ResourceSource`

P6-2G 不保证：

- 事件一定送达
- Bot 消息一定发送成功
- 进程重启后补发事件
- 多实例部署下只通知一次

这些属于 Outbox / durable notification 阶段。

## 幂等与重复通知

P6-2G 第一版不新增通知幂等表。

固定行为：

- 同一个 RawMessage 第一次 dedup 成功时可能发布事件
- 第二次处理同一 RawMessage 若已是 final dedup 状态，返回 `already_processed`，不得补发事件
- 重复 RawMessage 入库由 P6-2E ingest disposition 处理
- 重复资源归并由 DedupService 判定

P6-2G 不通过通知层反推业务幂等。

## 测试验收

建议新增测试集中在 application 层和 app assembly，不跑真实 Telegram HTTP：

必测：

| 场景 | 预期 |
|------|------|
| 默认不注入 EventBus | `eventbus_enabled=false`，不发布事件 |
| `app.state.event_bus` 存在但 boundary 未注入 | `eventbus_enabled=false` |
| P6-2F 默认兼容 | 默认行为与 P6-2F 一致 |
| 注入 EventBus + 新资源 | `dedup_new`，捕获 `ResourceCreated` |
| 一个 RawMessage 产生多个新 Resource | 捕获多个 `ResourceCreated` |
| mixed new + matched | 事件类型和数量按实际 dedup result 产生 |
| matched，新增 source 或 link | 捕获 `ResourceMerged` |
| matched，但没有新增 source / link | 不发布 `ResourceMerged` |
| 注入 EventBus + 空 parsed_data | `dedup_skipped`，不发布事件 |
| already_processed | 不补发事件 |
| parse_failed | 不发布事件 |
| dedup_failed | 不发布事件 |
| 非 dedup 路径 | EventBus spy 零调用 |
| EventBus handler 抛异常 | processing 仍返回 dedup 成功状态，DB 不回滚 |
| EventBus 对第一个事件失败 | DB 不回滚，继续尝试同批后续事件 |
| Bot notify handler 查询 ViewModel | 使用 `ResourceQueryService`，不暴露 ORM |
| 查询 ViewModel 返回不存在 | handler 安全跳过，不发送 |
| formatter 抛异常 | 不回滚 DB，不影响 processing result |
| Bot transport 单 chat 失败 | 不阻断后续 chat_id |
| Telegram 配置缺失 | processing 不受影响 |

禁止测试：

- 不访问真实 Telegram Bot API
- 不注册真实 webhook
- 不启动长期 monitor
- 不访问 Telegram client / Telethon
- 不引入队列或后台 worker

## 建议验收报告

```text
P6-2G_EVENTBUS_BOT_RESULT:
- boundary_eventbus_optional_dependency: pass/fail
- default_eventbus_disabled: pass/fail
- p6_2f_backward_compatibility: pass/fail
- resource_created_event_published: pass/fail
- multiple_created_events_supported: pass/fail
- mixed_new_and_matched_events_supported: pass/fail
- matched_with_change_event_published: pass/fail
- matched_without_change_event_not_published: pass/fail
- dedup_skipped_event_not_published: pass/fail
- already_processed_event_not_replayed: pass/fail
- parse_failed_event_not_published: pass/fail
- dedup_failed_event_not_published: pass/fail
- event_handler_failure_isolated_from_db: pass/fail
- bot_notification_uses_viewmodel: pass/fail
- bot_command_query_regression: pass/fail
- per_chat_transport_failure_isolated: pass/fail
- telegram_config_missing_processing_unaffected: pass/fail
- telegram_api_accessed: no
- telethon_accessed: no
- monitor_runtime_changed: no
- outbox_used: no
- worker_started: no
- notification_retry_enabled: no
- blockers:
  - ...
```

## 完成标准

P6-2G 通过时，只能得出：

- Processing boundary 可受控注入 EventBus
- Dedup 成功后可 best-effort 发布资源事件
- 既有 Bot 通知 handler 可消费事件并使用 ViewModel 发送通知
- EventBus / Bot 失败不会回滚数据库状态
- P6-2F 默认无 EventBus 行为保持兼容

P6-2G 通过时，不能得出：

- 长期 monitor 已自动全链路处理消息
- 事件具备持久化可靠投递
- 通知具备重试、审计或去重
- Bot 支持用户订阅
- 生产多实例通知语义已经稳定
- Monitor 已自动触发 processing

## 下一阶段

P6-2G 实现完成后，后续应单独设计：

```text
P6-2H:
Monitor -> ingest -> process handoff 编排

P6-Outbox:
持久化事件与可靠通知

P6-Subscription:
用户订阅、过滤与定向通知
```

不得把这些内容混入 P6-2G。

## 阶段状态

```text
P6-2E: completed
P6-2F: completed
P6-2G design: approved_with_required_changes_applied
P6-2G implementation: allowed
P6-2H: not allowed
Outbox: not allowed
Subscription: not allowed
production notification: not allowed
```
