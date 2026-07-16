# P6-Deploy-4D-4C 临时 PostgreSQL 生产恢复演练设计

> 项目：tg-hub
> 阶段：P6-Deploy-4D-4C
> 日期：2026-07-16
> 状态：4C-2-admission-approved
> ALLOW_4C_1_IMPLEMENTATION：yes
> ALLOW_4C_2_IMPLEMENTATION：yes
> ALLOW_4C_3_IMPLEMENTATION：no
> ALLOW_4C_4_ARCHIVE：no
> ALLOW_REAL_TEMP_POSTGRES_EXECUTION：yes（仅 generated DB，须通过 4C-2 runtime preflight）
> ALLOW_PRODUCTION_RECOVERY：no

## 1. 阶段目标

4C 只回答：4B 已验收的 durable state machine 能否在**生成的临时 PostgreSQL 数据库**、
temp env/watchlist 与 fake lifecycle 上完成 replacement restore、只读验证、配置切换、readiness、
rollback 和 guarded cleanup。

本阶段不能证明真实生产恢复已执行，也不能读取 production package、`production.env`、真实
watchlist、LaunchAgent、Telegram session 或生产数据库。

允许链路固定为：

```text
synthetic temp source database
-> pg_dump custom fixture
-> generated replacement database
-> pg_restore explicit replacement
-> read-only schema/constraint/integrity verification
-> temp env/watchlist switch
-> temp application smoke
-> fake Monitor fence
-> rollback to temp source
-> production-recovery child cleanup
-> guarded DROP generated replacement/source
```

## 2. 不变量与禁止项

- 所有 PostgreSQL 数据库名称均由本轮 capability 生成，不接受 CLI 传入名称。
- source、replacement 和 maintenance database 三者不同。
- 禁止名称等于连接 URL 的初始 database。
- 禁止读取 `Settings.DATABASE_URL`；只接受测试 fixture 显式注入的非生产连接 spec。
- 禁止读取 `~/.tg-hub/backups`、真实 backup ID、verification sidecar 或 retention 状态。
- 禁止启动/停止 `com.tghub.service`、rotation agent、Monitor 或 Telegram client。
- 禁止 migration upgrade 生产库；Alembic/ORM schema 只作用于 generated source。
- 禁止把 URL、password、database name、identity token、SQL、stderr 或业务行输出到报告。
- 旧 temp source 在 rollback 验收完成前不得 DROP。
- replacement 成为 temp active 后，自动 cleanup 仍须先 rollback；不得边 active 边 DROP。

## 3. TempPostgresRehearsalCapability

新增独立、不可序列化 capability，不复用 `TempRecoveryCapability` 或 retention capability。mint
必须分为 observation 与 issuance 两层：

```text
TempPostgresObservationProvider
-> trusted ServerRoleObservation
-> TempPostgresCapabilityIssuer
-> TempPostgresRehearsalCapability
```

`ServerRoleObservation` 只能由受信 provider 创建，普通调用者不能构造。4C-1 使用 fake provider，
只返回测试控制的 observation，禁止创建连接；4C-2 才允许 real provider 通过受控 maintenance
connection 实时读取 observation。fake 与 real provider 必须经过同一 issuer、验证规则和 capability
类型，禁止为集成阶段另开宽松 mint 路径。

issuer 只允许两种显式 scope：

```text
initial
-> 生成新 run/source/replacement/token

resume_cleanup
-> 接受 opaque rehearsal record handle
-> safe-open 最小 record schema
-> 获取 stable run lock
-> 锁内重读完整 record
-> 获取受信 observation
-> 校验 root/server/role/phase/identity
-> 从 record 恢复既有 identity，签发 cleanup-only capability
```

`resume_cleanup` 不得接受调用者提供的数据库名或 token，不得生成新 identity，不得执行 restore、
config switch 或 Monitor 操作；只允许 record 当前 phase 对应的 guarded reconciliation/cleanup。
record 缺失、terminal、schema invalid、root/server/role mismatch 或 manual reconciliation 状态均拒绝
签发。普通 initial capability 不能通过字段修改升级为 resume scope。

cleanup targets 必须在签发时冻结：`workflow_terminal` 仅允许 replacement；replacement cleanup
durable 后进入 `source_cleanup_started` 才允许 source；其他 phase 不签发 DROP 权限。initial
capability 永远不携带 DROP 权限，target 集合签发后不可修改。

唯一 minting authority 是 4C fixture factory。factory 必须：

