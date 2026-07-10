# P6-2J 最小可视化管理台设计

> 状态：设计锁定
> 模式：UI / API / runtime control boundary design only
> 前置：P6-2I CLI、bootstrap、static preflight、长期 monitor runtime 已完成
> 不包含：生产部署、远程多用户、进程守护、Outbox、历史补偿、媒体下载

## 1. 阶段目标

P6-2J 只回答：

> 如何在本机浏览器中安全地管理监听频道、关注资源名，并查看和控制当前进程内的 monitor runtime？

固定链路：

```text
Browser UI
-> Admin HTTP API
-> WatchlistApplicationService / MonitorControlService
-> existing watchlist schema / MonitorBootstrap / MonitorRuntime
```

P6-2J 不是部署阶段，也不负责让进程自动常驻。关闭承载管理台的 FastAPI 进程后，管理台和由它持有的 monitor 都会停止。

## 2. 固定范围

### 2.1 本阶段包含

- 查看 monitor 当前状态
- 查看脱敏 heartbeat 与运行计数
- 执行 static preflight
- 受控启动 monitor
- 受控停止 monitor
- 查看、添加、编辑、启用、停用、删除 `source_channels`
- 查看、添加、编辑、启用、停用、删除 `watch_titles`
- 管理 watch title aliases
- schema 校验、冲突检测和原子保存 watchlist
- 配置变化后显示 `restart_required`
- 明确空态、加载态、失败态和操作反馈

### 2.2 本阶段不包含

- systemd / launchd / Docker Compose
- Web API 启动 CLI 子进程
- 任意 shell command 执行
- 多实例 monitor 调度
- 多用户、角色、团队权限
- 公网暴露和远程管理
- OAuth / SSO
- 实时 WebSocket 推送
- watchlist 存入 PostgreSQL
- 自动热重载 watchlist
- RawMessage 管理、重放或手工改状态
- Work / Resource / ResourceLink 编辑
- history backfill
- Outbox / retry queue
- Bot token、API hash、session、DATABASE_URL 管理

## 3. 产品定位

第一版是安静、紧凑、面向单一管理员的本地运维工具，不是营销页面。

固定导航只有两个一级页面：

```text
概览
监听配置
```

“资源名”在本阶段专指 `watch_titles`，不是 PostgreSQL 中的 `Resource` 业务记录。资源目录查询和详情浏览可在后续阶段单独加入，不能混入 P6-2J。

## 4. 运行拓扑

### 4.1 唯一 Web runtime owner

Web 控制模式固定为：

```text
FastAPI lifespan
-> create MonitorControlService
-> service owns at most one asyncio.Task
-> task runs MonitorBootstrap / MonitorRuntime
-> application shutdown requests graceful stop
```

固定约束：

- `MonitorControlService` 是 Web 模式下唯一 runtime owner
- 同一进程最多一个 monitor task
- API router 不直接持有 Telethon client
- UI 不直接持有 runtime 对象
- endpoint 只调用 application service
- application shutdown 必须请求 stop 并等待有限 drain
- 不调用 `subprocess`、`os.system`、shell 或 CLI `main()`

### 4.2 CLI 与 Web 模式互斥

P6-2J 不解决跨进程 leader election。使用 Web 管理台控制 monitor 时，不得同时执行：

```bash
python -m app.modules.monitor.cli run
```

第一版通过进程内状态保证 Web 侧单实例，但无法阻止另一个终端进程启动 CLI。因此使用说明必须明确二者互斥。

跨进程锁、数据库租约和多实例选主属于部署阶段。

## 5. 页面与信息架构

### 5.1 全局框架

桌面端使用固定侧栏，移动端使用顶部导航。界面保持工作台密度，不使用 hero、营销卡片或装饰性大图。

全局顶栏只显示：

- 产品名 `tg-hub`
- monitor 状态指示
- 当前配置是否需要重启
- 最后刷新时间

不在全局区域显示：

- Telegram API ID / hash
- Bot token
- session 路径
- DATABASE_URL
- 完整原始消息

### 5.2 概览页

概览页固定分为四个无嵌套区域。

#### A. Runtime 状态

显示：

