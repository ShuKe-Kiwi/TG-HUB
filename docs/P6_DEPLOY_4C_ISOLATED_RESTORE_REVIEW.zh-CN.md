# P6-Deploy-4C 隔离恢复验证实施评审

> 项目：tg-hub  
> 阶段：P6-Deploy-4C  
> 状态：C2-real-isolated-restore-accepted
> 前置：P6-Deploy-4B 真实备份与只读 validator 已通过  
> ALLOW_IMPLEMENTATION：C1-only  
> ALLOW_REAL_RESTORE_VERIFY：completed
> ALLOW_PRODUCTION_RESTORE：no  
> ALLOW_P6_DEPLOY_4D：no

## 1. 阶段目标

P6-Deploy-4C 实现受控的隔离恢复验证：

```text
validated final package
-> generated isolated database identity
-> private recovery record
-> empty restore target
-> pg_restore
-> schema/constraint/integrity verification
-> guarded DROP on success
-> desensitized report
```

4C 通过后只能证明选定 package 可恢复到本机同 major PostgreSQL 的隔离数据库。
不能证明生产恢复、跨机器迁移、凭据恢复或零数据损失。

## 2. 阶段 Gate

### Gate C1：代码实现

允许：

- restore target/recovery DTO；
- maintenance adapter、identifier guard、recovery record store；
- `pg_restore` owner；
- schema/constraint/read-only smoke verifier；
- guarded cleanup；
- fake adapter 和临时非生产 PostgreSQL 测试。

禁止访问当前真实 backup package、创建真实验收 target 或执行生产恢复。

### Gate C2：真实隔离恢复验收

需再次明确授权。只允许选定已通过 4B validator 的真实 package，在自动生成的隔离
数据库中恢复、验证并 guarded DROP。

C1 批准不等于 C2 批准。

## 3. 输入与锁生命周期

唯一输入为 canonical `backup_id`：

```text
python -m app.deploy.restore_verify run --backup-id <backup_id>
```

禁止接受：

- 任意 package path；
- 任意 target database name；
- production database name；
- 用户提供 identity token；
- `--clean`、`--create` 或附加 pg_restore 参数。

执行必须先获取 `.backup.lock` shared，并覆盖：

```text
package validation
-> recovery record
-> target create/comment
-> pg_restore
-> database verification
-> guarded DROP or failure report settlement
-> release shared lock last
```

因此 retention/backup creation 在整个 restore verify 期间不能获取 exclusive lock。

## 4. 数据库连接边界

继续使用 4B 的 `parse_pg_connection_spec()` 与 allowlist libpq env。生产 URL 只用于
取得 host/port/user/password 和 production database name；不得把生产 database 作为
restore target。

maintenance database 第一版固定为 `postgres`：

- 若 production database 本身为 `postgres`，仍允许连接 maintenance，但 target 必须
  为程序生成的独立名称；
- host/user/database 必须显式；
- URL query 与 Unix socket 继续拒绝；
- maintenance 与 target server major 必须等于 manifest source/pg_dump/pg_restore
  major；
- 不输出 URL、role、host、database name 或密码。

CREATE DATABASE、COMMENT 和 DROP 不能在普通事务中执行。maintenance adapter 使用
独立 AUTOCOMMIT connection；业务验证 connection 使用普通 transaction，并在连接后
立即确认 `current_database()`。

## 5. Identifier 与 SQL 安全

target name 先通过 `RESTORE_TARGET_PATTERN`，再使用服务端 PostgreSQL quoting：

```text
SELECT format('CREATE DATABASE %I', :target_name)
SELECT format('COMMENT ON DATABASE %I IS %L', :target_name, :comment)
SELECT format('DROP DATABASE %I', :target_name)
```

返回的单条 SQL 只能由 maintenance adapter 内部立即执行。固定规则：

- name/comment 都以 bind parameter 传入 `format()`；
- 执行前后二次校验 target name；
- format 结果不得写日志、结果或 recovery record；
- 禁止 Python f-string、`%`、`.format()`、shell 或裸字符串拼 identifier/literal；
- owner/comment 查询继续使用 bind parameter；
- identity comment 固定为 `tg-hub-restore-verify:<64-hex-token>`。

## 6. Recovery record 状态机

路径固定：

```text
~/.tg-hub/runtime/restore-recovery/<opaque-id>.json
```

目录 `0700`，文件 `0600`，safe-open、no-follow、atomic replace、directory fsync。

4C 将 phase 扩展为：

