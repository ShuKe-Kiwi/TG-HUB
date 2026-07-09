# TG-HUB 实施进度

> 架构基准：docs/ARCHITECTURE.md V2.2
> 最后更新：2026-07-09

---

## 阶段总览

| Phase | 目标 | 状态 | 测试 |
|-------|------|------|------|
| P1 | 数据模型 + RawMessage 入库 | ✅ 完成 | 7/7 |
| P2-A | Parser DTO + 样本测试框架 | ✅ 完成 | 14/14 |
| P2-B | Parser Pipeline 完整实现 | ✅ 完成 | 72/72 |
| P2-C | Parser 结果写回 + 失败状态记录 | ✅ 完成 | 5/5 |
| P3-A | Normalizer + Fingerprint 纯函数 | ✅ 完成 | 48/48 |
| P3-B | Parser + Normalizer 纯函数链路联调 | ✅ 完成 | 29/29 |
| P4-A | Resource Registry 模型 + Repository | ✅ 完成 | 6/6 |
| P4-B | Dedup / Merge Service | ✅ 完成 | 17/17 |
| P5-A | 事件契约 + InMemory EventBus | ✅ 完成 | 8/8 |
| P5-B | Commit 后发布资源事件 | ✅ 完成 | 6/6 |
| P5-C | Resource 查询 ViewModel | ✅ 完成 | 14/14 |
| P5-D | Telegram Bot 查询与通知适配器 | ✅ 完成 | 29/29 |
| P5-E | 应用装配与 MVP-B E2E 验收 | ✅ 完成 | 12/12 |
| P6-2D | 长期 monitor runtime 生命周期 | ✅ 第一版完成 | 6/6 |
| P6-2E-1 | RawMessage ingest disposition 前置改造 | ✅ 完成 | 8/8 |

---

## P6-2E-1 详细记录

### 完成日期
2026-07-09

### 实现内容

- 新增 `RawMessageIngestResult`
- `RawMessageService.ingest()` 返回 `raw_message + disposition`
- 新建消息返回 `disposition=stored`
- 已存在消息返回 `disposition=duplicate`
- 并发唯一约束冲突时 rollback 后重新读取胜出记录，并返回 `duplicate`
- 兼容既有调用点，可继续通过返回值访问 `RawMessage` 字段

**P6-2E-1 合计：8 个 RawMessage ingest 测试，全部通过**

### 边界确认

- ✅ 只改 RawMessage ingest 前置能力
- ✅ 未实现 `IngestionBoundary.ingest_incoming()`
- ✅ 未让 Monitor 访问 DB / Repository / RawMessageService
- ✅ 未接 Parser / Normalizer / Dedup
- ✅ 未发布 EventBus
- ✅ 未发送 Bot 通知
- ✅ 未下载媒体或回溯历史消息

---

## P6-2D 详细记录

### 完成日期
2026-07-09

### 实现内容

- 新增 `MonitorRuntime` 作为唯一 runtime owner
- 显式有限状态机：`created -> starting -> preflight -> resolving_channels -> connecting -> registering_handler -> listening -> draining -> stopped`
- 只维护一个 Telethon-compatible client
- 只注册一个 `NewMessage` handler，覆盖全部 resolved channel ids
- 新增 `MonitorRuntimeConfig`，接入 P6-2D runtime 配置项
- 新增 heartbeat payload，包含 liveness / readiness、运行计数、错误码和边界 no flags
- 新增 graceful shutdown：stop、drain、remove handler、disconnect、final summary
- 新增 reconnect/backoff 路径：disconnect 后先移除旧 handler，再连接并重新注册单 handler
- 新增稳定错误报告 `MonitorRuntimeError`
- 新增脱敏 final summary `MonitorRuntimeSummary`

**P6-2D 合计：6 测试，全部通过**

### 边界确认

- ✅ Runtime 收到消息后只执行 `IncomingMessage -> watchlist filter -> counters/heartbeat/summary`
- ✅ 不访问数据库
- ✅ 不创建 `AsyncSession`
- ✅ 不调用 `RawMessageService`
- ✅ 不运行 Parser / Normalizer / Dedup
- ✅ 不发送 Bot 通知
- ✅ 不下载媒体
- ✅ 不回溯历史消息
- ✅ 不持久化 raw event
- ❌ 未实现生产入库
- ❌ 未接 P6-2E ingestion/application boundary

