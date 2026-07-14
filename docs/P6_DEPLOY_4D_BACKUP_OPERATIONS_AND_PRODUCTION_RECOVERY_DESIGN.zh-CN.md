# P6-Deploy-4D 备份运维与生产恢复设计

> 项目：tg-hub
> 阶段：P6-Deploy-4D / Production Recovery
> 状态：4D-1-implementation-complete / later-gates-closed
> 前置：P6-Deploy-4B、P6-Deploy-4C-C1/C2 已完成
> ALLOW_P6_DEPLOY_4D_1：yes
> ALLOW_P6_DEPLOY_4D_1B：no
> ALLOW_P6_DEPLOY_4D_2：no
> ALLOW_P6_DEPLOY_4D_4：no
> ALLOW_REAL_PIN_WRITE：no
> ALLOW_PRODUCTION_RESTORE_IMPLEMENTATION：no
> ALLOW_REAL_RETENTION_DELETE：no
> ALLOW_REAL_PRODUCTION_RESTORE：no
> ALLOW_P6_DEPLOY_5：no

## 1. 目标与结论边界

本设计同时定义两类工作，但不得混成一个命令或一个授权：

```text
P6-Deploy-4D
  = 备份清单、状态、保留策略、预算、清理和恢复 Runbook

Production Recovery
  = 灾难发生后，将已验证备份恢复到新的 replacement database，
    经人工确认后切换生产配置
```

4D 可以完成而不执行生产恢复。生产恢复设计锁定也不代表允许实现或执行真实恢复。

本阶段不得声称：

- 数据库与 watchlist 是同一时刻的原子快照；
- 凭据、Telethon session 或 Telegram 登录状态已备份；
- 跨机器恢复已经验证；
- 恢复点之后的数据不会丢失；
- 生产恢复可以无人值守执行；
- 单次备份足以替代外部备份或异地备份。

## 2. 不可突破的架构边界

备份运维属于部署层，不进入业务处理链路：

```text
BackupOperationsService
  -> BackupPackageValidator
  -> backup-root lock
  -> inventory / retention decision / guarded package delete
```

禁止：

- MonitorRuntime 参与备份清理；
- Parser、Normalizer、Dedup、EventBus 或 Bot 参与备份状态判定；
- 通过数据库业务 Repository 删除备份文件；
- 管理台直接拼接 shell、SQL、package path 或 database name；
- retention 修改 final package 内任一文件；
- production restore 复用 C2 的自动 DROP 成功路径；
- 自动删除旧生产数据库。

## 3. 阶段与 Gate

### 4D-1：只读 inventory 与状态模型

允许实现：

- final package 枚举；
- manifest safe-open；
- validator 结果聚合；
- package size、created_at、age、验证状态；
- pin sidecar 的只读模型；
- verification sidecar store 与 C2 成功写入集成；
- CLI JSON 报告；
- fake/temp 测试。

禁止删除、创建备份、恢复数据库或修改 pin。

现有 C2 验收早于 verification sidecar 契约，不允许根据历史文档反向伪造 sidecar。
4D-1 实现通过后必须另设 `4D-1B`：单独授权对选定真实 package 重跑 C2；只有新的 C2
全链路成功且 sidecar durable 后，真实 package 才能得到 `restore_verified=yes`。4D-1B
未通过前禁止 4D-3。

### 4D-2：retention 选择与 dry-run

允许实现：

- deterministic slot selection；
- archive budget 计算；
- protected/deletable 分类；
- dry-run 删除计划；
- pin/unpin 的独立受控命令和审计记录；
- 单元测试和临时目录集成测试。

禁止删除真实 `BACKUP_DIR` 中的 package。

### 4D-3：真实 retention 验收

需单独授权。先创建新的已验证生产备份，再对真实目录执行 dry-run；只有候选集合、
保底包、pin 和预算结果经人工确认后，才允许删除明确列出的 package。

### 4D-4：生产恢复 Runbook 与演练

只允许：

- 归档 Runbook；
- fake adapter 演练；
- 临时数据库演练；
- config staging/rollback 测试；
- 启停顺序测试。

不得修改真实生产数据库、生产 watchlist 或 `production.env`。

### Production Recovery：真实灾难恢复

必须另开逐 Gate 授权，不属于 4D 默认授权。

## 4. Backup inventory

唯一根目录为 `Settings.BACKUP_DIR`。inventory 获取 `.backup.lock` shared/non-blocking，
只接受 backup root 的直接子目录，目录名必须是 canonical `backup_id`。

每个条目分类为：

```text
valid
invalid
commit_uncertain
temp
pinned
in_use
recovery_required
```

同一条目可以同时带 observational flags，但最终 retention disposition 必须唯一：

```text
keep
delete_candidate
protected
manual_review
```

inventory 固定字段：

