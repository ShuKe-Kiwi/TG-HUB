# P6-Deploy-4B 生产备份创建实施评审

> 项目：tg-hub  
> 阶段：P6-Deploy-4B  
> 状态：implementation-complete  
> 前置：P6-Deploy-4A 已完成并提交（`de26812`）  
> BLOCKERS：0  
> REQUIRED_CLARIFICATIONS：1  
> ALLOW_IMPLEMENTATION：yes  
> ALLOW_REAL_BACKUP：no  
> ALLOW_RESTORE_VERIFY：no  
> ALLOW_PRODUCTION_RESTORE：no

## 1. 阶段目标

P6-Deploy-4B 实现并验证以下代码链路：

```text
Settings
-> static backup preflight
-> exclusive backup-root lock
-> bounded pg_dump subprocess
-> custom dump catalog validation
-> consistent watchlist snapshot
-> payload checksum + manifest
-> final directory commit
-> shared-lock package validation
-> desensitized result
```

4B 代码完成不等于已执行真实生产备份。实现、fake/integration 测试和真实备份必须
分别授权。

## 2. 固定范围

4B 允许实现：

- `backup_service.py`：备份编排和唯一 subprocess owner；
- `backup_verify.py`：final package 只读验证；
- `backup_fs.py`：temp/final package、safe-open、exactly-N、hash、fsync、atomic rename；
- `backup_models.py`：仅补充 4B 必需且不改变 4A 语义的结果/check DTO；
- `python -m app.deploy.backup_service --preflight`；
- `python -m app.deploy.backup_service create`，但真实 production 执行另设 Gate；
- `python -m app.deploy.backup_verify --backup-id <id>`；
- fake pg tools、临时目录和受控临时 PostgreSQL 的测试装配。

4B 禁止：

- 隔离 restore database、`pg_restore` 数据恢复或 guarded DROP；
- retention 删除；
- backup scheduler/LaunchAgent；
- Admin 页面/API；
- 自动停止 Monitor 或主 LaunchAgent；
- 生产恢复；
- 备份 `production.env`、Telethon session 或日志；
- 上传远端；
- 未单独授权时读取生产数据库或写入真实 `BACKUP_DIR`。

## 3. 入口与结果

### 3.1 Static preflight

```text
python -m app.deploy.backup_service --preflight
```

只允许读取配置和文件元数据，不连接数据库、不启动 PostgreSQL 工具子进程、不创建
lock/package。允许以下固定 argv 的只读 Git 子进程：

```text
git rev-parse HEAD
git status --porcelain=v1 --untracked-files=normal
```

Git 调用必须固定 repository working directory、禁用 shell、限制 stdout/stderr 和
timeout；正式报告只输出 commit/clean 状态，不输出变更文件名或 Git stderr。
报告检查：

- `APP_ENV=production`；
- `BACKUP_DIR` 与 watchlist 位于私有根且路径安全；
- Git worktree clean；
- dependency lock 存在且可计算 SHA-256；
- 配置 schema 支持；
- 命令入口装配完整。

### 3.2 Create

```text
python -m app.deploy.backup_service create
```

默认是可能读取生产数据库和写入真实备份目录的外部动作，因此实现完成后仍需单独
批准真实执行。CLI 只输出 `BackupRunResult` 的脱敏 JSON。

### 3.3 Verify

```text
python -m app.deploy.backup_verify --backup-id <backup_id>
```

只接受 canonical `backup_id`，禁止任意 path。只验证 final package，不执行恢复。

## 4. 数据库连接与工具契约

### 4.1 URL 解析

`Settings.DATABASE_URL` 当前为 SQLAlchemy async URL，例如：

```text
postgresql+asyncpg://localhost/tg_hub
```

不得把该字符串直接传给 `pg_dump`，也不得用字符串替换 driver 名。固定使用
SQLAlchemy `make_url()` 解析为结构化字段，并只允许 PostgreSQL driver。

程序必须从空字典构建受控 libpq 环境，禁止 `os.environ.copy()`：

- 明确批准的最小 process runtime、locale 和 `PATH`；
- `PGHOST`、`PGUSER`、`PGDATABASE`；
- URL 显式提供 port 时才设置 `PGPORT`；
- 如 URL 含密码，仅在子进程私有 env 中设置 `PGPASSWORD`；
- 所有继承环境中以 `PG` 开头的 key 一律不继承，再显式添加上述批准字段；
- 不把 URL、密码或完整 env 放入 argv、日志、结果或异常文本。

