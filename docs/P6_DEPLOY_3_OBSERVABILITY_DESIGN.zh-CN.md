# P6-Deploy-3：本机可观测性与日志轮转设计

> 状态：design-locked
> 模式：design-only
> 前置：P6-Deploy-2 已完成真实安装验收
> BLOCKERS：0
> ALLOW_P6_DEPLOY_3A_IMPLEMENTATION：yes
> ALLOW_P6_DEPLOY_3B_IMPLEMENTATION：yes
> ALLOW_P6_DEPLOY_3C_IMPLEMENTATION：no
> ALLOW_P6_DEPLOY_3D_IMPLEMENTATION：no

## 1. 阶段目标

P6-Deploy-3 只建立本机长期运行所需的可观测性闭环：

```text
application / MonitorRuntime
-> structured log / desensitized heartbeat
-> launchd-owned files
-> bounded rotation
-> local status observation
```

通过后只能证明日志、heartbeat、脱敏和轮转可长期稳定运行，不能证明备份恢复或最终交付完成。

## 2. 当前差异

实现前必须承认并修正以下现状：

1. `app/infra/logger.py` 只有一个 stderr handler，普通日志与 fatal 日志没有分流。
2. LaunchAgent 当前写入 `app.log`、`app.error.log`，与总设计中的 `app.stdout.log`、`app.stderr.log` 命名不一致。
3. CLI 的 `JsonlHeartbeatSink` 长期持有文件句柄，不适合 rename 轮转。
4. 管理台启动 Monitor 时使用 `ControlHeartbeatSink`，heartbeat 只保存在内存，未写入 `HEARTBEAT_PATH`。
5. 当前没有统一 redaction filter，部分模块仍可能把动态标识放入自由文本日志。
6. Deploy-2 的 startup preflight 只检查 session 文件和目录，不验证 `is_user_authorized()`；该遗留项单独补强，但不得让 static preflight 访问 Telegram。

## 3. 固定边界

包含：

- 单行 JSON 结构化日志；
- stdout / stderr 分流；
- 统一字段白名单和脱敏 filter；
- 管理台与 CLI 共用的持久化 heartbeat；
- 日志与 heartbeat 轮转；
- 独立用户级每小时轮转检查 LaunchAgent；
- 本机只读状态汇总；
- 文件权限、保留份数和磁盘上限。

不包含：

- PostgreSQL、watchlist 或 session 备份；
- restore、migration workflow；
- 最终部署手册和完整 smoke test；
- 远程日志平台、Prometheus、OpenTelemetry；
- 修改 Monitor 重连、ingestion、Parser、Dedup、EventBus 或 Bot 业务语义；
- P6-2K 热播模块。

## 4. 结构化日志契约

每条应用日志固定为单行 JSON，字段白名单为：

```text
timestamp
level
logger
event
error_code
request_id
runtime_state
retry_count
recoverable
```

除 `timestamp`、`level`、`logger`、`event` 外，其余字段可为空并省略。禁止把任意 model、exception `repr` 或请求 payload 自动展开到日志。

`event` 必须是稳定枚举式标识，例如：

```text
monitor.start_requested
monitor.channel_resolution_failed
monitor.reconnect_scheduled
ingestion.failed
notification.transport_failed
deploy.preflight_failed
```

异常日志只记录异常类型映射后的稳定 `error_code`。默认不输出 traceback；仅在明确开发模式下允许 traceback，生产模式仍须经过 redaction filter。

最终 formatter 采用“字段白名单 + 值清洗”双层模型。未知 `extra` 一律不进入 JSON；`record.msg` 只允许稳定 event，`record.args` 不接受任意对象，生产 exception formatter 不输出异常原文或 traceback。

应用 logger 传入非法或未知 event 时降级为 `logging.invalid_event` 和 `LOG_EVENT_INVALID`，不得输出原始 msg。第三方 logger 降级为对应的 `third_party.*`，同样不输出原始 msg。

推荐调用：

```python
logger.error(
    "ingestion.failed",
    extra={"error_code": "DATABASE_UNAVAILABLE", "recoverable": True},
)
```

禁止把正文、对象或异常作为 `%s`、`%r` 参数写入动态消息。

## 5. stdout 与 stderr

固定分流：

- stdout filter 接受：level 小于 `ERROR`，以及 `level == ERROR and recoverable is True`；
- stderr filter 接受：level 大于 `ERROR`，以及 `level == ERROR and recoverable is not True`；
- 未显式声明 `recoverable` 的 `ERROR` 默认进入 stderr；
- `CRITICAL` 和未捕获异常永远进入 stderr；
- 同一条记录只能进入一个流，不重复写入；
- Python 不创建 FileHandler；文件所有权继续属于 launchd。

