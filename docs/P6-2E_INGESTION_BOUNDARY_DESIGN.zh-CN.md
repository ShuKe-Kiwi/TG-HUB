# P6-2E Ingestion Boundary 设计

> 状态：设计锁定候选，已按并发幂等与频道 ID 语义修正
> 范围：Monitor 到 RawMessage 入库应用边界
> 不包含：实现代码、Parser、Normalizer、Dedup、EventBus、Bot 通知

## 阶段位置

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2D | 长期 monitor runtime 生命周期 | 第一版已完成 |
| P6-2E | Monitor -> RawMessage ingestion boundary | 本文档定义 |
| P6-2F | RawMessage -> Parser / Normalizer / Dedup 编排 | 未开始 |
| P6-2G | EventBus / Bot 查询通知接入 | 未开始 |

P6-2E 只能回答：

> 一条 `IncomingMessage` 是否可以通过受控应用边界幂等写入 `RawMessage`？

P6-2E 不能回答：

> 这条消息是否可解析为资源、是否可去重、是否会触发通知、是否已进入完整生产 pipeline。

## 固定边界

P6-2E 接通的是传输适配层和应用入库边界：

```text
MonitorRuntime
-> IncomingMessage
-> IngestionBoundary.ingest_incoming(message)
-> application-owned session
-> Channel lookup
-> RawMessageCreate mapping
-> RawMessageService.ingest()
-> stable ingestion result
```

Monitor 仍然是传输适配层，不直接拥有数据库、Parser、Dedup 或 Bot 编排。

允许的范围：

- 定义 ingestion application boundary
- `IncomingMessage` 到 `RawMessageCreate` 的字段映射
- `source_ref` numeric Telegram channel id 到 `Channel.id` 的查找
- 调用 `RawMessageService.ingest()`
- 返回脱敏、稳定、可测试的 ingestion result
- 区分 stored / duplicate / rejected / failed
- 保持 session 生命周期在 application boundary 内部
- 要求幂等结果来自实际写入层返回值，而不是写入前推测

禁止的范围：

- `monitor/runtime.py` 直接访问 DB
- `monitor/runtime.py` 创建 `AsyncSession`
- `monitor/runtime.py` 直接调用 `ChannelRepository`
- `monitor/runtime.py` 直接调用 `RawMessageService`
- Parser
- Normalizer
- Dedup
- EventBus publish
- Bot notification
- media download
- history backfill
- channel auto-create
- watchlist hot reload
- 修改长期 runtime 生命周期语义

职责归属：

| 层 | 职责 |
|----|------|
| Monitor runtime | 接收消息并提交 `IncomingMessage` |
| Ingestion boundary | 校验、频道查找、DTO 映射、事务生命周期、结果归一化 |
| RawMessageService | RawMessage 幂等写入 |
| Parser 等后续链路 | 本阶段不调用 |

Monitor 不得感知：

- `AsyncSession`
- Repository
- `RawMessageService`
- 事务提交与回滚
- ORM 模型

## 现有接口事实

当前统一入口 DTO：

```text
IncomingMessage
- source_ref: str
- source_message_id: str | int
- text: str | null
- caption: str | null
- raw_payload: dict | null
- published_at: datetime | null
```

正文选择规则已固定：

```text
text strip 后非空
-> content_text = text.strip()
否则 caption strip 后非空
-> content_text = caption.strip()
否则
-> content_text = ""
```

当前 RawMessage 入库 DTO：

```text
RawMessageCreate
- channel_id: int
- tg_message_id: int
- raw_text: str
- raw_media_refs: list[dict] | null
- raw_payload: dict | null
- published_at: datetime | null
```

当前幂等服务：

```text
RawMessageService.ingest(data: RawMessageCreate) -> RawMessageIngestResult
```

`RawMessageService.ingest()` 已负责：

- `(channel_id, tg_message_id)` 幂等
- 新消息 `ingest_status = stored`
- 新消息 `parse_status = parse_pending`
- 新消息 `dedup_status = dedup_pending`
- `raw_text` / `raw_payload` / `raw_media_refs` 保存
- `content_hash` 计算
- commit