```text
backup_id
created_at_utc
package_bytes
manifest_status
database_dump_status
watchlist_snapshot_status
catalog_status
restore_verified: yes | no
verification_status: missing | valid | invalid | orphaned
verification_version: integer | null
pinned
pin_reason_code
recovery_held: yes | no
recovery_hold_status: missing | valid | invalid | orphaned
retention_slot
retention_disposition
error_code
report_desensitized: yes
```

不得输出绝对路径、数据库 URL、用户名、密码、watchlist 内容或 manifest 中的业务内容。
内部 inventory model 必须保留 verification sidecar 文件 identity、内容 SHA-256 和绑定的
package identity，供 retention plan 使用；这些 checksum 不进入对外报告。

高于当前支持版本的 verification sidecar 不忽略未知字段，也不降级解释：

```text
restore_verified=no
verification_status=invalid
retention_disposition=manual_review
error_code=BACKUP_VERIFICATION_IDENTITY_INVALID
```

invalid、temp、commit_uncertain 和 recovery_required 不得被伪装成可恢复备份，也不得由
普通 retention 自动删除；它们进入 manual review。

## 5. Pin 契约

final package 内容不可修改。pin 使用 backup root 下的私有 sidecar：

```text
<BACKUP_DIR>/.pins/<backup_id>.json
```

字段固定为：

```text
schema_version: 1
backup_id
pinned_at_utc
reason_code:
  pre_upgrade
  incident
  operator_hold
```

不允许自由文本 reason、用户名、路径或备注。目录 `0700`、文件 `0600`，使用 safe-open、
atomic replace 和 directory fsync。pin/unpin 获取 `.backup.lock` exclusive/non-blocking。

孤立 pin（对应 package 不存在）不自动删除，报告 `BACKUP_PIN_ORPHANED`，等待人工处理。

pin 的代码实现与真实目录写入分开授权：

```text
ALLOW_PIN_IMPLEMENTATION
ALLOW_REAL_PIN_WRITE
ALLOW_RETENTION_PLAN_IMPLEMENTATION
ALLOW_REAL_RETENTION_DELETE
```

允许实现 pin 命令不等于允许修改真实 backup root。

## 5.1 Restore verification sidecar

恢复验证结果不修改 final package，固定写入：

```text
<BACKUP_DIR>/.verifications/<backup_id>.json
```

字段固定为：

```text
schema_version: 1
backup_id
verified_at_utc
verification_version: 1
manifest_sha256
database_dump_sha256
watchlist_snapshot_sha256
result: passed
```

identity 定义：

- `manifest_sha256` 是 final `manifest.json` 全文件 SHA-256；
- `database_dump_sha256` 必须等于 manifest 记录且经流式复验的 dump SHA-256；
- `watchlist_snapshot_sha256` 必须等于 manifest 记录且经复验的 watchlist SHA-256；
- 三项与 canonical `backup_id` 共同构成 verification identity。

C2 只有在真实隔离 restore、schema、constraint、integrity 和 guarded DROP 全部成功且无
cleanup requirement 后，才由 restore-verification owner 写 sidecar。写入使用独立私有
目录 `0700`、文件 `0600`、safe-open、no-follow、atomic replace、file/directory fsync。
C2 报告成功但 sidecar 写入失败时，C2 验收仍保留数据库验证事实，但 package 的
`restore_verified` 必须为 `no`，并返回 `BACKUP_VERIFICATION_WRITE_FAILED` 供重试。

固定映射：

```text
sidecar missing
  -> restore_verified=no

sidecar schema/backup_id/checksum/result mismatch
  -> restore_verified=no
  -> BACKUP_VERIFICATION_IDENTITY_INVALID
  -> manual_review

sidecar valid and identity exact
  -> restore_verified=yes

sidecar exists but package missing
  -> BACKUP_VERIFICATION_ORPHANED
  -> manual_review
```

invalid/orphan sidecar 不自动删除。verification sidecar 的文件 identity、内容 SHA-256 和
绑定的 package identity 必须进入 retention plan；apply 时在 exclusive lock 内重新验证，
任何变化使尚未执行的 plan stale。

sidecar 写入失败后的重试不能只凭旧 C2 报告签发。重试必须获取 backup shared lock，
重新验证 backup ID、manifest/dump/watchlist checksum，并验证原 C2 result identity 与当前
package identity 完全一致；任一变化都要求重新执行 C2，不得补写 sidecar。

## 5.2 Production recovery hold

生产恢复选定 package 后使用独立 hold，不复用普通 pin：

```text
<BACKUP_DIR>/.recovery-holds/<backup_id>.<incident_id>.json
```

字段固定为：

```text
schema_version: 1
incident_id
selected_backup_id
package_identity
created_at_utc
```

hold 在 backup exclusive lock 内 safe-open、atomic write、file/directory fsync。有效 hold 使
package 永久 `protected`，retention 不得选择为 candidate。hold identity 必须进入 inventory
snapshot 和 retention plan。

incident record 缺失但 hold 存在时：

```text
recovery_hold_status=orphaned
retention_disposition=protected
error_code=BACKUP_RECOVERY_HOLD_ORPHANED
```