startup blocker 不新增自由字段，由稳定 `event/error_code` 映射决定。第三方 logger 的 `ERROR` 默认不可恢复。

`DEBUG`、`INFO`、`WARNING` 固定进入 stdout。生产默认 level 为 `INFO`，因此不生成 DEBUG 记录。

LaunchAgent 文件名统一为：

```text
~/.tg-hub/logs/app.stdout.log
~/.tg-hub/logs/app.stderr.log
```

升级时旧 `app.log`、`app.error.log` 不删除，只停止写入并由运维记录归档；不得在安装脚本中静默搬移或覆盖。

## 6. 脱敏规则

日志 formatter 前必须经过统一 redaction filter。禁止字段包括：

- Telegram 消息正文、caption、raw payload；
- API ID、API hash、Bot token、webhook secret；
- session 路径、session 内容和 auth key；
- 完整 `DATABASE_URL`；
- 完整 chat ID、channel ID、message link；
- 完整 username、频道标题和资源链接；
- HTTP request body、Authorization、Cookie 和 query secret。

允许输出的标识只能使用现有稳定 mask helper 或不可逆短 fingerprint。禁止先写原始值再依赖轮转脚本清洗。

redaction 必须同时覆盖：

- `record.msg` 与格式化参数；
- `extra` 字段；
- exception message；
- Uvicorn、SQLAlchemy、Telethon 和 httpx 第三方 logger。

生产默认关闭 Uvicorn access log和 Telethon DEBUG。

第三方日志固定映射：

```text
uvicorn.error     -> third_party.uvicorn
sqlalchemy.engine -> third_party.sqlalchemy
telethon          -> third_party.telethon
httpx/httpcore    -> third_party.httpx
```

保留原 logger 名，但不原样输出第三方 message 或 exception。生产固定 `SQLAlchemy echo=false`，Telethon、httpx 和 httpcore 至少为 `WARNING`。

## 7. Heartbeat 持久化

`MonitorHeartbeat` DTO 保持既有脱敏契约，不新增正文、链接或 raw payload。

管理台启动和 CLI 启动必须使用相同的组合 sink：

```text
MonitorRuntime
-> CompositeHeartbeatSink
   -> ControlHeartbeatSink
   -> JsonlHeartbeatSink(HEARTBEAT_PATH)
```

规则：

- 内存 sink 失败不得影响文件 sink；文件 sink 失败不得停止 Monitor；
- 每个 sink 独立记录稳定错误码；
- `CompositeHeartbeatSink.emit()` 对单个 sink 进行异常隔离；
- `aclose()` 幂等并尝试关闭全部 sink；
- heartbeat 写失败通过管理台健康观察字段显示，但不改变 application readiness；
- 若 Monitor 未运行，heartbeat 文件不伪造新的存活记录。

文件 sink 自己维护 `HeartbeatPersistenceStatus`，不修改 `MonitorHeartbeat` DTO：

```text
enabled
last_attempt_at
last_success_at
status: ok | write_failed | permission_denied | path_invalid | closed
error_code
```

`CompositeHeartbeatSink.statuses()` 提供只读聚合；管理台读取不得触发文件 I/O。状态更新须协程安全，`aclose()` 后的 emit 返回稳定 `HEARTBEAT_SINK_CLOSED`，但不得影响其他 sink。

## 8. Heartbeat 写入与轮转语义

固定采用“每次 emit 短生命周期 append”方案：

```text
open append
-> write exactly one JSON line
-> flush
-> close
```

不再长期持有文件句柄。一条 heartbeat 先完整序列化为 `utf8_json + b"\n"`，限制为 `64 KiB`。正常路径使用 write-all loop 并确认全部字节写入；无法写满时 emit 标记为 `HEARTBEAT_SHORT_WRITE`。

安全打开固定要求：父目录 `lstat` 后拒绝 symlink；文件使用 `O_APPEND | O_CREAT | O_WRONLY | O_NOFOLLOW`；打开后 `fstat` 确认为 regular file；权限收口为 `0600`。不得以 `is_symlink()` 后普通 `open()` 代替，避免 TOCTOU。

heartbeat writer 与 rotator 使用同一个专用内核锁：

```text
~/.tg-hub/runtime/heartbeat.lock
```

