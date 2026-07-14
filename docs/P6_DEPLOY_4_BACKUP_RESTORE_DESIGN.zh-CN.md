# P6-Deploy-4 备份与恢复设计

> 项目：tg-hub  
> 阶段：P6-Deploy-4  
> 状态：design-locked  
> 前置：P6-Deploy-3D-4 已完成并归档  
> ALLOW_IMPLEMENTATION：P6-Deploy-4A-only  
> ALLOW_REAL_BACKUP：no  
> ALLOW_RESTORE_VERIFY：no  
> ALLOW_PRODUCTION_RESTORE：no

## 1. 阶段目标

P6-Deploy-4 为当前单用户 macOS 本机生产环境建立可重复、可校验的备份与
隔离恢复验证能力：

```text
PostgreSQL + watchlist + reproducibility metadata
-> application-immutable backup package
-> internal completeness marker + external directory commit
-> isolated restore database
-> schema and integrity verification
-> desensitized report
```

本阶段通过后只能证明：完整备份包可以在本机恢复到隔离数据库，且恢复后的
schema 与基础数据完整性成立。它不能证明跨机器迁移、敏感凭据恢复、灾难发生时
零数据损失，或任何 PostgreSQL 版本间都可恢复。

## 2. 固定边界

### 2.1 第一版备份内容

必须包含：

- PostgreSQL `pg_dump` custom format；
- 当前 `watchlist.json` 的一致快照；
- `manifest.json`；
- manifest 中记录当前 Git commit、Alembic revision、配置 schema、Python
  版本和 dependency lock SHA-256；
- 每个 payload 文件的 SHA-256 和字节数。

第一版明确排除：

- `production.env`：`secret_material_excluded`；
- Telethon `.session`、`-journal`、`-wal`、`-shm`：
  `authentication_session_excluded`；
- Bot token、Telegram API hash、webhook secret；
- app/rotation 日志与 archive；
- heartbeat、lock、pending、runtime status；
- Python venv、Git 工作树和缓存；
- 已存在的其他 backup package。

tg-hub 不自创备份加密格式。备份包即使排除了凭据，仍包含 RawMessage 和资源
业务数据，必须按私有数据处理。未来若需包含 env/session，必须单独设计 age、
Keychain 或加密磁盘及密钥托管，不得在本阶段顺手加入。

### 2.2 恢复范围

本阶段允许设计和实现：

- 备份包静态校验；
- 在新建的隔离数据库中执行 `pg_restore`；
- 验证 Alembic revision、核心表、约束和基础计数；
- 成功后删除隔离数据库；
- 失败时默认保留隔离数据库并输出受控清理命令。

本阶段禁止：

- 对生产数据库执行 `pg_restore`；
- 使用 `--clean`、`--create` 让 dump 自行选择或删除数据库；
- 自动停止/启动主 LaunchAgent 或 Monitor；
- 自动恢复 `production.env` 或 Telethon session；
- 自动切换 Git commit、安装依赖或执行应用升级；
- 修改 watchlist、业务数据或数据库迁移；
- 上传到云盘、NAS 或任何远端；
- 增加备份 LaunchAgent 定时器；
- 进入 P6-Deploy-5。

## 3. 目录与备份包

固定根目录：

```text
~/.tg-hub/backups/
├── .backup.lock
├── .tmp/
└── <backup_id>/
    ├── database.dump
    ├── watchlist.json
    └── manifest.json
```

`backup_id` 固定为：

```text
YYYYMMDDTHHMMSS.ffffffZ-<128-bit-random-hex>
```

安全要求：

- `BACKUP_DIR` 必须位于 `~/.tg-hub/`，自身和父目录不得为 symlink；
- backup root、`.tmp`、package directory 权限为 `0700` 或更严格；
- lock、dump、watchlist snapshot、manifest 权限为 `0600`；
- 所有创建和打开使用 `O_NOFOLLOW`（平台支持时）、`fstat` 和 regular-file
  校验；
