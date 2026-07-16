# tg-hub 生产恢复 Runbook

> 阶段：P6-Deploy-4D-4D
> 状态：archived
> 默认策略：只读评估；真实生产恢复必须逐 Gate 单独授权

## 1. 适用范围

本手册用于 PostgreSQL 生产库不可用、数据损坏或需要回退到已验证备份时的人工恢复。恢复固定采用
replacement database，不覆盖当前生产库。真实恢复不是日常启动流程，也不是 Deploy-5 的必做动作。

禁止把本手册用于：日常迁移、测试数据导入、历史回填、自动 failover 或无人值守恢复。

## 2. 已验证能力

- 真实生产备份创建与只读 package 校验；
- 真实备份到隔离数据库的 restore、schema/constraint/integrity 验证和 guarded DROP；
- verification sidecar、inventory、pin/hold 与 retention 保护；
- fake 全流程的配置切换、readiness、rollback 和 child cleanup；
- generated PostgreSQL 的 Alembic、dump、restore、READ ONLY verify、临时切换、rollback、双层 cleanup；
- generated database 全局 residue 为 0。

这些证据不等于真实生产恢复已经执行。

## 3. 操作前硬门槛

1. 明确事故负责人、操作人和复核人；同一人不得跳过复核 Gate。
2. 停止 Monitor，并确认 `telethon-session.lock` 空闲。
3. 确认主应用停止且数据库连接已 drain。
4. 从 backup inventory 选择 `manifest_status=pass`、`catalog_status=pass`、
   `restore_verified=yes`、`verification_status=valid` 的 package。
5. 为选定 package 创建 production recovery hold；不得依赖普通 pin 代替。
6. 冻结原数据库、replacement 名称、owner、Alembic revision、package 三项 checksum、env/watchlist
   checksum 和授权 nonce。
7. 先创建 protection backup，除非事故负责人单独签署 skip 授权。
8. PostgreSQL maintenance 连接、`pg_dump`、`pg_restore` 和磁盘空间预检全部通过。

任一身份、checksum、锁、连接或 prepared transaction 不确定时立即停止，进入人工协调，不猜测。

## 4. 只读检查

在 `backend/` 目录执行：

```bash
TG_HUB_ENV_FILE="$HOME/.tg-hub/production.env" \
  ../.venv/bin/python -m app.deploy.backup_inventory

../deploy/status.sh
../deploy/rotation_status.sh
curl -fsS http://127.0.0.1:8010/health/ready
```

不得在终端、工单或报告中输出 DSN、密码、Telegram session、token、完整路径或业务数据。

## 5. 真实恢复 Gate

每一 Gate 都需要新的明确授权；前一 Gate 成功不自动授权下一 Gate。

### PR-R0：事故与输入冻结

- 创建 durable recovery record 和 operation lease；
- 冻结选定 backup、原数据库、replacement、配置及 owner identity；
- 创建 recovery hold；
- 只读校验 package 与 verification sidecar。

### PR-R1：保护备份

- 对当前生产状态创建 protection backup；
- 验证 package final commit、manifest、dump、watchlist snapshot；
- 失败时不得创建 replacement。

### PR-R2：创建 replacement

- durable `replacement_create_started` intent 先落盘；
- 创建自动生成的 replacement database；
- 校验名称、owner、COMMENT、连接数和 prepared transaction；
- durable identity commit 后才可 restore。

### PR-R3：Restore 与只读验证

- `pg_restore` 目标必须同时绑定 argv 与 allowlisted `PGDATABASE`；
- restore 完成后使用 PostgreSQL `BEGIN READ ONLY`；
- 验证 Alembic revision、核心表、列、约束、外键动作和完整性；
- partial/unknown catalog 停止，不重复盲跑 restore。

### PR-R4：停止生产服务

- 停止 Monitor；
- 确认 session lease 空闲；
- 停止应用并确认连接 drain；
- 未全部确认前不得修改生产配置。

### PR-R5：配置切换

- 先保护原 env/watchlist；
- 原子切换 `DATABASE_URL` 的 database component；
- watchlist 只有在单独授权时才切换；
- 不修改 host、port、user、password 或 Telegram 配置。

### PR-R6：应用 readiness

- 启动应用但暂不启动 Monitor；
- 验证 current database、migration、核心只读查询和管理台 readiness；
- readiness 未通过时进入 rollback。

### PR-R7：Monitor 授权与观察

- 单独授权 Monitor 启动；
- 冻结 generation 与 write baseline；
- 有界观察首条写入；
- Monitor 一旦进入启动阶段，不再自动 rollback，必须人工协调。

## 6. Rollback

只允许在 Monitor 尚未进入启动阶段时执行自动化 rollback：

```text
停止 fake/real monitor（如尚未启动则确认）
-> 停止 replacement application
-> drain replacement connections
-> 恢复原 env/watchlist
-> 启动原数据库 application
-> 原数据库 readiness
-> rolled_back durable
-> child cleanup guarded DROP replacement
```

DROP 前必须重新验证 replacement 名称、owner、COMMENT token、active connections=0、
prepared transactions=0。DROP 成功后才记录 `drop_observed=yes`；child terminal 后 main 才能写
`cleanup_completed=yes`。

## 7. 事故后处理

- 保留旧生产数据库，不自动删除；其归档/删除属于新的维护 Gate。
- 确认 recovery hold、record、cleanup child 和配置快照均收口。
- 新建真实备份并做隔离恢复验证。
- 记录每个 Gate 的授权人、时间、稳定结果码和脱敏证据。
- 任何 orphan record/hold、unknown identity 或 residue 都必须进入人工复核。

## 8. 当前授权状态

```text
production_restore_executed: no
production_restore_authorized: no
runbook_archived: yes
generated_postgres_rehearsal: pass
real_backup_and_isolated_restore: pass
```