`JsonlHeartbeatSink.emit()` 获取 heartbeat lock 后才安全打开、write-all、close 并释放。heartbeat rotator 获取同一把锁后执行 rename 和新 active 创建；随后释放 heartbeat lock，但继续持有总 rotate lock 完成 gzip、retention 和状态提交。单进程本机部署使用互斥 `flock`，不引入读写锁。

`JsonlHeartbeatSink` 还持有一把进程内 `asyncio.Lock`，覆盖 closed 检查、`last_attempt_at`、文件安全打开与 write-all、close、成功或失败状态更新。`aclose()` 获取同一把锁后设置 closed；之后的 emit 不打开文件并返回 `HEARTBEAT_SINK_CLOSED`。进程内锁先获取，随后才获取 heartbeat flock。

锁等待超时返回 `HEARTBEAT_LOCK_TIMEOUT`。第一版固定 `flush_to_kernel=yes`、`fsync_each_emit=no`、`durability_claim=best_effort`。heartbeat 是观测数据，突然断电时允许丢失最后少量未落盘记录。archive 和 rotation-status 的原子提交仍执行必要 fsync。

`rotate.lock` 只防两个 rotator 并发，`heartbeat.lock` 只协调 writer 与 heartbeat rotation，两者不可互相替代。固定加锁顺序为：先 `rotate.lock`，后 `heartbeat.lock`；writer 只获取 heartbeat lock。

轮转器对 heartbeat 使用同目录原子 rename，再安全创建权限为 `0600` 的新文件；锁释放后下一次 emit 才打开新 inode。

验收必须证明：

- heartbeat lock 释放后旧 inode 不再增长；
- 新 heartbeat 写入新文件；
- successful emit 返回前已确认完整 payload 写入；
- active heartbeat 文件在进程 crash、kill、磁盘错误时允许存在末尾 partial line；
- reader 必须忽略并报告末尾 partial line，不得将其视为有效 heartbeat；
- rotation 只归档到最后一个完整换行符，partial tail 丢弃并计数；
- archive 中已提交的记录逐行可解析。

## 9. 应用日志轮转语义

launchd 长期持有 stdout/stderr 文件描述符，简单 rename 后进程仍会写旧 inode。因此默认应用日志使用受控 copy-truncate：

```text
acquire rotation lock
-> copy current file to temporary archive
-> fsync archive
-> atomic rename archive
-> truncate active file in place
-> release lock
-> gzip completed archive
```

copy snapshot 后只提交到最后一个完整换行符；临时 archive 的不完整尾部丢弃并计数。状态至少记录 `copied_bytes`、`truncated_bytes` 和 `partial_tail_detected`。

`rotate.lock` 只互斥多个 rotation process，不锁定 launchd writer，也不与应用 logging handler 协调。snapshot 完成到 truncate 之间的新记录可能永久丢失，且无法通过 `copied_bytes`、`truncated_bytes` 或 `partial_tail_detected` 精确计算实际损失。状态必须明确：

```text
rotation_consistency: best_effort_copy_truncate
concurrent_write_loss_possible: true
writer_paused: false
```

该方案保持服务不中断，但明确不保证零日志丢失，也不保证并发窗口内的完整日志全部被归档。验收口径固定为：

- active inode 不变；
- launchd 后续日志继续进入 active 文件；
- archive 除被显式丢弃的末尾不完整记录外逐行可解析；
- 不产生旧 inode 无限增长；
- 不以“零损坏、零丢失”作为通过结论。

若产品要求归档零丢失，则必须采用严格模式：`bootout -> 确认退出 -> rename/compress -> 创建 active -> bootstrap`。严格模式会产生短暂停服，必须单独批准，不得把它描述为不停服轮转。

轮转器不得读取或重写日志内容进行“事后脱敏”。

## 10. 轮转策略

固定阈值：

| 文件 | 轮转阈值 | 保留 | 压缩 |
|---|---:|---:|---|
| `app.stdout.log` | 10 MB | 7 | gzip |
| `app.stderr.log` | 10 MB | 7 | gzip |
| `heartbeat.jsonl` | 10 MB | 3 | gzip |

`10 MB` 是轮转触发阈值，不是 active 文件硬上限。固定检查策略为：每小时检查，并以距上次成功轮转达到 24 小时作为时间保底。两次检查之间 active 文件可能超过阈值，`hard_active_file_limit=none`。

age 不使用持续变化的 active mtime。每个 target 在 rotation status 中独立保存：

```text
files:
  <target>:
    active_generation_started_at
    last_rotated_at
    last_checked_at
    last_size_bytes
```