### 新增文件

| 文件 | 用途 |
|------|------|
| `backend/app/modules/monitor/runtime.py` | P6-2D 长期 monitor runtime 生命周期 |
| `backend/tests/monitor/test_runtime.py` | P6-2D 生命周期测试 |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/config.py` | 增加 P6-2D runtime 配置项 |
| `backend/app/modules/monitor/__init__.py` | 导出 runtime 类型与入口 |

---

## P5-E 详细记录

### 完成日期
2026-07-02

### 实现内容

- `main.py` 提供兼容既有导入的 `create_app()` 与全局 `app`
- lifespan 始终创建并暴露 `app.state.event_bus`
- Telegram 配置有效时注册 ResourceCreated / ResourceMerged 通知
- 每次通知使用独立短生命周期 Session 与 ResourceQueryService
- 新增 `/telegram/webhook`，secret 校验先于 body 与 Session
- 缺少 token / secret 或 chat ID 非法时安全禁用 Telegram
- `/health` 在 Telegram 禁用时保持正常
- FakeBotTransport E2E 锁定命令、通知、幂等与失败隔离闭环

**P5-E 合计：12 测试，全部通过**

### 边界确认

- ❌ 未注册 RawMessageFailed
- ❌ 未调用 setWebhook 或真实 Telegram API
- ❌ 未修改 Parser / Normalizer / Dedup / Query / Bot 业务逻辑
- ❌ 未新增 User / Subscription / Transfer
- ❌ 未新增数据库表、migration 或 index
- ❌ 未实现 Outbox / Redis / Kafka / 定时任务 / AI 查询
- ❌ 未进入 P6

---

## P5-D 详细记录

### 完成日期
2026-07-02

### 实现内容

- 新增仅保留 update/message/chat ID 与 text 的 Telegram 输入 DTO
- 新增注入式 `BotTransport` 与 `TelegramBotTransport`
- Token 普通构造函数显式注入，`from_env()` 只读取 `TELEGRAM_BOT_TOKEN`
- 新增 `/latest`、`/search`、`/resource`、`/help` 命令处理
- 新增 ResourceCreated / ResourceMerged 通知 handler
- HTML 统一由 formatter 生成，动态字段全部转义
- href 仅允许具有 host 的 HTTP / HTTPS URL
- 未授权 chat 静默返回，不查询也不发送
- 测试仅使用内存 FakeBotTransport，并封锁真实 HTTP

**P5-D 合计：29 测试，全部通过**

### 边界确认

- ❌ 未新增 FastAPI router / webhook route
- ❌ 未修改 main.py / lifespan
- ❌ 未调用或提供 setWebhook
- ❌ 未新增 User / Subscription / Transfer
- ❌ 未接 RawMessageService / DedupService
- ❌ 未实现 Bot 状态或数据库写入
- ❌ 未实现 Outbox / Redis / Kafka
- ❌ 未进入 P5-E

---

## P5-C 详细记录

### 完成日期
2026-07-02

### 实现内容

- 新增 `ResourceListItem`、`ResourceDetail`、`LinkView`、`SourceView`
- ViewModel 使用 Pydantic，从查询标量显式构造，不包含 ORM
- 新增 `latest()`、`search()`、`get_detail()` 三个只读查询
- Work / Resource 均按既有 `status=active` 过滤
- `latest()` 按 `last_seen_at DESC, id DESC` 稳定排序
- `search()` 使用参数绑定和 LIKE escape 处理 `\`、`%`、`_`
- 详情固定查询主体、Links、Sources，避免 N+1
- SourceView 通过 LEFT JOIN 映射现有 Channel 名称和用户名

**P5-C 合计：14 测试，全部通过**

### 只读与边界确认

- ✅ 仅执行 SELECT，不调用 flush / commit
- ✅ 查询前后四表计数及 Session new / dirty / deleted 不变
- ✅ 未暴露 ORM、relationship 或 parsed_snapshot
- ❌ 未修改 repository.py、RawMessageService 或 DedupService
- ❌ 未接 Telegram / EventBus / Notify
- ❌ 未新增 migration / index / table
- ❌ 未实现全文检索、模糊搜索、分页总数
- ❌ 未进入 P5-D / P5-E

---

## P5-B 详细记录

### 完成日期
2026-07-02

### 实现内容

- `RawMessageService.dedup_and_persist()` 支持可选 `EventBus`
- `DedupService` 保持 flush-only，commit 仍由 `RawMessageService` 负责
- `ResourceCreated` / `ResourceMerged` 仅在数据库 commit 成功后发布
- 完全幂等重放、skipped 和 commit 失败均不发布资源事件
- EventBus 发布异常只记录日志，不回滚已提交数据
- 本阶段不发布 `RawMessageFailed`

### 事件判定

| DedupResult | 事件 |
|-------------|------|
| `is_new=True` | `ResourceCreated` |
| `is_new=False` 且新增 source 或 link | `ResourceMerged` |
| `is_new=False` 且无数据变化 | 不发布 |

**P5-B 合计：6 测试，全部通过**

### 边界确认

- ❌ 未实现 Notify Handler / Telegram Bot
- ❌ 未实现 Query Service
- ❌ 未实现 Outbox / Redis / Kafka
- ❌ 未发布 RawMessageFailed
- ❌ 未修改 ARCHITECTURE.md
- ❌ 未进入 P5-C / P5-D / P5-E

---

## P5-A 详细记录

### 完成日期
2026-07-02

### 实现内容

- 定义仅携带标量字段的 `ResourceCreated`、`ResourceMerged`、`RawMessageFailed`
- 新增 `EventBus` 抽象接口和进程内 `InMemoryEventBus`
- `subscribe()` 按事件精确类型注册多个异步 handler
- `publish()` 按注册顺序逐个等待 handler
- 单个 handler 异常只记录日志，不阻断后续 handler，也不向发布方抛出
- 无订阅者时安全返回

**P5-A 合计：8 测试，全部通过**

### 边界确认

- ❌ 未接入 DedupService / RawMessageService
- ❌ 未修改 Resource Repository
- ❌ 未修改 main.py / lifespan
- ❌ 未新增数据库表或 migration
- ❌ 未实现持久化、重试、补发、Outbox、Redis 或 Kafka
- ❌ 未实现 Notify Handler / Telegram Bot / Transfer
- ❌ 未进入 P5-B

---

## P4-B 详细记录

### 完成日期
2026-07-01

### 实现内容

**DedupService** — 核心去重归并服务：
- 输入：`NormalizedResource` + `ParsedResource` + `raw_message_id` + `channel_id`
- 输出：`DedupResult`（resource_id, work_id, is_new, matched_reason, source_count, created_link_count, created_source）
- Create Path：resource_key miss → 新建 Work（如不存在）+ Resource + ResourceLink + ResourceSource
- Merge Path：resource_key hit → 新增 ResourceLink（如不存在）+ ResourceSource（如不存在），重算 source_count

**架构对齐：**
- `Work.type` = `normalized.content_type`（drama / movie / variety / anime / other）
- `Resource.resource_type` = `normalized.episode_kind`（single_episode / episode_range / full / unknown）
- `ResourceSource.match_type` = `"title_episode"`（P4-B 统一定义）
- `provider` 入库前统一转换为 `str`（`"quark"` 而非 `Quark` 枚举）

**8 条约束全部落实：**

| # | 约束 | 实现 |
|---|------|------|
| 1 | 统一事务 | 所有操作在 one AsyncSession 内 flush，commit 由调用方控制 |
| 2 | source_count 重算 | `SELECT COUNT(*) FROM resource_sources WHERE resource_id=?`，不盲+1 |
| 3 | ResourceSource 幂等 | `get_or_create()` SELECT-first，重复调用返回 existing，不抛异常 |
| 4 | url_hash 稳定 | `sha256(url.strip())` / `password.strip()` / `original_text.strip()` |
| 5 | provider 转 str | 入库前 `_ensure_provider_str()` 统一转换 |
| 6 | Work 存在但 Resource 新 | Create Path 先查 `work_key`，存在则复用，不新建 Work |
| 7 | created_link_count / created_source | 重复调用时均为 0 / False |
| 8 | is_new = 新建 Resource | `is_new=True` 表示新建 Resource，与 Work 无关 |

**5 个修正点全部落实：**

| # | 修正点 | 实现 |
|---|--------|------|
| 1 | Resource.resource_type | = `episode_kind`，非 `content_type` |
| 2 | match_type | 统一 `"title_episode"`，非 `"exact"` |
| 3 | serialize_parsed_resource | 专用函数处理 Enum→str，不依赖 `.model_dump()` |
| 4 | Create Path 也用 get_or_create | Create/Merge 路径统一使用 `get_or_create` |
| 5 | created_source 类型 | `bool`，非 int |

### 测试覆盖

| 测试 | 验收项 |
|------|--------|
| test_create_path_new_work_and_resource | resource_key miss → 新建 Work + Resource，type/knd 正确 |
| test_create_path_existing_work | Work 已存在，Resource 新建 → 不复写 Work |
| test_merge_path | resource_key hit → is_new=False, matched_reason 含 "hit" |
| test_link_dedup | 相同链接重复调用 → created_link_count=0, 表 count=1 |
| test_source_tracking | ResourceSource 含 raw_message_id / channel_id / parsed_snapshot |
| test_source_idempotent | 相同 raw_message_id 重复 → created_source=False |
| test_source_count_recalc | 2 个唯一 source → count=2, 重复调用不 +1 |
| test_matched_reason_readable | 含 "resource_key miss/hit" + resource_key 值 |
| test_power_idempotent | 完全相同的输入 3 次 → 记录数不变 |
| test_provider_stored_as_string | DB 存 "quark" 非枚举 |
| test_resource_type_is_episode_kind | Resource.resource_type = single_episode, Work.type = drama |
| test_multi_link_create | 2 个链接 → created=2, 重复 → created=0 |
| test_xunlei_command_link | password 作为 hash base, 重复幂等 |
| test_serialize_* | 序列化正确性、JSON 兼容、None metadata、空 links |

**P4-B 合计：17 测试，全部通过**

### 新增文件

| 文件 | 用途 |
|------|------|
| `backend/app/modules/resource/schema.py` | DedupResult DTO |
| `backend/app/modules/resource/service.py` | DedupService（Dedup / Merge 核心逻辑） |
| `backend/tests/resource/test_dedup_service.py` | P4-B 测试（17 条） |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/modules/resource/repository.py` | 增加 `get_or_create` + `count_by_resource` 方法 |
| `docs/IMPLEMENTATION_STATUS.md` | 本文件 |