1. 接受显式 `PgConnectionSpec`，不构造或读取生产 Settings；
2. 要求 host 为 loopback，禁止 remote host 和隐式 Unix socket；
3. 要求 user、host、database 显式存在，禁止 URL query/service 覆盖；
4. 从受信 provider observation 冻结 server major、system identifier 的脱敏 digest、role identity；
5. 生成 process nonce、run ID、source name、replacement name 和 identity token；
6. 数据库名称使用固定前缀与随机后缀，不接受调用者覆盖；
7. 绑定 temp recovery root device/inode 和 purpose=`production_recovery_temp_postgres`；
8. capability 不提供受支持的序列化或重建路径：pickle、copy、deepcopy、dataclass asdict、JSON
   encoder、公开构造器、环境变量和 CLI 均 fail closed，`repr` 不得泄露 token 或 connection spec。

production adapter 在 capability 验证前固定返回：

```text
PRODUCTION_RECOVERY_NOT_AUTHORIZED
```

4C-1 测试必须证明 fake provider 没有 PostgreSQL、socket、subprocess 或 Settings 依赖；real provider
在 4C-2 授权前不得装配或调用。

## 3.1 Durable Rehearsal Ownership

capability 只负责当前进程授权，不能承担崩溃恢复。4C 必须新增独立的
`TempPostgresRehearsalRecord`，不能把 source 身份塞入现有 `ProductionRecoveryRecord`。

record 位于 capability 绑定的私有 temp recovery root，目录 `0700`、文件 `0600`，使用 safe-open、
no-symlink、exact schema、atomic replace、file fsync 与 directory fsync。record 至少持久化：

```text
schema_version
run_id
server_identity_digest
connection_identity_digest
role_oid
role_identity_digest
source_database_identity
source_identity_token
replacement_database_identity
replacement_identity_token
dump_identity
workflow_record_id
phase
source_cleanup_status
replacement_cleanup_status
last_error_code
```

敏感 identity/token 允许存在于该私有 record，仅用于 guarded resume/cleanup；禁止进入日志、CLI、
管理台或验收报告。record 的 stable run lock identity 必须跨进程可重新获取，不能依赖 process nonce。
connection identity digest 必须覆盖初始 host/port/user/maintenance database/password，但不得保存或
输出明文；command/config builder 与 resume issuer 必须拒绝任何 spec 变化。

不可逆操作前必须先持久化 intent，phase 至少覆盖：

```text
planned
source_create_started
source_created
source_identity_commit_started
source_identity_committed
dump_started
dump_committed
replacement_bound
workflow_terminal
source_cleanup_started
source_cleanup_completed
rehearsal_terminal
manual_reconciliation_required
```

恢复 owner 必须先 safe-open record、获取 stable run lock、在锁内重读并校验，再根据 record 与实时
PostgreSQL 外部事实协调。source CREATE/COMMENT/DROP 与 phase 写入之间的崩溃窗口采用与 4C/4D
一致的 intent-first 规则；identity mismatch、未知连接或未知对象进入
`manual_reconciliation_required`，不得自动 DROP。

恢复 owner 只能通过前述 `resume_cleanup` issuance 获得权限；durable record 本身不是 capability，
仅持有 record handle 或读取 JSON 不授予任何数据库操作权限。

source DROP 已成功但 terminal phase 未写入时，只有 `source_cleanup_started` 且实时确认 source
absent 才可补写完成。`workflow_terminal` 不允许删除 record；仅在 source/replacement cleanup 均完成、
residue inventory 为 0 且 `rehearsal_terminal` durable 后，才允许删除或归档 record。

## 4. 数据库身份与所有权

generated source/replacement 均使用 database COMMENT：

```text
tg-hub-production-recovery-rehearsal:<run_id>:source:<token>
tg-hub-production-recovery-rehearsal:<run_id>:replacement:<token>
```

database owner 固定为 capability mint 时 `current_user` 对应的 role OID。capability 同时冻结 role
name、role OID 和二者的 identity digest；CREATE 不允许通过 `OWNER` 转交给其他角色。后续验证同时
比较 owner OID 和实时 role identity，同名角色被 DROP 后重新创建导致 OID 变化时必须判定 identity
mismatch。

所有检查点都必须验证 canonical generated name、owner、server identity，且目标不等于 initial/
maintenance database。COMMENT 和连接约束按阶段固定：

| 检查点 | COMMENT | current_database | active connections | prepared xacts |
|---|---|---|---|---|
| CREATE 后、COMMENT 前 | 必须为空 | maintenance connection | 0 | 0 |
| COMMENT read-back 后 | 必须等于 token | maintenance connection | 0 | 0 |
| restore 前 | 必须等于 token | maintenance connection | 0 | 0 |
| verification 前 | 必须等于 token | generated target | 仅当前受控连接 | 0 |
| DROP 前 | 必须等于 token | maintenance connection | 0 | 0 |