```text
planned
create_started
database_created
identity_commit_started
identity_committed
restore_started
restore_failed
verification_failed
verification_passed
drop_failed
```

合法转换：

```text
planned -> create_started
create_started -> database_created
database_created -> identity_commit_started
identity_commit_started -> identity_committed
identity_committed -> restore_started
restore_started -> restore_failed | verification_failed | verification_passed
verification_passed -> drop_failed
```

状态机模块必须硬校验以上转换，orchestrator 不得直接覆盖 phase。`drop_failed` 只能由
`verification_passed` 转入；`restore_failed`、`verification_failed`、
`identity_committed` 等 phase 的 cleanup DROP 失败时保留原 phase。

成功 DROP 后删除 recovery record，不增加 terminal `dropped` phase。任何 phase 更新必须
先原子持久化，再进入下一不可逆步骤。

固定顺序：

```text
generate name/token/opaque_id
-> planned record durable
-> create_started record durable
-> CREATE DATABASE
-> verify database exists and owner
-> database_created record durable
-> identity_commit_started record durable
-> COMMENT identity durable in PostgreSQL
-> read back and verify comment
-> identity_committed record durable
-> connect target and verify current_database()
-> restore_started record durable
-> pg_restore
-> verification phase durable
-> guarded DROP
-> delete record + fsync recovery directory
```

phase write 失败不得继续下一步。intent phase 必须在对应 PostgreSQL 不可逆操作之前持久化：

- `planned`：尚未声明执行 CREATE；
- `create_started`：CREATE 可能尚未执行，也可能已经成功但尚未完成结果确认；
- `database_created`：database 和 owner 已确认，尚未声明执行 COMMENT；
- `identity_commit_started`：COMMENT 可能尚未执行，也可能已成功但尚未完成结果确认；
- `identity_committed`：comment 已 read-back 并确认等于 token。

进程恢复不得根据“下一 phase 尚未写入”推断前一 PostgreSQL 操作未发生，必须按当前
intent phase 和 PostgreSQL 实际状态执行协调。

## 7. Target identity guards

所有检查点都必须验证 target name schema、production/maintenance 排除和 recovery record
backup ID。其余 guard 按阶段固定，不得套用一个无法满足的统一列表：

| 检查点 | exists | owner | comment | current_database | activity / prepared xact |
| --- | --- | --- | --- | --- | --- |
| CREATE 后 | 是 | expected | 必须为空 | 不要求 | 不要求 |
| COMMENT 后 | 是 | expected | 必须等于 token | 不要求 | 不要求 |
| restore 前 | 是 | expected | 必须等于 token | 必须匹配 | 不要求 |
| verification 前 | 是 | expected | 必须等于 token | 必须匹配 | 不要求 |
| DROP 前 | 是 | expected | 必须等于 token | 本轮连接已关闭 | 必须均为空 |

DROP 前 `pg_stat_activity` 不得存在其他 target connection，`pg_prepared_xacts` 不得存在
属于 target database 的 prepared transaction。

发现活动连接时不 terminate、不 kill，返回 `RESTORE_TARGET_IN_USE` 并保留数据库和
recovery record。发现 prepared transaction 时返回 `RESTORE_TARGET_PREPARED_XACT`
并保留数据库和 recovery record；本阶段不执行 `ROLLBACK PREPARED`。

phase cleanup 固定为：

- `planned`：数据库不存在时删除 record；若同名数据库存在，不推断其身份且禁止 DROP；
- `create_started`：数据库不存在时删除 record；数据库存在时，只有 name、owner、
  production/maintenance 排除和 comment 为空全部符合，才允许 guarded DROP；
- `database_created`：数据库必须存在，且 name、owner、production/maintenance 排除和
  comment 为空全部符合，才允许 guarded DROP；
- `identity_commit_started`：在其他 identity guard 均通过时，comment 为空或精确等于
  expected token 都允许 guarded DROP；其他 comment 值一律视为 identity mismatch；
- `identity_committed` 及后续 phase：必须通过 name、owner、comment token、backup ID、
  activity 和 prepared transaction 全部 guard 才允许 DROP。

`verification_passed` 或 `drop_failed` 下若 database 已不存在，表示 DROP 已完成但 record
删除/落盘收口未完成。此时只校验 record 自身 schema、phase、canonical backup ID、target
name pattern 和 production/maintenance 排除，随后删除 stale recovery record。除这两个
phase 外，数据库意外不存在不得被报告为成功清理。

cleanup 不迁移 phase。cleanup 成功时确认数据库不存在后直接删除 recovery record；
cleanup 失败时保留原 phase，不引入 `cleanup_done`、`cleanup_completed` 等新状态。

