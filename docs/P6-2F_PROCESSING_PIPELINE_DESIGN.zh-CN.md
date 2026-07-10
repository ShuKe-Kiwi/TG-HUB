# P6-2F Processing Pipeline 设计

> 状态：设计已按 required changes 修正
> 范围：RawMessage -> Parser / Normalizer / Dedup 应用编排
> 不包含：Monitor 改造、EventBus 发布、Bot 通知、队列 worker、媒体下载、history backfill

## 阶段位置

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2E | Monitor -> RawMessage ingestion boundary | 已完成 |
| P6-2F | RawMessage -> Parser / Normalizer / Dedup 编排 | 本文档定义 |
| P6-2G | EventBus / Bot 查询通知接入 | 未开始 |

P6-2F 只能回答：

> 一个已入库的 `RawMessage` 是否可以通过受控应用服务完成解析、归一化和资源去重归并？

P6-2F 不能回答：

> 是否已发送通知、是否已进入异步队列、是否已完成媒体下载、是否支持 history backfill。

## 固定边界

P6-2F 接通的是 RawMessage 后处理应用边界：

```text
RawMessage.id
-> RawMessageProcessingBoundary.process_raw_message(raw_message_id)
-> application-owned AsyncSession lifecycle
-> RawMessageService.parse_and_persist(raw_message_id)
-> RawMessageService.dedup_and_persist(raw_message_id, event_bus=None)
-> stable processing result
```

Monitor 仍然只负责传输适配和 handoff，不直接调用 Parser / Normalizer / Dedup。

允许的范围：

- 新增 processing application boundary
- 读取一个已存在的 `RawMessage`
- 调用现有 `RawMessageService.parse_and_persist()`
- 调用现有 `RawMessageService.dedup_and_persist(event_bus=None)`
- 根据 RawMessage 三状态返回稳定结果
- 统计 parsed / parse_failed / dedup_new / dedup_matched / dedup_skipped / dedup_failed
- 结果报告脱敏
- 记录当前 service 自行 commit 的事务事实
- 第一版接受按 ID 重复查询，不修改现有 service contract

禁止的范围：

- `monitor/runtime.py` 直接调用 Parser
- `monitor/runtime.py` 直接调用 Normalizer
- `monitor/runtime.py` 直接调用 Dedup
- `monitor/runtime.py` 直接访问 DB / Repository / `RawMessageService`
- 发布 EventBus
- 发送 Bot 通知
- 创建队列 worker / 定时任务 / 长期后台 processor
- 自动重试 parse_failed 消息
- history backfill
- 媒体下载
- 修改 Parser / Normalizer / Dedup 业务规则
- 新增 AI / 模糊匹配 / 跨语言映射

## 现有接口事实

当前 `RawMessage` 初始入库状态：

```text
ingest_status = stored
parse_status = parse_pending
dedup_status = dedup_pending
parse_attempts = 0
```

当前解析接口：

```text
RawMessageService.parse_and_persist(raw_msg_id) -> RawMessage
```

现有行为：

- 找不到 RawMessage 时抛 `LookupError`
- 每次调用递增 `parse_attempts`
- 成功时写入 `parsed_data`
- 成功时设置 `parse_status=parsed`
- 成功时写入 parser / rule version
- 空结果时设置 `parse_status=parse_failed`
- parser 异常时设置 `parse_status=parse_failed`
- 失败详情写入 `last_parse_error`
- 方法内部 commit

当前去重接口：

```text
RawMessageService.dedup_and_persist(
  raw_msg_id,
  event_bus=None
) -> RawMessage
```

现有行为：

- `parsed_data is None or []` -> `dedup_status=skipped`
- 至少一个新 Resource -> `dedup_status=new`
- 全部命中已有 Resource -> `dedup_status=matched`
- Dedup 异常 -> rollback 后保持 `dedup_status=dedup_pending`
- 内部执行 `deserialize_parsed_resource()`
- 内部执行 `normalize_resource()`
- 内部调用 `DedupService.dedup()`
- 方法内部 commit
- `event_bus` 非空时可发布 Resource 事件

P6-2F 第一版必须显式传入：

```text
event_bus=None
```

因此 P6-2F 不发布事件。EventBus / Bot 属于 P6-2G。

