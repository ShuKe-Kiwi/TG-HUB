# P6-Deploy-3C：轮转引擎实施评审

> STATUS：implementation-review-approved
> BLOCKERS：0
> ALLOW_P6_DEPLOY_3C_IMPLEMENTATION：yes
> ALLOW_P6_DEPLOY_3D_IMPLEMENTATION：no

## 1. 评审结论

P6-Deploy-3C 可以进入实现，但范围严格限定为本机一次性轮转引擎：

```text
fixed targets
-> rotate.lock
-> threshold / generation age decision
-> application copy-truncate OR heartbeat locked rename
-> durable pending archive
-> gzip final archive
-> retention / archive budget
-> atomic rotation-status.json
```

本阶段不安装或注册 rotation LaunchAgent，不修改主 LaunchAgent，不做在线 Telegram session preflight，也不在管理台增加轮转页面。

## 2. 当前实现基线

当前仓库尚无 rotation engine。已有可复用事实只有：

- `app.stdout.log`、`app.stderr.log` 由 launchd 长期持有 fd；
- `heartbeat.jsonl` 使用 short-open，并与 writer 共用 `heartbeat.lock`；
- runtime、日志和 heartbeat 路径已受 production private-root contract 约束；
- heartbeat active/lock 权限已经真实验收为 `0600`；
- P6-Deploy-3A 迁移窗口可能使当前真实 active log 含历史非 JSON 行。

3C 不修改 `JsonlHeartbeatSink` 的锁协议和写入 DTO。

## 3. 固定目录与目标

所有路径从 `Settings` 推导，禁止接受任意 target path：

```text
application active:
  Settings.LOG_DIR/app.stdout.log
  Settings.LOG_DIR/app.stderr.log

heartbeat active:
  Settings.HEARTBEAT_PATH

archive root:
  Settings.LOG_DIR/archive/

rotation state:
  ~/.tg-hub/runtime/rotation-status.json

locks:
  ~/.tg-hub/runtime/rotate.lock
  Settings.HEARTBEAT_PATH.parent/heartbeat.lock
```

`archive/` 和 runtime 目录必须是非 symlink directory，标准安装权限 `0700`，运行时要求 `mode & 0o077 == 0`。active、archive、pending、temp、status 和 lock 文件权限均收口为 `0600`。

production 下所有解析后路径必须位于 `~/.tg-hub/`。3C 不扩大为通用路径轮转工具。

## 4. 文件命名

最终 archive 名称固定为：

```text
<target>.<UTC timestamp>.<run_id>.jsonl.gz
```

其中 `<target>` 只能为：

```text
app.stdout.log
app.stderr.log
heartbeat.jsonl
```

`run_id` 为随机不可逆标识。名称不得包含用户名、PID、绝对路径或配置值。

中间文件分两类：

```text
*.tmp             可丢弃的未提交临时文件
*.pending         耐久 snapshot；是否已完成不可逆切换由 phase metadata 判定
*.pending.meta    应用日志 pending 的耐久事务阶段元数据
*.tmp.gz          gzip 尚未原子提交的临时压缩文件
```

`.pending` 和 `.pending.meta` 不得按普通 temp 删除。heartbeat pending 与应用日志 pending 使用不同恢复语义，不得仅凭扩展名推断 active 已被清空。

## 5. 锁协议

每次执行，包括 `--dry-run`，均先安全打开并尝试获取 `rotate.lock`：

```text
O_CREAT | O_RDWR | O_NOFOLLOW
-> fstat regular file
-> fchmod 0600
-> flock LOCK_EX | LOCK_NB
```

获取失败返回 `ROTATION_ALREADY_RUNNING`。lock 文件存在不表示任务运行，不读取 PID 决定锁状态，不 kill 其他进程。

固定锁顺序：

```text
rotator: rotate.lock -> heartbeat.lock
writer: heartbeat.lock only
```

任何路径都不得反向获取。应用日志 copy-truncate 只持有 `rotate.lock`，不声称锁住 launchd writer。

## 6. Dry-run

`python -m app.deploy.rotate_logs --dry-run` 必须：

- 获取 `rotate.lock`，保证计划基于单一轮转观察窗口；
- 只读取安全元数据和现有 rotation state；
- 输出脱敏 target key、触发原因、size、age、预计 retention/budget 动作；
- 不创建目录、archive、temp、pending 或 status；
- 不刷新 `last_checked_at`、generation time 或 `last_rotated_at`；
- 不 truncate、rename、gzip、chmod active 或清理文件。