第一版固定要求 database name、host 和 user 均显式存在；不使用操作系统用户、默认
database、默认 host 或隐式 Unix socket。port 可缺省，缺省时不设置 `PGPORT`，由
libpq 使用标准端口。密码缺失时不设置 `PGPASSWORD`，空字符串密码视为显式空值并
设置。IPv4、IPv6 和 percent-decoded user/password 只取自 `make_url()` 的结构化
字段；Unix socket host 和全部 URL query 参数第一版稳定拒绝。发现缺失、未知或冲突
字段时返回 `DATABASE_URL_UNSUPPORTED`。

### 4.2 版本矩阵

连接数据库只查询受控标量：server version、Alembic revision 和备份所需空间估计。
`pg_dump --version`、`pg_restore --version` 的解析必须有固定正则和 locale-independent
测试。

4B create 固定：

```text
source_server_major == pg_dump_major
```

4B package verify 固定：

```text
manifest.pg_dump_major == local pg_restore_major
```

完整 source/dump/restore/target 同 major 在 4C 再验证。

## 5. 前置检查与空间策略

持 exclusive lock 后按顺序执行：

```text
validate backup root/private modes
-> validate pg_dump + pg_restore tools
-> open bounded snapshot-owner database connection
-> BEGIN REPEATABLE READ READ ONLY
-> source server version
-> Alembic revision == code head
-> pg_database_size current database
-> SELECT pg_export_snapshot()
-> validate watchlist path/schema
-> require clean Git worktree
-> calculate required free space
```

空间下限固定采用保守公式：

```text
required_free_bytes = max(database_size_bytes * 2, database_size_bytes + 256 MiB)
```

第一版 custom dump 通常压缩，但不得依赖压缩率。空间不足在创建 temp package 前返回
`BACKUP_SPACE_INSUFFICIENT`。数据库 size 仅用于内部判断，不进入正式报告。

Alembic revision 与 pg_dump 内容必须绑定到同一个 exported MVCC snapshot。source
server version 和 `pg_database_size()` 在同一受控 snapshot-owner transaction 生命周期
内读取：server version 用于工具版本判断，database size 只作为 advisory free-space
estimate；不声明二者与 dump 内容具有 MVCC 一致性，运行期间仍可能发生空间变化和
ENOSPC。snapshot ID 只允许进入 `pg_dump --snapshot` 的私有 argv，不进入日志、结果
或 manifest。snapshot-owner transaction 和连接必须保持到 pg_dump 完成并完全收口后
才 COMMIT/ROLLBACK；取消时先 terminate/kill/reap pg_dump，再结束 transaction，最后
释放 exclusive backup lock。

snapshot export 或 transaction 生命周期失败返回
`DATABASE_SNAPSHOT_EXPORT_FAILED`。若 snapshot 外的一致性 guard 发现 schema revision
变化，返回 `DATABASE_SCHEMA_CHANGED_DURING_BACKUP`，不得提交 package。

## 6. pg_dump 子进程

固定 argv：

```text
pg_dump
--format=custom
--no-owner
--no-acl
--snapshot=<private exported snapshot id>
--file=<private temp database.dump.tmp>
```

数据库连接只由 libpq env 提供。禁止 shell、`shell=True`、URL argv、`--dbname`、
`--clean`、`--create` 和任意用户附加参数。

统一抽象 `PgToolRunner`，拥有 `pg_dump`、`pg_restore --list` 和 version query 的受控
执行。每次调用仍只有一个明确 owner，并负责：

```text
spawn
-> bounded communicate/wait
-> success or terminate
-> bounded wait
-> kill if required
-> wait/reap
-> close pipes
-> sanitize stderr to stable error code
-> settle temp package
-> release backup lock last
```

stdout/stderr 必须有界捕获；不得把 stderr 原文写入日志/API。取消最终继续传播。
create 中的 catalog runner 必须完全退出并 close/reap 后才可继续提交；validator 中的
catalog runner 必须完全退出并 close/reap 后才可释放 shared backup lock。

## 7. Dump catalog 校验

`pg_dump` 成功不等于 payload 有效。提交 `database.dump` 前必须执行：

```text
pg_restore --list <temp dump>
```

要求：

- exit code 0；
- dump 为 regular file、非 symlink、size > 0；
- catalog 输出有界；
- 必须存在以下固定 `public` schema catalog 对象：
  `alembic_version`、`channels`、`raw_messages`、`works`、`resources`、
  `resource_links`、`resource_sources` 的 TABLE；