任何 identity mismatch、未知连接或 prepared transaction 均 fail closed，不 terminate 未知连接。
无法查询或无法可靠确认 `pg_prepared_xacts` 时不得按 0 处理，固定 fail closed。

server identity 不能只在 capability mint 时读取。source CREATE 前、dump 前、replacement CREATE 前、
restore 前、verification 前和每次 DROP 前，必须从当前受控连接重新读取 server major、system
identifier digest 和 role identity，并与 capability 比较；调用者输入或缓存值不能替代实时检查。

## 5. Synthetic Source 与 Dump

4C 不读取真实 backup package。source fixture 顺序固定为：

```text
source_create_started durable
-> CREATE generated source
-> COMMENT/read-back
-> apply current Alembic head to generated source
-> seed bounded synthetic rows
-> verify source invariants
-> pg_dump --format=custom explicit source
-> pg_restore --list catalog validation
-> freeze dump SHA-256/size/tool major/schema revision
```

seed 仅使用合成数据，不复制生产数据。dump 位于 capability 绑定的 temp root，`0600`、regular、
no symlink；stream hash 使用 exactly-N + double-fstat。`pg_dump` 与 `pg_restore` 使用空环境加
allowlist libpq 字段，不继承未知 `PG*`。

source schema 唯一路径固定为对 generated source 执行当前 Alembic head，并读回验证
`alembic_version`。ORM metadata 只能用于 fake/unit assertion，不得替代 4C-2 的 Alembic 路径。

工具身份分别冻结为 `server_major`、`pg_dump_major`、`pg_restore_major`。4C 第一版要求 dump 与
restore tool major 相同，且按 PostgreSQL 兼容矩阵验证 server major；不得用单个 `tool_major`
混合表示。subprocess 环境固定安全 `PGAPPNAME`：`tg-hub-4c-pg-dump` 和
`tg-hub-4c-pg-restore`，不得包含 run ID、数据库名或 token；verifier 使用
`tg-hub-4c-verifier`。

## 6. Replacement Restore Adapter

4C 新增 production-recovery 专用 temp PostgreSQL adapter，组合而不复制：

- `RestoreDatabaseAdapter` 的安全 identifier/COMMENT/inspect primitive；
- `RestoreDatabaseVerifier` 的 PostgreSQL 强制 READ ONLY 验证；
- `PgToolRunner` 的 timeout/cancel/terminate/kill/reap；
- 4A/4B 的 `ProductionRecoveryRecord`、operation lease 和 child cleanup owner。

real adapter 必须同时读取 durable rehearsal record 与绑定的 4B workflow record。replacement
CREATE、identity commit、DROP 分别受主记录 phase 和 cleanup child phase 约束；rehearsal record 的
`workflow_record_id` 不能单独授予 replacement 操作权限。

不得调用 `RestoreVerificationService.run()`，因为它拥有独立 recovery record 并在成功后立即
DROP target，与 4D-4 生命周期冲突。

`pg_restore` target 契约固定：

```text
argv:
  pg_restore
  --dbname=<generated replacement>
  --no-owner
  --no-acl
  --exit-on-error
  <temp database.dump>
env:
  allowlisted PGHOST/PGPORT/PGUSER/PGPASSWORD
  PGDATABASE=<generated replacement>
```

`--dbname` 与 `PGDATABASE` 必须精确指向同一个 generated replacement；这是显式执行恢复所需的
双重目标约束。禁止 `-d`、URL、password、initial database 或 production database 出现在 argv。
不得仅设置 `PGDATABASE` 而省略 `--dbname`，否则 `pg_restore` 可能只向 stdout 输出 SQL，而不执行
数据库恢复。

## 7. Restore 崩溃协调

这里的 `empty catalog` 不是“数据库完全没有对象”，而是固定 allowlist 以外不存在 user-owned
对象。允许项只包括 PostgreSQL system schemas、`information_schema`、固定 `public` schema、
database identity COMMENT 和经 4C-1 锁定的 template 固有对象。禁止运行时发现对象后动态扩展
allowlist。任何额外 relation、sequence、function、type、extension、migration table 或 application
object 都不属于 empty catalog。

`restore_started` 不得盲目重复 `pg_restore`。新 owner 必须检查 replacement：