### 未做内容（边界确认）

- ❌ 未接 ParserPipeline 主链路
- ❌ 未批量处理 RawMessage.parsed_data
- ❌ 未实现 EventBus / Bot / Transfer
- ❌ 未实现模糊匹配 / AI 判断
- ❌ 未修改 ARCHITECTURE.md 架构内容
- ❌ 未修改 model.py / dto.py / normalizer 模块

---

## P4-A 详细记录

### 完成日期
2026-07-01

### 实现内容

- 新增 Work / Resource / ResourceLink / ResourceSource ORM
- 新增四个仅含 create/get 的最小 Repository
- 新增 Resource Registry Alembic migration `7b3f2a1c9d04`
- Alembic CLI 支持从 backend 目录正确加载 app 包
- 未接入 ParserPipeline、Normalizer 或 RawMessage.parsed_data

### PostgreSQL 验证

- `Work.work_key` UNIQUE 实际重复插入失败
- `Resource.resource_key` UNIQUE 实际重复插入失败
- `ResourceLink(resource_id, provider, url_hash)` UNIQUE 实际重复插入失败
- `ResourceSource(resource_id, raw_message_id)` UNIQUE 实际重复插入失败
- `pg_constraint` 确认四个命名约束真实存在
- `tg_hub_test` 完成 Alembic `upgrade head` / `check` / `downgrade base`

