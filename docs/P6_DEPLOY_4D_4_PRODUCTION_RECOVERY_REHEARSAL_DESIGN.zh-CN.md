# P6-Deploy-4D-4 生产恢复编排与演练设计

> 项目：tg-hub
> 阶段：P6-Deploy-4D-4
> 日期：2026-07-16
> 状态：design-locked-for-4D-4A
> 前置：P6-Deploy-4D-3 complete
> ALLOW_P6_DEPLOY_4D_4A_IMPLEMENTATION：yes
> ALLOW_P6_DEPLOY_4D_4B_IMPLEMENTATION：no
> ALLOW_P6_DEPLOY_4D_4C_TEMP_POSTGRES_REHEARSAL：no
> ALLOW_P6_DEPLOY_4D_4D_RUNBOOK_ARCHIVE：no
> ALLOW_REAL_PRODUCTION_CONFIG_READ：no
> ALLOW_REAL_SERVICE_LIFECYCLE_CHANGE：no
> ALLOW_REAL_PRODUCTION_RESTORE：no
> ALLOW_P6_DEPLOY_5：no

## 1. 阶段目标

P6-Deploy-4D-4 只实现并演练生产恢复编排，不执行真实生产恢复：

```text
verified backup fixture
-> generated replacement database fixture
-> staged config/watchlist fixture
-> fake service lifecycle
-> readiness fixture
-> rollback/reconcile fixture
-> runbook archive
```

本阶段完成只能证明：恢复状态机、durable intent、配置切换和回滚协议能在 fake/temp 资源上
确定性收敛。不能证明真实生产库已恢复，也不能授权真实服务停机或配置切换。

## 2. 架构边界

生产恢复属于部署层，不进入业务链路：

```text
ProductionRecoveryOrchestrator
  -> RecoveryRecordStore
  -> BackupPackageAdapter
  -> ReplacementDatabaseAdapter
  -> ConfigSwitchAdapter
  -> ServiceLifecycleAdapter
  -> ReadinessAdapter
  -> MonitorLifecycleAdapter
```

禁止：

- MonitorRuntime、Parser、Normalizer、Dedup、EventBus 或 Bot 参与恢复判定；
- router、CLI 或 adapter 各自重复 DROP、rollback、disconnect 或 release lock；
- 复用 C2 的“验证成功即自动 DROP”语义；
- 原地恢复当前生产数据库；
- 接受 package path、database name、SQL、DSN 或 env path 作为命令输入；
- source/eval `production.env`；
- 自动迁移 schema、自动启动 Monitor 或自动删除旧生产库。

唯一 orchestrator 拥有 recovery record、replacement client/process、staging、service lifecycle、
rollback decision 与最终 cleanup。adapter 只执行一个受控动作并返回稳定事实。

## 3. 资源与身份

唯一业务输入：

```text
selected_backup_id: canonical backup ID
incident_id: generated opaque ID
```

内部生成并绑定：

```text
replacement_database_name
replacement_identity_token
protection_backup_id | null
original_database_identity
original_database_revision: exact Alembic schema revision
selected_package_identity
original_env_sha256
staged_env_sha256
protected_env_sha256
original_watchlist_sha256
staged_watchlist_sha256
protected_watchlist_sha256
```

`selected_backup_id` 与 `protection_backup_id` 永远是独立字段。Protection backup 不得替换
恢复源。对外结果不得输出数据库名、token、checksum、路径、URL、用户名或业务数据。

4D-4 使用 `TempRecoveryCapability`。唯一 minting authority 是测试 fixture factory；factory
必须先验证 root 位于系统 temp root 下、排除真实 backup/runtime/config root，再绑定 root
device/inode、process-instance nonce 和固定 purpose=`production_recovery_rehearsal`。capability
必须是进程内不可序列化对象，不能由 CLI 参数、环境变量或普通构造器创建。Production adapter
在 4D-4 中固定 fail closed：`PRODUCTION_RECOVERY_NOT_AUTHORIZED`。

`original_database_revision` 只表示数据库 `public.alembic_version` 的 exact revision，不表示
PostgreSQL server version、WAL position 或任意业务数据 revision。