- 目标 package 已存在时 fail closed，不覆盖、不合并；
- 临时 package 与 final package 必须位于同一文件系统；
- final package 一旦提交即视为 application-immutable：tg-hub 不提供修改 final
  package 的路径，校验和恢复只读，retention 只允许整包删除，不在包内写入
  status、lock 或临时文件；这不宣称抵御同一 Unix 用户主动篡改，篡改由 checksum
  和 package validation 检测。

## 4. Manifest 契约

备份采用双层提交语义：

- `manifest.json` 是 temp package 内部完整性的最终文件，必须最后生成并通过同目录
  原子 rename 提交；
- temp package directory 原子 rename 到 final `backup_id` directory，才是备份包的
  对外提交点。

不使用额外空 `SUCCESS` 文件。有效备份必须同时位于 final backup root、包含
`backup_status=complete` 的 manifest，且全部 payload 校验通过。`.tmp/` 中即使已有
完整 manifest，也不得被校验器、恢复器或 retention 视为有效备份。

固定最小 schema：

```json
{
  "schema_version": 1,
  "backup_id": "...",
  "backup_status": "complete",
  "created_at_utc": "timezone-aware UTC",
  "app_git_commit": "40-char commit",
  "git_worktree_clean": true,
  "python_version": "major.minor.patch",
  "dependency_lock_filename": "uv.lock",
  "dependency_lock_sha256": "...",
  "alembic_revision": "...",
  "config_schema_version": 1,
  "database": {
    "format": "postgresql_custom",
    "filename": "database.dump",
    "sha256": "...",
    "size_bytes": 0,
    "source_server_version": "...",
    "source_server_major": 0,
    "pg_dump_version": "...",
    "pg_dump_major": 0
  },
  "watchlist": {
    "filename": "watchlist.json",
    "sha256": "...",
    "size_bytes": 0,
    "revision": "..."
  },
  "exclusions": {
    "production_env": "secret_material_excluded",
    "telethon_session": "authentication_session_excluded",
    "logs": "operational_data_excluded",
    "runtime_state": "ephemeral_data_excluded"
  }
}
```

禁止写入 manifest：

- DATABASE_URL、host、port、database name、用户名或密码；
- env/session/watchlist 的绝对路径；
- 频道 ID、username、RawMessage 内容或数据库行计数明细；
- exception text、命令行或进程环境。

所有 hash 使用小写 SHA-256 hex。manifest JSON 使用 UTF-8、固定 key 顺序和
紧凑序列化；读取时拒绝 unknown schema version、重复 key、非有限数字和路径
穿越 filename。

## 5. 生产备份流程

### 5.1 前置检查

```text
load production settings through Python parser
-> validate APP_ENV/config/private paths
-> acquire .backup.lock with LOCK_EX | LOCK_NB
-> verify backup root safety and free space
-> verify pg_dump availability/version
-> verify database reachable and Alembic current == head
-> validate watchlist schema
-> require clean Git worktree
```

锁竞争返回稳定错误，不等待。`.backup.lock` 是 backup root lifecycle lock：backup
creation 和 retention 使用 `LOCK_EX | LOCK_NB`，package validation 和 restore
verify 使用 `LOCK_SH | LOCK_NB`。shared lock 必须覆盖 package validation、
`pg_restore`、schema/integrity verification、guarded DROP 或失败报告收口的完整
生命周期；retention 因此不能删除正在使用的 package。该锁不得复用 rotation、
heartbeat 或 Telethon session lock。

第一版只支持同 major 本机备份恢复：

```text
source_server_major
== pg_dump_major
== pg_restore_major
== restore_target_server_major
```