## 8. pg_restore 契约

### 8.1 实施期发现的 PostgreSQL 工具约束

真实临时 fixture 验收确认：`pg_restore` 在 argv 同时缺少 `-d/--dbname` 和 `-f/--file`
时直接拒绝执行：

```text
pg_restore: error: one of -d/--dbname and -f/--file must be specified
```

因此，仅设置 `PGDATABASE=<generated target>` 不能构成可执行的 restore target 选择契约。
真实测试进一步确认固定空 literal `--dbname=` 只进入 SQL 输出模式，不会连接
`PGDATABASE`。最终最小修正已通过本机 PostgreSQL 16.14 临时 fixture 全链路验证：
显式 `--dbname=<generated target>` 进入 direct-restore 模式，且 env `PGDATABASE` 必须与
argv target 精确一致。

固定 argv：

```text
pg_restore
--dbname=<generated target>
--no-owner
--no-acl
--exit-on-error
<validated final database.dump>
```

generated target 是随机、受正则保护且不含凭据的临时标识，允许作为唯一非固定 argv
值。argv 不得包含 URL、密码、production database、`-d` 分离参数或 connection URI；
allowlist env 中 `PGDATABASE` 必须与 argv generated target 精确一致。
禁止 `--clean`、`--create`、jobs 并行和用户附加项。

测试必须同时断言：argv 恰好包含一个 `--dbname=<generated target>`，不包含 `-d`、URL、
密码或 production database；env `PGDATABASE` 精确等于同一 generated target。支持新的
PostgreSQL major 前必须重跑该真实临时 fixture 契约测试。

开始 `pg_restore` 前必须新建 target connection 并确认 `current_database()`、owner 和
identity comment，随后关闭该确认连接。不能仅凭 maintenance catalog 的先前结果启动恢复。

复用 `PgToolRunner` 的 bounded output/timeout/cancel/terminate/kill/reap。取消顺序：

```text
settle pg_restore
-> persist restore_failed when possible
-> close target connection
-> settle guarded target state
-> release backup shared lock last
-> propagate cancellation
```

取消不自动 DROP 已开始恢复的 target；保留 recovery record 供受控 cleanup。

timeout 来源固定为本机受控配置 `RESTORE_VERIFY_TIMEOUT_SECONDS`，必须为有限正整数，
使用代码定义的安全默认值和上下限。备份 manifest 不得控制执行时限；最终脱敏报告记录
实际采用的 timeout 秒数。

## 9. 版本与 package 前置

恢复前必须重新调用 `BackupPackageValidator`，并确认：

- final package/manifest/checksum/watchlist/catalog 全部 pass；
- `manifest.source_server_major == manifest.pg_dump_major`；
- `local pg_restore_major == manifest.pg_dump_major`；
- `restore target server major == manifest.source_server_major`；
- manifest Alembic revision 等于当前代码唯一 head。

不满足时不得创建 recovery record 或 target database。

## 10. 恢复后验证

恢复前不运行 Alembic upgrade。验证连接建立后必须由 PostgreSQL 执行数据库级只读事务：

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
```

不得仅依赖 ORM `session.begin()`、不调用 `flush()` 的约定或应用层标记。事务建立后读取
`transaction_read_only` 并要求为 `on`；否则立即失败并回滚。

### 10.1 Schema

- `alembic_version` 恰好一行且等于 manifest revision；
- required tables 精确存在于 `public`；
- 不接受同名 view/materialized view 代替 table；
- required columns 及基本类型/nullable 契约与当前 SQLAlchemy metadata 一致。

schema/column/type 查询统一使用 `pg_class`、`pg_namespace`、`pg_attribute` 和相关 PostgreSQL
catalog；第一版不使用 `information_schema` 作为验证真值来源。

### 10.2 Constraints

按 PostgreSQL catalog 的结构字段验证，不按 constraint text substring：

- required primary keys；
- 已锁定 unique constraints；
- required foreign keys 的 source/target columns；
- FK update/delete action；
- `raw_messages.channel_id -> channels.id` 为 `ON DELETE RESTRICT`；
- 所有 required constraints 均 validated。

不要求 constraint name 与 ORM 默认名字完全相同，按语义匹配。

### 10.3 Integrity 与 smoke

- 每个核心表在上述 `SET LOCAL statement_timeout = '30s'` 约束下执行内部 `COUNT(*)`，
  但不输出具体业务行数；
- orphan FK 检查为零；
- RawMessage 三状态字段满足既有约束；
- repository 查询使用 target session、read-only transaction；
- 禁止 flush/commit/DDL/写入；
- 不调用 Parser、Normalizer、Dedup、EventBus、Bot 或 Monitor。

报告只输出检查数、pass/fail、耗时和稳定错误码。

## 11. 成功与失败收口

成功：

```text
verification_passed durable
-> close every target connection
-> repeat identity/owner/comment/activity guards
-> DROP target
-> confirm database absent
-> delete recovery record
-> report pass
```

若 DROP 已成功但 record 删除失败，本次返回稳定失败并保留 cleanup handle；后续 cleanup
按第 7 节仅对 `verification_passed` / `drop_failed` 的 database-absent 状态删除 stale record，
不得再次执行 DROP。

失败：

- restore 失败：phase=`restore_failed`，保留 target/record；
- schema/integrity 失败：phase=`verification_failed`，保留 target/record；
- DROP 失败：phase=`drop_failed`，保留 target/record；
- identity/owner/token 不匹配：禁止 DROP；
- 正式报告不输出 target name/token；
- 只输出 opaque cleanup handle。

受控清理：

```text
python -m app.deploy.restore_verify cleanup --recovery-record <opaque-id>
  --backup-id <canonical-backup-id>