orphan hold 不自动删除。hold 只能在生产恢复明确取消且 replacement 未激活，或 selected
package 的 dump、watchlist 与全部 metadata 已完成读取并进入明确 durable phase后，由
production recovery owner 在 backup exclusive lock 内删除。任何不确定状态继续 protected。

## 6. Retention 默认策略

第一版固定：

```text
daily_slots: 7
weekly_slots: 4
minimum_valid_packages: 2
archive_budget_bytes: 5 GiB
```

预算只计算 final package，不包含 active temp、lock、pin sidecar 和 recovery record；
同时输出 `total_observed_bytes`，但不得把 excluded bytes 伪装成零。

slot 时间源只使用 manifest `created_at_utc`，不得使用目录 mtime、ctime 或遍历顺序。

选择顺序：

1. 按 `created_at_utc DESC, backup_id DESC` 排序；
2. 最近 7 个 UTC calendar day 各保留最新一个 valid package；
3. 最近 4 个 ISO UTC week 各保留最新一个尚未被 daily 选中的 valid package；
4. 所有 pinned package 永久 protected；
5. 全局至少保留最新 2 个 valid package；
6. 在全部 `valid && restore_verified=yes` package 中，至少 protected 最新一个；
7. 同一 package 命中多个保护条件时只计一次；
8. 其余 valid package 才可成为 delete candidate；
9. plan 必须证明 `post_plan_restore_verified_count >= 1`，否则返回
   `BACKUP_RETENTION_VERIFIED_FLOOR_VIOLATION` 且 candidate 集合为空；
10. 正常 retention plan 删除全部 delete candidate，按最旧到最新排序；
11. 预算不产生新 candidate，也不存在第二种“紧急 apply”模式；
12. plan 前预算状态固定为 `within_budget`、`exceeded_recoverable_after_plan` 或
    `exceeded_unrecoverable_after_plan`；
13. 删除全部 candidate 后仍无法满足预算时返回
   `BACKUP_BUDGET_EXCEEDED_UNRECOVERABLE`，不得扩大删除范围。

刚创建但尚未完成一次隔离恢复的最新备份仍可计入 valid，但在真实 retention 验收前必须
至少保留一个 `restore_verified=yes` 的 package。第一版不修改 manifest；恢复验证状态使用
独立、原子、私有 verification sidecar，并绑定 backup checksum identity。

## 7. Retention 执行与恢复性

dry-run 与真实删除必须是两个命令、两个授权：

```text
backup-retention plan
backup-retention apply --plan-id <opaque-id>
```

plan 存放于私有 runtime 目录，至少绑定：

```text
plan_id
created_at_utc
backup_root_identity:
  configured_logical_root_id
  resolved_root_device_id
  resolved_root_inode
  policy_namespace
policy_version
selection_algorithm_version
journal_schema_version
pending_path_scheme_version
canonical_inventory_snapshot_digest
all valid package identities
all protected package identities
daily/weekly slot winner identities
minimum-package protected identities
pinned package identities
verified_floor_backup_id
verified_floor_verification_identity
candidate identities:
  backup_id
  manifest SHA-256
  package byte count
  pin identity
  verification sidecar identity
```

绝对路径不进入 plan 的对外报告。root device/inode 只保存在私有 plan 内。

首次 apply 获取 `.backup.lock` exclusive 后重新 inventory；plan 绑定的 package 集合、
identity、checksum、pin、verification 或 policy 任一变化，整个 plan 失效。resume 时，
plan-owned 且由 journal 证明的 deleted/pending 变化属于预期；其他变化才返回
`BACKUP_RETENTION_PLAN_STALE`。

apply 必须证明 candidate selection inputs、protected selection inputs 与 verified-floor evidence
均未变化。不能只重新计算“当前仍存在某个 verified package”后继续使用旧 plan。

plan 是 immutable；apply 另建 mutable、原子写入且 fsync 的 execution journal。每个
candidate 状态固定为：

```text
planned
rename_started
pending
delete_started
deleted
cleanup_required
```

journal 每次状态转换前后遵守 durable-intent：先写 intent，再执行外部动作，再写完成
状态。不得原地追加半行；第一版使用完整 JSON atomic replace。

单包删除采用 rename-to-pending：

```text
final package
-> atomic rename to <BACKUP_DIR>/.retention-pending/<backup_id>.<plan_id>
-> fsync backup root
-> recursive delete pending directory without following symlinks
-> fsync pending parent
```

rename 成功后 package 不再是有效备份。删除失败保留 pending 并返回
`BACKUP_RETENTION_CLEANUP_REQUIRED`。普通 inventory 和 restore 不得读取 pending。

apply 不承诺跨多个 package 的事务原子性；结果必须逐项报告 `deleted` 或
`cleanup_required`。崩溃重入规则固定为：

- journal 为 `deleted`：final/pending 均缺失是预期状态；
- journal 为 `pending`：final 缺失且 exact pending identity 匹配，写 `delete_started` 后
  继续 guarded delete；