## Application Boundary 形态

正式应用边界名称固定为：

```text
class RawMessageProcessingBoundary:
    async def process_raw_message(
        self,
        raw_message_id: int,
    ) -> RawMessageProcessingResult:
        ...
```

不得再使用 `ProcessingPipelineBoundary` 作为正式名称，避免暗示这是通用 pipeline engine。

边界内部允许：

```text
validate raw_message_id
-> open application-owned AsyncSession
-> RawMessageService.get_by_id(raw_message_id)
-> decide processing path by parse_status / dedup_status
-> RawMessageService.parse_and_persist(raw_message_id)
-> if parse_status == parsed:
     RawMessageService.dedup_and_persist(raw_message_id, event_bus=None)
-> map final states to RawMessageProcessingResult
-> close AsyncSession
```

边界对外只暴露稳定 DTO，不暴露 ORM。

P6-2F 第一版明确接受重复 `get_by_id()`：

```text
RawMessageProcessingBoundary
-> RawMessageService.get_by_id(raw_message_id)
-> RawMessageService.parse_and_persist(raw_message_id)
   -> service 内部再次按 id 查询
-> RawMessageService.dedup_and_persist(raw_message_id)
   -> service 内部再次按 id 查询
```

原因：

- 不修改现有 service 接口
- boundary 只负责编排和状态映射
- 风险低，便于独立验收

不得在 P6-2F 第一版中扩展 service contract 来复用查询结果。

## 状态机

第一版固定为保守状态机：

```text
raw_message_not_found
invalid_raw_message_id
already_processed
parse_failed
dedup_skipped
dedup_new
dedup_matched
dedup_failed
processing_failed
```

合法状态组合固定为：

| parse_status | dedup_status | 处理 |
|--------------|--------------|------|
| `parse_pending` | `dedup_pending` | 执行 parse，再按 parse 结果决定是否 dedup |
| `parsed` | `dedup_pending` | 只执行 dedup |
| `parsed` | `new` | `already_processed` |
| `parsed` | `matched` | `already_processed` |
| `parsed` | `skipped` | `already_processed` |
| `parse_failed` | `dedup_pending` | `parse_failed`，不自动重试 |
| 其他组合 | 任意 | `processing_failed / RAW_MESSAGE_INVALID_STATE` |

非法组合示例：

```text
parse_failed + new
parse_failed + matched
parse_pending + matched
parse_pending + new
parsed + unknown_status
```

遇到非法组合时不得继续调用 service 猜测修复。

生产结果 DTO 第一版固定为：

```text
RawMessageProcessingResult
- status
- raw_message_id: int | null
- parse_status: str | null
- dedup_status: str | null
- parse_attempts: int | null
- parsed_resource_count: int
- error_code: str | null
- parse_executed: bool
- dedup_executed: bool
```

不要放入生产 DTO：

- `database_accessed`
- `parser_called`
- `normalizer_called`
- `dedup_called`
- `eventbus_published`
- `notification_sent`
- `media_downloaded`
- `history_backfill_called`
- `report_desensitized`
- `resource_created_count`
- `resource_merged_count`

这些属于测试断言、运行报告或阶段验收，不是领域结果。

`resource_created_count` / `resource_merged_count` 在第一版固定不可用。当前 service 只返回 `RawMessage`，无法从聚合 `dedup_status` 可靠推断多资源消息中的 created / merged 精确数量。

例如一条 RawMessage 解析出 3 个资源：

```text
1 个 new
2 个 matched
```

最终 `dedup_status` 可能只是：

```text
new
```

因此 P6-2F v1 不从 aggregate RawMessage.dedup_status 反推 per-resource created/merged counts，也不额外查询 `ResourceSource` 反推。

稳定错误码：

```text
RAW_MESSAGE_ID_NOT_INTEGER
RAW_MESSAGE_ID_NON_POSITIVE
RAW_MESSAGE_NOT_FOUND
RAW_MESSAGE_INVALID_STATE
RAW_MESSAGE_PARSE_ERROR
RAW_MESSAGE_DEDUP_ERROR
RAW_MESSAGE_PROCESSING_ERROR
INVALID_SERVICE_RESULT
DATABASE_ERROR
```

错误映射优先级固定为：