dry-run 在缺失必要目录时返回稳定 blocker，不为通过检查而修改环境。

## 7. Generation age

每个 target 独立保存：

```text
active_generation_started_at
last_rotated_at
last_checked_at
last_size_bytes
```

状态缺失时，优先使用 birthtime，无法获得时使用 ctime，并在非 dry-run 的首次成功状态提交中固化。不得使用持续变化的 mtime 计算 generation age。

触发条件固定为：

```text
size >= 10 MiB
OR
(size > 0 AND generation_age >= 24h)
```

空文件不按年龄轮转。not-modified、失败和 dry-run 均不得重置 generation time 或 `last_rotated_at`。`last_checked_at` 仅在非 dry-run 成功完成该 target 检查后更新。

## 8. 应用日志 copy-truncate

`app.stdout.log` 和 `app.stderr.log` 固定流程：

```text
safe open active
-> fstat regular file, capture inode and snapshot_size=N
-> copy exactly byte range [0, N) to unique *.tmp
-> retain bytes only through last complete newline
-> fsync tmp
-> atomic rename tmp -> *.pending
-> fsync archive directory
-> atomic write pending.meta phase=snapshot_committed
-> fstat active immediately before truncate
-> ftruncate active fd to zero
-> fsync active
-> verify active inode unchanged
-> atomic update pending.meta phase=active_truncated
-> gzip pending to *.tmp.gz
-> fsync tmp.gz
-> atomic rename tmp.gz -> final .gz
-> fsync archive directory
-> unlink pending and pending.meta
```

整个单文件流程、压缩、retention、budget 和状态提交均在 `rotate.lock` 内完成。主设计中“释放 rotate lock 后 gzip”的旧表述废止。

状态至少记录：

```text
copied_bytes
truncated_bytes
partial_tail_detected
rotation_consistency: best_effort_copy_truncate
concurrent_write_loss_possible: true
writer_paused: false
```

`truncated_bytes` 取 truncate 前的实际 active size；它不等于可证明归档的字节数。不得从这些字段推断零丢失。

snapshot 边界固定为首次 `fstat().st_size` 得到的 `N`。复制最多读取 `[0, N)`，不得持续追逐 writer 追加后的 EOF；读取不足 `N` 返回 `ROTATION_READ_FAILED`。writer 在复制期间追加的内容不进入 snapshot，并继续属于已明确接受的 copy-truncate 并发损失窗口。`complete_line_bytes <= snapshot_size`，`partial_tail_detected` 只描述该固定 N 字节快照。

每个应用日志 pending 必须配套 `<archive-base>.pending.meta`，最小字段为：

```text
schema_version
target
run_id
phase: snapshot_committed | active_truncated
active_inode
snapshot_size
complete_line_bytes
created_at
```

metadata 不包含绝对路径或正文。`snapshot_committed` metadata 未完成原子持久化前不得 truncate active；`active_truncated` metadata 未完成原子持久化前不得进入 gzip。事务 metadata 与最终汇总 `rotation-status.json` 职责不同。

## 9. Heartbeat 原子切换

heartbeat 固定流程：

```text
hold rotate.lock
-> acquire heartbeat.lock using 3B safe-open contract
-> safe open and validate active heartbeat
-> atomic rename active -> unique *.pending
-> create new active with O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW
-> fstat regular file and fchmod 0600
-> fsync new active and parent directory
-> release heartbeat.lock
-> keep rotate.lock
-> scan pending and calculate last complete newline offset
-> copy complete prefix to unique *.tmp without modifying pending
-> fsync derived tmp
-> gzip derived tmp to *.tmp.gz
-> fsync tmp.gz and atomically commit final archive
-> fsync archive directory
-> delete pending and derived tmp only after final commit
-> record source_bytes / complete_line_bytes / discarded_partial_tail_bytes
-> retention / budget / status
-> release rotate.lock
```

如果 rename 后创建新 active 失败，必须在仍持有 heartbeat lock 时尝试把 pending 原子恢复为 active，并 fsync parent。若回滚也失败，返回 `ROTATION_HEARTBEAT_RECOVERY_REQUIRED`，保留 pending，不删除唯一数据副本。

heartbeat lock 释放后，旧 pending inode 不得继续增长；后续 writer 只能 short-open 新 active。

禁止就地 truncate 或重写唯一 heartbeat pending。压缩、裁剪或 fsync 失败时，原始 pending 必须保持不变，使下一轮可以重新派生完整行 archive。