## 4. Recovery Record

私有 create-once record：

```text
schema_version: 1
incident_id
selected_backup_id
phase
resource identities
authorization observations
last_operation
last_error_code
last_error_at_utc
retryable
verification_result: not_started | passed | failed
verification_error_code: stable code | null
monitor_stopped: yes | no | unknown
session_lease_free: yes | no | unknown
application_stopped: yes | no | unknown
application_connections_drained: yes | no | unknown
cleanup_record_id: opaque ID | null
cleanup_requested: yes | no
cleanup_completed: yes | no
protection_backup_status: pending | completed | skipped_authorized
watchlist_switch_authorized: yes | no
watchlist_was_switched: yes | no
replacement_activated: yes | no
monitor_first_write_observed: yes | no | unknown
manual_reconciliation_required: yes | no
```

record 目录必须为真实 `0700` 目录，record 为 `0600` regular file。使用 stable create-once
lock inode；record phase 更新使用 atomic replace、file fsync 与 directory fsync。incident ID
只用于定位，不构成真实恢复授权。

phase 固定为：

```text
planned
protection_backup_started
protection_backup_completed
protection_backup_skipped_authorized
replacement_create_started
replacement_created
identity_commit_started
identity_committed
restore_started
restore_completed
verification_started
verification_completed
services_stop_started
services_stopped
config_protection_started
config_protection_completed
env_switch_started
env_switched
watchlist_switch_started
watchlist_switched
config_switched
application_start_started
application_started
readiness_started
readiness_passed
monitor_start_authorized
monitor_start_started
monitor_started
monitor_write_observed
completed
rollback_started
rollback_monitor_stopped
rollback_application_stop_started
rollback_application_stopped
rollback_env_started
rollback_env_completed
rollback_watchlist_started
rollback_watchlist_completed
rollback_application_start_started
rollback_application_started
rollback_readiness_started
rollback_readiness_passed
rolled_back
```

不存在通用 `failed` phase。失败只更新稳定 error fields，不能伪造外部动作已经完成。

## 5. 合法转换与 Durable Intent

每个不可逆或跨持久化域动作前必须先写 intent：

```text
protection_backup_started -> create backup -> protection_backup_completed
replacement_create_started -> CREATE -> replacement_created
identity_commit_started -> COMMENT/read-back -> identity_committed
restore_started -> pg_restore -> restore_completed
verification_started -> read-only verification -> verification_completed
services_stop_started -> stop Monitor/application and drain -> services_stopped
config_protection_started -> protection copies -> config_protection_completed
env_switch_started -> atomic replace/read-back -> env_switched
watchlist_switch_started -> atomic replace/read-back -> watchlist_switched
application_start_started -> start application -> application_started
readiness_started -> liveness/readiness/smoke -> readiness_passed
monitor_start_started -> start/observe -> monitor_started
rollback_application_start_started -> start original application -> rollback_application_started
rollback_readiness_started -> original liveness/readiness/smoke -> rollback_readiness_passed
rollback_application_stop_started -> stop exact application and drain -> rollback_application_stopped
```

状态机模块必须硬编码合法转换。orchestrator 不得自由赋值 phase。phase write 失败不得进入
下一外部动作，统一返回 `PRODUCTION_RECOVERY_RECORD_WRITE_FAILED`。

Verification 进入 `verification_started` 前必须将 `verification_result=not_started`。动作完成后
先 durable 写入 `verification_result=passed|failed` 与稳定 error code；只有 `passed` 才允许写
`verification_completed` 并进入 service stop。`failed` 保留 `verification_started`，不伪造完成。

Service stop 协调必须分别投影 `monitor_stopped`、`session_lease_free`、`application_stopped` 和
`application_connections_drained`。四项全部为 `yes` 才允许写 `services_stopped`。

### 5.1 完整正向转换表