| 层级 | 错误码 | 规则 |
|------|--------|------|
| 输入层 | `RAW_MESSAGE_ID_NOT_INTEGER` | 不能转 int，或传入 bool |
| 输入层 | `RAW_MESSAGE_ID_NON_POSITIVE` | 转换后 `<= 0` |
| 查询层 | `RAW_MESSAGE_NOT_FOUND` | 查询结果为空，或 `get_by_id` 抛 `LookupError` |
| 查询层 | `DATABASE_ERROR` | SQLAlchemy / DB 异常 |
| 状态层 | `RAW_MESSAGE_INVALID_STATE` | 当前 RawMessage 状态组合非法 |
| 状态层 | `INVALID_SERVICE_RESULT` | service 返回错误对象、错误 id 或未知状态 |
| 处理层 | `RAW_MESSAGE_PARSE_ERROR` | parse 后得到 `parse_failed`，或 parse service 抛异常 |
| 处理层 | `RAW_MESSAGE_DEDUP_ERROR` | dedup service 抛异常，或 dedup 调用后仍为 `dedup_pending` |
| 处理层 | `RAW_MESSAGE_PROCESSING_ERROR` | 未分类未知异常 |

`parse_and_persist()` 按现有行为吞掉 parser 异常并返回 `parse_failed` 时，boundary 不应再次要求 Python exception，只需稳定映射：

```text
status=parse_failed
error_code=RAW_MESSAGE_PARSE_ERROR
```

## 处理规则

### 输入校验

```text
raw_message_id 不能转 int
-> invalid_raw_message_id / RAW_MESSAGE_ID_NOT_INTEGER
-> 不打开 DB session

raw_message_id <= 0
-> invalid_raw_message_id / RAW_MESSAGE_ID_NON_POSITIVE
-> 不打开 DB session
```

Python 中 `bool` 是 `int` 子类，因此必须显式拒绝：

```text
raw_message_id=True / False
-> invalid_raw_message_id / RAW_MESSAGE_ID_NOT_INTEGER
-> 不打开 DB session
```

允许 strip 后转换：

```text
raw_message_id=" 12 "
-> raw_message_id=12
```

### RawMessage 不存在

```text
RawMessageService.get_by_id(raw_message_id) is None
-> raw_message_not_found / RAW_MESSAGE_NOT_FOUND

RawMessageService.get_by_id(raw_message_id) raises LookupError
-> raw_message_not_found / RAW_MESSAGE_NOT_FOUND
```

### 已完成消息

如果当前状态已经是终态：

```text
parse_status=parsed
dedup_status in {new, matched, skipped}
-> already_processed
```

第一版不自动重跑已完成消息。强制重跑必须单独设计。

### 历史 parse_failed

```text
parse_status=parse_failed
dedup_status=dedup_pending
-> parse_failed / RAW_MESSAGE_PARSE_ERROR
-> 不调用 parse_and_persist()
-> 不调用 dedup_and_persist()
```

第一版不自动重试历史 `parse_failed` 消息。

### 新消息处理

典型路径：

```text
parse_status=parse_pending
dedup_status=dedup_pending
-> parse_and_persist()
-> if parse_status == parsed:
     dedup_and_persist(event_bus=None)
-> map final result
```

### Parse 失败

```text
parse_and_persist() 后 parse_status=parse_failed
-> parse_failed
-> 不调用 dedup_and_persist()
```

P6-2F 第一版不自动重试历史 `parse_failed` 消息。后续如需重试策略，应单独设计：

```text
P6-2F-Retry:
- retry limit
- retry backoff
- parser version change retry
- dead-letter / manual retry
```

### Dedup skipped

```text
parse_status=parsed
但 parsed_data is None or []
-> dedup_and_persist(event_bus=None)
-> dedup_status=skipped
-> dedup_skipped
```

### Dedup 成功

```text
dedup_status=new
-> dedup_new

dedup_status=matched
-> dedup_matched
```

`new` 表示至少一个新 Resource 创建；`matched` 表示全部资源归并到已有 Resource。

### Dedup 失败

当前 `dedup_and_persist()` 在异常时会 rollback，并将 `dedup_status` 保持为 `dedup_pending`。

P6-2F boundary 固定判断顺序：