## 10. 崩溃恢复

每次非 dry-run 在处理新轮转前，先在 `rotate.lock` 内扫描固定 archive root：

- 删除本轮之前遗留且可证明未提交的 `*.tmp`、`*.tmp.gz`；
- heartbeat 合法 pending 可从原始副本重新派生完整行 temp 并 finalize，原始 pending 不得原地裁剪；
- 应用日志 pending 必须读取并验证同 basename 的 durable phase metadata；
- 应用日志 `phase=active_truncated` 时允许 finalize，不再次 truncate active；
- 应用日志 `phase=snapshot_committed` 时不得 finalize、不得自动 truncate active，返回 `ROTATION_RECOVERY_REQUIRED` 并保留 active、pending 和 metadata；
- 应用日志 metadata 缺失、损坏或字段不匹配时不得猜测阶段，返回 `ROTATION_RECOVERY_REQUIRED`；
- pending finalize 成功并原子提交 final 后才删除 pending；
- pending 无法解析到已批准 target/run id 时不自动删除，返回 `ROTATION_RECOVERY_REQUIRED`；
- recovery 未完成时不得开始同一 target 的新轮转；
- pending 不计入 retention 份数，但计入 `total_observed_bytes`，且不可被 budget 清理。

这一区分保证 active 已清空或切换后，不会因下一轮“清理 temp”删除唯一 archive 副本。

第一版不自动恢复 `snapshot_committed`，因为崩溃后 launchd writer 可能继续向 active 追加，恢复器无法安全区分 snapshot 尾部与新增内容。该状态需要后续受控人工处理，不属于普通自动轮转路径。

## 11. Retention 与预算

最终 `.gz` archive 保留：

```text
app.stdout.log: 7
app.stderr.log: 7
heartbeat.jsonl: 3
```

清理顺序固定为：

```text
disposable temp cleanup
-> pending recovery
-> per-target retention by completed_at oldest first
-> compute all final archive bytes
-> global archive budget cleanup oldest first
```

archive 硬预算为 `250 MiB`，只约束可删除的 final archive，不包含 active 的硬上限承诺。状态同时报告：

```text
archive_bytes
active_bytes
total_observed_bytes
archive_budget_status: within_budget | cleaned | exceeded_unrecoverable
active_oversize: true | false
```

永不删除 active、pending、本轮尚未提交的 archive、lock 或 rotation status。清理完所有允许删除项仍超预算时返回 `exceeded_unrecoverable`，但不得破坏 active。

## 12. Rotation status 所有权

3C 必须实现 `rotation-status.json` 的内部 schema 和原子持久化，因为 generation age、崩溃恢复和预算都依赖它；3D 只负责 LaunchAgent 调度与状态投影，不重新定义 schema。

固定顶层字段：

```text
schema_version
run_id
check_started_at
check_completed_at
status: pass | partial | fail
error_code
rotated_files
cleaned_archives
archive_bytes
active_bytes
total_observed_bytes
archive_budget_status
active_oversize
files
rotation_consistency
concurrent_write_loss_possible
writer_paused
report_desensitized: yes
```

`files` 不是自由结构，固定按三个 target key 保存：

```text
files:
  <target>:
    last_checked_at
    active_generation_started_at
    last_rotated_at
    last_size_bytes
    trigger_reason: size | age | none | recovery
    result: rotated | not_modified | recovered | failed
    error_code
    archive_name
    copied_bytes
    complete_line_bytes
    truncated_bytes
    partial_tail_detected
    discarded_partial_tail_bytes
    legacy_content_possible
```

`archive_name` 只允许 basename。禁止 absolute path、exception text、raw line、pending full path 或任意自由 extra。

顶层状态固定聚合为：

- `pass`：全部 target 检查成功，可包含 `not_modified`；
- `partial`：至少一个 target 成功或 not-modified，且至少一个 target 失败；
- `fail`：全局 guard/lock/path/recovery 失败，或所有 target 均失败。

状态文件不得包含绝对路径、异常原文、日志正文、heartbeat payload、用户名、PID、Telegram 配置或数据库配置。

原子写固定为同目录 unique temp、`O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW`、write-all、`fchmod 0600`、fsync temp、atomic replace、fsync parent。汇总状态提交失败返回 `ROTATION_STATUS_WRITE_FAILED`，不回滚已经安全提交的 final archive。