- journal 为 `delete_started`：
  - final 缺失且 exact pending identity 匹配：delete 尚未完成，继续 guarded delete；
  - final 缺失且 exact pending 不存在：只有 journal 已持久化 pending identity、pending
    path 可由 backup ID + plan ID 唯一推导、同 backup ID 无其他 pending、backup root 与
    pending parent identity 未变化时，才认定 delete 已完成并补写 `deleted`；
  - 其他状态进入 manual review；
- journal 为 `cleanup_required`：
  - final 缺失且 exact pending identity 匹配：写 `delete_started` 并重试 guarded delete；
  - final 缺失且 pending 不存在，且满足上述 delete_started 缺失证明：写 `deleted`；
  - final 存在：invariant violation，manual review；
  - pending path/identity 不匹配：manual review；
  - 只允许转换到 `delete_started` 或 `deleted`，不是终态；
- journal 为 `rename_started`：允许 final 仍存在，或 final 缺失且唯一 pending identity
  匹配；两者之外均需 manual review；
- journal 为 `planned`：final、manifest、pin、verification sidecar 和 package identity
  必须与 immutable plan 完全一致；
- 任何 plan 外 package 变化、planned 项变化或 pending identity 不一致，返回
  `BACKUP_RETENTION_PLAN_STALE`，停止后续 candidate；
- 已完成项不因自身预期缺失而使 plan stale；
- 不重新选择 candidate，不把新 package 加入旧 plan。

apply resume 必须继续原 plan 中尚未完成的 candidate；如存在 cleanup_required，则先收敛
该项，不能跳过后继续删除其他 package。

## 8. 生产恢复基本策略

生产恢复固定使用 replacement database，不在当前生产库原地恢复：

```text
current production database: preserved
selected backup: read-only
replacement database: generated identity
production.env: staged then atomic switch
```

明确禁止：

- `pg_restore --clean` 指向当前生产库；
- `DROP DATABASE <current production>`；
- 用户输入 replacement database name；
- 自动删除、rename 或 truncate 旧生产库；
- 在主服务或 Monitor 运行时切换数据库；
- 自动启动 Monitor；
- 从备份包恢复任何凭据或 Telethon session；
- 未通过 C2 的 package 直接进入生产恢复。

## 9. 生产恢复输入与身份

唯一可选输入：

```text
canonical backup_id
incident_id: opaque generated id
```

禁止接受 package path、database name、SQL、connection URL 或 arbitrary env path。

replacement database 名称由系统生成：

```text
tg_hub_recovery_<UTC timestamp>_<random suffix>
```

并写入 production recovery record。`selected_backup_id` 与恢复前新建的
`protection_backup_id` 是两个独立字段；创建 protection backup 永远不得替换已选恢复源。
状态机复用 C1 的 durable-intent 原则，但使用独立 record schema 和独立 phase：

```text
planned
protection_backup_started
protection_backup_completed
replacement_create_started
replacement_created
identity_commit_started
identity_committed
restore_started
restore_completed
verification_completed
services_stopped
config_protection_started
config_protection_completed
env_switch_started
env_switched
watchlist_switch_started
watchlist_switched
config_switched
application_started
readiness_passed
monitor_start_authorized
monitor_start_started
monitor_started
monitor_write_observed
completed
rollback_started
rollback_monitor_stopped
rollback_application_stopped
rollback_env_started
rollback_env_completed
rollback_watchlist_started
rollback_watchlist_completed
rollback_application_started
rollback_readiness_passed
rolled_back
```

phase 只允许合法前向转换；每个不可逆外部动作前先 durable intent。恢复失败保留原 phase，
不得进入通用 `failed` phase。最近一次失败使用独立字段：

```text
last_operation
last_error_code
last_error_at_utc
retryable
```

phase 只表达已持久化的生命周期事实，错误字段不改变 phase，也不得包含自由文本异常。

record 还必须绑定：

```text
original_env_sha256
staged_env_sha256
protected_env_sha256
original_watchlist_sha256
staged_watchlist_sha256
protected_watchlist_sha256
watchlist_switch_authorized: yes | no
watchlist_was_switched: yes | no
replacement_activated: yes | no
protection_backup_status: completed | skipped_authorized
original_database_component
original_database_identity
original_database_owner
original_database_revision
monitor_generation_id
monitor_write_baseline
monitor_first_write_observed: yes | no | unknown
monitor_first_write_observed_at_utc: datetime | null
```

原生产数据库身份字段只保存在私有 record，不进入对外报告。Monitor baseline/generation
只用于观察和事故分析，不用于证明“从未写入”，也不作为自动回滚授权依据。

`protection_backup_status=skipped_authorized` 是人工 Gate 结果，不使用错误码；真正失败才
返回 `PROTECTION_BACKUP_FAILED`。