`active_generation_started_at` 在首次观察 active 文件时记录，每次成功 truncate/create 后重置。状态文件缺失时优先使用文件 birthtime，不可用时安全退化到 ctime，初始化后持久化到状态文件。触发条件固定为 `size >= threshold OR (size > 0 AND now - active_generation_started_at >= 24h)`。空文件不按年龄轮转；not-modified、失败和 dry-run 均不得刷新 generation time 或 `last_rotated_at`。

补充规则：

- 每小时 LaunchAgent 只触发检查，未达大小或时间阈值不轮转；
- 手动 `rotate --dry-run` 只输出脱敏计划，不写文件；
- 只处理配置根目录下的固定文件名，拒绝路径穿越和 symlink；
- lock 文件位于 `~/.tg-hub/runtime/rotate.lock`；
- safe open 后使用 `flock(fd, LOCK_EX | LOCK_NB)`，只有 flock 失败才返回 `ROTATION_ALREADY_RUNNING`；
- lock 文件允许长期存在，文件存在本身不表示任务运行；进程退出或崩溃后由内核释放锁；
- lock 内容可为空，或只写脱敏启动时间和 PID；PID 仅供观察，不用于判断锁有效性或 kill；
- heartbeat lock 采用相同的内核锁和安全打开规则；
- 临时文件中断后不计为有效 archive；
- 所有 active、archive、lock 文件权限固定为 `0600`，目录为 `0700`；
- archive 硬预算默认 250 MB，超限按最旧 archive 清理，不删除 active 文件；
- `total_observed_bytes` 统计 active、archive、temporary 和 lock，但不宣称整个目录有 250 MB 硬上限；
- 状态固定包含 `archive_budget_status=within_budget|cleaned|exceeded_unrecoverable` 和 `active_oversize=true|false`。

archive 清理顺序固定为：删除无效 temp、逐 target 执行保留份数、计算全部有效 archive、按 `completed_at` 从旧到新清理超预算 archive。永不删除 active 和本轮未完成提交的 archive。只有清理全部允许删除项后仍超预算，才返回 `exceeded_unrecoverable`。

archive 名称固定包含 target、UTC 微秒时间和唯一 run ID，例如：

```text
app.stdout.log.20260713T093012.123456Z.<run_id>.jsonl.gz
```

## 11. 轮转 LaunchAgent

新增独立用户级 label：

```text
com.tghub.rotate-logs
```

固定要求：

- `StartInterval=3600`，每小时检查一次；
- `ProgramArguments` 直接调用 `backend/.venv/bin/python -m app.deploy.rotate_logs`；
- `ProgramArguments[0]` 使用绝对 Python 路径，后续参数固定为 `-m`、`app.deploy.rotate_logs`；
- `WorkingDirectory` 固定为仓库 `backend/` 的绝对路径；
- 仅注入非敏感绝对 `TG_HUB_ENV_FILE`；
- 不设置 KeepAlive；
- 不与主服务共用 PID 或生命周期状态；
- 重复安装幂等或稳定返回已安装错误；
- 卸载主服务时是否卸载轮转 agent 必须显式选择，不隐式删除日志。

禁止依赖 `PATH`、`PWD`、`PYTHONPATH` 或 shell profile。当前不要求 editable install，以固定 `WorkingDirectory` 作为模块解析基线。

rotation agent 自身不写主应用日志。stdout/stderr 指向 `/dev/null`，所有结果原子更新：

```text
~/.tg-hub/runtime/rotation-status.json
```

字段固定为：

```text
schema_version
run_id
check_started_at
check_completed_at
last_started_at
last_completed_at
status
error_code
rotated_files
cleaned_archives
archive_bytes
active_bytes
```

LaunchAgent 本身无法启动时通过 `launchctl print gui/$UID/com.tghub.rotate-logs` 观察。状态文件不得包含路径或异常原文。

rotation-status 使用安全原子写：验证 runtime 目录、同目录创建唯一临时文件、`O_NOFOLLOW`、完整写入、fsync temp、chmod `0600`、atomic replace、fsync parent directory。`run_id` 为随机不可逆标识，不包含路径、用户名或 PID。

单文件轮转和压缩全部在 rotate flock 内完成：copy temp、fsync、提交未压缩 archive、truncate active、gzip 到 `.tmp.gz`、fsync、atomic rename 为 `.gz`、删除未压缩 archive、执行 retention/budget、更新状态、最后释放锁。下一轮不得观察或处理本轮未完成 archive。