| From | 条件 | To | 必需外部事实 |
|---|---|---|---|
| `planned` | protection backup required | `protection_backup_started` | durable intent |
| `planned` | source unreadable + independent skip authorization | `protection_backup_skipped_authorized` | skip authorization exact-valid |
| `protection_backup_started` | backup valid | `protection_backup_completed` | protection backup ID/identity fixed |
| `protection_backup_completed` | always | `replacement_create_started` | selected ID unchanged |
| `protection_backup_skipped_authorized` | always | `replacement_create_started` | skipped status durable |
| `replacement_create_started` | target exact | `replacement_created` | generated name/owner, empty comment |
| `replacement_created` | always | `identity_commit_started` | target exact |
| `identity_commit_started` | comment exact | `identity_committed` | owner/comment token exact |
| `identity_committed` | always | `restore_started` | package identity frozen |
| `restore_started` | restore success | `restore_completed` | subprocess settled |
| `restore_completed` | always | `verification_started` | read-only verification configured |
| `verification_started` | result passed | `verification_completed` | schema/constraint/integrity pass |
| `verification_completed` | always | `services_stop_started` | verification result passed |
| `services_stop_started` | four stop facts yes | `services_stopped` | Monitor/app stopped and drained |
| `services_stopped` | always | `config_protection_started` | services still stopped |
| `config_protection_started` | copies exact | `config_protection_completed` | original/protected identity equal |
| `config_protection_completed` | always | `env_switch_started` | staged env valid |
| `env_switch_started` | active env staged | `env_switched` | read-back exact |
| `env_switched` | watchlist authorized | `watchlist_switch_started` | independent authorization durable |
| `env_switched` | watchlist not authorized | `config_switched` | active watchlist remains original |
| `watchlist_switch_started` | active watchlist staged | `watchlist_switched` | read-back exact |
| `watchlist_switched` | always | `config_switched` | env/watchlist both staged |
| `config_switched` | always | `application_start_started` | service still stopped |
| `application_start_started` | exact replacement process running | `application_started` | single process, replacement identity |
| `application_started` | always | `readiness_started` | replacement identity proven |
| `readiness_started` | checks pass | `readiness_passed` | liveness/readiness/static/smoke pass |
| `readiness_passed` | independent authorization | `monitor_start_authorized` | authorization durable |
| `monitor_start_authorized` | always | `monitor_start_started` | online preflight pass |
| `monitor_start_started` | generation owner proven | `monitor_started` | Monitor fixture running |
| `monitor_started` | first committed write observed | `monitor_write_observed` | observational watermark advanced |
| `monitor_started` | bounded observation, healthy, no write | `completed` | observed=no durable |
| `monitor_write_observed` | observation settled | `completed` | observed=yes durable |

表外转换全部禁止。`verification_started` 且 result failed、任何 intent 的未知第三种事实、以及
`monitor_start_started` 后的故障都不自动跳转，由 error fields 与 manual reconciliation 表达。
写入 `completed` 还必须要求 `manual_reconciliation_required=no`；
`monitor_first_write_observed` 必须为 `yes` 或 `no`，不能保留 `unknown`。`completed` 表示
readiness、独立 Monitor 授权、Monitor fixture 启动和有界 post-start observation 均已通过，
不只是 orchestrator 函数返回。

## 6. 崩溃协调

恢复不能只信 phase，必须比较 record 与外部事实：

| Phase | 可接受事实 | 收敛 |
|---|---|---|
| `replacement_create_started` | target absent | 可重试 CREATE |
| `replacement_create_started` | exact target/owner, comment empty | 补写 `replacement_created` |
| `identity_commit_started` | comment empty | 可重试 COMMENT |
| `identity_commit_started` | comment exact token | 补写 `identity_committed` |
| `env_switch_started` | active env == original | 可重试 replace |
| `env_switch_started` | active env == staged | 补写 `env_switched` |
| `watchlist_switch_started` | active == original | 可重试 replace |
| `watchlist_switch_started` | active == staged | 补写 `watchlist_switched` |
| rollback intent | active == staged | 可执行 restore |
| rollback intent | active == original | 补写完成 phase |
| `services_stop_started` | 四项 stop fact 部分为 yes | 只重试未完成 stop/check，禁止启动 |
| `application_start_started` | app stopped | 可重试一次 start |
| `application_start_started` | 单一 app 运行且 replacement identity exact | 补写 `application_started` |
| `application_start_started` | 多进程或 identity 不明 | manual reconciliation |
| `readiness_started` | app exact replacement 且运行 | 重试纯检查，不重复 start |
| `rollback_application_start_started` | old app stopped | 可重试一次 start |
| `rollback_application_start_started` | 单一 app 运行且 original DB identity exact | 补写 `rollback_application_started` |
| `rollback_application_start_started` | 多进程或 DB identity 不明 | manual reconciliation |
| `rollback_readiness_started` | app exact original 且运行 | 重试纯检查，不重复 start |
| `rollback_application_stop_started` | exact replacement app running | 重试 stop/drain |
| `rollback_application_stop_started` | app stopped且连接 drained | 补写 `rollback_application_stopped` |
| `rollback_application_stop_started` | wrong/duplicate process | manual reconciliation |