备份前不满足 source/pg_dump 同 major 时返回 `PG_DUMP_VERSION_UNSUPPORTED`；恢复
前不满足 manifest/pg_restore/target 同 major 时返回
`PG_RESTORE_VERSION_UNSUPPORTED`，不得尝试恢复后再推断兼容性。未来支持跨 major
必须另行设计兼容矩阵并真实验收。命令、日志和报告不得输出 DATABASE_URL；认证
只通过受控子进程环境或本机 PostgreSQL 认证机制传递，不把密码拼进命令行。

### 5.2 一致性语义

PostgreSQL 使用单次 `pg_dump --format=custom --no-owner --no-acl` 取得其自身
一致性快照。主服务和 Monitor 无需停机，备份期间发生的后续写入不属于该 dump。

watchlist 与数据库不声明跨介质原子快照。watchlist snapshot 固定为：

```text
safe open regular file
-> fstat N/device/inode
-> exactly-N read
-> second fstat device/inode/type/size unchanged
-> schema validation
-> write snapshot with 0600
```

若读取期间变化，返回 `WATCHLIST_CHANGED_DURING_BACKUP`，删除本轮 temp package；
第一版不自动无限重试。

### 5.3 提交顺序

```text
create private temp package
-> pg_dump to database.dump.tmp
-> fsync + validate custom dump catalog
-> commit database.dump
-> capture and validate watchlist snapshot
-> commit watchlist.json
-> compute payload hashes and metadata
-> write + fsync manifest.tmp
-> atomic rename manifest.json
-> fsync package directory
-> atomic rename temp package to final backup_id
-> fsync backup root
-> release lock
```

final directory rename 前的 package 永远不是有效备份。任一步失败必须清理本轮可
证明由当前进程创建的 temp；若安全清理失败，保留 temp 并返回
`BACKUP_CLEANUP_REQUIRED`，不得把它纳入恢复或 retention。

若 final directory rename 已成功但 backup root `fsync` 失败，返回
`BACKUP_COMMIT_UNCERTAIN`，保留 final package 且禁止自动删除。后续只能通过完整
package validation 将其分类；已进入 final root 的 package 不得按普通 temp 清理。

## 6. 备份包校验

校验是只读操作：

```text
acquire .backup.lock shared
-> safe-open package and manifest
-> schema/backup_id/filename guards
-> verify mode and regular-file identity
-> recompute SHA-256 and size
-> pg_restore --list database.dump
-> validate watchlist schema and revision
-> emit desensitized result
-> release shared lock
```

有效包必须满足：

- manifest 最后提交且 `backup_status=complete`；
- package basename 与 manifest backup_id 一致；
- payload 文件集合恰好等于 manifest 声明集合；
- checksum 和字节数一致；
- dump catalog 可读；
- watchlist schema 有效且 revision 一致；
- package 内无 symlink、socket、device、FIFO 或额外未知文件。

校验器只接受 final backup root 下的 package；`.tmp/` package 无论 manifest 是否
完整都必须拒绝。

## 7. 隔离恢复验证

### 7.1 目标保护

恢复数据库名必须由程序生成：

```text
tg_hub_restore_verify_<UTC timestamp>_<random suffix>
```

创建、连接、验证和 DROP 前每次都必须重新确认：

- 名称严格匹配固定正则和前缀；
- 绝不等于 production database name；
- 当前角色拥有该数据库；
- 数据库为本轮创建且 identity token 匹配；
- 当前连接确实指向隔离数据库。

任何 guard 失败立即返回 `RESTORE_TARGET_UNSAFE`。禁止接受用户自由输入数据库名，
禁止根据 manifest 选择目标库。

identity token、opaque ID 和 target name 必须在建库前随机生成，不来源于 manifest，
不包含用户名、路径、PID 或数据库凭据。恢复事务先建立 recovery record，再创建
数据库：

```text
generate target name + identity token + opaque_id
-> atomic write recovery record phase=planned
-> create database
-> COMMENT identity token
-> atomic update recovery record phase=identity_committed
-> begin pg_restore
```

recovery record 最小 schema 为：