**P4-A 合计：6 测试，全部通过**

### 边界确认

- ❌ 未实现 Dedup / Merge 业务流程
- ❌ 未接入 ParserPipeline / Normalizer
- ❌ 未读取或写入 RawMessage.parsed_data
- ❌ 未实现 EventBus / Bot / Transfer
- ❌ 未进入 P4-B

---

## P3-B 详细记录

### 完成日期
2026-07-01

### 实现内容

- 仅新增测试层 Parser → Normalizer 联调验证，生产代码零修改
- 20 条 fixture 均执行 Raw Text → ParsedResource → NormalizedResource
- 每条 fixture 重复执行完整链路，校验归一化结果和四类身份值稳定
- 无效输入在 Parser 阶段返回空列表，不调用 Normalizer 生成假 key

### 测试覆盖

- 20 条 fixture 的 Parser 输出可被 Normalizer 正常消费
- work_key / resource_key / episode_key / content_fingerprint 重复执行稳定
- single_episode 才生成 episode_key，其余类型保持 None
- 6 个无效输入不生成 ParsedResource、NormalizedResource 或 key
- 3 个代表性 fixture 的 resource_key / episode_key 精确值

**P3-B 合计：29 测试，全部通过；Normalizer 合计：77 测试**

### 边界确认

- ❌ 未修改 Parser / Normalizer 生产逻辑
- ❌ 未创建或修改数据库表
- ❌ 未写入数据库
- ❌ 未创建 Work / Resource / ResourceLink / ResourceSource
- ❌ 未实现 Dedup / Merge
- ❌ 未实现 EventBus / Bot / Transfer
- ❌ 未进入 P4