`watchlist_was_switched` 不是调用者任意赋值：active watchlist checksum 等于 staged 时
协调为 `yes`，等于 original 且从未完成 replace 时为 `no`，第三种内容停止。active env 的
database component 等于 replacement 时，`replacement_activated` 必须作为 `yes`，即使
record 普通字段尚未成功更新；active env 是 cleanup 的最终事实源。

## 10. 生产恢复逐 Gate Runbook

### PR-R0：事件确认，只读

- 确认真实故障，而不是普通 Monitor 错误；
- 记录 incident ID；
- 选择 canonical backup ID；
- validator 重新通过；
- 确认该 package 已通过 C2 或更新的隔离恢复验证；
- 确认 PostgreSQL major、代码 revision 和 migration 兼容；
- 输出预计 RPO：backup `created_at_utc` 到故障时刻之间的数据可能丢失。

### PR-R1：当前现场保护备份

在生产数据库仍可读时，创建 protection backup 并完整验证。若数据库不可读而无法备份，
必须单独确认 `protection_backup_status=skipped_authorized`，不能静默继续；这不是错误码。

PR-R1 开始时先获取 backup exclusive lock，重新验证 selected package 并 durable 写入
production recovery hold，然后释放 lock。随后 BackupService 独立获取 exclusive lock 创建
protection backup；hold 在锁间窗口持续保护 selected package。protection backup 完成后，
PR-R2 立即获取 shared lock。禁止 shared -> exclusive 锁升级或嵌套获取同一 backup lock。

### PR-R2：恢复 replacement database

主服务尚可保持运行，因为 replacement 与当前库隔离。执行：

```text
create generated replacement
-> identity comment
-> pg_restore explicit generated target
-> schema/constraint/integrity verification
```

PR-R2 获取 backup shared lock 后重新验证 selected package identity，并持续持有到 PR-R3
watchlist staging 和所有后续 package metadata 复制完成。此 Gate 结束时不切换配置，也不
DROP replacement。

### PR-R3：准备 watchlist staging

备份包中的 watchlist snapshot 写入私有 staging 文件，校验 schema 和 checksum。保留当前
watchlist 的 protection copy。由于 DB 与 watchlist 不是原子快照，必须展示二者时间差并
要求人工确认。staging 完成并 fsync、且后续不再读取 package 后，才释放 backup shared
lock；retention 在此之前无法获得 exclusive lock。

### PR-R4：停止服务

按顺序：

```text
request Monitor stop
-> confirm Monitor stopped
-> confirm telethon-session.lock free
-> bootout/stop main LaunchAgent
-> confirm HTTP unavailable
-> confirm DB application connections drained
```

禁止 kill PostgreSQL，禁止终止未知连接；存在活动连接时停止 Gate。

### PR-R5：原子配置切换

在主服务停止后：

1. 再次验证 replacement identity、owner、comment 和 revision；
2. 写入 `config_protection_started`；
3. 创建 `production.env` 与当前 watchlist 的私有 protection copy；
4. safe-open 重读 active 文件，记录 original/protected SHA-256 并要求完全一致；
5. 写入 `config_protection_completed`；
6. 生成只改变 `DATABASE_URL` database component 的 staged env；
7. 保留原 host、port、user、password 语义并记录 staged SHA-256；
8. 写入 `env_switch_started`；
9. safe-open、`0600`、fsync、atomic replace `production.env` 并 fsync parent；
10. 重读 active env，只有 SHA-256 等于 staged 才写 `env_switched`；
11. watchlist 获单独确认时写 `watchlist_switch_started`，执行 atomic replace、fsync、
    重读并确认 staged SHA-256，再写 `watchlist_switched`；
12. watchlist 未获授权时必须保持 original SHA-256，且不得进入 watchlist switch phase；
13. 两个 active 文件均与 record 中授权状态一致后，写 `config_switched`。

不得 source/eval env，不得把 URL 写入报告或日志。

### PR-R5 崩溃协调矩阵

resume 必须 safe-open active、staged、protected 文件并比较 record checksum，不能只信 phase：

```text
config_protection_started:
  active == original, protection missing
    -> 可重新创建 protection
  active == original, protection == original
    -> 可完成 config_protection_completed
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN，停止

env_switch_started:
  active env == original
    -> env replace 尚未完成，可重试
  active env == staged
    -> replace 已完成，写 env_switched
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN，停止

watchlist_switch_started:
  active watchlist == original
    -> watchlist replace 尚未完成，可重试
  active watchlist == staged
    -> replace 已完成，写 watchlist_switched
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN，停止

env_switched but watchlist not switched:
  watchlist_switch_authorized=yes
    -> 继续 watchlist durable-intent 流程
  watchlist_switch_authorized=no and active watchlist == original
    -> 可写 config_switched
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN，停止

config_switched phase write failed:
  active env/watchlist 与授权后的 staged/original checksum 完全一致
    -> 可补写 config_switched
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN，停止
```

任何未知第三种内容都不得猜测、覆盖或自动回滚。若仅 env 已切换且无法继续 watchlist
切换，允许使用 protection checksum 执行明确 rollback intent，将 env 恢复为 original；
不得留下未经记录的半切换状态。