| 外部事实 | 收敛 |
|---|---|
| exact identity + empty catalog | 可执行一次 restore |
| exact identity + 完整 verifier pass | 补写 restore/verification facts，不重复 restore |
| exact identity + partial schema/data | `TEMP_REHEARSAL_RESTORE_PARTIAL`，停止并请求 child cleanup |
| identity mismatch / unknown objects | manual reconciliation，不 DROP |
| target absent | 仅在 create intent 允许重新 CREATE |

不得通过“pg_restore 进程不存在”推断 restore 未发生。取消必须等待 subprocess 和连接全部收口，
再更新 error facts并释放 recovery lease。

## 8. Config、Lifecycle 与 Write Fence

配置仍使用 4B temp adapter，不读取真实 env/watchlist。区别仅在 temp `DATABASE_URL` 的 database
component 指向 generated source/replacement。host、port、user、password 语义保持不变。

生命周期使用 temp application fixture：

- start 时建立到指定 generated database 的受控连接；
- readiness 查询 current_database、Alembic revision 和 bounded synthetic smoke；
- stop 后关闭 engine/pool；
- drain 通过 `pg_stat_activity` 确认该 fixture 的 `application_name` 连接为 0；
- 不监听生产端口，不启动 Uvicorn、Bot 或 Monitor。

pg_dump/pg_restore/verifier 使用前述稳定 `PGAPPNAME`；temp application fixture 单独使用
`tg-hub-4c-app-<opaque suffix>`。suffix 由 run identity 的单向截断 digest 生成，不包含数据库名、
token 或原始 run ID，只用于本轮 drain inventory。

Monitor 继续使用 fake generation/write fence。进入 `monitor_start_started` 后仍无条件禁止自动
rollback；4C 不允许真实 Monitor 写入临时或生产数据库。

## 9. Rollback 与 Cleanup

Rollback 固定为：

```text
fake Monitor stopped
-> temp application stop/drain
-> temp env/watchlist restore
-> application reconnect generated source
-> source readiness/integrity pass
-> rolled_back durable
-> main requests child cleanup
-> child cleanup owns guarded replacement DROP
-> child terminal
-> main cleanup_completed
```

source cleanup 是 rehearsal record fixture teardown 的独立 owner，必须在 replacement cleanup、workflow
record terminal
和连接 drain 后执行。production child primitive只能 DROP replacement，永远不能 DROP source、
initial 或 maintenance database。

cleanup 失败必须保留数据库和 record，报告 cleanup handle；禁止测试 teardown 用无条件 DROP
掩盖身份/连接错误。

终态必须区分：

```text
recovery workflow terminal
= rolled_back + replacement child cleanup terminal

rehearsal run terminal
= recovery workflow terminal + source cleanup terminal + residue inventory completed
```

production recovery record terminal 不能单独推出 4C 成功。最终 `status=success` 必须同时满足：

```text
rollback_completed=yes
replacement_cleanup_completed=yes
source_cleanup_completed=yes
residue_count=0
```

## 10. Lock 与资源生命周期

顺序固定：

```text
production-recovery stable lease
-> temp-postgres run lock
-> controlled database connection/subprocess
-> settle action + facts
-> release database resource
-> release run lock
-> release recovery lease
```

禁止反向获取。每个 engine、connection、transaction、subprocess 和 temp file只有一个 owner。
取消不等于资源已停止；必须 shield settlement，最终继续传播 `CancelledError`。

## 11. 稳定结果与脱敏

4C 报告只输出：

```text
status
run_id
phase
source_created
dump_validated
replacement_created
restore_completed
verification_completed
config_switched
rollback_completed
replacement_cleanup_completed
source_cleanup_completed
residue_count
error_code
report_desensitized: yes
```

数据库名称、URL、role、token、checksum、SQL、路径、stderr 和 synthetic row 内容均禁止输出。

稳定错误码至少包括：

```text
TEMP_REHEARSAL_NOT_AUTHORIZED
TEMP_REHEARSAL_CONNECTION_UNSAFE
TEMP_REHEARSAL_SERVER_IDENTITY_MISMATCH
TEMP_REHEARSAL_SOURCE_CREATE_FAILED
TEMP_REHEARSAL_DUMP_FAILED
TEMP_REHEARSAL_DUMP_INVALID
TEMP_REHEARSAL_RESTORE_FAILED
TEMP_REHEARSAL_RESTORE_PARTIAL
TEMP_REHEARSAL_VERIFICATION_FAILED
TEMP_REHEARSAL_CONNECTIONS_ACTIVE
TEMP_REHEARSAL_CLEANUP_FAILED
TEMP_REHEARSAL_RESIDUE_PRESENT
PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED
```

## 12. 阶段拆分与授权

### 4C-1：契约与 adapter 实现