事务 phase metadata 使用同等安全原子写，但它是不可逆阶段的前置条件：`snapshot_committed` meta 写失败时不得 truncate active；`active_truncated` meta 写失败时不得进入 gzip，并保留 pending/meta 进入人工恢复状态。不得使用最终汇总状态替代 phase metadata。

## 13. 稳定结果与错误码

一次执行返回稳定结果，不把普通轮转失败以 traceback 暴露给 CLI：

```text
ROTATION_ALREADY_RUNNING
ROTATION_PATH_INVALID
ROTATION_PERMISSION_DENIED
ROTATION_ACTIVE_NOT_REGULAR
ROTATION_READ_FAILED
ROTATION_WRITE_FAILED
ROTATION_FSYNC_FAILED
ROTATION_TRUNCATE_FAILED
ROTATION_COMPRESS_FAILED
ROTATION_STATUS_WRITE_FAILED
ROTATION_RECOVERY_REQUIRED
ROTATION_HEARTBEAT_RECOVERY_REQUIRED
ROTATION_BUDGET_EXCEEDED_UNRECOVERABLE
```

未知编程错误可返回 `ROTATION_UNEXPECTED_ERROR`，生产输出仍不得包含异常文本。

## 14. 历史非 JSON 日志

3C engine 按字节和完整换行边界工作，不读取、重写或“补做脱敏”历史日志内容。

单元/集成验收使用 3A 格式的 JSONL fixture，必须证明 final archive 逐行可解析。当前真实 active log 可能含 3A 迁移窗口前后的历史非 JSON 行，因此 3C 不能仅凭真实现存文件宣称“全部 archive JSON 可解析”。

3D 真实轮转前必须单独选择并记录：

- 受控迁移历史 active 后再验收 JSON archive；或
- 将首次 archive 标记为 `legacy_content_possible`，不把其逐行 JSON 验证作为通过结论。

不得为通过验收静默删除真实历史日志。

## 15. 测试门槛

至少覆盖：

1. dry-run 完全只读且不刷新状态；
2. 固定 target/path，拒绝 symlink parent、active、archive、status 和 lock；
3. rotate flock 互斥，遗留 lock 文件不阻塞；
4. size 与 generation age 触发，空文件不按年龄触发；
5. app copy-truncate 保持 inode，后续 writer 继续写 active；
6. partial tail 丢弃并计数，JSON fixture archive 逐行可解析；
7. heartbeat rename 后 writer 写入新 inode，旧 inode 不增长；
8. heartbeat 新 active 创建失败回滚；
9. durable pending 在下一轮恢复，不被 temp cleanup/budget 删除；
10. gzip final 原子提交，失败保留 pending；
11. retention 7/7/3；
12. 250 MiB archive budget 与清理顺序；
13. active/pending 不被预算删除，unrecoverable 状态稳定；
14. generation timestamps 在失败、not-modified、dry-run 时不重置；
15. status 原子写、权限 `0600`、字段脱敏；
16. 所有目录 `0700`、目标文件 `0600`；
17. CLI exit code 与稳定错误码；
18. 既有 Deploy、Monitor 和完整回归通过。
19. app pending 在 `snapshot_committed`、active 未 truncate 时崩溃：下一轮不 finalize、不自动 truncate，返回 recovery required，并保留 active/pending/meta；
20. app pending 在 `active_truncated` 后崩溃：下一轮安全 finalize，不重复 truncate，final archive 原子提交；
21. heartbeat pending 含 partial tail：pending 原文件不被修改，archive 仅含完整行，gzip 失败后可从 pending 重试；
22. snapshot 只复制首次 `fstat.st_size`，持续追加 writer 不导致无限复制；
23. phase metadata 写失败时不进入下一不可逆阶段。

## 16. 禁止范围

P6-Deploy-3C 不得实现：

- `com.tghub.rotate-logs` plist、安装或卸载；
- 修改 `com.tghub.service`；
- 管理台轮转页面或 stale heartbeat reader；
- online Telethon authorization preflight；
- 自动 bootout/bootstrap 主服务；
- 严格停服零丢失轮转；
- 日志内容重写、补脱敏或历史日志静默删除；
- DB、Parser、Normalizer、Dedup、EventBus 或 Bot 业务改动。

## 17. 最终授权

```text
P6-DEPLOY-3C_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  allow_P6_Deploy_3C: yes
  allow_P6_Deploy_3D: no
```

下一步只允许实现 P6-Deploy-3C rotation engine、retention、budget、locks、dry-run 和内部 status persistence。