```

cleanup 重新加载 record、获取 backup shared lock、重复全部 identity/owner/comment/activity
guards，并要求 CLI `backup_id` 与 record 精确一致。禁止裸 `dropdb` 和任意 target name 参数。

为避免首次读取与加锁之间的 TOCTOU，cleanup 顺序固定为：

```text
parse opaque id
-> safe-open record and read only minimal schema/backup_id
-> acquire corresponding backup shared lock
-> safe-open record again inside lock
-> verify full record and ensure identity has not changed
-> execute guarded cleanup
-> release shared lock last
```

首次读取结果只用于确定锁归属，不得作为 cleanup identity guard 的可信输入。

## 12. 稳定结果与错误码

结果 DTO：

```text
status: pass | fail
backup_id: canonical id
target_created: yes | no
restore_completed: yes | no
schema_verified: yes | no
constraints_verified: yes | no
integrity_verified: yes | no
target_dropped: yes | no
cleanup_required: yes | no
cleanup_handle: opaque id | null
restore_timeout_seconds: bounded integer
error_code: stable code | null
report_desensitized: yes
```

新增错误码：

```text
RESTORE_MAINTENANCE_UNAVAILABLE
RESTORE_TARGET_IN_USE
RESTORE_TARGET_PREPARED_XACT
RESTORE_TARGET_IDENTITY_MISMATCH
RESTORE_RECOVERY_PHASE_INVALID
RESTORE_RECOVERY_WRITE_FAILED
RESTORE_VERSION_UNSUPPORTED
RESTORE_TIMEOUT_INVALID
RESTORE_TIMEOUT
RESTORE_SCHEMA_MISMATCH
RESTORE_CONSTRAINT_MISMATCH
RESTORE_READONLY_SMOKE_FAILED
RESTORE_CLEANUP_GUARD_FAILED
```

复用总设计既有 restore 错误码。异常原文、SQL、target name、role、URL、token 和业务
数据不得进入正式结果/API/log。

## 13. 模块边界

```text
backend/app/deploy/restore_models.py
  result/check DTO and phase contract

backend/app/deploy/restore_recovery.py
  private recovery record store

backend/app/deploy/restore_database.py
  maintenance/target adapters and guarded DDL

backend/app/deploy/restore_verify.py
  orchestration, pg_restore owner, verification, cleanup CLI

