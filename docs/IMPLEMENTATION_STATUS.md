# TG-HUB 实施进度

> 架构基准：docs/ARCHITECTURE.md V2.1-final
> 最后更新：2026-07-01

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
| P4 | Dedup + Merge + ResourceSource | ⏳ 待开始 | — |
| P5 | EventBus + Bot | ⏳ 待开始 | — |

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

**P3-B 合计：29 测试，全部通过；Normalizer 合计：77 测试；全量合计：175 测试**

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

**P3-A 合计：48 测试，全部通过；全量合计：146 测试，全部通过**

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

**P2-C 合计：5 测试，全部通过；全量合计：98 测试，全部通过**

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

**P2-B 合计：72 测试，全部通过；Parser 合计：86 测试，全部通过**

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