```text
schema_version
opaque_id
generated_target_name
identity_token
created_at
backup_id
phase: planned | identity_committed | restore_started |
       restore_failed | verification_failed
```

planned record 写失败时不得创建数据库；数据库创建失败后删除 planned record。
COMMENT 失败时不得进入 restore，应尝试受控 DROP；DROP 成功才删除 record，DROP
失败则保留 planned record。identity comment 成功但 `identity_committed` 原子更新
失败时同样不得进入 restore，应尝试 guarded DROP，并且只有确认 DROP 成功后才可
删除 record。

创建数据库后立即执行：

```sql
COMMENT ON DATABASE <safely-quoted-generated-name>
IS <bound-or-driver-literal-identity-comment>;
```

所有数据库标识符必须先通过严格正则，再使用数据库驱动提供的 Identifier/quote API；
comment 值必须使用 bind parameter 或驱动 Literal API。`CREATE DATABASE`、
`COMMENT ON DATABASE`、`DROP DATABASE` 及 owner/comment 查询均禁止普通字符串插值
或 shell 拼 SQL，token 不得进入日志和错误输出。

每次危险操作前必须从 maintenance database 重新读取数据库名称、owner 和 comment，
并确认 comment token 与本轮 token 一致；连接目标后还必须确认
`current_database()`。DROP 前重复全部 guard。

创建成功但 COMMENT 提交失败时不得进入 `pg_restore`。只有本进程刚创建目标、名称
和 owner 匹配、仍处于当前受控创建流程且目标尚未交给后续步骤时，才允许立即清理；
否则返回 `RESTORE_TARGET_IDENTITY_UNCOMMITTED` 并保留目标供人工处理。

recovery record 固定原子写入：

```text
~/.tg-hub/runtime/restore-recovery/<opaque-id>.json
```

文件权限为 `0600`，不得包含 DSN、host、密码、路径或异常原文。正式报告/API 不
输出 target name 和 token。

cleanup 按 phase fail closed：`identity_committed` 必须验证 comment token 后才可
DROP；`planned` 在数据库不存在时可安全删除 record，数据库存在但 comment 缺失或
不匹配时不得自动 DROP。

### 7.2 恢复顺序

```text
validate backup package
-> validate pg_restore/server compatibility
-> connect maintenance database
-> create empty isolated database
-> pg_restore --no-owner --no-acl --exit-on-error
-> connect isolated database
-> verify Alembic revision equals manifest revision
-> verify required tables and constraints
-> run integrity smoke queries in read-only transactions
-> write desensitized verification report
-> success: guarded DROP isolated database
-> failure: retain database and print guarded project cleanup command
```

恢复前不运行 Alembic upgrade。只有 dump 原样恢复后 revision 与 manifest 一致，
才能证明该备份可恢复。当前代码 head 与 manifest revision 不同时报告
`RESTORE_CODE_REVISION_MISMATCH`，不得静默迁移后宣称原备份通过。

失败后的清理入口固定为：

```text
python -m app.deploy.restore_verify cleanup --recovery-record <opaque-id>
```

禁止输出裸 `dropdb`。cleanup 必须重新验证固定名称正则、非生产数据库、owner、
comment identity token、maintenance connection 和无活动 restore owner；任一失败均
禁止 DROP。成功 DROP 后删除 recovery record，失败则保留。

基础验证至少包括：

- `alembic_version` 恰好一个 revision；
- `channels`、`raw_messages`、`works`、`resources`、`resource_links`、
  `resource_sources` 存在；
- 已锁定 unique/FK 约束存在；
- `raw_messages.channel_id` 的 FK 为 `ON DELETE RESTRICT`；
- 所有核心表可执行 `SELECT COUNT(*)`；
- PostgreSQL constraint validation 无失败；
- 应用 repository 只读 smoke query 可执行且不 flush/commit 业务写入。

报告只输出 pass/fail、稳定错误码、表/约束检查计数和总耗时，不输出业务行数、
标题、消息、频道或链接内容。