- 必须存在上述业务核心表适用的 TABLE DATA 条目；
- 不解析业务数据；
- 不把 catalog 原文输出到报告。

4B package verify 必须再次执行同样 catalog validation，并输出稳定字段
`required_catalog_objects_present=yes|no`。任一核心对象缺失均返回
`DATABASE_DUMP_INVALID`；任意其他数据库即使 custom catalog 可读也不得通过。4B 不
验证 FK/unique 的数据库语义，这些属于 4C restore 后检查。

## 8. Watchlist snapshot

不得调用会再次按路径打开文件的普通 helper 拼接快照。固定使用同一个 fd：

```text
lstat parent and path
-> O_RDONLY | O_NOFOLLOW
-> first fstat device/inode/type/size N
-> exactly-N read with upper bound
-> second fstat device/inode/type/size unchanged
-> parse WatchlistConfig
-> compute canonical revision using existing watchlist revision contract
-> write exact source bytes to private temp file
-> fsync
```

读取上限第一版为 `1 MiB`。超过上限返回 `WATCHLIST_UNREADABLE`。snapshot 保留原始
合法 JSON bytes，不重新序列化，以便 checksum 对应真实运行配置。

## 9. Package 提交与失败恢复

严格顺序沿用总设计：

```text
private temp package
-> database.dump.tmp fsync + catalog validate
-> database.dump rename + package fsync
-> watchlist.json.tmp fsync + rename + package fsync
-> payload hashes
-> manifest.tmp fsync + rename + package fsync
-> temp package rename to final backup_id
-> backup root fsync
```

外部提交点仅为 final directory rename。

- rename 前失败：清理当前 owner 创建的 temp；
- 清理失败：保留 temp，`BACKUP_CLEANUP_REQUIRED`；
- final rename 后 root fsync 失败：保留 final，返回 `commit_uncertain` 和
  `backup_id`；
- 不扫描或删除其他历史 temp/final package；
- create 不执行 retention。

## 10. Package validator

validator 必须持 shared backup lock。manifest/watchlist 在各自上限内 exactly-N
完整读取；`database.dump` 禁止整文件载入内存，固定使用流式 hash：

```text
open dump fd
-> first fstat N/device/inode/type
-> stream exactly N bytes in bounded chunks
-> SHA-256 + byte count
-> second fstat device/inode/type/size unchanged
```

提前 EOF、读取后增长、inode/type/size 变化均返回
`BACKUP_PACKAGE_CHANGED_DURING_VERIFY`。随后固定验证：

- package 位于 final root，basename 等于 backup ID；
- directory/file 均非 symlink，权限私有；
- 文件集合恰好为 `database.dump`、`watchlist.json`、`manifest.json`；
- manifest schema/backup status/exclusions；
- payload size 和 SHA-256；
- watchlist schema/revision；
- dump catalog；
- 本机 `pg_restore_major == manifest.pg_dump_major`。

validator 不修改 package，不写 package 内状态，不修复权限，不删除异常文件。
`pg_restore --list` 也通过 `PgToolRunner` 执行；子进程完全收口后才释放 shared lock。

## 11. Manifest 来源

字段来源必须唯一：

- `backup_id`/created time：本轮进程生成；
- Git commit/clean：受控 Git 命令或库接口；
- dependency hash：仓库 `backend/uv.lock` safe-open 计算；
- Alembic revision：生产 DB 受控查询；
- config schema：Settings；
- DB/tool versions：受控标量与固定 version parser；
- watchlist revision：现有 watchlist canonical revision；
- payload hash/size：已提交 temp payload 的 fd/stat。

manifest 写入前必须重新确认本轮数据库 dump、watchlist snapshot 和 metadata 都属于
同一个 `BackupAttempt`，禁止从目录扫描猜测 payload。

## 12. 4B 新增稳定错误码

```text
DATABASE_URL_UNSUPPORTED
DATABASE_SNAPSHOT_EXPORT_FAILED
DATABASE_SCHEMA_CHANGED_DURING_BACKUP
BACKUP_PACKAGE_CHANGED_DURING_VERIFY
PG_TOOL_TIMEOUT
PG_TOOL_OUTPUT_LIMIT_EXCEEDED
```

ENOSPC 无论发生在 temp dump、snapshot、manifest 或 fsync 阶段，都必须映射为
`BACKUP_SPACE_INSUFFICIENT` 或更具体的既有稳定写入错误；不得声称前置空间估计能
保证运行期间一定充足。

