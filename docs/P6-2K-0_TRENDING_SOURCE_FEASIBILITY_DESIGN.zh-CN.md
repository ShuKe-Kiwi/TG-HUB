# P6-2K-0 热播数据源可行性调查与设计

## 1. 阶段定义

```text
PROJECT: tg-hub
PHASE: P6-2K-0
MODE: feasibility-and-design
STATUS: design-locked
ALLOW_COLLECTOR_IMPLEMENTATION: no
ALLOW_WATCHLIST_MUTATION: no
```

P6-2K-0 只回答：哪些官方视频平台数据源适合用于构建“热播资源目录”，以及后续采集模块应遵守什么边界。

该目录服务于运营发现：展示当前热播、更新中、已完结或即将上线的资源，由管理员人工决定是否加入 `watch_titles`。

它不是 Telegram Monitor 的运行统计，也不是自动订阅或推荐系统。

## 2. 固定边界

允许链路：

```text
Official platform source
-> source adapter
-> TrendingCatalogItem
-> normalize / candidate grouping for display
-> local admin catalog
-> operator confirms
-> existing WatchlistApplicationService
```

禁止：

- 在 `monitor/runtime.py` 中抓取视频平台
- 自动把热播资源写入 `watch_titles`
- 绕过 revision 冲突和 watchlist schema 校验
- 使用个人账号 Cookie、会员 token 或验证码绕过
- 抓取视频、字幕、评论、弹幕或个人观看数据
- 将不同平台的热度值直接横向比较
- 用 AI 猜测播出状态、集数或别名
- 把第三方非官方排行伪装成平台官方数据

### 2.1 项目结构

P6-2K 使用独立的 `trending` 业务模块，不放入 `monitor`、`resource` 或 `admin` 模块内部：

```text
backend/
├── app/
│   ├── modules/
│   │   ├── trending/
│   │   │   ├── __init__.py
│   │   │   ├── schema.py
│   │   │   ├── ports.py
│   │   │   ├── service.py
│   │   │   ├── normalizer.py
│   │   │   ├── merger.py
│   │   │   ├── snapshot.py
│   │   │   └── adapters/
│   │   │       ├── __init__.py
│   │   │       ├── base.py
│   │   │       ├── iqiyi.py
│   │   │       ├── wetv.py
│   │   │       ├── netflix_top10_dataset.py
│   │   │       └── youku.py
│   │   ├── admin/
│   │   │   ├── router.py
│   │   │   ├── pages.py
│   │   │   ├── templates/
│   │   │   │   └── trending.html
│   │   │   └── static/
│   │   │       ├── admin.css
│   │   │       └── admin.js
│   │   ├── monitor/
│   │   └── resource/
│   └── main.py
├── tests/
│   ├── trending/
│   │   ├── fixtures/
│   │   │   ├── iqiyi/
│   │   │   ├── wetv/
│   │   │   ├── netflix/
│   │   │   └── youku/
│   │   ├── test_schema.py
│   │   ├── test_normalizer.py
│   │   ├── test_merger.py
│   │   ├── test_snapshot.py
│   │   ├── test_iqiyi_adapter.py
│   │   ├── test_wetv_adapter.py
│   │   ├── test_netflix_top10_dataset_adapter.py
│   │   └── test_youku_adapter.py
│   └── admin/
│       ├── test_trending_api.py
│       └── test_trending_pages.py
└── runtime/
    └── trending/
        ├── sources/
        │   ├── iqiyi.json
        │   ├── wetv.json
        │   ├── netflix.json
        │   └── youku.json
        ├── catalog.json
        └── manifest.json

docs/
├── P6-2K-0_TRENDING_SOURCE_FEASIBILITY_DESIGN.zh-CN.md
└── P6-2K-1_*_DESIGN.zh-CN.md
```

`backend/runtime/trending/` 仅表示默认运行时产物位置，必须加入 `.gitignore`。实际路径应可配置，生产环境不得假定仓库目录可写。

### 2.2 模块职责