- `monitor_state`
- `liveness`
- `readiness`
- `connected`
- `handler_registered`
- `uptime_seconds`
- `shutdown_reason`
- `restart_required`

状态使用稳定枚举，不由前端自由推断：

```text
stopped
starting
running
stopping
degraded
failed
```

#### B. 操作区

固定操作：

- `启动`：仅在 `stopped` / `failed` 可用
- `停止`：仅在 `starting` / `running` / `degraded` 可用
- `运行预检`：不连接 Telegram、不连接数据库
- `刷新`：重新获取状态

操作按钮必须有 loading 和 disabled 状态，避免重复提交。

第一版不提供：

- 强制终止
- 重启按钮
- 清空数据
- 重跑 Parser/Dedup
- 回填历史消息

#### C. 运行统计

显示脱敏聚合数据：

- enabled / resolved channel count
- events seen / matched / rejected
- ingest stored / duplicate / rejected / failed
- process success / already done / failed
- reconnect attempts
- heartbeat total
- last event time
- last successful event time

计数只来自 runtime snapshot，不通过数据库反推。

#### D. 最近错误

最多显示最近 10 条稳定错误记录：

- `error_code`
- `error_phase`
- `recoverability`
- `occurred_at`
- `retry_scheduled`

不显示自由异常堆栈、消息正文、raw payload 或第三方响应体。

### 5.3 监听配置页

使用两个 tab：

```text
监听频道
关注资源名
```

#### 监听频道 tab

表格列固定为：

- 引用：`ref`
- 输入类型：numeric / username / t.me URL / invalid
- 状态：enabled / disabled
- 本地校验结果
- 操作：编辑、启停、删除

新增与编辑使用 modal，不在表格内做复杂行内编辑。

允许输入：

```text
@username
https://t.me/username
numeric channel id
```

保存时只做本地 schema 和引用分类，不访问 Telegram API。真实 username 解析发生在 monitor 启动流程，不由每次编辑触发。

#### 关注资源名 tab

表格列固定为：

- 标题：`title`
- aliases 数量
- 状态：enabled / disabled
- 操作：编辑、启停、删除

编辑 modal 字段：

- `title`：必填
- `aliases`：可选，可逐项增删
- `enabled`：开关

固定校验：

- title strip 后不能为空
- alias strip 后不能为空
- 同一条目 aliases 去重
- alias 不得与本条 title 归一化后相同
- 所有 title 与 aliases 组成同一个全局名称集合
- 任一名称归一化后不得与其他条目的 title 或 alias 冲突
- enabled 与 disabled 条目都参与唯一性约束

名称唯一性归一化固定为：

```text
strip
-> Unicode NFKC
-> casefold
-> collapse internal whitespace
```

唯一性校验不做标点删除、简繁转换、拼音或模糊匹配。disabled 条目仍占用名称，避免重新启用时才出现延迟冲突。

P6-2J 不增加模糊匹配、拼音、AI 别名或资源类型识别。

## 6. Watchlist 存储边界

### 6.1 单一事实来源

第一版继续以配置文件为单一事实来源：

```text
WATCHLIST_PATH
default: ~/.tg-hub/watchlist.json
```

不创建 watchlist 数据库表，不把文件和 PostgreSQL 做双写。

### 6.2 Application service

固定服务接口语义：

```text
WatchlistApplicationService.get_snapshot()
WatchlistApplicationService.validate(candidate)
WatchlistApplicationService.replace(candidate, expected_revision)
```

router 不得直接调用 `Path.write_text()` 或拼接 JSON。

### 6.3 Revision 与并发控制

读取结果必须包含：

```text
revision = sha256(canonical_json_bytes)
```

更新请求必须提交 `expected_revision`。

若文件在读取后被 CLI、编辑器或另一浏览器修改：

```text
expected_revision != current_revision
-> HTTP 409 WATCHLIST_REVISION_CONFLICT
```

前端必须提示重新加载，不能自动覆盖。

`replace()` 必须持有 `WatchlistApplicationService` 的进程内 `asyncio.Lock`。固定流程：