### PR-R6：只启动应用，不启动 Monitor

```text
bootstrap main LaunchAgent
-> liveness
-> readiness: database/migration/watchlist/assembly
-> static preflight
-> read-only application smoke query
```

`MONITOR_AUTO_START` 必须为 false。任何失败立即进入 rollback decision，不得自动启动
Monitor 或执行 migration upgrade。

application 启动 resume 固定为：

```text
phase=config_switched and LaunchAgent stopped
  -> 可 bootstrap
phase=config_switched and LaunchAgent running
  -> 验证进程实际使用 replacement database
  -> 成功则补写 application_started
  -> 无法证明则停止并进入 manual review
phase=application_started
  -> 不重复 bootstrap，只执行 liveness/readiness 收敛
```

### PR-R7：人工确认启动 Monitor

只有 R6 全部通过后，用户再次明确授权：

```text
receive independent authorization
-> write monitor_start_authorized
-> online preflight
-> capture durable replacement write baseline and monitor generation
-> write monitor_start_started
-> start Monitor
-> confirm generation ownership
-> write monitor_started
-> observe heartbeat
-> compare stable database write watermark
-> first committed write exists: write monitor_write_observed
-> observe ingest/process counters
```

`monitor_start_authorized` 只表示人工授权，不表示 Monitor 已启动。自动 rollback 只允许在
尚未进入 `monitor_start_started` 时发生。一旦 `monitor_start_started` durable，无论 Monitor
是否实际启动、是否观察到写入、进程是否停止，都永久禁止自动回滚，统一进入
`PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED`。这是第一版保守 write fence，避免
用 count/max-id、heartbeat、PID、WAL LSN 或进程状态错误证明“从未写入”。

合法尾部转换固定为：发生首个 committed write 时
`monitor_started -> monitor_write_observed -> completed`；有界观察窗口内无可见写入但
Monitor 健康时可 `monitor_started -> completed`，同时保持
`monitor_first_write_observed=no`。该字段仅具观察意义；从 `monitor_start_started` 起就已
没有自动 rollback。

旧生产数据库继续保留，不自动删除。何时归档或删除旧库属于新的独立维护阶段。

## 11. 回滚策略

切换前失败：

- 生产配置未变；
- 主服务可继续使用旧库；
- replacement 保留并通过 recovery record guarded cleanup；
- 不触发生产回滚。

切换后、Monitor 启动前失败：

```text
stop main LaunchAgent
-> restore protected production.env
-> restore protected current watchlist if it was switched
-> atomic replace + fsync
-> start application against old database
-> verify liveness/readiness
-> Monitor remains stopped
```

回滚同样使用逐动作 durable intent：

```text
rollback_started（仅 monitor_start_started 前合法）
-> confirm Monitor stopped
-> rollback_monitor_stopped
-> stop application
-> rollback_application_stopped
-> rollback_env_started
-> restore protected env
-> verify active env == original
-> rollback_env_completed
-> if watchlist_was_switched=yes:
     rollback_watchlist_started
     -> restore protected watchlist
     -> verify active watchlist == original
     -> rollback_watchlist_completed
-> if watchlist_was_switched=no:
     verify active watchlist == original
-> bootstrap application against original database
-> rollback_application_started
-> verify liveness/readiness and original database identity
-> rollback_readiness_passed
-> rolled_back
```

回滚协调矩阵：

```text
rollback_env_started:
  active env == staged
    -> restore 尚未执行，可重试
  active env == original
    -> restore 已完成，补写 rollback_env_completed
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN

rollback_watchlist_started:
  active watchlist == staged
    -> restore 尚未执行，可重试
  active watchlist == original
    -> restore 已完成，补写 rollback_watchlist_completed
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN

rollback_env_completed:
  watchlist_was_switched=yes
    -> only legal next phase is rollback_watchlist_started
  watchlist_was_switched=no and active watchlist == original
    -> legal next phase is rollback_application_started
  otherwise
    -> PRODUCTION_RECOVERY_CONFIG_IDENTITY_UNKNOWN

rollback_application_started:
  LaunchAgent stopped
    -> bootstrap 尚未完成，可重试
  LaunchAgent running and proven to use original database
    -> bootstrap 已完成，继续 readiness
  otherwise
    -> PRODUCTION_RECOVERY_ROLLBACK_FAILED
```

`rolled_back` 只能在 active env 为 original、watchlist 为 original 或从未切换、应用已证明
连接原数据库、readiness 通过且 Monitor 停止时写入。任何第三种文件内容或数据库身份都
停止自动回滚。

Monitor 已在新库启动并产生写入后，不允许自动切回旧库，因为会产生双写历史分叉。此时
进入 `PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED`，停止 Monitor并保留两库，
由人工决定数据协调方案。

### Replacement cleanup

Production Recovery 不重新实现 DROP primitive，复用 4C 已验收的 database identity、
owner、activity、prepared transaction 和 guarded DROP adapter，但使用 production
recovery 独立 record。