---

## P3-A 详细记录

### 完成日期
2026-07-01

### 实现内容

- 新增纯函数 `normalizer` 模块，不依赖 ORM / Repository / Session
- `content_type` 仅表示 drama / movie / variety / anime / other
- `episode_kind` 独立表示 single_episode / episode_range / full / unknown
- Metadata 优先于标题推断，并保留 `episode_source`
- `episode_range` 统一为 epN / epN-M / all / sXXeN / sXXeN-M / unknown
- 生成稳定的 work_key / resource_key / episode_key / content_fingerprint
- `episode_key` 仅 single_episode 生成
- 标题归一化保持保守，仅删除独立前缀、装饰和明确集数
- 输入 `ParsedResource` 不原地修改，归一化输出为 frozen dataclass

### 测试覆盖

- 架构中的 6 组标题与集数归一化示例
- 全角、大小写、空白、装饰符和独立前缀
- 防止过度删除有效标题内容
- Metadata/标题冲突与 episode_source
- 尾部两位纯数字正例及带 year/quality/file_size 的负例
- 单集、中英文范围、全集、季度集数和 unknown
- key 精确值、episode_key 适用范围和非法范围拒绝
- SHA-256 稳定性与身份字段差异
- 输入不变性和输出不可变性

**P3-A 合计：48 测试，全部通过**

### 边界确认

- ❌ 未接入 ORM / Repository / Session
- ❌ 未创建或修改数据库表
- ❌ 未写入 RawMessage.parsed_data
- ❌ 未创建 Work / Resource / ResourceLink / ResourceSource
- ❌ 未实现 Dedup / Merge
- ❌ 未实现 EventBus / Bot / Transfer

---

## P2-C 详细记录

### 完成日期
2026-07-01

### 实现内容

- `RawMessageService.parse_and_persist()` 调用现有 `ParserPipeline`
- 成功时将 `list[ParsedResource]` 序列化快照直接写入 `parsed_data`
- 成功时写回 `parse_status=parsed` / parser 和 rule 版本
- 空结果或异常时写回 `parse_status=parse_failed` / `last_parse_error`
- 每次尝试递增 `parse_attempts` 并更新 `last_parsed_at`
- 重试成功时清除旧的 `last_parse_error`
- RawMessage 已有全部 P2-C 字段，无需新增 Alembic migration

### 测试覆盖

- 解析成功并持久化 DTO 快照与版本
- 空结果持久化为明确失败
- Pipeline 异常持久化错误类型与消息
- 失败后重试成功，次数递增并清除旧错误
- RawMessage 不存在时抛出 `LookupError`

**P2-C 合计：5 测试，全部通过**

### 边界确认