backend/tests/deploy/test_restore_recovery.py
backend/tests/deploy/test_restore_database.py
backend/tests/deploy/test_restore_verify.py
```

`RestoreVerificationService` 是唯一生命周期 owner。router/CLI 不重复 rollback、
terminate、DROP、record delete 或 lock release。

## 14. 测试门槛

至少覆盖：

1. 只接受 canonical backup ID，不接受 path/target name；
2. package validator fail 时不创建 recovery record/database；
3. production/maintenance name 永远不能成为 target；
4. target/opaque/token 随机且符合固定 schema；
5. planned 和 `create_started` 必须在 CREATE 前依次 durable；
6. `create_started` 下 database absent 可删除 record；
7. CREATE 成功但 `database_created` 写入失败/崩溃时可由 `create_started` guarded cleanup；
8. CREATE 成功后验证 owner，再持久化 `database_created`；
9. `identity_commit_started` 必须在 COMMENT 前 durable；
10. COMMENT 未执行/失败且 comment 为空时可 guarded cleanup；
11. COMMENT 成功但 `identity_committed` 写入失败/崩溃时，token 精确匹配后可 guarded cleanup；
12. COMMENT 为其他值时禁止 cleanup DROP；
13. COMMENT 成功后必须 read-back 一致才能进入 `identity_committed`；
14. recovery phase 非法跳转拒绝；
15. SQL identifier/comment 只经 server format + bind parameter；
16. owner/comment/token/backup ID 任一不匹配禁止 DROP；
17. 各检查点严格执行 phase-specific guard matrix；
18. target active connection 时不 terminate 且不 DROP；
19. prepared transaction 存在时不 rollback 且不 DROP；
20. pg_restore 前重新连接并验证 current database/owner/comment；
21. pg_restore argv 恰含 `--dbname=<generated target>`，不含 `-d`/URL/密码/production
    database，env `PGDATABASE` 等于同一 generated target；
22. pg_restore argv/env 无生产 DB、URL、密码、clean/create；
23. timeout 只来自有界本机配置，非法值在建库前拒绝；
24. pg_restore success/fail/timeout/cancel/kill/reap；
25. cancel 后保留 target/record 且 shared lock 最后释放；
26. source/dump/restore/target major 任一不符时不建库；
27. Alembic revision mismatch 不建库或不宣称通过；
28. required table/view 类型通过 PostgreSQL catalog 精确检查；
29. PK/unique/FK/action/validated 语义检查；
30. RawMessage FK DELETE RESTRICT；
31. 验证事务由 PostgreSQL 强制 READ ONLY，并确认 transaction_read_only=on；
32. COUNT/orphan/smoke 均受 statement_timeout 限制；
33. 成功验证后 guarded DROP 并删除 record；
34. DROP 成功但 record 删除失败时返回 cleanup handle；
35. `verification_passed`/`drop_failed` 且 database absent 时只删除 stale record；
36. 其他 phase 下 database absent 不伪造 cleanup 成功；
37. restore/schema/integrity/drop 失败保留 record；
38. cleanup 要求 opaque ID 与 backup ID，并重复全部 guards；
39. cleanup 成功直接删除 record，失败保留原 phase；
40. target name/token/role/URL/业务数据不进入报告/log；
41. fake adapter 全链路；
42. 临时非生产 PostgreSQL create/restore/verify/drop 集成测试；
43. 完整回归；
44. 对真实 4B package 执行隔离恢复必须单独授权。

## 15. 当前结论

```text
P6-DEPLOY-4C_REVIEW:
  result: C2_real_isolated_restore_accepted
  architecture_direction: aligned
  lifecycle_completeness: pass
  crash_recovery_model: pass
  PostgreSQL_safety: pass
  implementation_contract: pass
  blockers: 0
  original_blockers_resolved: 4/4
  rereview_blockers_resolved: 3/3
  recommendations: 3
  C1_focused_tests: 38_passed_3_skipped
  C1_full_regression: 600_passed_3_skipped
  temporary_restore_fixture: pass
  real_4B_package_validation: pass
  real_isolated_restore: pass
  real_schema_verification: pass
  real_constraint_verification: pass
  real_integrity_verification: pass
  guarded_target_drop: pass
  cleanup_required: no
  generated_database_residue: none
  allow_P6_Deploy_4C_C1: complete
  allow_P6_Deploy_4C_C2: complete
  allow_real_restore_verify: complete
  allow_P6_Deploy_4D: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

真实临时 fixture 测试依次否定“无 dbname”和“空 `--dbname=`”，最终验证显式 generated
target + 同值 allowlist `PGDATABASE` 可完成 direct restore、schema/constraint/integrity
验证和 guarded DROP。C1 的 fake、临时 PostgreSQL 集成测试及完整回归均已通过。
C2 使用已通过 4B validator 的真实 package 完成隔离恢复、只读验证和 guarded DROP，
无需 cleanup，未遗留生成的隔离数据库或 recovery record。4D 和生产恢复继续禁止。

本次 C2 首次执行在创建数据库前因 `production.env` 的 `DATABASE_URL` 使用隐式系统用户
名而返回 `DATABASE_URL_UNSUPPORTED`；随后只对验收进程使用同一 host/database 的显式
本机角色完成验证。生产连接串随后已固定为显式用户名，主 LaunchAgent 重启后 liveness
和 readiness 均通过，后续 backup/restore CLI 不再依赖隐式系统用户名。