### 7.3 子进程与取消所有权

`pg_dump`、`pg_restore --list` 和 `pg_restore` 各自由唯一 subprocess owner 管理。
收到 `CancelledError`、TERM 或受控终止时固定执行：

```text
request child terminate
-> bounded wait
-> kill when required
-> wait/reap child
-> close pipes/fds
-> settle temp package or restore target/recovery record
-> release backup root lock last
-> propagate cancellation or stable termination result
```

禁止先释放 `.backup.lock` 再让子进程后台运行。kill/reap 失败返回
`BACKUP_SUBPROCESS_CLEANUP_FAILED` 或 `RESTORE_SUBPROCESS_CLEANUP_FAILED`，不得继续
retention，也不得宣称同类操作可立即安全开始。

## 8. 生产恢复 Runbook 边界

Deploy-4 第一版不提供一键生产恢复命令。生产恢复必须是独立、逐 Gate 授权的
灾难恢复流程：

```text
confirm incident and selected backup
-> verify backup in isolated database
-> separately supply production.env and authorized Telethon session
-> stop main service and confirm session lock free
-> create protection backup of current production database when possible
-> create a new replacement database
-> restore into replacement, never overwrite in place
-> verify schema/readiness with Monitor disabled
-> explicitly switch DATABASE_URL
-> start application only
-> static + online preflight
-> operator explicitly starts Monitor
```

该 Runbook 只锁定安全方向，不授权实现或执行。不得在原生产数据库上使用
`pg_restore --clean`，不得自动删除旧生产数据库，也不得自动启动 Monitor。

## 9. Retention 与磁盘预算

建议策略：最近 7 个 daily slot + 最近 4 个 weekly slot，升级前备份可额外 pin。
第一版 retention 必须基于 manifest `created_at_utc` 和 `backup_id`，不得使用 mtime。
retention 必须持有 `.backup.lock` exclusive；validation/restore verify 持有 shared
lock 时，retention 必须稳定返回锁竞争结果，不得删除任何 package。

清理只允许删除：

- schema 可读；
- checksum 已验证；
- `backup_status=complete`；
- 不处于 restore verify 使用中；
- 未 pinned；
- 明确被 retention 选中的 final package。

禁止删除当前 temp、lock、校验失败包或唯一可用备份。无法达到预算时返回
`BACKUP_BUDGET_EXCEEDED_UNRECOVERABLE`，不扩大删除范围。具体默认预算在实现
评审中根据真实数据库大小锁定，本设计不拍定数值。

## 10. 稳定结果与错误码

备份结果：

```text
status: pass | fail
backup_id: present when final package directory exists, otherwise null
package_state: not_created | temp_only | final_committed | commit_uncertain
manifest_valid: yes | no
database_dump_valid: yes | no
watchlist_snapshot_valid: yes | no
backup_bytes: integer | null
error_code: stable code | null
report_desensitized: yes
```

固定映射：正常成功为 `status=pass`、`package_state=final_committed` 并返回
`backup_id`；final rename 成功但 root fsync 失败为 `status=fail`、
`package_state=commit_uncertain`、返回 `backup_id` 和
`BACKUP_COMMIT_UNCERTAIN`；final rename 前失败为 `temp_only` 或 `not_created`，且
`backup_id=null`。temp 路径不得向外输出。

uncertain final package 的后续校验入口固定为：

```text
python -m app.deploy.backup_verify --backup-id <backup_id>
```

只接受通过 schema 校验的 `backup_id`，不得接受任意 package path。

恢复验证结果：

```text
status: pass | fail
backup_id: desensitized stable id
target_created: yes | no
restore_completed: yes | no
schema_verified: yes | no
integrity_verified: yes | no
target_dropped: yes | no
cleanup_required: yes | no
error_code: stable code | null
report_desensitized: yes
```

最小稳定错误码：

