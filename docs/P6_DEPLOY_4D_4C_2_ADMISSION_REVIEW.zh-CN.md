# P6-Deploy-4D-4C-2 本机临时 PostgreSQL 集成演练准入评审

> 项目：tg-hub
> 阶段：P6-Deploy-4D-4C-2
> 日期：2026-07-16
> 状态：happy-path-complete

> 实施进度：4C-2A real provider/adapter 最终代码复审已通过；首次 read-only runtime preflight
> 未通过；dedicated role 创建后第二次 read-only runtime preflight 已通过。真实 generated
> PostgreSQL happy-path 演练已完成；脱敏结果见
> `P6_DEPLOY_4D_4C_2_HAPPY_PATH_RESULT.zh-CN.md`。

4C-2A 实现期增加了 durable replacement workflow guard：replacement CREATE、identity commit
分别要求 4B 主记录处于 `replacement_create_started`、`identity_commit_started`；replacement DROP
要求主记录 `rolled_back` 且绑定 cleanup child 处于 `cleanup_started`。仅有 rehearsal record 或
capability 不足以执行 replacement 操作。

## 1. 前置证据

- 4C-1 最终代码复审通过；
- commit：`24cff51 feat: add temporary postgres recovery rehearsal contracts`；
- 相关回归：`108 passed, 1 skipped`；
- capability 已不可变，并冻结 root、server/role、connection spec 与 phase-bound cleanup targets；
- durable rehearsal record、intent-first phase、stable lock 和 record-bound resume 已实现；
- production adapter 与 real provider 在授权前保持 fail closed；
- 工作区在评审开始时 clean。

## 2. 评审结论

```text
P6-DEPLOY-4D-4C-2_ADMISSION_REVIEW:
  result: approved_with_runtime_gate
  architecture_direction: aligned
  blockers: 0
  required_clarifications: 0
  allow_4C_2_implementation: yes
  allow_read_only_runtime_preflight: yes
  allow_real_generated_temp_postgres_execution: yes_after_preflight
  allow_real_source_and_replacement_create: yes_after_preflight
  allow_real_generated_database_drop: yes_after_preflight
  allow_production_config_read: no
  allow_production_package_read: no
  allow_existing_database_restore: no
  allow_unknown_connection_termination: no
  allow_LaunchAgent_or_Telegram: no
  allow_4C_3_fault_injection: no
  allow_production_recovery: no
  allow_Deploy_5: no
```

4C-2 可以实现 real observation/provider、generated database adapter、synthetic source/dump、restore、
read-only verifier、temp config/lifecycle、rollback 和 guarded cleanup。该授权不包含任何现有数据库
的数据恢复或删除。

## 3. Runtime Preflight

首次真实连接前必须只读验证，且全部通过：

1. connection spec 由显式 4C test fixture 注入，不读取 `Settings`、`production.env` 或
   `~/.tg-hub`；
2. host 是数值 loopback，port/user/maintenance database 显式存在，无 URL query/service/socket；
3. maintenance database 不等于任何 generated source/replacement；
4. server identity、current role OID/name digest 可读取；
5. role 具备 `CREATEDB`，但 `rolsuper=false`；
6. prepared transaction 与 activity inventory 查询可用，权限不足按 fail 处理；
7. `pg_dump`、`pg_restore` 使用已验证绝对路径，major 相同且兼容 server；
8. temp recovery root 为本轮私有目录，`0700`、no symlink；
9. generated source/replacement 名称在 server 上均不存在；
10. production database 名、URL、备份目录或业务数据没有被读取。

任一项失败时必须在 CREATE DATABASE 前停止，只输出稳定脱敏错误码。

## 4. 真实动作授权边界

preflight 通过后，本次 Gate 只允许：

```text
CREATE generated source
-> COMMENT/owner read-back
-> current Alembic head
-> bounded synthetic rows
-> custom pg_dump
-> CREATE generated replacement
-> COMMENT/owner read-back
-> pg_restore explicit generated replacement
-> PostgreSQL READ ONLY verification
-> temp application switch/readiness
-> fake Monitor write fence
-> rollback generated source
-> phase-bound guarded DROP replacement
-> phase-bound guarded DROP source
-> residue inventory == 0
```

所有数据库名由 capability 生成。禁止 CLI、环境变量或调用者指定 source/replacement 名称。