```text
调用 dedup_and_persist(event_bus=None)
-> 验证返回值是 RawMessage 或预期 service result
-> 验证返回 raw_message.id == raw_message_id
-> 验证 parse_status == parsed
-> 检查 dedup_status
```

结果映射：

```text
dedup_status=new
-> dedup_new

dedup_status=matched
-> dedup_matched

dedup_status=skipped
-> dedup_skipped

dedup_status=dedup_pending
-> dedup_failed / RAW_MESSAGE_DEDUP_ERROR

其他 dedup_status
-> processing_failed / INVALID_SERVICE_RESULT
```

如果 dedup service 抛出异常：

```text
-> dedup_failed / RAW_MESSAGE_DEDUP_ERROR
```

如果 service 返回错误 RawMessage id、错误 parse_status、未知 dedup_status 或非法对象：

```text
-> processing_failed / INVALID_SERVICE_RESULT
```

报告不得输出异常原文、SQL、raw_text 或 raw_payload。

### 非法状态组合

任何不在合法状态组合表中的当前状态都固定返回：

```text
processing_failed / RAW_MESSAGE_INVALID_STATE
```

不得调用 parse 或 dedup 猜测修复。

## 事务边界

P6-2F 固定：

```text
application boundary owns AsyncSession lifecycle
RawMessageService.parse_and_persist() 当前自行 commit
RawMessageService.dedup_and_persist() 当前自行 commit / rollback
```

因此第一版不得伪装成一个外层大事务。

实际提交边界：

```text
parse_and_persist()
-> commit parser fields

dedup_and_persist()
-> commit resource / dedup fields
```

如果 parse 成功但 dedup 失败，允许出现：

```text
parse_status=parsed
dedup_status=dedup_pending
```

这是可恢复状态，不应回滚 parse 成功结果。

准确职责：

```text
Boundary:
- 创建 AsyncSession
- 关闭 AsyncSession

Service:
- parse_and_persist 自行 commit
- dedup_and_persist 自行 commit / rollback
```

## 并发与幂等

P6-2F 第一版不引入队列锁、分布式锁或 worker claim 字段。

要求：

- 同一 `raw_message_id` 的重复串行调用必须安全
- 已完成消息默认返回 `already_processed`
- Dedup 依赖既有唯一约束和 `DedupService` 幂等
- 不承诺多进程同时处理同一 RawMessage 的 parse_attempts 精确值

后续如需并发 worker，应单独设计：

```text
P6-2F-Worker:
- processor claim
- processing_status
- locked_at
- locked_by
- stale lock recovery
- max attempts
- dead letter
```

## 与 P6-2E 的关系

P6-2E 已证明：

```text
IncomingMessage
-> RawMessage stored / duplicate
```

P6-2F 接收的是：

```text
RawMessage.id
```

P6-2F 不重新校验 Telegram channel id，不重新选择 text/caption，不重新写 RawMessage 原始证据。

后续装配可以是：

```text
IngestionBoundary returns stored(raw_message_id)
-> RawMessageProcessingBoundary.process_raw_message(raw_message_id)
```

但 P6-2F 第一版不要求修改 monitor runtime。若要让 monitor 自动触发 P6-2F，应单独设计 P6-2F handoff，不把这部分混进 processing boundary。

## 与 P6-2G 的关系

P6-2F 明确不发布 EventBus。

虽然 `RawMessageService.dedup_and_persist()` 已支持可选 `event_bus`，P6-2F 第一版必须传：

```text
event_bus=None
```

P6-2G 才允许定义：

```text
dedup result
-> ResourceCreated / ResourceMerged
-> EventBus
-> Bot notification / query refresh
```

## 实现文件边界

建议 P6-2F 实现放在 application 层，例如：

```text
backend/app/application/raw_message_processing.py
backend/app/application/schema.py
backend/tests/application/test_raw_message_processing.py
```

如果项目最终选择其他目录，也必须保持语义一致：这是应用编排层，不是传输层，也不是单一领域服务。

不要放到：

```text
monitor/
parser/
normalizer/
resource/
rawmessage/service.py
```

原因：

- Monitor 是传输适配层
- Parser / Normalizer / Dedup 是领域能力
- `RawMessageService` 已承担单消息 parse / dedup 持久化能力
- P6-2F 需要的是跨服务状态编排和稳定结果映射