```text
BACKUP_IN_PROGRESS
BACKUP_PATH_INVALID
BACKUP_SPACE_INSUFFICIENT
BACKUP_TOOL_MISSING
PG_DUMP_VERSION_UNSUPPORTED
DATABASE_UNAVAILABLE
MIGRATION_NOT_AT_HEAD
GIT_WORKTREE_DIRTY
WATCHLIST_UNREADABLE
WATCHLIST_CHANGED_DURING_BACKUP
DATABASE_DUMP_FAILED
DATABASE_DUMP_INVALID
BACKUP_MANIFEST_INVALID
BACKUP_CHECKSUM_MISMATCH
BACKUP_COMMIT_UNCERTAIN
BACKUP_CLEANUP_REQUIRED
BACKUP_SUBPROCESS_CLEANUP_FAILED
RESTORE_TARGET_UNSAFE
RESTORE_TARGET_IDENTITY_UNCOMMITTED
RESTORE_RECOVERY_RECORD_WRITE_FAILED
RESTORE_RECOVERY_RECORD_INVALID
RESTORE_TOOL_MISSING
PG_RESTORE_VERSION_UNSUPPORTED
RESTORE_CREATE_FAILED
RESTORE_FAILED
RESTORE_CODE_REVISION_MISMATCH
RESTORE_SCHEMA_INVALID
RESTORE_INTEGRITY_FAILED
RESTORE_DROP_FAILED
RESTORE_SUBPROCESS_CLEANUP_FAILED
```

exception text、stderr 原文和 DSN 不进入 API、CLI、manifest 或正式日志。

## 11. 模块与文件边界

建议实现结构：

```text
backend/app/deploy/
├── backup_models.py
├── backup_fs.py
├── backup_service.py
├── backup_verify.py
└── restore_verify.py

backend/deploy/
├── backup.sh
└── restore_verify.sh

backend/tests/deploy/
├── test_backup_models.py
├── test_backup_service.py
├── test_backup_verify.py
└── test_restore_verify.py
```

Python 层拥有 settings 解析、路径安全、manifest、subprocess 参数、状态映射和
恢复 target guard。shell wrapper 只定位绝对 Python、工作目录和 env file，使用
`exec` 调用 Python；不得 source/eval env，不得解析 DATABASE_URL。

Admin 页面、API、LaunchAgent scheduler 和远端上传不属于 Deploy-4 第一版。

## 12. 阶段拆分

```text
P6-Deploy-4A
  internal/external commit contract
  backup root shared/exclusive lock protocol
  PostgreSQL same-major version matrix
  restore target identity/recovery record DTO
  stable error codes + unit tests

P6-Deploy-4B
  production backup creation + package verification

P6-Deploy-4C
  isolated restore verification + target guards

P6-Deploy-4D
  retention + real backup/restore acceptance + runbook archive
```

授权必须逐段进行。4A 通过前不得进入 4B；4B 未产生有效 package 前不得进入
4C；4C 未通过隔离恢复前不得进入 4D。任何阶段都不授权生产恢复。

## 13. 测试与验收矩阵

至少覆盖：

1. private path、symlink、mode 和 traversal 拒绝；
2. backup lock 非阻塞竞争；
3. backup_id 唯一且不可注入路径；
4. manifest unknown version、重复 key、unknown payload 拒绝；
5. payload checksum/size mismatch；
6. dump 失败不提交 manifest/final package；
7. watchlist 并发变化稳定失败；
8. dirty worktree、migration 非 head、工具缺失和版本不兼容；
9. DB URL/secret/session path 不进入参数输出、日志或 manifest；
10. valid custom dump catalog 校验；
11. restore target prefix、production-name、owner、identity 四重 guard；
12. restore 不使用 `--clean`/`--create`；
13. restore 后 Alembic revision 与 manifest 精确匹配；
14. RawMessage FK `ON DELETE RESTRICT` 等核心约束存在；
15. restore success 后 guarded DROP；
16. restore failure 默认保留 target 并给出清理指令；
17. cleanup guard 失败不 DROP；
18. CancelledError/TERM 后子进程收口且 lock 不提前释放；
19. retention 不删除 temp、invalid、pinned、in-use 或唯一有效包；
20. 完整非数据库回归；
21. 临时 PostgreSQL database 的真实 dump/restore 集成测试；
22. 当前生产数据库真实备份需单独授权；
23. 真实备份到隔离数据库恢复需再次单独授权。
24. temp package 已有 complete manifest 但未 final rename：不得识别、恢复或计入
    retention slot；