一旦 active `production.env` 的 database component 指向 replacement，必须设置不可逆事实
`replacement_activated=yes`。如果 phase 写入失败，cleanup 也必须通过 active env 再次判断；
只要 active env 当前指向 replacement，自动 cleanup 永远拒绝 DROP。

只有同时满足以下条件才允许自动 guarded cleanup：

- active env 不指向 replacement；
- replacement 尚未激活，或已通过 durable rollback 恢复旧库；
- 主服务与 Monitor 均停止；
- replacement identity token、owner、comment 全部匹配；
- active connections 与 prepared transactions 均为零；
- recovery record 明确允许 cleanup。

切换完成后的 replacement 已是生产数据库，不得再使用 C2 success cleanup 语义。

## 12. Lock 与所有权

固定锁顺序：

```text
production-recovery.lock exclusive
-> backup.lock exclusive（仅 protection backup 阶段）
-> release backup.lock
-> backup.lock shared（仅验证和读取选定 package 的阶段）
-> release backup.lock after pg_restore, watchlist staging and metadata copy settle
-> telethon-session.lock non-blocking（停止阶段确认）
```

不得持有 shared lock 后尝试升级为 exclusive，也不得同时持有两个 backup lock fd。
protection backup 必须先完成并释放 exclusive lock，随后才允许以 shared lock 固定选定
package。retention 只获取 backup.lock exclusive，不获取 production-recovery.lock；生产
恢复持有 backup shared lock 时 retention 稳定失败，不得删除选定 package。

唯一 orchestrator 持有：

- production recovery record；
- replacement DB adapter；
- pg_restore process；
- config/watchlist staging；
- LaunchAgent lifecycle；
- rollback decision。

router、CLI 和 adapter 不得重复 cleanup、DROP、disconnect 或释放锁。

## 13. 稳定错误码

4D：

```text
BACKUP_INVENTORY_INVALID
BACKUP_PIN_INVALID
BACKUP_PIN_ORPHANED
BACKUP_RECOVERY_HOLD_ORPHANED
BACKUP_VERIFICATION_WRITE_FAILED
BACKUP_VERIFICATION_IDENTITY_INVALID
BACKUP_VERIFICATION_ORPHANED
BACKUP_RETENTION_PLAN_STALE
BACKUP_RETENTION_NOTHING_TO_DELETE
BACKUP_RETENTION_CLEANUP_REQUIRED
BACKUP_RETENTION_VERIFIED_FLOOR_VIOLATION
BACKUP_BUDGET_EXCEEDED_UNRECOVERABLE
```

生产恢复：

```text
PRODUCTION_RECOVERY_NOT_AUTHORIZED
PRODUCTION_RECOVERY_IN_PROGRESS
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
PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED
```

异常文本、SQL、URL、路径、token 和业务数据不得进入报告。

## 14. 管理台与 CLI 边界

4D 第一版优先 CLI，不要求管理台实现删除或恢复按钮。

允许的管理台只读展示：

- valid backup 数量；
- 最新备份时间；
- 最新 restore-verified 时间；
- archive bytes / budget status；
- invalid/manual-review 数量；
- retention 最近执行状态。

禁止在管理台第一版提供：

- 一键生产恢复；
- 任意 package path 输入；
- database name 输入；
- 无二次 Gate 的 retention apply；
- 自动启动 Monitor。

## 15. 测试与验收矩阵

至少覆盖：

1. inventory 拒绝 symlink、traversal、非 canonical ID；
2. inventory shared lock 与 create/retention exclusive lock 竞争；
3. invalid/temp/pending/uncertain 不成为 delete candidate；
4. daily/weekly slot 跨 UTC 日、ISO 周和边界排序；
5. minimum valid packages 永远保留；
6. pinned package 永不删除；
7. orphan pin 报告但不自动删除；
8. budget 无法满足时不扩大删除；
9. plan 生成后 package、checksum、pin 或 policy 变化则整体 stale；
10. apply 只删除 plan 中 candidate；
11. rename-to-pending 后崩溃可收敛；
12. 不跟随 package 内 symlink；
13. shared restore lock 阻止 retention apply；
14. production restore 拒绝未通过 C2 的 package；
15. replacement target 永远不是当前生产 database；
16. pg_restore argv 不含当前生产 database、URL 或 password；
17. 所有不可逆动作前 durable intent；
18. watchlist staging checksum/schema 验证；
19. DB/watchlist snapshot 时间差明确报告；
20. 服务未完全停止时禁止 config switch；
21. active DB connections 存在时禁止 switch；
22. env staging 只改变 database component；
23. env 原子切换失败可回滚；
24. readiness 失败时 Monitor 不启动；
25. R7 必须二次明确授权；
26. Monitor 在新库产生写入后禁止自动回滚；
27. 旧生产数据库永不自动 DROP；
28. 凭据/session 不进入 backup 或报告；
29. fake adapter 全流程与取消测试；
30. 临时 PostgreSQL replacement/switch/rollback 演练；
31. 完整非数据库与数据库回归；
32. 真实 retention 删除单独授权；
33. 真实生产恢复逐 Gate 单独授权。
34. verification sidecar 缺失、identity mismatch 和 orphan 映射准确；
35. verification sidecar/pin 变化使 planned candidate stale；
36. execution journal 在 planned/rename_started/pending/delete_started/deleted 各阶段
    崩溃后确定性恢复；