P6-2E-1 已完成的实现前置条件：

```text
RawMessageService.ingest()
已能可靠返回 stored / duplicate disposition
```

当前接口：

```text
RawMessageIngestResult
- raw_message: RawMessage
- disposition: stored | duplicate
```

后续实现 P6-2E boundary 时必须读取 `disposition`，不得用写入前 precheck 推测 stored / duplicate。

## Application Boundary 形态

建议新增应用边界，而不是让 Monitor 直接接 DB：

```text
IncomingMessageIngestionBoundary
  ingest_incoming(message: IncomingMessage) -> IncomingIngestionResult
```

边界内部才允许：

```text
validate source_ref
-> canonicalize Telegram channel id
-> validate source_message_id
-> select content text
-> validate non-empty content
-> open application-owned AsyncSession
-> ChannelRepository.get_by_tg_id(canonical_tg_id)
-> build RawMessageCreate
-> RawMessageService.ingest(data)
-> map service disposition to IncomingIngestionResult
-> close AsyncSession
```

Monitor 只能依赖抽象协议：

```text
class IncomingMessageIngestionBoundary(Protocol):
    async def ingest_incoming(
        self,
        message: IncomingMessage,
    ) -> IncomingIngestionResult: ...
```

这样 P6-2D runtime 后续只需要一个 handoff 点，不需要知道 DB、Channel、RawMessageService 的存在。

## 字段映射

固定映射：

| IncomingMessage | RawMessageCreate | 规则 |
|-----------------|------------------|------|
| `source_ref` | `channel_id` | `source_ref` 必须可解析为 canonical numeric Telegram channel id，再用 `Channel.tg_id` 查 `Channel.id` |
| `source_message_id` | `tg_message_id` | 必须可转换为 int |
| 选中正文字段 | `raw_text` | 判空使用 `strip()`，实际 `raw_text` 保留原始选中字段 |
| `raw_payload` | `raw_payload` | 原样传入，但报告不得输出完整内容 |
| `published_at` | `published_at` | 原样传入 |
| 无 | `raw_media_refs` | P6-2E 固定为 `None` |

P6-2E 不从 `raw_payload` 提取媒体，也不下载媒体。

正文规则：

```text
text.strip() 非空
-> 选择 text
否则 caption.strip() 非空
-> 选择 caption
否则
-> empty_content
```

判空使用 `strip()`，实际写入 `RawMessage.raw_text` 时保留原始选中字段，避免无意修改原始证据。

## Telegram Channel ID 规范

P6-2E 必须固定 Telegram channel id 的 canonical form，禁止在 boundary 中猜测多种形式。

常见形式：

```text
Telegram entity/channel id: 1234567890
Peer/channel marked id:     -1001234567890
```

项目必须只采用一种形式作为 `Channel.tg_id` 规范。P6-2E 第一版锁定为：

```text
CHANNEL_ID_CANONICAL_FORM:
- telethon_marked_peer_id
- 即 Telethon NewMessage event.chat_id / telethon.utils.get_peer_id(entity) 形式
- 示例：-1001234567890
- IncomingMessage.source_ref 必须是这个形式的十进制 numeric ref
- Channel lookup 只按 canonical tg_id 查询
- 禁止同时尝试正数和 -100 形式
- ID 规范由单一 canonicalization helper 负责
```

因此 `Channel.tg_id` 必须存储 `-100...` marked peer id。裸 Telegram entity/channel id，例如 `1234567890`，在 P6-2E boundary 中固定视为非 canonical，不做猜测转换。

当前来源约束：

```text
真实监听事件:
Telethon NewMessage event.chat_id
-> IncomingMessage.source_ref
-> canonicalize_source_channel_id(source_ref)
-> Channel.tg_id

一次性频道解析:
telethon.utils.get_peer_id(entity)
-> resolved numeric_channel_id
-> Channel.tg_id
```