```text
acquire process-local write lock
-> read current bytes
-> validate current file and calculate current revision
-> compare expected_revision
-> validate candidate
-> write and fsync sibling temporary file
-> re-check target revision where detectable
-> atomic os.replace
-> fsync parent directory where supported
-> release lock
```

这提供的是“revision 乐观并发 + Web 进程内写互斥”，不是跨进程文件锁。它能保证同一 Web 进程内更新结果确定，但不能阻止编辑器或其他进程在临界区写文件。外部竞争无法被检测时，最后一次原子 replace 可能胜出。

跨进程强互斥不属于 P6-2J。

### 6.4 原子保存

固定写入流程：

```text
validate candidate with WatchlistConfig
-> serialize canonical JSON
-> write sibling temporary file
-> flush
-> fsync temporary file
-> atomic os.replace(temp, WATCHLIST_PATH)
-> return new revision
```

固定要求：

- 保留 UTF-8
- 输出稳定缩进
- 文件权限不得扩大
- 写失败保留旧文件
- 临时文件不得遗留敏感内容到报告
- 不使用读取后直接覆盖的非原子写法
- 临时文件必须与目标文件位于同一目录
- 临时文件名由服务端生成，不接受请求参数
- 现有文件存在时，替换文件继承原文件 mode
- 目标文件不存在时，以 `0600` 创建
- `WATCHLIST_PATH` 是目录时拒绝
- `WATCHLIST_PATH` 是 symlink 时拒绝写入
- 父目录必须存在且可写

symlink 第一版采用严格拒绝策略，不跟随 symlink 保存。路径检查失败使用稳定错误码，不向响应暴露完整服务器路径。

是否额外保留一个 `.bak` 备份留到实现评审决定，不作为 P6-2J 必需项。

### 6.5 非法文件恢复

watchlist 状态固定为：

```text
valid
missing
invalid
unsafe
```

revision 只针对有效 canonical config：

- valid：返回 revision
- missing / invalid / unsafe：`revision = null`

第一版允许管理员修复 missing 或 invalid 文件，但必须同时满足：

```text
expected_revision = null
recovery_confirmed = true
candidate schema valid
path and parent safety checks pass
```

`unsafe`，包括 symlink 或目录，不允许通过恢复流程覆盖。

## 7. 配置生效语义

保存 watchlist 不自动修改正在运行的 runtime。

固定语义：

```text
runtime stopped
-> save succeeds
-> next start uses new configuration

runtime running
-> save succeeds
-> current runtime keeps startup snapshot
-> restart_required = yes
```

`restart_required` 的比较依据是：

```text
runtime_started_revision != current_watchlist_revision
```

异常场景固定：

```text
runtime active + file missing
-> current_watchlist_revision = null
-> watchlist_status = missing
-> restart_required = yes

runtime active + file invalid
-> current_watchlist_revision = null
-> watchlist_status = invalid
-> restart_required = yes

runtime never started
-> runtime_started_revision = null
-> restart_required = no
```

watchlist 被删除或损坏不会立即停止当前 runtime；当前 runtime 继续使用启动时内存快照。

本阶段不做：

- handler 动态增删频道
- 运行中替换 watch titles
- 自动 stop/start
- 文件系统 watch 自动重载

## 8. Monitor Control Boundary

### 8.1 固定接口

建议协议：

```text
MonitorControlService.status() -> MonitorControlSnapshot
MonitorControlService.start(expected_watchlist_revision?) -> StartResult
MonitorControlService.stop() -> StopResult
MonitorControlService.run_preflight() -> MonitorStartupPreflightReport
```

所有结果必须是脱敏 DTO，不返回 runtime、task、client、session 或 exception 对象。

### 8.2 状态快照

`MonitorControlSnapshot` 至少包含：

```text
control_state
runtime_state
liveness
readiness
connected
handler_registered
started_at
uptime_seconds
runtime_started_revision
current_watchlist_revision
restart_required
heartbeat
last_summary
last_errors
operation_in_progress
allowed_actions
```

`allowed_actions` 由服务端计算：

```text
can_start
can_stop
can_preflight
```

前端按钮只能依据 `allowed_actions`，不得自行组合 control/runtime 枚举推断权限。

### 8.3 控制状态机

`control_state` 是唯一用于控制操作的状态：

