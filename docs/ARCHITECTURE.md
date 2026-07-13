# tg-hub 架构约束文档 V2.2

> 更新日期：2026-07-09
> 状态：**已确认，按阶段持续校准**
> 原则：概念正确 → 落库清晰 → 状态流转无歧义

---

## 目录

1. [资源粒度定义](#1-资源粒度定义)
2. [核心数据模型](#2-核心数据模型)
3. [Parser Pipeline DTO](#3-parser-pipeline-dto)
4. [指纹与去重规则](#4-指纹与去重规则)
5. [状态机](#5-状态机)
6. [EventBus](#6-eventbus)
7. [目录结构](#7-目录结构)
8. [MVP 范围与验收标准](#8-mvp-范围与验收标准)
9. [实施顺序](#9-实施顺序)
10. [设计约束清单](#10-设计约束清单)
11. [P6 后续阶段边界](#11-p6-后续阶段边界)

---

## 1. 资源粒度定义

### 1.1 三层模型

```
Work          作品本体：「家业」
  ↓
Resource      资源条目：「家业 更新至10集」「家业 全集」「家业 第1集」
  ↓
ResourceLink  平台链接：夸克 / 百度 / 迅雷 + 提取码 / 口令
```

### 1.2 层级职责

| 层级 | 代表什么 | 数据示例 | 生命周期 |
|------|---------|---------|---------|
| **Work** | 作品本体 | `家业` (drama, 2026) | 长期，除非作品下架 |
| **Resource** | 可用资源包 | `家业 更新至10集` | 中期，有更新或过期时状态变化 |
| **ResourceLink** | 具体平台链接 | `quark: https://pan.quark.cn/s/abc123 提取码: xyz` | 短期，链接可能过期 |

### 1.3 为什么三层

- **Work** 解决「同一作品不同资源包」的聚合问题
- **Resource** 解决「同一资源包跨频道去重」问题
- **ResourceLink** 解决「同一资源包多平台链接」问题

### 1.4 不允许的退化

| 退化 | 后果 |
|------|------|
| Resource = Work | 「第1集」和「全集」混在一起，无法区分 |
| Resource = ResourceLink | 跨频道去重失效，同一资源重复创建 |
| 砍掉 Work 表 | 后续补建需全量数据迁移 |

---

## 2. 核心数据模型

### 2.1 Channel

```
Channel
──────────────────────────────────────
id                      SERIAL PK
name                    VARCHAR(100)    -- 显示名
tg_id                   BIGINT          -- TG 频道内部 ID
tg_username             VARCHAR(100)    -- @username
source_type             VARCHAR(20)     -- telegram (预留扩展)
status                  VARCHAR(20)     -- active / paused / error
rule_profile            VARCHAR(50)     -- 规则集标识：short_drama_v1
special_parser          VARCHAR(50)     -- NULL 或特殊解析器名（极少使用）
last_message_id         BIGINT          -- 已处理的最大 message_id
last_checked_at         TIMESTAMP       -- 最后一次拉取时间
created_at              TIMESTAMP
updated_at              TIMESTAMP
```

**唯一约束：** `tg_id`

### 2.2 RawMessage

```
RawMessage
──────────────────────────────────────
id                      SERIAL PK
channel_id              FK → Channel
tg_message_id           BIGINT          -- TG 消息 ID
raw_text                TEXT            -- 原始消息文本，完整保存
raw_media_refs          JSONB           -- 受控媒体引用，不包含 access_hash/file_reference，不下载文件
raw_payload             JSONB           -- 稳定、可 JSON 化的结构快照（可选，用于回溯）
content_hash            VARCHAR(64)     -- sha256(raw_text)，备用去重
                                        -- 仅用于无 tg_message_id 或手动导入场景
                                        -- Telegram 主去重依据是 (channel_id, tg_message_id)
published_at            TIMESTAMP       -- TG 消息发布时间
received_at             TIMESTAMP       -- 系统接收时间

-- 三状态分离
ingest_status           VARCHAR(20)     -- received / stored / duplicate / ignored
parse_status           VARCHAR(20)     -- parse_pending / parsed / parse_failed / skipped
dedup_status            VARCHAR(20)     -- dedup_pending / matched / new / skipped

-- 解析元信息
parsed_data             JSONB           -- Parser Pipeline 输出快照
parser_version          VARCHAR(20)     -- 最近一次解析的 pipeline 版本
rule_version            VARCHAR(20)     -- 最近一次解析的规则集版本
parse_attempts          INT             -- 解析次数，默认 0
last_parse_error        TEXT            -- 最近一次解析错误
last_parsed_at          TIMESTAMP       -- 最近一次解析时间

created_at              TIMESTAMP
updated_at              TIMESTAMP
```

**唯一约束：** `(channel_id, tg_message_id)`
**备用去重：** `(channel_id, content_hash)`

**原始证据约束：**

- `raw_text` 保存被选中的原始 `text` 或 `caption`，不得写入归一化结果；
- `raw_media_refs` 只保存媒体类型、Telegram 媒体 ID、MIME 类型、大小等稳定引用，不保存 `access_hash`、`file_reference`，也不下载媒体；
- `raw_payload` 保存版本化的受控结构快照，不直接序列化整个 Telethon 对象，不重复保存正文或凭据；
- `RawMessage.channel_id -> Channel.id` 使用 `ON DELETE RESTRICT`，有原始消息证据的 Channel 不得物理删除，只能通过状态停用。

### 2.3 Work

```
Work
──────────────────────────────────────
id                      SERIAL PK
title                   VARCHAR(500)    -- 原始标题
title_norm              VARCHAR(500)    -- 归一化标题
type                    VARCHAR(20)     -- drama / movie / variety / anime / other
aliases                 TEXT[]          -- 别名列表
year                    INT             -- 年份（可空）
work_key                VARCHAR(200)    -- {type}:{title_norm}:{year_or_unknown}
status                  VARCHAR(20)     -- active / archived
created_at              TIMESTAMP
updated_at              TIMESTAMP
```

**唯一约束：** `work_key` — 格式 `{type}:{title_norm}:{year_or_unknown}`
- year 有值时：`drama:家业:2026`
- year 为空时：`drama:家业:unknown`
- 避免同名同年同类型作品被误合并，也避免不同年份翻拍剧被错误合并

### 2.4 Resource

```
Resource
──────────────────────────────────────
id                      SERIAL PK
work_id                 FK → Work
title                   VARCHAR(500)    -- 资源条目标题：「家业 更新至10集」
title_norm              VARCHAR(500)    -- 归一化
resource_type           VARCHAR(20)     -- full / episode_range / single_episode

-- 集数信息
episode_no              INT             -- 单集编号（可空）
season_no               INT             -- 季编号（可空，默认1）
episode_range           VARCHAR(50)     -- 「ep1-10」「all」「ep1」
year                    INT             -- 继承自 Work
quality                 VARCHAR(50)     -- 1080p / 720p / 4K（可空）

-- 多维指纹（修正2）
resource_key            VARCHAR(200)    -- 资源级：drama:家业:ep1-10
episode_key             VARCHAR(200)    -- 剧集级：drama:家业:s01:e01
content_fingerprint     VARCHAR(64)     -- 综合指纹：sha256(title_norm + episode_range + type + year)

description             TEXT
tags                    TEXT[]

-- 状态
status                  VARCHAR(20)     -- active / merged / archived / ignored
source_count            INT             -- 来源数量，默认 1
first_seen_at           TIMESTAMP       -- 首次发现时间
last_seen_at            TIMESTAMP       -- 最近发现时间

created_at              TIMESTAMP
updated_at              TIMESTAMP
```

**唯一约束：** `resource_key` — 同一资源级指纹唯一
**索引：** `episode_key`, `content_fingerprint`, `work_id`

### 2.5 ResourceLink

```
ResourceLink
──────────────────────────────────────
id                      SERIAL PK
resource_id             FK → Resource

provider                VARCHAR(20)     -- quark / baidu / xunlei / magnet / other
original_text           TEXT            -- 频道原文中的链接片段（留底）
original_url            VARCHAR(500)    -- 原始 URL（可能为空，如迅雷口令）
normalized_url          VARCHAR(500)    -- 标准化 URL
url_hash                VARCHAR(64)     -- sha256(normalized_url 或 original_text)
share_id                VARCHAR(100)    -- 平台资源 ID
access_code             VARCHAR(50)     -- 提取码
password                VARCHAR(200)    -- 迅雷口令等
link_type               VARCHAR(20)     -- url / command / magnet / text_code
status                  VARCHAR(20)     -- unknown / active / expired

first_seen_at           TIMESTAMP
last_seen_at            TIMESTAMP
created_at              TIMESTAMP
updated_at              TIMESTAMP
```

**唯一约束：** `(resource_id, provider, url_hash)`
**索引：** `share_id`, `provider`

### 2.6 ResourceSource（归并证据表）

```
ResourceSource
──────────────────────────────────────
id                      SERIAL PK
resource_id             FK → Resource
raw_message_id          FK → RawMessage
channel_id              FK → Channel

-- 归并证据
match_type              VARCHAR(20)     -- exact_url / title_episode / fuzzy_title / manual
confidence              FLOAT           -- 0.0 - 1.0
matched_reason           TEXT            -- 人可读的归并原因
parsed_snapshot         JSONB           -- Parser 输出完整快照

parser_version          VARCHAR(20)
rule_version            VARCHAR(20)
detected_at             TIMESTAMP
```

**唯一约束：** `(resource_id, raw_message_id)` — 同一条原始消息对同一资源只记录一次归并

### 2.7 User / Subscription / TransferTask

> MVP 不落表，仅保留设计。

```
User
──────────────────────────────────────
id                      SERIAL PK
tg_id                   BIGINT UNIQUE
username                VARCHAR(100)
role                    VARCHAR(20)     -- user / admin
drive_cookies           JSONB           -- 加密存储
status                  VARCHAR(20)
created_at              TIMESTAMP

Subscription
──────────────────────────────────────
id                      SERIAL PK
user_id                 FK → User
channel_id              FK → Channel (NULL=全部)
keyword                 VARCHAR(200)    -- NULL=全匹配
drive_type              VARCHAR(20)     -- 指定转存网盘
auto_transfer           BOOLEAN
target_dir              VARCHAR(500)
status                  VARCHAR(20)     -- active / paused

TransferTask
──────────────────────────────────────
id                      SERIAL PK
subscription_id         FK → Subscription
resource_link_id        FK → ResourceLink
drive_type              VARCHAR(20)
target_dir              VARCHAR(500)
status                  VARCHAR(20)     -- pending / running / success / failed_retryable / failed_final
result                  JSONB
error_msg               TEXT
retry_count             INT
started_at              TIMESTAMP
finished_at             TIMESTAMP
created_at              TIMESTAMP
```

### 2.8 ER 关系总览

```
┌──────────┐  1:N  ┌──────────────┐
│ Channel  │──────►│ RawMessage   │
└──────────┘       └──────┬───────┘
                          │
                          │ N:1 (多条消息 → 一个资源)
                          ▼
┌──────────┐  1:N  ┌──────────────────┐  1:N  ┌────────────────────┐
│ Work     │──────►│ Resource         │──────►│ ResourceLink       │
└──────────┘       └──────┬───────────┘       └────────────────────┘
                          │
                   ┌──────┴───────┐
                   │ResourceSource│  ← 归并证据表
                   └──────────────┘
                          │
                   (resource_id, raw_message_id)
                          │
                          ▼
                   ┌──────────────┐
                   │ RawMessage   │
                   └──────────────┘

┌──────────┐  1:N  ┌────────────────┐  1:N  ┌───────────────┐
│ User     │──────►│ Subscription   │──────►│ TransferTask  │
└──────────┘       └────────────────┘       └───────────────┘
                          (后期)
```

---

## 3. Parser Pipeline DTO

### 3.1 设计原则

每个 Pipeline Stage 只负责补充字段，不修改其他 Stage 的输出。
最终输出统一为 `ParsedResource` 列表，不允许偏离。

### 3.2 ParsedLink

```python
@dataclass
class ParsedLink:
    provider: str                    # quark / baidu / xunlei / magnet / other
    original_text: str               # 频道原文片段
    url: str | None = None           # 提取的 URL
    share_id: str | None = None      # 平台资源 ID
    access_code: str | None = None   # 提取码
    password: str | None = None      # 迅雷口令等
    link_type: str = "url"           # url / command / magnet / text_code
    confidence: float = 1.0
```

### 3.3 ParsedMetadata

```python
@dataclass
class ParsedMetadata:
    episode_no: int | None = None
    season_no: int | None = None
    episode_range: str | None = None     # "ep1-10" / "all" / "ep1"
    year: int | None = None
    quality: str | None = None           # "1080p" / "720p" / "4K"
    file_size: str | None = None         # "12.5GB"
    language: str | None = None          # "国语" / "粤语"
    subtitle: str | None = None          # "中字" / "内嵌"
```

### 3.4 ParsedResource

```python
@dataclass
class ParsedResource:
    title: str                           # 提取的标题
    raw_title: str                       # 原始标题（解析前）
    description: str | None = None
    resource_type: str = "drama"         # drama / movie / variety / anime / other
    links: list[ParsedLink] = None
    metadata: ParsedMetadata | None = None
    tags: list[str] = None
    confidence: float = 1.0
    parser_version: str = ""             # 由 pipeline 注入
    rule_version: str = ""               # 由 pipeline 注入
```

### 3.5 Pipeline 编排

```
RawMessage.raw_text
    │
    ▼
PreProcessor          # 清理 TG 特殊字符、去水印、统一编码
    │
    ▼
RuleParser            # 按规则集提取标题、描述、标签
    │                 # 规则可配置（正则/关键词），不绑定频道
    │                 # channel.rule_profile 指定规则集
    ├── 规则命中 ──────┐
    │                 │
    └── 规则未命中 ───► SpecialParser.can_handle()?
                         │
                         ├── True ──────┐
                         │              │
                         └── False ───► parse_status = parse_failed
                                        │
                  ┌─────────────────────┘
                  ▼
ProviderDetector      # 检测网盘链接，提取 URL / share_id / access_code / 口令
    │
    ▼
MetadataExtractor     # 提取集数、季、年份、清晰度
    │
    ▼
PostProcessor         # 注入 parser_version / rule_version / confidence
    │
    ▼
list[ParsedResource]  → 写入 RawMessage.parsed_data
```

### 3.6 SpecialParser 约束

```python
class SpecialParser(ABC):
    """特殊频道解析器 — 受控逃生口"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def version(self) -> str: ...

    @abstractmethod
    def can_handle(self, raw_text: str, channel_config: dict) -> bool:
        """声明适用条件，不允许无条件 True"""
        ...

    @abstractmethod
    def parse(self, raw_text: str) -> list[ParsedResource]:
        """输出必须符合 ParsedResource DTO"""
        ...
```

**三条硬规则：**
1. 只能作为 Pipeline 的一个 stage，不允许绕过 Pipeline
2. 输出必须符合 `ParsedResource` DTO
3. 必须声明 `name` / `version` / `can_handle`，且附带测试样例

---

## 4. 指纹与去重规则

### 4.1 三个指纹

| 指纹 | 用途 | 生成规则 | 示例 |
|------|------|---------|------|
| `resource_key` | 资源级去重 | `{type}:{title_norm}:{episode_range}` | `drama:家业:ep1-10` |
| `episode_key` | 剧集级去重（仅 single_episode） | `{type}:{title_norm}:s{season}:e{episode}` | `drama:家业:s01:e01` |
| `content_fingerprint` | 综合防误判 | `sha256(title_norm + episode_range + type + year)` | `a3f2e1...` |

### 4.1.1 episode_key 适用范围

`episode_key` **仅用于 `single_episode` 类型资源**。

| resource_type | 是否生成 episode_key | 说明 |
|---------------|---------------------|------|
| `single_episode` | ✅ 是 | 如「家业 第1集」→ `drama:家业:s01:e01` |
| `episode_range` | ❌ 否 | 如「家业 第1-10集」→ 只用 `resource_key` |
| `full` / `all` | ❌ 否 | 如「家业 全集」→ 只用 `resource_key` |

`episode_key` 为空时，该字段存储 NULL，不参与去重查询。

### 4.2 归一化规则

标题归一化 (`title_norm`) 处理顺序：

1. 全角转半角
2. 转小写
3. 去除首尾空白
4. 去除特殊符号（【】★◆等装饰符）
5. 去除常见前缀（「资源」「分享」「更新」等无意义词）
6. 统一集数表示 → 提取到 episode_range

**归一化示例：**

| 输入 | title_norm | episode_range |
|------|-----------|---------------|
| `【家业】第一集` | `家业` | `ep1` |
| `家业 EP01` | `家业` | `ep1` |
| `家业 01` | `家业` | `ep1` |
| `家业 更新至10集` | `家业` | `ep1-10` |
| `家业 全集` | `家业` | `all` |
| `家业 第1-5集` | `家业` | `ep1-5` |

### 4.3 去重决策树

```
新 ParsedResource 进入 Dedup
    │
    ▼
生成 resource_key
    │
    ▼
查询 DB: resource_key 匹配?
    │
    ├── 命中 ──► 归并到已有 Resource
    │            │   追加 ResourceLink（如链接不重复）
    │            │   记录 ResourceSource
    │            │   更新 source_count
    │            └── 发布 ResourceMerged 事件
    │
    └── 未命中
        │
        ▼
    查询 episode_key 匹配?
        │
        ├── 命中 ──► 新建 Resource（同 Work，不同范围）
        │            │   关联同一 Work
        │            │   记录 ResourceSource
        │            └── 发布 ResourceCreated 事件
        │
        └── 未命中
            │
            ▼
        新建 Work + Resource
            │   记录 ResourceSource
            └── 发布 ResourceCreated 事件
```

### 4.4 链接去重

ResourceLink 唯一约束：`(resource_id, provider, url_hash)`

- `url_hash = sha256(normalized_url)` — 有 URL 的链接
- `url_hash = sha256(original_text)` — 无 URL 的链接（如迅雷口令）

新链接尝试追加时，如果 `(resource_id, provider, url_hash)` 已存在，跳过不重复插入。

---

## 5. 状态机

### 5.1 RawMessage 状态机

三个独立状态维度，不互相污染：

```
ingest_status:
    received → stored → (duplicate / ignored)

parse_status:
    parse_pending → parsed → (skipped)
                   parse_pending → parse_failed → parse_pending (重试)

    重解析不是独立状态，通过 parse_attempts / last_parsed_at / parser_version 记录

dedup_status:
    dedup_pending → matched (归并到已有)
                  → new (创建新资源)
                  → skipped (解析失败，未参与去重)
```

**完整流转：**

```
消息到达
  ↓
ingest_status = received
  ↓ 存储成功
ingest_status = stored
parse_status = parse_pending
dedup_status = dedup_pending
  ↓
Parser Pipeline
  ↓
parse_status = parsed
  ↓
Dedup
  ↓
dedup_status = matched / new

异常路径:
  parse_status = parse_failed, last_parse_error = "..."
  → 修复规则后手动重跑
  → parse_attempts +1, parse_status = parse_pending → parsed
  → 重解析不是独立状态，由 parse_attempts / last_parsed_at / parser_version 表达
```

### 5.2 Resource 状态机

```
active          资源可用
  ↓
merged          被去重归并到另一个 Resource
  ↓
archived        过期或手动归档

ignored         解析后判断无价值，不进入正常流程
```

**说明：**
- 没有 `draft` 状态。Resource 只有通过基本校验（title 非空 + 至少一个 link）才创建。
- `merged` 表示该 Resource 的内容已归并到另一个 Resource，自身不再活跃。
- 后续如需人审，可增加 `candidate` 状态。

### 5.3 TransferTask 状态机（后期）

```
pending
  ↓
running
  ├── success
  ├── failed_retryable → pending (重试)
  └── failed_final
```

MVP 不落表，不实现。

---

## 6. EventBus

### 6.1 边界定义

| 属性 | MVP |
|------|-----|
| 实现 | asyncio 内存事件总线 |
| 可靠性 | best-effort，不保证投递 |
| 真实状态源 | 数据库 |
| 事件丢失影响 | 不影响核心数据 |
| 进程重启 | 不要求补发 |

### 6.2 事件定义

```python
# MVP 事件
ResourceCreated    → Bot/Notification 适配层推送新资源
ResourceMerged     → Bot/Notification 适配层推送归并通知
RawMessageFailed   → 记录日志 + 告警
```

### 6.3 事件流

```
DB Transaction COMMIT  ← 真实状态源
  ↓
publish in-memory event (best-effort)
  ↓
Bot/Notification handler 执行
  ↓
失败 → 记日志，不影响数据
```

### 6.4 接口设计

```python
class EventBus(ABC):
    @abstractmethod
    async def publish(self, event: DomainEvent) -> None: ...

    @abstractmethod
    def subscribe(self, event_type: str, handler: EventHandler) -> None: ...


class InMemoryEventBus(EventBus):
    """MVP 实现：进程内，best-effort"""
    ...


class OutboxEventBus(EventBus):
    """后期：写 outbox_events 表，独立 worker 轮询投递"""
    ...
```

### 6.5 Outbox 表设计（预留，MVP 不建）

```sql
CREATE TABLE outbox_events (
    id            BIGSERIAL PRIMARY KEY,
    event_type    VARCHAR(100) NOT NULL,
    aggregate_type VARCHAR(50) NOT NULL,
    aggregate_id  BIGINT NOT NULL,
    payload       JSONB NOT NULL,
    status        VARCHAR(20) DEFAULT 'pending',
    retry_count   INT DEFAULT 0,
    created_at    TIMESTAMP DEFAULT NOW(),
    published_at  TIMESTAMP
);
```

---

## 7. 目录结构

本节以当前实现组织为准，修正早期草图中的目录漂移。当前不为了匹配旧文档强制搬迁代码。

```
tg-hub/
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI 入口
│   │   ├── config.py                  # 配置加载
│   │   ├── database.py                # DB 连接/Session
│   │   │
│   │   ├── modules/                   # 按领域组织
│   │   │   ├── channel/
│   │   │   │   ├── model.py           # Channel ORM
│   │   │   │   ├── repository.py      # 数据访问
│   │   │   │   ├── service.py         # 业务逻辑
│   │   │   │   ├── api.py             # REST 路由
│   │   │   │   └── schema.py          # Pydantic DTO
│   │   │   │
│   │   │   ├── rawmessage/
│   │   │   │   ├── model.py
│   │   │   │   ├── repository.py
│   │   │   │   └── service.py
│   │   │   │
│   │   │   ├── parser/                # 解析流水线
│   │   │   │   ├── dto.py             # ParsedResource / ParsedLink / ParsedMetadata
│   │   │   │   ├── pipeline/
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   └── core.py        # 当前 Pipeline 编排实现
│   │   │   │   └── rules/             # 规则集
│   │   │   │
│   │   │   ├── normalizer/            # 标题归一化与指纹
│   │   │   │   ├── core.py
│   │   │   │   └── fingerprint.py
│   │   │   │
│   │   │   ├── resource/              # 统一资源中心
│   │   │   │   ├── model.py           # Work, Resource, ResourceLink, ResourceSource
│   │   │   │   ├── repository.py
│   │   │   │   ├── service.py
│   │   │   │   ├── query_service.py   # 查询/Bot 展示数据读取
│   │   │   │   ├── query_schema.py    # 查询 DTO
│   │   │   │   └── schema.py
│   │   │   │
│   │   │   ├── bot/                   # Bot 查询与通知适配
│   │   │   │   ├── formatter.py
│   │   │   │   ├── handlers.py
│   │   │   │   ├── router.py
│   │   │   │   ├── schema.py          # Bot 展示 DTO/ViewModel
│   │   │   │   └── transport.py
│   │   │   │
│   │   │   ├── monitor/               # Telegram 传输适配层
│   │   │   │   ├── config.py          # watchlist 配置读取
│   │   │   │   ├── schema.py          # IncomingMessage DTO
│   │   │   │   ├── filter.py          # watch_titles 纯过滤
│   │   │   │   ├── source_channels.py # source_channels 引用分类预检
│   │   │   │   ├── resolver.py        # 受控解析编排
│   │   │   │   ├── telethon_resolver.py
│   │   │   │   ├── runtime_preflight.py
│   │   │   │   └── listener_dry_run.py
│   │   │   │
│   │   │   ├── transfer/              # 转存（后期）
│   │   │   │   ├── model.py
│   │   │   │   ├── service.py
│   │   │   │   ├── providers/
│   │   │   │   │   ├── quark.py
│   │   │   │   │   ├── baidu.py
│   │   │   │   │   └── xunlei.py
│   │   │   │   └── api.py
│   │   │   │
│   │   │   ├── subscription/          # 订阅（后期）
│   │   │   │   ├── model.py
│   │   │   │   ├── service.py
│   │   │   │   └── api.py
│   │   │   │
│   │   │   └── user/                  # 用户（后期）
│   │   │       ├── model.py
│   │   │       ├── service.py
│   │   │       └── api.py
│   │   │
│   │   ├── infra/                     # 基础设施
│   │   │   ├── eventbus.py            # 事件总线
│   │   │   ├── events.py              # 事件定义
│   │   │   └── logger.py
│   │
│   ├── alembic/                       # 数据库迁移
│   ├── tests/
│   │   ├── parser/                    # Parser 样本测试
│   │   ├── dedup/                     # 去重逻辑测试
│   │   └── fixtures/                  # 真实消息样本
│   │       └── samples/               # .txt 原始消息样本
│   ├── requirements.txt
│   └── alembic.ini
│
├── docker-compose.yml                 # 一键部署（后期）
└── README.md
```

### 7.1 Bot ViewModel 约束

Bot 展示使用 ViewModel，**不直接使用 ORM Model**：

```python
# modules/bot/schema.py
# 或后续独立 notification/viewmodel.py

@dataclass
class ResourceListItem:
    """Bot /latest 列表项"""
    resource_id: int
    title: str                    # Resource.title
    work_title: str               # Work.title
    providers: list[str]          # ["quark", "baidu"]
    source_count: int             # Resource.source_count
    last_seen_at: datetime

@dataclass
class ResourceDetail:
    """Bot /resource <id> 详情"""
    resource_id: int
    title: str
    work_title: str
    description: str | None
    links: list[LinkView]
    sources: list[SourceView]
    first_seen_at: datetime
    last_seen_at: datetime

@dataclass
class LinkView:
    provider: str
    url: str | None
    access_code: str | None
    status: str

@dataclass
class SourceView:
    channel_name: str
    detected_at: datetime
    match_type: str
    confidence: float
```

当前实现中，资源查询读取能力位于 `modules/resource/query_service.py` 与 `modules/resource/query_schema.py`，Bot 层负责展示格式、命令路由和传输适配。Bot 不应绕过查询服务直接暴露 ORM。

---

## 8. MVP 范围与验收标准

### MVP-A：解析与去重验证

**目标：** Telegram → RawMessage → Parser → Resource → DB

| 范围 | 做 | 不做 |
|------|----|----|
| 监控 | Telethon 监听指定频道 | 多频道动态管理 |
| 存储 | RawMessage 完整入库 | 媒体文件下载 |
| 解析 | Parser Pipeline 基础版 | SpecialParser |
| 网盘 | 只支持夸克 | 百度/迅雷 |
| 去重 | resource_key + 链接去重 | 模糊匹配 |
| 事件 | EventBus 接口预留，不接通知 | Bot 通知 |
| 查看 | 命令行/日志 | Bot 查询 |
| 转存 | 不做 | — |

**MVP-A 验收标准：**

1. ✅ 同一 TG message 重复接收，不重复插入 RawMessage
2. ✅ raw_text / raw_payload / raw_media_refs 完整保存
3. ✅ 至少 20 条真实样本解析通过
4. ✅ 解析失败有明确 error，可追溯
5. ✅ parsed_data JSONB 有版本号
6. ✅ 同一资源在 3 个频道出现，只创建 1 个 Resource
7. ✅ 3 条 RawMessage 都关联到同一个 Resource（ResourceSource 可查）
8. ✅ 夸克链接去重正确，无重复 ResourceLink
9. ✅ title_norm / episode_no / resource_key / episode_key 输出稳定

### MVP-B：Bot 通知与查询

**目标：** ResourceCreated / ResourceMerged → Bot 通知 + Bot 查询

| 范围 | 做 | 不做 |
|------|----|----|
| 事件 | EventBus 内存版 | Outbox |
| 通知 | ResourceCreated / ResourceMerged | TransferFailed |
| Bot | /latest /search /resource | /save /subscribe |
| 展示 | ViewModel | Web 后台 |

**MVP-B 验收标准：**

1. ✅ 新资源创建 → Bot 推送通知
2. ✅ 资源归并 → Bot 推送归并通知
3. ✅ 通知失败不影响主链路（数据已入库）
4. ✅ /latest 返回 ViewModel，不暴露 ORM
5. ✅ /search keyword 支持关键词搜索
6. ✅ /resource <id> 展示详情含来源追溯

---

## 9. 实施顺序

| Phase | 目标 | 关键验收 |
|-------|------|---------|
| **P1** | 数据模型 + RawMessage 入库 | TG 消息完整、幂等入库 |
| **P2** | Parser Pipeline + DTO | 20+ 真实样本解析通过 |
| **P3** | Normalizer + Fingerprint | 归一化输出稳定 |
| **P4** | Dedup + Merge + ResourceSource | 跨频道同资源归并 |
| **P5** | EventBus + Bot | 通知 + 查询可用 |
| **P6-2D** | 长期 monitor runtime | 仅生命周期：连接、重连、心跳、退出、观测 |
| **P6-2E** | Monitor → RawMessage ingestion boundary | 传输适配层只交付 IncomingMessage |
| **P6-2F** | RawMessage → Parser / Normalizer / Dedup 编排 | 主处理链路由应用服务承接 |
| **P6-2G** | EventBus / Bot 查询通知接入 | 事件与通知接入，不反向污染 Monitor |

### P1 详细验收

- 同一 TG message 重复接收 → 不重复插入
- `raw_text` / `raw_payload` / `raw_media_refs` 完整保存
- `ingest_status` / `parse_status` / `dedup_status` 初始值正确

### P2 详细验收

- 至少 20 条真实样本解析通过
- 解析失败有明确 `last_parse_error`
- `parsed_data` JSONB 包含 `parser_version` 和 `rule_version`
- 输出符合 `ParsedResource` DTO

### P3 详细验收

- `家业 第一集` / `家业 EP01` / `家业 01` → `title_norm=家业, episode_range=ep1`
- `resource_key` / `episode_key` / `content_fingerprint` 输出稳定（相同输入 → 相同输出）

### P4 详细验收

- 同一资源来自 3 个频道 → 只创建 1 个 Resource
- 3 条 ResourceSource 记录可追溯
- ResourceLink 无重复
- `matched_reason` 人可读

### P5 详细验收

- `ResourceCreated` 推送成功
- `ResourceMerged` 推送成功
- 通知失败不影响主链路
- Bot 返回 ViewModel，不暴露 ORM

---

## 10. 设计约束清单

实现过程中必须遵守的硬约束：

| # | 约束 | 说明 |
|---|------|------|
| 1 | Resource 全局唯一 | 不绑定 Channel，通过 ResourceSource 关联来源 |
| 2 | RawMessage 永不删 | 原始消息完整保存，支持重新解析 |
| 3 | 三状态分离 | RawMessage 的 ingest/parse/dedup 独立流转 |
| 4 | Parser 不绑频道 | 用 rule_profile 指定规则集，不是 1 频道 1 文件 |
| 5 | SpecialParser 受控 | 三条硬规则 + ParsedResource DTO |
| 6 | 多维指纹 | resource_key + episode_key + content_fingerprint |
| 7 | ResourceSource 是证据表 | 记录归并原因，不只是关联 |
| 8 | EventBus best-effort | DB 是真实状态源，事件丢失不影响数据 |
| 9 | Bot 用 ViewModel | 不直接暴露 ORM Model |
| 10 | TransferTask 后置 | MVP 不落表 |
| 11 | MVP 两阶段 | MVP-A 验证解析去重，MVP-B 做 Bot |
| 12 | 三层资源模型 | Work → Resource → ResourceLink，MVP 也建 Work |
| 13 | Monitor 是传输适配层 | 只构造 IncomingMessage，不直接承担 DB/Parser/Normalizer/Dedup/Bot 编排 |
| 14 | 原始证据受控保存 | 保存 text/caption 原文、稳定媒体引用和版本化结构快照，不保存 Telethon 凭据或下载媒体 |
| 15 | Channel 不级联删除证据 | RawMessage 外键使用 RESTRICT，频道通过状态停用而非物理删除 |

---

## 11. P6 后续阶段边界

### 11.1 当前架构定性

当前核心主链路已经接通：生产 Monitor 只通过 application boundary 交付消息，后续 ingestion、Parser、Normalizer、Dedup、EventBus 与 Bot notification 保持分层。P6-Deploy 仍在完成真实轮转、备份恢复和最终交付验收，这属于交付进度，不代表业务架构偏离。

目录组织与早期草图存在轻微漂移，但业务边界暂未越界。后续以当前 `app/modules/...` 结构为准更新文档，不为了形式一致强制搬迁代码。

### 11.2 Monitor 固定职责

Monitor runtime 固定为传输适配层：

```
Monitor runtime
  -> IncomingMessage
  -> ingestion/application boundary
```

Monitor 不直接承担数据库、解析、归一化、去重或通知编排。它的职责是连接 Telegram、维护运行生命周期、接收消息、构造统一入口 DTO，并把 DTO 交给应用边界。

应用服务负责主业务链路：

```
Application service
  -> RawMessageService.ingest()
  -> Parser
  -> Normalizer
  -> Dedup
  -> EventBus
```

### 11.3 禁止耦合

明确禁止形成以下调用关系：

- `monitor/runtime.py` 直接访问 DB
- `monitor/runtime.py` 直接调用 Parser
- `monitor/runtime.py` 直接调用 Normalizer
- `monitor/runtime.py` 直接调用 Dedup
- `monitor/runtime.py` 直接发送 Bot 通知

### 11.4 阶段拆分

| 阶段 | 范围 | 明确不做 |
|------|------|----------|
| **P6-2D** | 长期 monitor runtime，仅生命周期 | 不接 DB / Parser / Normalizer / Dedup / Bot |
| **P6-2E** | Monitor → RawMessage ingestion boundary | 不扩展解析、去重、通知 |
| **P6-2F** | RawMessage → Parser / Normalizer / Dedup 编排 | 不把业务编排塞回 Monitor |
| **P6-2G** | EventBus / Bot 查询通知接入 | 不让 Bot 或事件处理反向依赖 Monitor runtime |

### 11.5 P6-2D 允许范围

P6-2D 只处理长期运行生命周期：

- client connect / disconnect
- handler 注册与移除
- 重连策略
- 心跳与健康状态
- shutdown signal
- 运行状态观测
- 异常隔离
- bounded retry / backoff
- 配置重载策略是否存在

P6-2D 不包含：

- `RawMessageService.ingest()`
- DB transaction
- Parser / Normalizer / Dedup
- EventBus publish
- Bot notification
- history backfill
- 媒体下载

### 11.6 后续评审硬边界

后续 P6-2D、P6-2E 评审时，应优先检查是否出现长期 runtime 与 ingestion pipeline 混合。一旦 Monitor 直接接入 DB、Parser、Normalizer、Dedup 或 Bot，即视为越过阶段边界，需要拆回应用服务层。