## 必测场景

Application boundary：

| 场景 | 预期 |
|------|------|
| `raw_message_id` 不是整数 | `invalid_raw_message_id`，不打开 DB |
| `raw_message_id` 是 bool | `invalid_raw_message_id / RAW_MESSAGE_ID_NOT_INTEGER`，不打开 DB |
| `raw_message_id=" 12 "` | strip 后按 `12` 处理 |
| `raw_message_id <= 0` | `invalid_raw_message_id`，不打开 DB |
| RawMessage 不存在 | `raw_message_not_found` |
| parse_pending + 可解析文本 | parse called，dedup called |
| parse_pending + 无资源文本 | `parse_failed`，dedup 不调用 |
| Parser 抛异常 | `parse_failed`，dedup 不调用 |
| parsed + dedup_pending | 只调用 dedup |
| parsed + dedup new | `dedup_new` |
| parsed + dedup matched | `dedup_matched` |
| parsed + dedup skipped | `dedup_skipped` |
| DedupService 抛异常 | `dedup_failed`，不泄露异常原文 |
| service 返回错误 RawMessage ID | `processing_failed / INVALID_SERVICE_RESULT` |
| service 返回未知 parse/dedup 状态 | `processing_failed / INVALID_SERVICE_RESULT` |
| parse_failed + dedup final 非法组合 | `processing_failed / RAW_MESSAGE_INVALID_STATE` |
| already parsed + dedup final | `already_processed`，不重复 parse/dedup |
| 重复串行调用同一 RawMessage | 第二次 `already_processed` |
| 多资源 parsed_data | 可创建多个 ResourceSource |
| 结果报告 | 不包含 raw_text / raw_payload / parsed_snapshot |
| P6-2F 运行 | `eventbus_published=no` |
| P6-2F 运行 | `notification_sent=no` |
| P6-2F 运行 | `media_downloaded=no` |

边界防回归：

| 场景 | 预期 |
|------|------|
| Monitor runtime | 不直接调用 Parser |
| Monitor runtime | 不直接调用 Normalizer |
| Monitor runtime | 不直接调用 Dedup |
| Monitor runtime | 不直接访问 DB |
| Processing boundary | 不发送 Bot 通知 |
| Processing boundary | 不 history backfill |

## 验收报告

建议 P6-2F summary：

```text
P6-2F_PROCESSING_RESULT:
- boundary_schema: pass/fail
- input: raw_message_id
- output: RawMessageProcessingResult
- raw_message_id:
- status:
- parse_status:
- dedup_status:
- parse_attempts:
- parsed_resource_count:
- parser_called: yes/no
- normalizer_called: yes/no
- dedup_called: yes/no
- database_accessed: yes/no
- eventbus_published: no
- notification_sent: no
- media_downloaded: no
- history_backfill_called: no
- raw_text_full_output: no
- raw_payload_full_output: no
- already_processed_count:
- parse_failed_count:
- dedup_new_count:
- dedup_matched_count:
- dedup_skipped_count:
- dedup_failed_count:
- blockers:
  - ...
```

说明：

- `parser_called` / `normalizer_called` / `dedup_called` 属于测试 spy/mock 或阶段验收观察字段。
- `database_accessed` / `eventbus_published` / `notification_sent` / `media_downloaded` / `history_backfill_called` 属于运行报告或阶段验收字段。
- 这些字段不得进入第一版 `RawMessageProcessingResult` 生产 DTO。
- `resource_created_count` / `resource_merged_count` 第一版不统计，不在验收报告中伪造。

## 完成标准

P6-2F 通过时，只能得出：

- 已入库 RawMessage 可以通过应用服务完成 Parser / Normalizer / Dedup 编排
- RawMessage 三状态能稳定推进
- Work / Resource / ResourceLink / ResourceSource 可由 DedupService 创建或归并
- 编排结果可脱敏观测

P6-2F 通过时，不能得出：

- Monitor 已自动触发完整处理链
- EventBus 已发布资源事件
- Bot 已发送通知
- 支持后台队列和失败自动重试
- 支持媒体下载或历史回填

下一阶段：

```text
P6-2G:
Dedup result
-> EventBus
-> Bot 查询通知接入
```
