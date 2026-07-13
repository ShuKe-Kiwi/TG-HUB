# P6-Arch-Fix-1 原始证据边界修正

> 状态：implemented
> 日期：2026-07-14
> 范围：RawMessage 原始证据完整性与不可删除约束

## 目标

修正两项与 `ARCHITECTURE.md` 不一致的实现：

1. 真实 Telethon 消息未向 RawMessage 传递媒体引用和结构快照；
2. 删除 Channel 会通过数据库外键级联删除 RawMessage。

## 已锁定契约

真实消息转换为 `IncomingMessage` 时保存：

- 原始 `text` / `caption`；
- `raw_media_refs`：媒体类型、Telegram 媒体 ID、MIME 类型和大小；
- `raw_payload`：`schema_version`、消息 ID、频道 ID、媒体存在性、分组、回复关系、编辑时间和媒体类型。

明确禁止保存：

- `access_hash`；
- `file_reference`；
- Telegram Session 或 API 凭据；
- 整个 Telethon 对象的无界序列化；
- 媒体文件下载；
- 在运行报告中输出正文或完整 payload。

## 删除策略

`raw_messages.channel_id -> channels.id` 固定为 `ON DELETE RESTRICT`。

存在 RawMessage 的 Channel：

- 不允许物理删除；
- 使用 `status=paused/error/archived` 等状态管理生命周期；
- ResourceSource 证据关系继续保留。

## 边界确认

- Monitor runtime 仍不直接访问数据库；
- adapter 只构造 DTO；
- ingestion boundary 继续拥有 RawMessage 映射；
- Parser、Normalizer、Dedup、EventBus 和 Bot 行为不变；
- 不下载媒体，不增加历史回填。

## 验收

- photo/document 受控证据快照测试通过；
- ingestion 媒体引用原样保存测试通过；
- Channel 删除被数据库拒绝且 RawMessage 继续存在；
- Alembic 从空测试库升级至新 head 通过；
- 完整回归：`537 passed`。