## 13. 实现拆分

```text
backend/app/deploy/backup_models.py
  4B check/result DTO additions only

backend/app/deploy/backup_fs.py
  private directories, safe read/write, hash, fsync, atomic package commit

backend/app/deploy/backup_service.py
  preflight, DB metadata, pg_dump owner, watchlist snapshot, manifest assembly

backend/app/deploy/backup_verify.py
  final package read-only validation and CLI

backend/tests/deploy/test_backup_service.py
backend/tests/deploy/test_backup_verify.py
```

不新增 shell wrapper，除非实现评审证明 Python module 入口无法满足本机运维需求。

## 14. 测试门槛

至少覆盖：

1. async SQLAlchemy URL 结构化转换，不泄露密码；
2. 非 PostgreSQL、未知 query 和缺失 database 拒绝；
3. pg_dump/pg_restore version parser 与同 major 拒绝；
4. preflight 不连接 DB、不启动 PostgreSQL 工具子进程、不创建目录，只允许固定只读
   Git 调用；
5. exclusive lock 竞争稳定失败；
6. 空间公式和不足拒绝；
7. argv 不含 URL/password/shell/clean/create；
8. subprocess success/fail/timeout/cancel/terminate/kill/reap；
9. stderr 和 env 不进入结果/log；
10. dump 空文件、symlink、catalog failure/oversize 拒绝；
11. watchlist exactly-N、inode/size/type 变化、1 MiB 上限；
12. snapshot 保留合法原始 bytes 且 revision 使用现有算法；
13. manifest 字段来源和 payload hash/size；
14. temp complete manifest 仍不被 validator 接受；
15. final package unknown/missing/extra file 拒绝；
16. checksum、size、watchlist revision mismatch；
17. validator shared lock 阻止 exclusive create/retention；
18. rename 前失败只清理本轮 temp；
19. final rename 后 root fsync 失败返回 uncertain ID；
20. cleanup failure 保留 temp 且不当作有效包；
21. package 内无 env/session/log/runtime material；
22. fake tool 端到端 create + verify；
23. 完整回归；
24. 真实生产备份必须另行授权。
25. exported snapshot 下 revision 与 pg_dump 使用同一 snapshot，owner transaction
    在 pg_dump 完成前不结束，取消时先收口 pg_dump 再结束 transaction/lock；
26. 父进程 seeded `PGSERVICE`、`PGPASSFILE`、`PGOPTIONS` 等不被继承，实际连接字段
    只来自 allowlist，argv/env 输出不泄漏密码；
27. 任一 required tg-hub table 或 `alembic_version` 缺失时 dump invalid，其他数据库
    的可读 custom dump 不通过；
28. `pg_restore --list` timeout/cancel 后 terminate/kill/reap，再释放 shared/exclusive
    lock，不留下后台进程；
29. 大 dump 流式 hash，不整文件载入内存，短读、增长、inode/type 变化稳定拒绝；
30. static preflight 仅允许固定只读 Git 调用，不启动 pg_dump/pg_restore、不连接 DB、
    不创建 backup lock/package。

## 15. 验收与授权

4B 实现通过只能证明：

```text
controlled backup inputs
-> deterministic package creation code
-> final package validation code
```

不能证明：

```text
production data was backed up
the package can be restored
retention is safe
production recovery is possible
```

```text
P6-DEPLOY-4B_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 1
  allow_P6_Deploy_4B_implementation: yes
  allow_real_backup: no
  allow_P6_Deploy_4C: no
  allow_restore_verify: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

独立复审已批准 4B 代码实现。唯一非阻塞澄清已修正文档；实现仍不得执行真实生产
备份、写入真实 `~/.tg-hub/backups`、恢复数据库或进入 4C。

## 16. 实施结果

```text
P6-DEPLOY-4B_IMPLEMENTATION:
  status: complete
  production_database_accessed: no
  real_backup_created: no
  restore_database_created: no
  retention_executed: no
  fake_contract_tests: pass
  full_regression: 588 passed
  allow_real_backup: no
  allow_P6_Deploy_4C: no
```

已实现 `BackupService`、`PgToolRunner`、exported snapshot owner、allowlist libpq env、
watchlist snapshot、manifest/final package commit 和只读 validator。当前只以 fake pg
tools、临时目录和既有非生产测试环境验证代码边界；尚未对当前生产数据库执行
`pg_dump`，也未在真实 `BACKUP_DIR` 生成 package。