这意味着监听别人的频道时，项目不要求用户手动知道裸 entity id。只要账号有权访问该公开/私有频道，受控 resolver 可以通过 `t.me` / `@username` 解析出 `-100...`，监听事件也会提供同一 canonical form。

错误示例：

```text
source_ref 是裸数字 1234567890
但 boundary 同时猜测 1234567890 和 -1001234567890
-> 频道 ID 语义被隐藏
```

正确示例：

```text
source_ref
-> canonicalize_source_channel_id(source_ref)
-> ChannelRepository.get_by_tg_id(canonical_tg_id)
```

`canonicalize_source_channel_id()` 稳定返回：

```text
valid:
- canonical_tg_id
- channel_id_canonical_form = telethon_marked_peer_id

invalid_source_ref:
- SOURCE_REF_NOT_NUMERIC
- SOURCE_REF_OUT_OF_RANGE
- SOURCE_REF_NOT_CANONICAL
```

## Channel 约束

P6-2E 第一版不自动创建 Channel。

原因：

- 自动创建 Channel 会引入频道生命周期语义
- 需要定义 name / username / status / rule_profile
- 可能绕过已有频道治理
- 会扩大 P6-2E 范围

固定规则：

```text
IncomingMessage.source_ref
-> canonical numeric tg_id
-> Channel.tg_id must already exist
-> use Channel.id for RawMessageCreate.channel_id
```

找不到 Channel 时返回：

```text
channel_not_registered
```

后续如需自动注册 Channel，必须单独设计。

## 结果模型

建议结果 DTO：

```text
IncomingIngestionResult
- status
- masked_source_ref: str
- masked_source_message_id: str | null
- raw_message_id: int | null
- error_code: str | null
- report_desensitized: yes
- database_accessed: yes
- parser_called: no
- normalizer_called: no
- dedup_called: no
- notification_sent: no
- media_downloaded: no
- history_backfill_called: no
- raw_event_persisted: no
```

`status` 使用有限枚举，不再额外引入自由文本 reason：

```text
stored
duplicate
channel_not_registered
invalid_source_ref
invalid_message_id
empty_content
ingest_failed
```

不要使用自由文本作为业务判断依据。

建议不要把以下数据放入结果：

- 完整 `raw_text`
- 完整 `raw_payload`
- 完整频道名称或 username
- DB 异常文本
- SQL
- session 信息

`raw_message_id` 是否输出可按项目脱敏规范决定。若报告不需要定位数据库记录，也可以只输出：

```text
raw_message_created: yes/no
```

## 幂等判定

当前 `RawMessageService.ingest()` 返回 `RawMessageIngestResult`，其中 `disposition` 直接说明本次是新建还是重复。

P6-2E 禁止采用写入前 precheck 推测 stored / duplicate：

```text
existing = RawMessageService.get_by_channel_and_msg_id(channel_id, tg_message_id)
raw_message = RawMessageService.ingest(data)

if existing is None:
  reason = stored
else:
  reason = duplicate
```

这个方案在并发下存在竞态：

```text
请求 A: precheck 不存在
请求 B: precheck 不存在
请求 A: ingest 创建
请求 B: ingest 命中重复
```

此时 A、B 的 `existed_before` 都是 no，boundary 可能把两次都报告为 stored。

正确设计：

```text
IngestionBoundary
-> RawMessageService.ingest(data)
-> service 返回 RawMessageIngestResult(disposition=stored|duplicate)
-> boundary 根据 disposition 映射 IncomingIngestionResult.status
```

因此 P6-2E boundary 实现前置条件已经满足：

```text
RawMessageService.ingest()
可靠返回 stored / duplicate disposition
```

结果必须来自唯一约束或原子写入结果，而不是写入前推测。并发提交相同消息时必须保证：

```text
一个 stored
其余 duplicate
```

这是 P6-2E 幂等验收核心，不是后续 hardening 项。

## 输入校验顺序

固定顺序：