- ❌ 未创建 Work / Resource / ResourceLink / ResourceSource
- ❌ 未实现 Normalizer / Fingerprint / Dedup / Merge
- ❌ 未实现 EventBus / DomainEvent / Notify / Bot / Transfer
- ❌ 未进入 P3

---

## P2-B 详细记录

### 完成日期
2026-07-01

### 实现内容

**Pipeline 5 阶段（顺序不可变）：**
1. **PreProcessor** — 清理 Emoji、TG Markdown，统一空白，保留中文/链接/提取码/【】
2. **RuleParser** — 从文本提取 title/raw_title/resource_type/tags，不依赖 links
3. **ProviderDetector** — 检测 quark/baidu/aliyun URL + xunlei 口令/magnet，提取 share_id/access_code
4. **MetadataExtractor** — 提取 episode_no/season_no/episode_range/year/quality/file_size/language/subtitle
5. **PostProcessor** — 注入 parser_version/rule_version，计算 confidence，保证 links/tags 默认值

**版本：**
- DTO 默认 parser_version / rule_version = `""`
- parser_version = "0.2.0"
- rule_version = "0.2.0"

**Provider 合约：**
- `LinkProvider` 为 `str` compatible Enum
- 支持 quark / baidu / xunlei / aliyun / mega / google / other
- 未知 provider 统一归一为 `other`，不暴露 `lnz`

### 测试覆盖

| 测试类 | 测试数 | 覆盖内容 |
|--------|--------|----------|
| TestPreProcessor | 9 | 空文本、中文保留、链接保留、【】保留、Markdown 清理、Emoji 清理、空白归一 |
| TestProviderDetector | 7 | 夸克+提取码、百度无码、阿里云+码、迅雷口令、magnet、多链接+独立码、无链接 |
| TestMetadataExtractor | 15 | 第N集(阿拉伯/中文)、第N-M集、更新至N集、全集、SxxExx、SxxExx-Eyy、画质、文件大小、语言、字幕、年份、季、无元数据 |
| TestRuleParser | 8 | 【】标题、无括号标题、raw_title 保留、drama 类型、movie 类型、tags 提取、空文本、纯链接 |
| TestPostProcessor | 6 | 版本注入、confidence 计算(3场景)、links 默认、tags 默认 |
| TestPipelineIntegration | 20 | 20 条 fixture 全字段断言 |
| TestPipelineBoundary | 6 | 空文本、纯空白、无链接、纯链接无标题、纯提取码、None 输入 |

**P2-B 合计：72 测试，全部通过；Parser 合计：86 测试**

### 新增文件

| 文件 | 用途 |
|------|------|
| `backend/tests/parser/test_parser_p2b.py` | P2-B 测试（72 条） |
| `docs/IMPLEMENTATION_STATUS.md` | 本文件 |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/modules/parser/dto.py` | DTO 合约：版本默认值为空、未知 provider 归一为 other |
| `backend/app/modules/parser/pipeline/core.py` | 重写：5 Stages 完整实现 + Pipeline 编排 |
| `backend/tests/parser/fixtures.py` | 增加 expected_title / expected_result_count；修正 magnet URL |
| `backend/tests/parser/test_parser_p2a.py` | 校验 DTO 版本默认值和 provider fallback |

### 未做内容（边界确认）

- ❌ 未创建 Work / Resource / ResourceLink / ResourceSource
- ❌ 未实现 Normalizer / Fingerprint / Dedup / Merge
- P2-B 本身不写数据库；RawMessage 解析结果写回由 P2-C 负责
- ❌ 未实现 EventBus / DomainEvent / Notify
- ❌ 未实现 Bot 命令
- ❌ 未实现 Transfer / Subscription / User
- ❌ 未实现 SpecialParser
- ❌ 未实现 AI 解析 / 模糊匹配
- ❌ 未修改 ARCHITECTURE.md 架构内容

---

## P2-A 详细记录

### 完成日期
2026-07-01

### 实现内容
- ParsedLink DTO（8 字段）
- ParsedMetadata DTO（8 字段）
- ParsedResource DTO（10 字段）
- 20 条样本 fixture
- Pipeline 空骨架
- 14 条测试