出现第三种文件内容、target owner/comment 不匹配或 identity 不可证明时，稳定停止：
`PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN` 或
`PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED`。不得猜测、覆盖或自动删除。

## 7. 配置切换契约

4D-4 的 config adapter 只操作 temp fixture。实现必须使用结构化 dotenv parser，保留原条目
顺序不是安全承诺，但必须保证除 `DATABASE_URL` 的 database component 外所有有效配置语义
不变。禁止字符串 replace、shell source 或重新拼接密码。

切换顺序：

```text
safe-open active env/watchlist
-> freeze original identity
-> create private protection copies
-> fsync/read-back
-> create staged files
-> schema/identity validation
-> services proven stopped
-> env durable intent
-> atomic env replace + read-back
-> optional independent watchlist authorization
-> watchlist durable intent + atomic replace + read-back
-> config_switched
```

`watchlist_switch_authorized=no` 时 active watchlist 必须保持 original identity。DB 与 watchlist
不是原子快照，报告必须保留时间差分类，但不得输出正文。

`replacement_activated` 是单调历史事实，不是当前 active database 的别名：

```text
initial: no
env_switch_started + active env == staged replacement
  -> 先 durable reconcile replacement_activated=yes
  -> 再写 env_switched
replacement_activated=yes
  -> 永远不得改回 no
rollback 后 active env == original
  -> replacement_activated 仍为 yes
```

任何 resume/cleanup 发现 active env 指向 replacement 且 record 仍为 `no`，必须先将其协调为
`yes`，不得按“未激活”判断 cleanup-safe。当前 active database 由 safe-open active env 的结构化
database component 单独投影。

## 8. 服务与 Monitor Write Fence

4D-4A/B 只使用 fake lifecycle adapter；4D-4C 可使用 temp process fixture，不操作真实
LaunchAgent。

停止顺序：

```text
request fake Monitor stop
-> confirm stopped
-> confirm fake session lease free
-> stop fake application
-> confirm unavailable
-> confirm application DB connections drained
```

启动顺序：

```text
start application only
-> liveness
-> readiness
-> static preflight
-> read-only smoke
-> independent monitor-start authorization fixture
-> online-preflight fixture
-> durable monitor_start_started
-> start Monitor fixture
```

从 `monitor_start_started` durable 起无条件禁止自动 rollback。heartbeat、PID、counter、max ID
或 watermark 都不能证明“从未写入”。此后任何失败进入
`PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED`。

## 9. Rollback 与 Cleanup

自动 rollback 只允许同时满足：

- phase 早于 `monitor_start_started`；
- Monitor 已停止；
- replacement/original/config identities 全部可证明；
- protection files exact-valid；
- active 文件只能是 original 或 staged 两态；
- orchestrator 持有 recovery lock。

rollback 顺序固定为：停止 Monitor、停止 application、恢复 env、按需恢复 watchlist、启动旧库
application、readiness、保持 Monitor 停止、写 `rolled_back`。

进入 `rollback_started` 前必须先协调当前正向 intent，使 active env/watchlist 和 process 状态
落入可证明两态；未知状态不得用 rollback 覆盖。合法 rollback 入口固定为：