```text
stopped
starting
running
stopping
degraded
failed
```

允许迁移：

```text
stopped -> starting
failed -> starting
starting -> running | degraded | failed | stopping
running -> degraded | stopping | failed
degraded -> running | stopping | failed
stopping -> stopped | failed
```

禁止：

```text
stopped -> running
running -> starting
stopping -> starting
```

`runtime_state` 只保留底层 runtime 原始观测值，不控制 UI 操作。`operation_in_progress` 不能与 control state 形成矛盾；实现应由同一把 lock 内的状态迁移统一更新。

### 8.4 启动

固定启动流程：

```text
acquire async operation lock
-> reject if starting/running/stopping
-> load current watchlist snapshot
-> run static preflight
-> if fail, return PRECHECK_FAILED
-> set state starting
-> create one background task
-> task enters MonitorBootstrap.run()
-> expose running/degraded/failed state
```

HTTP 请求不应等待长期 monitor 结束。启动 endpoint 只等待任务被接受或启动前检查失败。

`POST /monitor/start` 返回 `202 Accepted` 只代表：

```text
start_accepted = yes
background task created and owned
control_state = starting
```

它不代表 Telegram 已连接或 handler 已注册。后续通过 status 观察 `starting -> running / degraded / failed`。第一版不需要 operation ID。

### 8.5 停止

固定停止流程：

```text
acquire async operation lock
-> reject if already stopped
-> set state stopping
-> call bootstrap.stop()
-> await task with bounded timeout
-> retain final summary
-> set state stopped or failed
```

超时后本阶段不强杀 client 或进程，只返回稳定错误：

```text
MONITOR_STOP_TIMEOUT
```

stop timeout 后固定语义：

```text
control_state = degraded
task remains owned and tracked
can_start = false
can_stop = true
last_error_code = MONITOR_STOP_TIMEOUT
```

此时不得设置 `self._task = None`。后续 stop 请求继续作用于同一个 task；task 最终结束时由 done callback 安全收敛状态。FastAPI shutdown 仍对该 task 再执行一次有限 drain，不能创建第二条 stop 流程。

### 8.6 并发规则

- start 与 stop 使用同一把 `asyncio.Lock`
- 重复 start 返回 `409 MONITOR_ALREADY_ACTIVE`
- stopped 时重复 stop 可返回幂等成功，建议 `200 already_stopped`
- operation 进行中时不创建第二个 task
- 服务启动后初始状态为 `stopped`
- FastAPI shutdown 时调用同一 stop boundary
- stop timeout 或 task 尚存时，任何 start 返回 `MONITOR_TASK_STILL_RUNNING`
- task 意外取消映射为 failed，不伪装成 stopped

## 9. HTTP API 边界

建议前缀：

```text
/api/admin/v1
```

固定最小 endpoints：

| Method | Path | 职责 |
|--------|------|------|
| GET | `/monitor/status` | 获取 runtime/control snapshot |
| POST | `/monitor/preflight` | 执行纯静态 preflight |
| POST | `/monitor/start` | 接受一次受控启动 |
| POST | `/monitor/stop` | 请求优雅停止 |
| GET | `/watchlist` | 获取配置、revision 和校验摘要 |
| PUT | `/watchlist` | 以 expected_revision 原子替换配置 |

第一版前端可以在本地编辑完整 candidate 后整体 PUT。暂不设计单条 channel/title 的 REST endpoint，避免文件存储下出现多套并发语义。

所有管理 API 响应必须包含：

```text
api_version: "v1"
schema_version: 1
request_id: opaque string
```

watchlist 响应额外使用字段名 `watchlist_schema_version`，第一版值为 `1`。错误响应带同一个 request ID，便于关联日志，不暴露异常信息。

HTTP 映射固定：

| 结果 | HTTP |
|------|------|
| start accepted | 202 |
| already active / control busy / state conflict | 409 |
| preflight failed | 422 |
| watchlist revision conflict | 409 |
| invalid candidate | 422 |
| internal start/write failure | 500 |

### 9.1 稳定错误码

至少固定：