| 文件或模块 | 职责 | 禁止承担 |
|---|---|---|
| `trending/schema.py` | 统一 DTO、枚举和稳定字段 | HTTP、HTML 解析 |
| `trending/ports.py` | adapter、snapshot store 的 Protocol | 平台实现细节 |
| `trending/adapters/*` | 单一官方来源获取和原始字段映射 | 跨平台合并、watchlist 写入 |
| `trending/normalizer.py` | 保守标题和状态归一化 | AI 推断、模糊匹配 |
| `trending/merger.py` | 生成未确认的候选展示分组 | 确认作品身份、丢弃平台 item |
| `trending/snapshot.py` | 原子快照、stale 和 last-success 管理 | 数据库业务事务 |
| `trending/service.py` | 编排 adapter、聚合结果和查询 | 直接渲染页面、修改 Monitor |
| `admin/router.py` | 本机只读目录 API；后期承接受控加入请求 | 抓取平台页面 |
| `admin/templates/trending.html` | 筛选、展示、人工选择 | 自动订阅 |

### 2.3 依赖方向

```text
admin page / admin API
        |
        v
TrendingCatalogService
   |             |
   v             v
SourcePort    SnapshotStorePort
   ^
   |
platform adapters
```

依赖只能指向领域内层：

- adapter 依赖 `schema` 和 `ports`，不能依赖 admin。
- admin 依赖 `TrendingCatalogService`，不能直接调用 adapter。
- trending 不依赖 MonitorRuntime、RawMessage、Parser、Dedup 或 EventBus。
- “加入监听”只能由后续 handoff service 调用 `WatchlistApplicationService`。
- `main.py` 只负责装配，不放采集、归一化或合并逻辑。

### 2.4 运行时数据流

```text
scheduler / manual refresh
-> TrendingCatalogService.refresh()
-> enabled SourcePort.fetch()
-> adapter parses official response or dataset
-> TrendingCatalogItem validation
-> conservative normalize / candidate grouping
-> atomic per-source snapshot replace
-> rebuild catalog projection and manifest
-> Admin API reads snapshot
-> UI displays source time and stale state
```

单个平台失败不得让其他平台失败，也不得用空结果覆盖该平台最后一次成功快照。

## 3. 数据源调查

### 3.1 爱奇艺

官方来源：