| 当前 durable phase | 必须协调的事实 | 自动 rollback | 首个动作 |
|---|---|---|---|
| `env_switch_started` | env 为 original 或 staged；watchlist original | yes | 协调 env 后写 rollback intent |
| `env_switched` | env staged；watchlist original | yes | 写 rollback intent |
| `watchlist_switch_started` | env staged；watchlist original 或 staged | yes | 协调 watchlist 后写 rollback intent |
| `watchlist_switched` | env/watchlist staged | yes | 写 rollback intent |
| `config_switched` | active 文件符合授权后的 exact identity | yes | 写 rollback intent |
| `application_start_started` | app stopped 或单一 replacement app | yes | 写 intent 后停止 app |
| `application_started` | app 单一且使用 replacement | yes | 写 intent 后停止 app |
| `readiness_started` | app 单一且使用 replacement | yes | 写 intent 后停止 app |
| `readiness_passed` | Monitor 尚未授权/启动 | yes | 写 intent 后停止 app |
| `monitor_start_authorized` | Monitor 尚未进入 start intent | yes | 写 intent 后停止 app |
| `monitor_start_started` 及以后 | 任意 | no | manual reconciliation |

`rollback_started` 后固定转换：

```text
rollback_started -> rollback_monitor_stopped
-> rollback_application_stop_started
-> rollback_application_stopped
-> rollback_env_started -> rollback_env_completed
-> if watchlist_was_switched=yes:
     rollback_watchlist_started -> rollback_watchlist_completed
-> if watchlist_was_switched=no and active watchlist==original:
     rollback_application_start_started
-> rollback_application_started
-> rollback_readiness_started
-> rollback_readiness_passed
-> rolled_back
```

未切换 watchlist 时禁止写虚假的 rollback watchlist phase。

`rollback_started` 是停止 Monitor 的 durable intent；Monitor exact-stopped 后写
`rollback_monitor_stopped`。随后必须先写 `rollback_application_stop_started`，才能停止 exact
replacement application 并等待 connections drained；只有 application stopped 且 drained 才能写
`rollback_application_stopped`。任何 wrong/duplicate process 或连接无法归属都进入 manual
reconciliation。

Rollback application/readiness 的合法转换固定为：

| From | 条件 | To | 外部事实 |
|---|---|---|---|
| `rollback_started` | Monitor exact stopped | `rollback_monitor_stopped` | Monitor stopped/session lease free |
| `rollback_monitor_stopped` | always | `rollback_application_stop_started` | application identity frozen |
| `rollback_application_stop_started` | app stopped and drained | `rollback_application_stopped` | exact app absent/connections zero |
| `rollback_application_stopped` | always | `rollback_env_started` | Monitor/app stopped |
| `rollback_env_started` | active env original | `rollback_env_completed` | protection identity exact |
| `rollback_env_completed` | watchlist 从未切换且 active original | `rollback_application_start_started` | env/watchlist original |
| `rollback_env_completed` | watchlist 已切换 | `rollback_watchlist_started` | active watchlist staged |
| `rollback_watchlist_started` | active watchlist original | `rollback_watchlist_completed` | protection identity exact |
| `rollback_watchlist_completed` | active env/watchlist original | `rollback_application_start_started` | protection restore exact |
| `rollback_application_start_started` | 单一 original app running | `rollback_application_started` | original DB identity exact |
| `rollback_application_started` | always | `rollback_readiness_started` | app remains exact original |
| `rollback_readiness_started` | checks pass | `rollback_readiness_passed` | liveness/readiness/static/smoke pass |
| `rollback_readiness_passed` | Monitor stopped | `rolled_back` | original config/DB ready |

表外 rollback 转换禁止。`rollback_application_start_started` 和 `rollback_readiness_started` 都是
durable intent；对应 result phase 写失败时按外部进程/readiness 事实补写，不重复启动应用。

Replacement cleanup 不在主 recovery phase 中新增 DROP 状态，也不复制 guard 实现。新增
production-recovery 专用 child cleanup record；4C 与该 child adapter 共用抽取后的
`GuardedDatabaseCleanupPrimitive`，该 primitive 只拥有 identity/owner/comment/activity/
prepared-xact 检查、单次 DROP 和 read-back，不拥有任一上层 record 生命周期。