```text
WATCHLIST_NOT_FOUND
WATCHLIST_SCHEMA_INVALID
WATCHLIST_REVISION_CONFLICT
WATCHLIST_WRITE_FAILED
WATCHLIST_PATH_UNSAFE
WATCHLIST_PARENT_NOT_WRITABLE
WATCHLIST_CURRENT_FILE_INVALID
MONITOR_PRECHECK_FAILED
MONITOR_ALREADY_ACTIVE
MONITOR_START_FAILED
MONITOR_STOP_TIMEOUT
MONITOR_CONTROL_BUSY
MONITOR_TASK_STILL_RUNNING
MONITOR_STATE_CONFLICT
ADMIN_ORIGIN_REJECTED
ADMIN_CSRF_REJECTED
INVALID_REQUEST
```

HTTP body 不返回 Python exception message。

## 10. 安全边界

P6-2J 第一版仅支持本机管理：

```text
host = 127.0.0.1
```

固定要求：

- 不绑定 `0.0.0.0`
- 不配置 permissive CORS
- 管理 API 只接受 same-origin 请求
- mutation endpoint 校验 `Origin` / `Host`
- 所有响应设置 `Cache-Control: no-store`
- 不在 HTML、JavaScript bundle 或 API DTO 中注入 secrets
- 不提供读取 `.env`、session 文件或任意路径的 API
- `WATCHLIST_PATH` 只能来自服务端 Settings，不能由请求传入
- 不允许浏览器指定 heartbeat 输出路径

### 10.1 最小 CSRF 防护

服务每次启动时生成进程内随机 CSRF token。token 注入同源首屏 `<meta>` 或 bootstrap data，由 progressive JavaScript 放入请求 header：

```text
X-TG-Hub-CSRF
```

以下请求都必须校验 token：

- `POST /monitor/preflight`
- `POST /monitor/start`
- `POST /monitor/stop`
- `PUT /watchlist`

mutation/control 请求统一校验：

```text
allowed Host
same-origin Origin
Content-Type: application/json
valid X-TG-Hub-CSRF
```

token 不进入 URL、日志、通用状态 DTO 或错误响应。它只用于本地 CSRF 防护，不是用户认证凭据。

本地限制不是完整身份认证。若后续需要局域网或公网访问，必须先单独设计认证、TLS、CSRF、审计和权限模型，不能直接修改 bind address。

## 11. UI 交互规则

### 11.1 状态刷新

第一版使用 HTTP polling：

```text
monitor active: every 3 seconds
monitor stopped: every 10 seconds
tab hidden: pause or reduce polling
```

不引入 WebSocket / SSE。

### 11.2 危险操作

- 删除频道需要确认 modal
- 删除 watch title 需要确认 modal
- 停止 monitor 需要确认，但不要求输入二次文本
- 启动不弹确认，按钮进入 loading
- 保存配置前显示变更摘要
- 保存成功且 runtime active 时明确显示“重启后生效”

### 11.3 可访问性与响应式

- 所有操作可用键盘完成
- 状态不能只依赖颜色表达
- 表单错误紧邻字段显示
- icon button 提供 tooltip 和 accessible label
- 窄屏下表格转换为紧凑列表，不横向溢出关键操作
- 按钮和动态状态使用稳定尺寸，避免 polling 引起布局跳动

## 12. 前端技术边界

P6-2J 前端技术方案固定为：

```text
FastAPI server-rendered HTML
+ Jinja2 templates
+ native ES modules / small progressive JavaScript
+ scoped CSS
```

固定原因：

- 页面只有两个
- 无复杂客户端状态
- 无实时协作
- 部署产物更少
- same-origin 安全边界更直接
- 无独立前端构建链和 SPA router

仍然必须保持：

```text
UI rendering
!= application service
!= monitor runtime
```

模板或 JavaScript 不得直接读写 watchlist 文件。

## 13. 可观测性与审计

第一版只记录结构化管理操作日志：

```text
watchlist_read
watchlist_updated
preflight_requested
monitor_start_requested
monitor_start_accepted
monitor_stop_requested
monitor_stopped
monitor_control_failed
```

日志允许字段：

- operation
- result
- stable_error_code
- timestamp
- previous/new revision 的短前缀
- channel/title 条目数量
- request_id