Python 可执行模块固定为 `backend/app/deploy/rotate_logs.py`；`backend/deploy/` 只存放 `com.tghub.rotate-logs.plist.template`、安装和卸载脚本。不得把 Python 模块放在 `backend/deploy/rotate_logs.py`。

## 12. 状态与健康展示

Deploy-3 只增加本机观察字段：

```text
logging:
  stdout_writable
  stderr_writable
  last_rotation_at
  last_rotation_status
heartbeat:
  persistence_enabled
  last_persisted_at
  write_status
  stale
```

`stale` 只在 Monitor 处于 running/listening 时按 `max(3 * heartbeat_interval, 90s)` 判断。Monitor stopped 时返回 `not_applicable`，不得误报故障。

日志或 heartbeat 写失败不使 `/health/live` 失败。application readiness 是否降级只记录观察字段，第一版不返回 `503`，避免磁盘观测问题让管理台不可用。

## 13. Telethon Session 遗留补强

Deploy-3 可补充真实运行 preflight，但必须保持 static/online 分层：

- static preflight：只检查依赖、配置、session 文件存在和权限，不访问 Telegram；
- online resolver preflight：bounded connect 后调用 `is_user_authorized()`，未授权返回 `SESSION_UNAUTHORIZED`；
- 未授权时不得继续把 12 个频道分别报告为同一个 `SESSION_UNAVAILABLE`；
- 管理台应优先展示 session blocker，再展示频道解析结果；
- 不自动登录、不请求验证码、不替换 session。

稳定错误码至少区分：

```text
SESSION_UNAUTHORIZED
TELEGRAM_CONNECT_TIMEOUT
TELEGRAM_NETWORK_UNAVAILABLE
TELEGRAM_RPC_ERROR
SESSION_CORRUPTED
SESSION_PATH_INVALID
```

执行顺序固定为：static preflight、session 文件权限、bounded connect、授权检查、频道解析、finally disconnect。未授权时频道整体标记为 `blocked_by_session`，不逐频道调用 resolver。该补丁属于 3D，不混入 3A。

## 14. 验收矩阵

必须覆盖：

1. JSON 日志字段稳定且每行可解析；
2. stdout/stderr 不重复并按严重性分流；
3. seeded token、hash、URL、正文、chat ID、channel ID 不出现在日志；
4. 第三方 logger 经过同一 redaction；
5. 管理台启动 Monitor 后 heartbeat 持久化；
6. heartbeat sink 单点失败被隔离；
7. heartbeat rename 轮转后新文件继续增长；
8. 应用日志 copy-truncate 后 launchd 继续写 active 文件；
9. active heartbeat crash partial tail 被 reader 忽略并报告；archive 只包含完整 JSON 行；heartbeat lock 释放后旧 inode 不再增长；
10. 重复轮转由 flock 稳定拒绝，崩溃遗留 lock 文件不造成永久阻塞；
11. archive 保留份数和 250 MB archive 预算生效；
12. active 文件不会被预算清理删除，清理顺序和 `exceeded_unrecoverable` 语义稳定；
13. 所有文件和目录权限正确；
14. rotation LaunchAgent plist 无敏感值；
15. Monitor stopped 时 heartbeat stale 为 `not_applicable`；
16. online preflight 对未授权、网络、RPC 和损坏 session 返回不同稳定错误码；
17. 完整非数据库回归通过；
18. 真实 LaunchAgent 下连续运行和至少一次真实轮转通过。

## 15. 阶段拆分建议

```text
P6-Deploy-3A
structured logging contract + redaction + stdout/stderr split

P6-Deploy-3B
CompositeHeartbeatSink + short-open JSONL persistence

P6-Deploy-3C
rotation engine + retention + budget + lock

P6-Deploy-3D
rotation LaunchAgent + status fields + real rotation acceptance
```

每个子阶段单独评审和提交。P6-Deploy-3A 已完成；P6-Deploy-3B 已通过独立实施评审；3C、3D 不自动授权。

## 16. 评审结论

P6-Deploy-3 架构方向和可验收契约已通过最终评审。前一轮七项问题及后续三个实质 blocker、三个 clarification 均已关闭，设计正式锁定。

```text
P6-DEPLOY-3_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 0
  allow_design_lock: yes
  allow_P6_Deploy_3A: yes
  allow_P6_Deploy_3B: yes
  allow_P6_Deploy_3C: no
  allow_P6_Deploy_3D: no
```

P6-Deploy-3A 已完成。当前新增许可严格限定为 P6-Deploy-3B heartbeat 持久化与组合 sink；rotation、retention、online session preflight 和 Deploy-4/5 均未授权。