```text
main recovery record:
  cleanup_record_id
  cleanup_requested
  cleanup_completed

child cleanup record:
  schema_version: 1
  cleanup_record_id
  incident_id
  replacement_database_identity
  replacement_identity_token
  expected_owner_identity
  phase: planned | cleanup_started | cleanup_completed
  drop_observed: yes | no
  last_error_code
```

orchestrator 先生成 deterministic opaque child ID，并在 main record 中一次 durable 写
`cleanup_requested=yes + cleanup_record_id`。若随后崩溃且 child 不存在，resume 必须根据 main
record create-once child；已存在则 exact read-back，identity 不一致停止。child adapter 是 DROP
的唯一 owner；main 只 read-back child terminal result，成功后写 `cleanup_completed=yes`，不得
调用 primitive 或再次 DROP。

child 顺序固定为：

```text
planned
-> recheck main eligibility and active env not replacement
-> cleanup_started durable
-> GuardedDatabaseCleanupPrimitive.drop_once()
-> target absence read-back
-> cleanup_completed durable
```

`cleanup_started + target exact exists` 可重试 primitive；`cleanup_started + target absent` 表示
DROP 已完成但 phase 未写入，可补写 `drop_observed=yes, cleanup_completed`。其他 target identity、
active env 指向 replacement、活动连接或 prepared transaction 均 fail closed。

允许请求 cleanup 的场景只有：切换前恢复/验证失败且 replacement 未激活，或 durable
`rolled_back` 后人工选择清理。`completed` 流程不自动 cleanup，replacement 继续作为当前生产
数据库；旧生产库永久保留。`rolled_back` 不代表 replacement 已清理。

协调矩阵：

```text
cleanup_requested=yes, child started, replacement exact exists
  -> child adapter resume guarded cleanup
cleanup_requested=yes, child completed, replacement absent
  -> main read-back child terminal and write cleanup_completed=yes
cleanup_requested=yes, child absent
  -> exact main identity/eligibility read-back后 create-once child
cleanup_requested=yes, child create failed
  -> main remains requested; no DROP; retryable stable error
cleanup_requested=yes, child nonterminal, replacement absent
  -> only cleanup_started may coordinate completed; planned + absent is identity failure
child cleanup_started, DROP succeeded, terminal write failed
  -> target absent + child identity exact; retry writes cleanup_completed
child cleanup_completed, main update failed
  -> read-back terminal child; retry main cleanup_completed=yes
cleanup_completed=yes, replacement exists
  -> PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED
```

只要 active env 指向 replacement，创建 child cleanup record和执行 DROP 均稳定拒绝。旧生产库
和已激活 replacement 都不得自动删除。`cleanup_required` 是由 main/child durable state 与
replacement 外部事实计算的投影，不是调用者可写字段。

## 10. Lock 顺序与取消

固定顺序：

```text
production-recovery stable lock
-> backup exclusive（仅 protection backup fixture）
-> release
-> backup shared（验证/读取 selected fixture）
-> release after staging
-> fake session lease check
```

禁止 shared-to-exclusive upgrade。所有 blocking subprocess/task 由 orchestrator 单一 owner 管理。
取消必须等待 active worker、disconnect、terminate/kill/reap 和 record update 收口后才能释放锁；
`CancelledError` 最终继续传播。

## 11. 稳定结果与错误码

结果最小字段：

```text
status: pass | fail | partial
incident_id
phase
replacement_created
restore_completed
verification_completed
services_stopped
config_switched
application_ready
monitor_started
rollback_status
cleanup_required
error_code
report_desensitized: yes
```

稳定错误码：

```text
PRODUCTION_RECOVERY_NOT_AUTHORIZED
PRODUCTION_RECOVERY_IN_PROGRESS
PRODUCTION_RECOVERY_RECORD_INVALID
PRODUCTION_RECOVERY_RECORD_WRITE_FAILED
PRODUCTION_RECOVERY_PHASE_INVALID
PRODUCTION_RECOVERY_PACKAGE_INVALID
PRODUCTION_RECOVERY_PACKAGE_NOT_VERIFIED
PROTECTION_BACKUP_FAILED
PRODUCTION_RECOVERY_TARGET_INVALID
PRODUCTION_RECOVERY_RESTORE_FAILED
PRODUCTION_RECOVERY_VERIFICATION_FAILED
PRODUCTION_RECOVERY_SERVICE_STOP_FAILED
PRODUCTION_RECOVERY_CONNECTIONS_ACTIVE
PRODUCTION_RECOVERY_CONFIG_WRITE_FAILED
PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN
PRODUCTION_RECOVERY_READINESS_FAILED
PRODUCTION_RECOVERY_ROLLBACK_FAILED
PRODUCTION_RECOVERY_CLEANUP_FORBIDDEN
PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED
```