允许：capability、DTO、adapter protocol、argv/env builder、fake subprocess/DB tests、temp config
assembly、production fail-closed。禁止连接任何 PostgreSQL。

### 4C-2：本机临时 PostgreSQL 集成测试

前置 4C-1 最终代码复审并单独授权。允许连接 loopback maintenance DB，创建 generated source/
replacement，执行 synthetic dump/restore/verify/rollback/cleanup。禁止 production config/package。

### 4C-3：崩溃、取消与 residue 验收

前置 4C-2 通过并单独授权。只在 generated databases 注入 restore cancel、partial state、连接占用
和 cleanup resume。禁止 terminate 未知连接或放宽 guard。

### 4C-4：归档

汇总 temp PostgreSQL 证据和 residue=0 结果，更新 runbook。不得执行真实生产恢复。

## 13. 项目结构

```text
backend/app/deploy/
├── production_recovery_postgres.py
├── production_recovery_postgres_models.py
├── production_recovery_postgres_record.py
├── production_recovery_postgres_real.py
└── production_recovery_postgres_cli.py   # 仅后续显式 temp Gate

backend/tests/deploy/
├── test_production_recovery_postgres_contracts.py
├── test_production_recovery_postgres_fake.py
├── test_production_recovery_postgres_real.py
└── test_production_recovery_postgres_integration.py
```

## 14. 验收门槛

至少覆盖：

1. production Settings/config/package 读取在 adapter 构造前 fail；
2. fake observation provider 不创建 PostgreSQL/socket/subprocess，real provider 在 4C-1 不可装配；
3. observation 不能由普通调用者构造，fake/real 共用 issuer 和 capability validation；
4. initial/resume_cleanup scope 不可互换；resume 必须 record-first、stable lock、锁内重读，只能恢复
   record 既有 identity 并执行 phase 允许的 cleanup；
5. capability 的已知标准序列化、复制、公开构造和重建入口全部拒绝，`repr` 脱敏，root/server
   identity 变更拒绝；
6. rehearsal record safe-open、0600、atomic durability、exact schema 和 stable run lock；
7. source CREATE/COMMENT/DROP intent-first 崩溃窗口及 absent-after-DROP 收敛；
8. workflow terminal 后 resume capability 可恢复 source cleanup，rehearsal terminal 前不得删除；
9. remote host、implicit user/database/socket/query 拒绝，connection spec digest 变化拒绝；
10. generated names 不接受调用者输入，且不等于 initial/maintenance；
11. source/replacement COMMENT、owner OID/role identity、实时 server identity exact，同名 role
   重建拒绝；
12. generated source 只通过当前 Alembic head 建立，schema/revision/seed invariants；
13. pg_dump/pg_restore 绝对路径、独立 major、兼容矩阵、固定安全 PGAPPNAME 和 allowlist env；
14. restore argv 仅含一个 `--dbname=<generated replacement>`，与 `PGDATABASE` 精确一致，且不含
   `-d`、URL/password/initial/production database；
15. dump exactly-N hash、double-fstat 和 catalog 校验；
16. replacement empty 固定 allowlist及 complete/partial/unknown 四态协调，额外 user object 拒绝；
17. PostgreSQL 强制 READ ONLY verification；
18. required schema/constraints/RawMessage DELETE RESTRICT/integrity；
19. config 只改变 database component；
20. temp application 使用脱敏 per-run application_name，current_database/readiness/drain；
21. source 在 rollback 前后数据 identity 不变；
22. replacement active 时 cleanup 拒绝；
23. rolled_back 后 child cleanup guarded DROP；
24. target absent、DROP 后 terminal 写失败可收敛；
25. source/initial/maintenance 永不被 child primitive DROP；
26. active/prepared transaction 阻止 DROP；
27. restore timeout/cancel 完整 terminate/kill/reap；
28. 每个 engine/connection/subprocess 最终关闭；
29. residue inventory 仅识别本 run ID，未知对象不自动删除；
30. workflow terminal 与 rehearsal terminal 分离，成功必须 rollback/replacement cleanup/source
    cleanup 全部完成且 residue_count=0；
31. 报告脱敏；
32. 4C 专项与完整回归通过。

## 15. 完成口径

4C 通过只能得出：

```text
生产恢复状态机可在 generated temp PostgreSQL + temp config + fake lifecycle 上完成
restore -> verify -> switch -> readiness -> rollback -> guarded cleanup
```

不能得出：

```text
真实 production package 已恢复
真实 production database 已切换
真实 LaunchAgent/Monitor 已操作
真实生产恢复已授权或执行
```