日志禁止字段：

- title 以外的消息正文
- raw payload
- API hash
- Bot token
- session path
- DATABASE_URL
- 完整 Telegram entity payload

本阶段不新增数据库 audit table。

## 14. 实现拆分建议

P6-2J 实现不得一次把所有层混在 router 中。建议拆为：

```text
P6-2J-1
WatchlistApplicationService
revision / validation / atomic replace

P6-2J-2
MonitorControlService
single-task lifecycle / status / start / stop

P6-2J-3
Admin API
DTO / stable errors / local-only guards

P6-2J-4
Minimal UI
overview / listening config / polling / forms

P6-2J-5
Integration acceptance
browser workflow / shutdown / conflict / redaction
```

## 15. 测试验收

### 15.1 Watchlist service

- valid snapshot returns revision
- invalid file returns stable error
- valid replace is atomic
- invalid candidate never changes file
- stale revision returns conflict
- write failure preserves old file
- concurrent replace has one deterministic winner
- file permissions are not broadened
- symlink path is rejected
- new file mode is `0600`
- existing file permission is preserved
- parent directory fsync is attempted where supported
- external revision change is rejected where detectable
- invalid current file requires explicit recovery confirmation

### 15.2 Monitor control

- initial state is stopped
- start creates exactly one task
- repeated start does not create a second task
- stop calls graceful boundary
- repeated stop is idempotent
- start/stop race is serialized
- preflight fail prevents task creation
- runtime failure is retained in status
- app shutdown requests stop
- configuration revision drift sets restart_required
- stop timeout retains task ownership
- stop timeout prevents a second start
- task completion after timeout reconciles state
- unexpected cancellation becomes failed
- lifespan shutdown does not create a second stop flow

### 15.3 API

- status and watchlist responses are desensitized
- mutation rejects invalid origin/host
- no permissive CORS
- request cannot override WATCHLIST_PATH
- stable error code maps to expected HTTP status
- API never returns exception stack
- start endpoint returns without waiting for runtime completion
- mutation without CSRF is rejected
- foreign Origin and malicious Host are rejected
- `text/plain` mutation is rejected
- GET endpoints have no side effects
- responses include `Cache-Control: no-store`

### 15.4 UI

- overview renders every runtime state
- start/stop button availability follows server state
- polling does not create overlapping requests
- channel CRUD produces valid candidate
- title/alias validation is visible
- stale revision conflict prompts reload
- running config save displays restart_required
- controls use server-provided allowed_actions
- invalid watchlist recovery requires explicit confirmation
- start accepted displays starting, not running
- stop timeout displays degraded and keeps start disabled
- loading, empty, error, and success states are complete
- desktop and mobile have no overlap or clipped controls
- secrets never appear in DOM or network payload

测试禁止访问真实 Telegram API、真实 Bot API或启动无限监听。浏览器验收使用 fake control service 和临时 watchlist；最终本地 smoke test可单独受控执行。

## 16. 完成标准

P6-2J 通过时，可以得出：

- 管理员能在本机浏览器中管理监听频道和关注资源名
- watchlist 更新具有 schema 校验、revision 冲突检测和原子保存
- 管理员能查看脱敏 runtime 状态与统计
- 管理员能通过 application control boundary 启动和优雅停止单个 monitor task
- 配置变化不会静默改变正在运行的 runtime
- UI/API 不接触 Telethon client、DB Session 或业务 pipeline internals

P6-2J 通过时，不能得出：

- 项目已完成生产部署
- monitor 能在系统重启后自动恢复
- 支持多个管理员或远程访问
- 支持多实例选主
- 通知具有可靠投递保证
- 支持历史补偿或消息重放
- watchlist 已迁移到 PostgreSQL

## 17. 下一阶段门槛

P6-2J 完成后可分别进入：

```text
P6-Deploy
进程守护、密钥、数据库迁移、备份、日志轮转、健康检查

P6-Reconcile
parse_pending / dedup_pending 历史补偿

P6-Outbox
持久化事件与可靠通知
```

这些阶段不得反向侵入 `MonitorControlService`，也不得让管理台承担 supervisor 或业务 pipeline 职责。