## 5. Fail-Closed 与停止条件

遇到以下任一情况必须停止，不得尝试扩大权限或无条件清理：

- server/role/connection/root identity 变化；
- 同名 generated database 已存在；
- COMMENT、owner OID 或 identity token 不匹配；
- catalog 为 partial/unknown；
- 未知 active connection 或 prepared transaction；
- restore、verification、rollback 或 record durability 失败；
- cleanup target 不在 capability 的 phase-bound target 集合；
- record/lock stale、replaced、invalid 或 manual reconciliation；
- source/replacement 以外出现疑似本轮对象。

失败后保留 record 和可证明身份的 residue，报告 cleanup handle；禁止 teardown 用无条件 DROP
伪造通过。

## 6. 实施与验收顺序

```text
4C-2A real provider/adapter implementation
-> fake + contract tests
-> code review
-> read-only runtime preflight
-> generated source/dump/restore happy-path execution
-> rollback/guarded cleanup
-> residue=0
-> acceptance archive
```

4C-2 不注入 cancel、partial restore、未知连接占用或 cleanup crash；这些属于未授权的 4C-3。

## 7. 完成口径

4C-2 通过只能证明：

```text
4D-4 durable state machine 可以在本机 generated PostgreSQL databases 上完成
synthetic dump -> restore -> verify -> switch -> rollback -> guarded cleanup
```

不能证明真实备份包已恢复、生产数据库已切换，或生产恢复已授权。

## 8. 4C-2A 最终代码复审

```text
P6-DEPLOY-4D-4C-2A_FINAL_CODE_REVIEW:
  result: approved
  blockers: 0
  required_changes: 0
  tests: 116 passed, 1 skipped
  real_postgres_accessed: no
  allow_read_only_runtime_preflight: yes
  allow_generated_database_create: no_before_preflight
  allow_generated_database_drop: no_before_preflight
  allow_4C_3: no
  allow_production_recovery: no
```

复审期间关闭的关键问题：

- cleanup capability 的 DROP target 已绑定 durable rehearsal phase；
- capability、observation 和 resume evidence 均不可变；
- connection spec 通过脱敏 digest 冻结并在 builder/resume 时复验；
- CREATE/COMMENT 必须存在 durable rehearsal intent；
- replacement CREATE/COMMENT/DROP 同时受 4B 主记录和 cleanup child ownership 约束；
- CREATE 后 owner/comment/activity/prepared-xact read-back 固定；
- real engine 使用有界 connect/command timeout；
- 首次只读预检发现 overall latency 未收口后，已增加 provider overall timeout 和取消清理测试；
- empty catalog 覆盖 schema/relation/sequence/function/type/extension，并拒绝 user-owned system
  schema object。

## 9. 首次 Read-Only Runtime Preflight

```text
P6-DEPLOY-4D-4C-2_RUNTIME_PREFLIGHT:
  status: fail
  error_code: TEMP_REHEARSAL_CONNECTION_UNSAFE
  loopback_target: yes
  maintenance_database: postgres
  pg_dump_major: 16
  pg_restore_major: 16
  role_can_create_database: yes
  role_is_superuser: yes
  blocker: dedicated_non_superuser_createdb_role_missing
  database_writes_attempted: no
  generated_database_created: no
  generated_database_dropped: no
  production_config_accessed: no
  production_package_accessed: no
  allow_generated_postgres_rehearsal: no
```

预检严格按 Gate 在角色检查处停止。当前角色虽然具备 `CREATEDB`，但同时为 superuser，不满足
4C-2 dedicated non-superuser role 边界。不得通过放宽检查继续；需要先单独设计/创建受限测试角色，
然后重新执行 read-only preflight。

## 10. Dedicated Role 后重新预检

```text
P6-DEPLOY-4D-4C-2_RUNTIME_PREFLIGHT_V2:
  status: pass
  dedicated_role_exact: yes
  loopback_target: yes
  maintenance_database: postgres
  server_major: 16
  pg_dump_major: 16
  pg_restore_major: 16
  role_can_create_database: yes
  role_is_superuser: no
  prepared_xacts_readable: yes
  activity_inventory_readable: yes
  system_identifier_readable: yes
  generated_names_absent: yes
  database_writes_attempted: no
  allow_generated_postgres_happy_path: yes
  allow_4C_3_fault_injection: no
  allow_production_recovery: no
```