```text
1. 校验 source_ref
2. canonicalize Telegram channel id
3. 校验 source_message_id
4. 选择正文并计算 content_text
5. 校验非空内容
6. 打开 application session
7. lookup Channel.tg_id
8. build RawMessageCreate
9. RawMessageService.ingest()
10. commit / rollback 按 service 事务约定完成
11. 映射稳定结果
12. close session
```

无效输入不得打开数据库 session。

## 事务边界

P6-2E 固定：

```text
application boundary owns session lifecycle
```

当前 `RawMessageService.ingest()` 已自行 commit。P6-2E 设计必须记录这一实际所有权，不能写成 boundary 和 service 都可能提交。

第一版语义：

```text
stored / duplicate
-> 按 RawMessageService.ingest() 事务约定完成

invalid_source_ref / invalid_message_id / empty_content
-> 不打开 session
-> 不写入

channel_not_registered
-> 打开 session 查询 Channel
-> 不调用 RawMessageService
-> 不写入 RawMessage

service / DB 异常
-> rollback 可用 session
-> 返回 ingest_failed
-> 不向 monitor 暴露原始异常
```

## 错误分类

稳定错误码：

```text
SOURCE_REF_NOT_NUMERIC
SOURCE_REF_OUT_OF_RANGE
SOURCE_REF_NOT_CANONICAL
MESSAGE_ID_NOT_INTEGER
MESSAGE_ID_NON_POSITIVE
CONTENT_EMPTY
CHANNEL_TG_ID_NOT_FOUND
RAW_MESSAGE_INGEST_ERROR
DATABASE_ERROR
INVALID_SERVICE_RESULT
```

建议映射：

| status | error_code |
|--------|------------|
| `invalid_source_ref` | `SOURCE_REF_NOT_NUMERIC` |
| `invalid_source_ref` | `SOURCE_REF_OUT_OF_RANGE` |
| `invalid_source_ref` | `SOURCE_REF_NOT_CANONICAL` |
| `invalid_message_id` | `MESSAGE_ID_NOT_INTEGER` |
| `invalid_message_id` | `MESSAGE_ID_NON_POSITIVE` |
| `empty_content` | `CONTENT_EMPTY` |
| `channel_not_registered` | `CHANNEL_TG_ID_NOT_FOUND` |
| `ingest_failed` | `RAW_MESSAGE_INGEST_ERROR` |
| `ingest_failed` | `DATABASE_ERROR` |
| `ingest_failed` | `INVALID_SERVICE_RESULT` |

不要把 SQLAlchemy 或数据库驱动异常名称直接写入报告。

错误报告不得包含：

- 完整消息正文
- 完整 raw_payload
- 完整 username
- invite link
- URL query
- token
- raw Telethon event

## Monitor 集成方式

P6-2E 只允许在 runtime 中引入抽象 handoff 点：

```text
event
-> IncomingMessage
-> watchlist filter
-> if matched:
     ingestion_boundary.ingest_incoming(incoming)
-> counters/report
```

是否只对 matched 消息入库，需要在 P6-2E 实现前固定。建议第一版只入库 matched 消息：

- P6-2B 已定义 watchlist 是纯过滤边界
- P6-2D runtime 只统计 matched / rejected
- 入库未关注标题会增加无意义 RawMessage

因此建议：

```text
matched -> call ingestion boundary
rejected -> do not ingest
```

但这意味着 RawMessage 只保留关注资源的原始证据，不保留所有频道消息。若后续需要全量审计，必须单独设计。

## Runtime 计数扩展

P6-2E 可在 runtime summary / heartbeat 中增加：

```text
ingest_attempt_total
ingest_stored_total
ingest_duplicate_total
ingest_rejected_total
ingest_failed_total
last_ingest_at
last_ingest_error_code
production_ingest_enabled: yes
```

但仍必须保留：

```text
parser_called=no
normalizer_called=no
dedup_called=no
notification_sent=no
media_downloaded=no
history_backfill_called=no
```

## 必测场景