exception text、SQL、stderr、DSN、path、token、checksum 与业务内容不得进入报告。

## 12. 阶段拆分

### P6-Deploy-4D-4A：契约与纯状态机

允许：DTO、phase transition table、record store 的 temp-only 实现、child cleanup contract、
fake adapters、纯协调器、production adapter fail-closed、单元测试。

禁止：真实配置读取、PostgreSQL、LaunchAgent、真实备份目录、真实 session、网络访问。

### P6-Deploy-4D-4B：Fake 全流程与 rollback 演练

前置 4A 代码复审通过。演练 R0-R7、每个 intent 崩溃窗口、取消、rollback 和 manual
reconciliation fence。仍只使用 temp filesystem 与 fake adapters。

### P6-Deploy-4D-4C：临时 PostgreSQL 演练

前置 4B 通过并单独授权。只允许临时数据库、temp env/watchlist 和 fake service lifecycle。
不得读取生产 package、生产 env 或生产数据库。

### P6-Deploy-4D-4D：Runbook 归档

汇总 fake/temp 证据、失败注入结果和逐 Gate 人工操作说明。不得执行真实 PR-R0-R7。

## 13. 项目结构

```text
backend/app/deploy/
├── production_recovery_models.py
├── production_recovery_record.py
├── production_recovery_orchestrator.py
├── production_recovery_config.py
├── production_recovery_adapters.py
└── production_recovery_cli.py

backend/tests/deploy/
├── test_production_recovery_models.py
├── test_production_recovery_record.py
├── test_production_recovery_orchestrator.py
└── test_production_recovery_config.py

docs/
├── P6_DEPLOY_4D_4_PRODUCTION_RECOVERY_REHEARSAL_DESIGN.zh-CN.md
└── P6_DEPLOY_4D_4_PRODUCTION_RECOVERY_RUNBOOK.zh-CN.md
```

可按现有代码风格合并小模块，但不能把 CLI、状态机、文件安全、数据库 adapter 和生命周期
编排揉成单一长文件。

## 14. 4D-4A 测试门槛

至少覆盖：