37. 已删除 candidate 的预期缺失不使原 plan stale；
38. protection backup ID 不覆盖 selected backup ID；
39. shared package lock 覆盖 dump restore 与 watchlist staging；
40. env/watchlist 在每个 intent phase 的“未执行/已执行”两态均可收敛；
41. env/watchlist 出现第三种 checksum 时稳定停止且不覆盖；
42. replacement 已成为 active production 时自动 cleanup 永远拒绝 DROP；
43. 通用失败只更新 error fields，不改变 durable phase。
44. newest restore-verified package 永远 protected，post-plan verified floor 不低于 1；
45. delete_started 且 final/pending 均缺失时，仅在完整 identity 证明下补写 deleted；
46. cleanup_required 的四种文件事实均按固定矩阵收敛或 manual review；
47. rollback env/watchlist 在动作未执行与已执行未记 phase 两态均可恢复；
48. rollback 文件出现第三种 checksum 时 fail closed；
49. application 已启动但 phase 未写入时按 replacement/original DB identity 收敛；
50. monitor generation + DB watermark 仅用于观察，不授权自动 rollback；
51. monitor_start_started durable 后无条件禁止自动 rollback；
52. inventory 稳定输出 verification status/version/restore_verified。
53. plan 绑定 candidate、slot winner、minimum、pin 和 verified-floor 全部依赖 identity；
54. verified-floor sidecar 在 apply 前变化使旧 plan stale；
55. recovery hold 在 protection backup 的锁间窗口持续保护 selected package；
56. orphan recovery hold 保持 protected 并进入 manual review；
57. watchlist 从未切换时 rollback_env_completed 可直接进入 application rollback；
58. watchlist 实际已切换但 phase 未写入时通过 checksum 协调
    `watchlist_was_switched=yes`；
59. `monitor_start_authorized` 在 online preflight 前 durable；
60. 从 `monitor_start_started` 起任何自动 rollback 均稳定拒绝。

## 16. 完成标准

P6-Deploy-4D 完成只能得出：

```text
backup inventory stable
retention selection deterministic
retention delete guarded and recoverable
production recovery runbook archived and rehearsed with fake/temp resources
```

不能得出：

```text
production restore executed
old production database can be deleted
cross-machine recovery proven
credentials/session recoverable
zero data loss guaranteed
```

真实 Production Recovery 只有全部 PR-R0 至 PR-R7 Gate 分别通过后才能归档为 executed；
任一 Gate 的授权不得自动继承到下一 Gate。

## 17. 建议实施顺序

```text
4D-1 inventory/status contracts
-> review and commit
4D-1B separately authorized C2 rerun and verification sidecar issuance
-> archive
4D-2 retention plan/dry-run
-> review and commit
4D-3 separately authorized real retention acceptance
-> archive
4D-4 production recovery orchestration with fake/temp rehearsal
-> archive runbook
Deploy-5 final delivery acceptance
```

真实生产恢复不是 Deploy-5 的必做动作。只要真实备份、真实隔离恢复、retention 保护和
生产恢复 Runbook/演练均通过，Deploy-5 可以在 `production_restore_executed=no` 下完成。

## 18. 当前设计结论

```text
P6-DEPLOY-4D_DESIGN_REVIEW:
  result: 4D_1_implementation_complete
  architecture_direction: aligned
  previous_review_blockers_addressed: 6
  previous_review_recommendations_addressed: 6
  second_review_blockers_addressed: 5
  second_review_recommendations_addressed: 5
  third_review_new_blockers_addressed: 4
  third_review_recommendations_addressed: 5
  blockers:
    4D_1: 0
    later_stages: fourth_review_pending
  4D_1_focused_tests: 39_passed_1_skipped
  4D_1_full_regression: 608_passed_3_skipped
  real_verification_sidecar_issued: no
  real_backup_package_accessed: no
  allow_P6_Deploy_4D_1: complete
  allow_P6_Deploy_4D_1B: no
  allow_P6_Deploy_4D_2: no
  allow_real_retention_delete: no
  allow_P6_Deploy_4D_4: no
  allow_production_restore_implementation: no
  allow_real_production_restore: no
  allow_P6_Deploy_5: no
```

第三轮独立复审批准的 4D-1 已完成：inventory/status、verification sidecar store、C2
integration code 与 fake/temp 测试均已实现，完整回归通过，真实 backup package 未访问，
真实 sidecar 未签发。下一步只能独立评审 4D-1B，不得连带进入 retention、真实 pin、
4D-4 或生产恢复。