Application boundary：

| 场景 | 预期 |
|------|------|
| 有效 `IncomingMessage` | `stored` |
| 同一消息重复调用 | `duplicate` |
| `source_ref` 不是 numeric | `invalid_source_ref` |
| `source_message_id` 不能转 int | `invalid_message_id` |
| `Channel.tg_id` 不存在 | `channel_not_registered` |
| `text` 和 `caption` 都为空 | `empty_content` |
| `text` 有值且 `caption` 有值 | `raw_text` 使用 `text` |
| `text` 只有空白且 `caption` 有值 | `raw_text` 使用 `caption` |
| `raw_payload` 存入 | 报告不输出完整 payload |
| `RawMessageService.ingest()` 抛错 | `ingest_failed` |
| `source_ref` canonical form 与 `Channel.tg_id` 一致 | 可查到 Channel |
| `source_ref` 合法但不符合项目 canonical 规范 | `invalid_source_ref` |
| `source_message_id` 为 0 或负数 | `invalid_message_id` |
| `content_text` 仅包含空白字符 | `empty_content` |
| service 返回非法 disposition | `ingest_failed` / `INVALID_SERVICE_RESULT` |
| 并发提交相同消息 | 一个 `stored`，其余 `duplicate` |
| `channel_not_registered` | 不调用 `RawMessageService` |
| 输入校验失败 | 不打开 DB session |
| DB 异常 | rollback 且 session 正常关闭 |
| 结果和日志 | 不包含 `raw_text` / `raw_payload` |

Monitor handoff：

| 场景 | 预期 |
|------|------|
| filter matched | 调用 ingestion boundary |
| filter rejected | 不调用 ingestion boundary |
| ingestion stored | runtime 计数 stored |
| ingestion duplicate | runtime 计数 duplicate |
| ingestion failed | runtime 继续运行 |
| draining 中事件到达 | 不调用 ingestion boundary |

Boundary：

| 场景 | 预期 |
|------|------|
| P6-2E 运行 | 不调用 Parser |
| P6-2E 运行 | 不调用 Normalizer |
| P6-2E 运行 | 不调用 Dedup |
| P6-2E 运行 | 不发布 EventBus |
| P6-2E 运行 | 不发送 Bot 通知 |
| P6-2E 观察到媒体消息 | 不下载媒体 |
| P6-2E 重连 | 不 history backfill |

## 验收报告

建议 P6-2E summary：

```text
P6-2E_INGESTION_BOUNDARY_RESULT:
- boundary_schema: pass/fail
- input_dto: IncomingMessage
- output_dto: IncomingIngestionResult
- channel_lookup_mode: existing_channel_only
- channel_id_canonical_form:
- channel_auto_create: no
- ingest_only_matched: yes
- sample_count:
- stored_count:
- duplicate_count:
- rejected_count:
- failed_count:
- invalid_source_ref_count:
- invalid_message_id_count:
- channel_not_registered_count:
- empty_content_count:
- raw_payload_full_output: no
- database_accessed: yes
- parser_called: no
- normalizer_called: no
- dedup_called: no
- eventbus_published: no
- notification_sent: no
- media_downloaded: no
- history_backfill_called: no
- runtime_direct_db_access: no
- monitor_direct_rawmessage_service_call: no
- stored_duplicate_from_service_disposition: yes
- concurrent_idempotency_passed: yes/no
- blockers:
  - ...
```

## 完成标准

P6-2E 通过时，只能得出：

- `IncomingMessage` 可以通过 application boundary 幂等写入 `RawMessage`
- Channel 必须预先存在
- Monitor 与 DB 访问保持隔离
- 入库结果可脱敏观测

P6-2E 通过时，不能得出：

- 消息可解析为资源
- 资源可归一化
- 资源可去重
- 可触发 EventBus
- 可发送 Bot 通知
- 生产 pipeline 已完整接通

下一阶段：

```text
P6-2F:
RawMessage
-> Parser
-> Normalizer
-> Dedup
```