- [爱奇艺风云榜](https://www.iqiyi.com/ranks1PCW/home)
- [爱奇艺电视剧热播榜](https://www.iqiyi.com/ranks/hotplay/tv)

公开页面可观察字段：

- 排名
- 资源名
- 内容类型
- 实时热度
- 更新至第 N 集 / N 集全
- 即将上线 / 今日上线
- 页面更新时间
- 详情页链接

可行性：`high`

理由：官方榜单可匿名访问，字段相对丰富，能够区分部分更新、完结和待上线状态。爱奇艺官方说明风云榜包含热度、播放指数和飙升等榜单。

风险：页面结构和内部接口可能变化；“实时热度”只具有爱奇艺站内语义，不得与其他平台热度做数值比较。

### 3.2 WeTV 与中国大陆腾讯视频

官方来源：

- [WeTV 官方排行榜](https://wetv.vip/biu/ranks)

公开页面可观察字段：

- 分类排名
- 资源名
- 内容类型
- 腾讯视频详情页链接

来源身份：

```text
source_platform: wetv
source_kind: official_rank
operator_label: WeTV 官方排行
source_region: adapter 调查后显式配置，禁止默认 CN
```

可行性：`probationary`

理由：腾讯体系下的官方页面可匿名读取电视剧、综艺、电影、动漫和纪录片等分类 Top 10，结构清晰。

限制：`wetv.vip` 是 WeTV 国际版入口，不能等同于中国大陆 `v.qq.com` 腾讯视频榜单，也不能默认两者片库、地区和热度口径一致。公开列表未稳定提供更新集数、完结状态或统一热度值。

中国大陆腾讯视频来源单独登记为：

```text
source_id: tencent_video_cn_official_rank
status: research_pending
collection_enabled: no
```

在找到并验证 `v.qq.com` 官方公开来源前，不创建名为“腾讯视频中国大陆”的生产 adapter。

### 3.3 优酷

官方来源：

- [优酷电视剧频道](https://www.youku.com/channel/webtv/list)
- [优酷首页](https://www.youku.com/channel/webhome)

公开页面可观察字段：

- 今日排行榜或频道榜单
- 资源名
- 更新 / NEW / N 集全等展示文案
- 分类和详情入口

可行性：`medium`

理由：官方频道页面存在目录和更新状态展示，但当前只能确认它是页面候选源，不能认定为稳定排行榜协议。

风险：页面依赖动态渲染。实现前必须验证服务端 HTML、公开内嵌 JSON、JavaScript 要求、登录墙和地区差异。在验证完成前固定 `source_kind=official_catalog`、`source_rank=null`，并通过 feature flag 禁用。

### 3.4 Netflix

首选官方来源：

- [Netflix Tudum Top 10](https://www.netflix.com/tudum/top10/tv)
- Netflix 官方 Top 10 结构化工作簿，例如 `all-weeks-global.xlsx`、`most-popular.xlsx`

公开页面可观察字段：

- 全球或国家/地区
- 周期
- 电影 / 节目分类
- 排名
- 标题和季
- views
- runtime
- hours viewed
- 上榜周数

可行性：`high`

理由：Netflix 提供官方结构化历史数据，稳定列包括 `week`、`category`、`weekly_rank`、`show_title`、`season_title`、`weekly_hours_viewed`、`runtime` 和 `weekly_views`。自动采集必须优先下载 XLSX、校验 workbook schema 后映射 DTO；Tudum HTML 只用于人工核验和可见性 fallback，不作为首选采集协议。

限制：它是周榜，不直接表达“更新至第 N 集”或“已完结”。`airing_status` 默认应为 `unknown`；季信息保留为平台原始副标题。官方也说明各地区可用内容不同。

### 3.5 Disney+

官方依据：

- [Disney+ Top 10 Today 官方说明](https://thewaltdisneycompany.com/news/disney-plus-top-ten/)
- [Disney+ What to Watch](https://www.disneyplus.com/explore/what-to-watch)

官方确认 Top 10 Today 按国家或地区展示，并受到订阅内容、账号 profile 和内容分级设置影响；榜单综合单日观看和新内容增长等因素。

可行性：`low_for_anonymous_v1`

原因：

- 榜单可能要求登录或订阅上下文
- 不同国家/地区和 profile 结果不同
- 没有发现适合本项目匿名定时读取的稳定公开数据下载接口
- 不能使用运营人员个人 Cookie 作为后台采集凭证

结论拆分为两个来源：

```text
disney_plus_top10
- source_kind: official_rank
- status: deferred
- 原因: 依赖订阅、profile 和地区上下文

disney_plus_editorial_catalog
- source_kind: official_catalog
- status: manual_or_low_frequency_candidate
- 只能表示新上线、推荐或即将推出，不能标记为 Top 10
```

任何第三方榜单必须明确标记 `source_kind=third_party`。

## 4. 首版数据源决策

```text
P6-2K-1 candidate adapters:
- iqiyi_official_rank: include
- netflix_official_top10_dataset: include
- wetv_official_rank: probationary
- tencent_video_cn_official_rank: research_pending
- youku_official_channel: probationary
- disney_plus_top10: defer
- disney_plus_editorial_catalog: manual_or_low_frequency_candidate
```

建议实施顺序：

1. 爱奇艺：字段最接近“更新/完结资源池”。
2. Netflix：以官方 XLSX 验证周榜、地区和外文标题处理。
3. WeTV：重新确认产品和地区语义，只做 probationary fixture。
4. 中国大陆腾讯视频：继续调查 `v.qq.com` 官方公开来源。
5. 优酷：完成动态页面稳定性试验后再开启。
6. Disney+：Top 10 延期；公开 editorial catalog 只作独立候选来源。

## 5. 统一 DTO

```text
TrendingCatalogItem
- source_platform: iqiyi | wetv | tencent_video_cn | youku | netflix | disney_plus
- source_kind: official_rank | official_catalog | manual | third_party
- source_region: str
- source_category: tv | movie | variety | anime | documentary | other
- source_list_id: str
- source_list_name: str
- ranking_scope: global | country | category | editorial | other
- ranking_period: realtime | daily | weekly | editorial | unknown
- period_start: date | datetime | null
- period_end: date | datetime | null
- source_rank: int | null
- source_item_id: str | null
- title: str
- subtitle: str | null
- season_label: str | null
- airing_status: updating | completed | upcoming | unknown
- latest_episode: int | null
- total_episodes: int | null
- popularity_value: decimal | null
- popularity_unit: str | null
- popularity_label: str | null
- status_evidence: str | null
- source_url: str
- source_updated_at: datetime | null
- collected_at: datetime
- adapter_version: str
- source_fingerprint: str
- source_document_hash: str
```

字段规则：

- `title` 必须来自来源页面，不从简介猜测。
- `airing_status` 只能由明确文案映射。
- “更新至 N 集”映射为 `updating` 和 `latest_episode=N`。
- “N 集全 / 全 N 集 / 已完结”映射为 `completed`。
- “即将上线 / 预约 / 定档”映射为 `upcoming`。
- 无明确证据时为 `unknown`。
- `popularity_value` 必须与 `popularity_unit`、平台和榜单同时展示。
- `source_rank` 必须连同 `source_list_id`、`source_list_name`、`ranking_scope` 和 `ranking_period` 解释。
- Netflix 的 `week` 映射到 `period_start/period_end`，不能塞入 `source_updated_at`。
- `status_evidence` 仅保存清理 HTML 后的短文案并限制长度，例如“更新至18集”；不保存完整响应。
- `source_fingerprint` 标识来源 item，`source_document_hash` 用于内容去重和 schema drift 证据。
- 完整平台响应不得进入公开 DTO、管理 API 或日志。

## 6. 标题归一化与候选展示分组

第一版只做保守归一化：

- Unicode NFKC
- trim 和连续空白压缩
- 英文大小写归一
- 标准化“第 N 季 / Season N”仅用于季字段

禁止仅凭相似标题自动确认作品身份。以下键只能命名为 `candidate_group_key`：

```text
normalized_title + season_label + source_category
```

数据结构固定为两层：

```text
TrendingCatalogItem
- 每个平台 item 永远独立保留

TrendingDisplayGroup
- candidate_group_key
- items[]
- identity_status: unconfirmed | operator_confirmed | conflicting
```

第一版所有自动分组均为 `unconfirmed`。即使 group key 相同，也不能丢弃平台 item、合并热度/状态/链接或自动认定为同一作品。

## 7. 更新策略

建议默认频率：

- 爱奇艺：每 60 分钟
- WeTV：每 2 小时，仅在 probationary adapter 获批后
- 优酷：每 2 小时，连续失败自动暂停
- Netflix：每日最多检查一次 metadata 或内容 hash；只有官方 workbook 内容变化才生成新 source snapshot
- Disney+：首版不自动采集

每个 adapter 必须实现：

- timeout
- bounded retry
- 可配置且诚实的产品 User-Agent 标识
- 单平台并发上限 1
- 条件请求或内容 hash 去重
- fixture contract test
- schema drift 检测
- 最近成功时间和错误码

不得通过高频轮询模拟实时榜单。

### 7.1 来源访问与合规记录

每个 adapter 启用前必须保存人工审查记录：

```text
SourceAccessProfile
- public_without_login: yes | no
- robots_reviewed_at: datetime | null
- terms_reviewed_at: datetime | null
- requires_cookie: yes | no
- requires_javascript: yes | no
- official_download_available: yes | no
- collection_enabled: yes | no
- review_note: str
```

不绕过 robots 或访问控制；不规避 CAPTCHA/反自动化挑战；不旋转代理；不伪装浏览器指纹；401、403 或 CAPTCHA 立即停止；页面内部接口不得天然视作公共 API。

## 8. 存储与生命周期

P6-2K-1 使用“每来源独立成功快照 + 聚合 projection”，不接 Monitor heartbeat，也不写 RawMessage。

```text
SourceSnapshotEnvelope
- schema_version
- source_id
- adapter_version
- fetched_at
- source_updated_at
- last_success_at
- content_hash
- item_count
- stale_after
- items[]
- last_error_code: str | null
```

刷新事务：

```text
single source fetch succeeds
-> validate outcome and non-abnormal empty result
-> atomically replace sources/{source_id}.json
-> rebuild catalog projection from every last-success source snapshot
-> atomically replace catalog.json
-> atomically replace manifest.json last
```

`manifest.json` 是 projection 可见性的提交点，并记录引用的每个 source content hash。读取方只接受 manifest 与 catalog/source hashes 一致的版本。来源失败时保留原 snapshot，仅更新该来源 envelope 的错误和 stale 状态，聚合视图继续使用最后成功数据。

### 8.1 FetchOutcome 与空榜语义

```text
FetchOutcome
- status: success | not_modified | empty_confirmed | schema_drift |
          access_denied | rate_limited | timeout | source_error
- items[]
- source_updated_at
- content_hash
```

- HTML 或 workbook 解析得到 0 items 默认是 `schema_drift`。
- 只有来源提供明确“当前无内容”证据时才允许 `empty_confirmed`。
- `not_modified` 不生成新 catalog version。
- `schema_drift`、访问拒绝或超时不得用空数据覆盖 last-success snapshot。

进入持久化阶段后建议独立表：

```text
trending_catalog_snapshots
trending_catalog_items
```

它们不属于三层资源业务模型的 `RawMessage`，也不能直接创建 `Resource`。只有运营点击“加入监听”时，才通过现有 watchlist application boundary 提交候选配置。

## 9. 管理台边界

新增独立页面“热播资源”，支持：

- 平台、地区、类型和播出状态筛选
- 显示平台排名、热度单位、更新时间和来源链接
- 标记已在 `watch_titles` 中的资源
- 单项或多选“准备加入监听”
- 加入前显示候选标题并要求人工确认
- revision 冲突时重新加载，不静默覆盖 watchlist

不得提供“自动监听全部热播资源”。

加入链路固定为：

```text
TrendingCatalogItem
-> WatchTitleCandidate
-> operator edits and confirms
-> WatchlistApplicationService
```

```text
WatchTitleCandidate
- original_title
- proposed_title
- source_platform
- source_url
- already_present
- normalization_conflicts[]
- expected_watchlist_revision
```

管理员必须看到平台原始标题、拟写入标题、是否已存在、title/alias 归一化冲突和当前 revision。批量加入必须一次确认、一次 revision 检查、一次原子更新，禁止逐项保存造成半批次写入。

## 10. 风险与降级

| 风险 | 处理 |
|---|---|
| 页面结构变化 | adapter 失败关闭，不输出空榜覆盖旧快照 |
| 地区差异 | 所有记录必须带 `source_region` |
| 榜单口径不同 | 不生成跨平台统一热度分数 |
| 状态文案不明确 | 保存为 `unknown` |
| 标题重名 | 仅候选分组，身份保持 unconfirmed |
| 异常空榜 | 视为 schema drift，保留 last-success snapshot |
| 登录或验证码 | 停止采集，不绕过 |
| 来源条款不明确 | adapter 保持禁用，支持人工录入 |
| 数据过期 | UI 显示 stale，不伪装为最新 |

## 11. P6-2K-1 准入门槛

进入实现前必须锁定：

- 先完成 trending core contracts，不直接从平台 adapter 起步
- 首批正式候选仅为爱奇艺和 Netflix 官方结构化数据
- WeTV 与中国大陆腾讯视频身份和地区必须分开
- 优酷保持 feature flag，先完成 fixture 验证
- Disney+ 自动采集延期
- 每个平台保留官方来源 URL 和地区
- 不使用登录 Cookie
- 不做跨平台热度总分
- 不自动修改 watchlist
- 页面结构变化时保留最后成功快照并明确标记过期
- 每个来源完成 `SourceAccessProfile` 人工审查并显式 `collection_enabled=yes`
- DTO 包含榜单身份、统计周期、状态证据和来源 fingerprint/hash
- 实现 per-source last-success snapshot、FetchOutcome 和异常空榜规则
- merge 降级为不确认身份的 display grouping

## 12. 完成标准

P6-2K-0 通过时，只能得出：

- 已找到若干可供后续受控验证的官方热播来源；公开可见不代表获得长期自动采集承诺
- 已固定统一数据模型、地区语义和状态映射
- 已明确自动采集、人工录入和延期来源

不能得出：

- 任何平台 adapter 已稳定运行
- 榜单可永久免费或无限制抓取
- 平台热度可以直接横向比较
- 热播资源会自动进入监听
- Disney+ 已具备稳定匿名数据源

下一阶段：

```text
P6-2K-1A: Trending core contracts
- schema / ports / source snapshot envelope
- normalizer / candidate grouping / offline-only tests

P6-2K-1B: 爱奇艺官方榜单 adapter
- source contract investigation / sanitized fixture / no scheduler

P6-2K-1C: Netflix official XLSX adapter
- workbook schema contract / offline fixture / no scheduler

P6-2K-1D: WeTV / 腾讯体系来源重新调查
- 明确产品、地区和榜单身份；未确认前不启用生产 adapter

P6-2K-1E: 优酷官方频道 probationary adapter
- fixture only / feature flag disabled

P6-2K-2: Catalog service + snapshot projection + admin read-only page
P6-2K-3: Controlled watchlist handoff
```
