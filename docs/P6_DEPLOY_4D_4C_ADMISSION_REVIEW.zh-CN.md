# P6-Deploy-4D-4C 临时 PostgreSQL演练准入评审

> 项目：tg-hub
> 日期：2026-07-16
> 评审对象：`P6_DEPLOY_4D_4C_TEMP_POSTGRES_REHEARSAL_DESIGN.zh-CN.md`
> 结果：approved_for_4C_1_only

> 历史说明：本文记录 4C-1 当时的准入状态；4C-2 当前授权以
> `P6_DEPLOY_4D_4C_2_ADMISSION_REVIEW.zh-CN.md` 为唯一准入依据。

## 1. 总体结论

设计方向与 ARCHITECTURE.md、4C 隔离恢复、4D-4 durable recovery state machine 一致。数据库
adapter、配置 adapter、lifecycle 与 cleanup owner 已拆分，没有把 PostgreSQL 操作塞入 Monitor
或管理台。

```text
P6-DEPLOY-4D-4C_ADMISSION_REVIEW:
  result: approved_for_4C_1_only
  architecture_direction: aligned
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_4D_4C_1_implementation: yes
  allow_P6_Deploy_4D_4C_2_real_temp_postgres: no
  allow_P6_Deploy_4D_4C_3_fault_acceptance: no
  allow_P6_Deploy_4D_4C_4_archive: no
  allow_production_config_read: no
  allow_production_package_read: no
  allow_LaunchAgent_or_Telegram: no
  allow_production_recovery: no
  allow_P6_Deploy_5: no
```

## 2. 关键边界复核

通过项：

- synthetic source/dump 与真实 package 完全隔离；
- capability 由 fixture 唯一 mint，数据库名不能由调用者指定；
- capability mint 已拆为 fake/real observation provider 与统一 issuer，4C-1 不需连接 PostgreSQL；
- issuer 区分 initial 与 record-bound resume_cleanup，durable record 本身不授予操作权限；
- 独立 durable rehearsal record 持有 source/replacement cleanup ownership；
- loopback、显式 libpq spec 和 server identity 均有 guard；
- high-level `RestoreVerificationService` 被禁止复用，避免提前 DROP replacement；
- lower-level CREATE/COMMENT/verify/PgToolRunner 采用组合复用；
- CREATE、COMMENT、restore、verification、DROP 使用阶段化 identity guard；
- `pg_restore --dbname` 与 allowlist `PGDATABASE` 双重绑定同一 generated replacement；
- owner 使用 role OID 与 identity digest，同名 role 重建不能冒充原 owner；
- server identity 在每个关键操作前通过实时受控连接复验；
- empty catalog 使用固定 allowlist，不允许动态放宽；
- workflow terminal 与 rehearsal terminal 分离，source residue 会阻止整体成功；
- restore partial state 不盲目重跑；
- temp application fixture 不启动真实应用或端口；
- Monitor write fence 继续为 fake，未访问 Telegram；
- rollback 后才允许 child cleanup replacement；
- source cleanup 与 replacement child owner 分离；
- residue cleanup 不得用无条件 DROP 掩盖失败。

## 3. 实施期硬约束

4C-1 只能实现纯契约和 fake adapter。任何测试中出现以下行为均视为越界：

- `create_async_engine()` 连接真实地址；
- 执行 `pg_dump`、`pg_restore` 或 PostgreSQL DDL；
- 读取 `Settings()`、`TG_HUB_ENV_FILE` 或 `~/.tg-hub`；
- 构造真实 backup inventory/package；
- 调用 launchctl、Telethon、Uvicorn 或 Bot；
- 新增可以由 CLI/环境变量普通构造的 temp PostgreSQL capability。

4C-1 应交付：

- strict DTO/capability；
- fake observation provider、统一 issuer 及 real provider fail-closed boundary；
- initial/resume_cleanup scope enforcement 与 record-bound resume issuance；
- durable rehearsal record、stable run lock 与 source intent-first recovery；
- generated identity/name factory；
- connection policy validator；
- pg_dump/pg_restore argv 与 allowlist env builder；
- adapter protocols 和 fake implementations；
- partial/complete/unknown 纯协调函数；
- production fail-closed entry；
- fake/temp filesystem tests；
- 完整回归。

## 4. 非阻塞建议

1. generated source/replacement name 使用不同固定前缀，便于 residue inventory 分类；
2. server system identifier 只保存 digest，不输出原值；
3. 4C-2 前单独确认本机 PostgreSQL role 具备 CREATEDB，但不得是 superuser 作为设计前提；
4. integration fixture 使用脱敏 per-run `application_name`，便于 drain guard。

## 5. 最终准入复审

上一轮两个 blocker 已关闭：

1. capability mint 不再要求 4C-1 连接 maintenance database。fake provider 与未来 real provider
   通过同一不可公开构造的 observation 和 issuer 收口，真实 provider 仍受 4C-2 Gate 阻止。
2. source 身份和 cleanup phase 不再只存在于不可重建 capability。独立 durable rehearsal record
   采用 intent-first phase、stable run lock 和 guarded resume，可在 workflow terminal 后继续收口
   source cleanup，并以 rehearsal terminal + residue=0 判定整体成功。恢复权限通过 record-first、
   锁内重读和受信 observation 签发 cleanup-only capability；读取 record 本身不构成授权。

同时已固定 generated source 只使用当前 Alembic head，且 temp application 使用脱敏 per-run
`application_name`。

```text
P6-DEPLOY-4D-4C_FINAL_ADMISSION_REREVIEW:
  result: approved_for_4C_1_only
  architecture_direction: aligned
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_4D_4C_1_implementation: yes
  allow_P6_Deploy_4D_4C_2_real_temp_postgres: no
  allow_P6_Deploy_4D_4C_3_fault_acceptance: no
  allow_P6_Deploy_4D_4C_4_archive: no
  allow_production_config_read: no
  allow_production_package_read: no
  allow_production_recovery: no
```

## 6. 最终准入

当前只批准 **P6-Deploy-4D-4C-1 契约与 fake adapter 实现**。

4C-1 完成后必须先做最终代码复审。只有复审通过并单独授权，才可进入 4C-2，在本机 loopback
PostgreSQL 上创建 generated temp databases。真实生产恢复在整个 4C 阶段始终禁止。