25. final rename 成功、backup root fsync 失败：返回
    `BACKUP_COMMIT_UNCERTAIN`，保留 final package，后续可完整校验分类；
26. restore target 名称匹配但 comment token 或 owner 不匹配时禁止 DROP，production
    name 永远禁止；
27. restore verify 持 shared backup lock 时 retention 无法取得 exclusive lock，
    package 不被删除；
28. source/pg_dump/pg_restore/target major 任一不符合矩阵即稳定拒绝；
29. pg_dump/pg_restore 取消后子进程被 terminate/kill/reap，lock 不提前释放且不留
    后台进程；
30. failed restore recovery record cleanup 重新执行全部 target guards，token 不匹配
    时禁止 DROP。
31. recovery record planned phase：record 写失败时不创建数据库，create 失败后清理
    planned record；
32. COMMENT 成功但 identity_committed record 更新失败：不执行 pg_restore，guarded
    DROP 成功才删除 record，DROP 失败则保留；
33. planned recovery record 遗留：数据库不存在时可清理 record，数据库存在但
    comment 不匹配时禁止 DROP；
34. `BACKUP_COMMIT_UNCERTAIN` 返回 final package `backup_id` 和
    `package_state=commit_uncertain`，可按 backup_id 完整验证且不暴露绝对路径；
35. CREATE/COMMENT/DROP identifier quoting：恶意或异常名称无法进入 SQL，token 不
    进入日志或异常结果。

## 14. 完成标准

P6-Deploy-4 完成必须同时满足：

- 备份 package/manifest 契约实现并通过安全测试；
- 当前生产数据完成一次用户授权的真实备份；
- 该 package checksum 和 dump catalog 校验通过；
- 同一 package 在严格隔离数据库中成功恢复；
- Alembic revision、核心表与约束验证通过；
- 成功后隔离数据库被 guarded DROP；
- 整个过程未停止 Monitor、未修改生产 DB/watchlist、未访问 Telegram；
- `production.env` 与 Telethon session 未进入 backup；
- 生产恢复 Runbook 已归档，但未执行生产恢复；
- 完整回归通过；
- 脱敏验收报告完成。

P6-Deploy-4 通过后只能得出：

```text
database + watchlist backup
-> package verification
-> isolated restore verification
```

不能得出：

```text
credentials/session are backed up
production restore was executed
cross-machine disaster recovery is proven
zero data loss is guaranteed
```

## 15. 当前设计结论

```text
P6-DEPLOY-4_DESIGN_REVIEW:
  result: approved
  architecture_direction: aligned
  previous_blockers_closed: 4
  previous_clarifications_closed: 3
  blockers: 0
  required_clarifications: 0
  allow_design_lock: yes
  allow_P6_Deploy_4A: yes
  allow_P6_Deploy_4B: no
  allow_P6_Deploy_4C: no
  allow_P6_Deploy_4D: no
  allow_real_backup: no
  allow_restore_verify: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

总设计已完成独立复审并锁定。下一步只允许进入 P6-Deploy-4A：实现 manifest、
内部/外部提交、backup root lock、同 major 版本矩阵、restore target identity、
recovery record DTO、稳定错误码及单元测试。4A 不得执行生产 `pg_dump`、创建真实
restore database、执行 retention 删除、真实备份、隔离恢复或任何生产恢复。