1. 只接受 canonical backup ID，不接受 path、DB name、URL 或 env path；
2. production adapter 在加载 Settings 或获取 lock 前 fail closed；
3. temp capability 不能用于 production adapter；
4. record/lock 目录、类型、权限、symlink 和 inode 替换 fail closed；
5. phase 只允许合法转换，失败不进入通用 failed phase；
6. 每个外部动作前 durable intent；
7. phase write 失败时外部动作未调用；
8. CREATE/COMMENT 前后崩溃两态可协调；
9. env/watchlist replace 前后崩溃两态可协调；
10. 第三种 active 文件内容稳定停止；
11. staged env 只改变 database component；
12. dotenv 中引号、转义、空密码和 query 均按批准规则处理或拒绝；
13. watchlist 未授权时保持 original；
14. service 未停止或连接未 drain 时禁止 switch；
15. readiness 失败时 Monitor 从未启动；
16. monitor-start 使用独立授权 fixture；
17. `monitor_start_started` 后无条件禁止自动 rollback；
18. rollback 恢复 original env/watchlist 并保持 Monitor 停止；
19. active env 指向 replacement 时 cleanup 永远拒绝 DROP；
20. owner/comment/activity/prepared-xact 不匹配时 cleanup 拒绝；
21. 旧生产数据库永不自动 DROP；
22. cancellation 等待 worker 收口并传播；
23. 报告不泄露路径、DSN、token、checksum、SQL 或异常文本；
24. fake/temp 专项测试和完整回归通过。
25. protection backup required/skipped 两条分支不混淆；
26. watchlist 未授权时 `env_switched -> config_switched` 且不写虚假 phase；
27. rollback 未切换 watchlist 时直接进入 application rollback；
28. verification/service stop/application start/readiness intent 崩溃两态可协调；
29. verification result failed 不得进入 service stop；
30. 四项 service-stop fact 全部为 yes 才能切换配置；
31. 所有 rollback 入口先协调正向 intent；
32. `readiness_passed` 可回滚但 `monitor_start_started` 后永久禁止；
33. cleanup child record 是唯一 DROP owner；
34. child DROP 已完成但 main 未更新时可 read-back 收敛；
35. `rolled_back` 与 `completed` 都不隐含 replacement cleanup；
36. completed 的 write-observed yes/no 两条尾部合法且 manual reconciliation 为 false。
37. rollback old application start 和 readiness 均有独立 intent/result；
38. rollback app 已启动但 result phase 未写时不重复启动；
39. `replacement_activated` 首次观察 staged env 后单调变为 yes；
40. rollback 后 `replacement_activated` 仍为 yes；
41. main requested 但 child absent 时只 create child、不直接 DROP；
42. child cleanup_started 且 target absent 时补写 terminal；
43. child terminal 后 main update 失败可 read-back 收敛；
44. 4C 与 production child 共用 primitive，但 record 生命周期互不复用。
45. rollback application stop 在 intent 前不调用 lifecycle adapter；
46. stop 成功但 result phase 未写时按 stopped/drained 事实补写；
47. wrong/duplicate rollback process 稳定进入 manual reconciliation。

## 15. 独立设计评审

### 边界审查

- 4D-4 与真实 Production Recovery 已严格拆开；
- replacement database 与当前生产 database 永不相同；
- C2 cleanup 语义不会用于已激活 replacement；
- config switch、rollback、Monitor write fence 均有 durable intent；
- fake/temp capability 不能升级为生产授权；
- 真实 PR-R0 至 PR-R7 仍需未来逐 Gate 单独授权。

### 风险审查

已锁死的高风险点：

- 跨文件系统/PostgreSQL phase write 崩溃窗口；
- env 与 watchlist 半切换；
- replacement 已激活却被误 DROP；
- Monitor 可能写入后错误自动回滚；
- service 未完全停止时切换配置；
- adapter/orchestrator 多 owner 重复 cleanup；
- shell 解析 env 和敏感信息泄露。

### 第一轮独立评审修订

已修订：

- 补齐 verification、service stop、application start、readiness durable intent；
- 增加 protection backup skip、watchlist no-switch 和 rollback no-watchlist 分支；
- 增加完整正向 transition table 与 rollback-entry matrix；
- replacement cleanup 改为 child record + 已验收 cleanup primitive 的单一 owner 模型；
- 固定 verification result、service stop 分项事实、Alembic revision 和 capability minting authority；
- 固定 `completed` 的 write-observed yes/no 两条合法尾部。

### 当前复审状态

```text
P6-DEPLOY-4D-4_DESIGN_REVIEW:
  result: approved_for_4D_4A
  architecture_direction: aligned
  previous_review_blockers_addressed: 4
  previous_review_recommendations_addressed: 5
  second_review_blockers_addressed: 3
  second_review_recommendations_addressed: 0
  third_review_blockers_addressed: 1
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_4D_4A_implementation: yes
  allow_P6_Deploy_4D_4B_implementation: no
  allow_P6_Deploy_4D_4C_temp_postgres_rehearsal: no
  allow_P6_Deploy_4D_4D_runbook_archive: no
  allow_real_production_config_read: no
  allow_real_service_lifecycle_change: no
  allow_real_production_restore: no
  allow_P6_Deploy_5: no
```

## 16. 完成标准

4D-4 完成只能得出：

```text
production recovery orchestration contracts are stable
fake/temp replacement, switch, readiness and rollback are rehearsed
manual PR-R0..R7 runbook is archived
```

不能得出：

```text
production restore executed
real production config switched
real Monitor restarted against replacement
old production database can be deleted
zero data loss or cross-machine recovery proven
```
