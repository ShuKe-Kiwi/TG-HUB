# P6-Deploy-3D-3 实施评审：Online Session Authorization Preflight

> 项目：tg-hub
> 阶段：P6-Deploy-3D-3
> 状态：implementation-approved
> BLOCKERS：0
> BLOCKERS：0
> ALLOW_IMPLEMENTATION：yes（仅 P6-Deploy-3D-3）
> 前置阶段：P6-Deploy-3D-1、3D-2 已完成

## 1. 阶段目标

P6-Deploy-3D-3 只验证现有 Telethon session 是否能在受控、短生命周期、无监听的条件下完成授权检查与频道解析，并为所有生产 Telethon session 使用方增加真实本机所有权锁。

固定链路：

```text
static preflight
-> canonical session path validation
-> non-blocking session ownership flock
-> create short-lived Telethon client
-> bounded connect
-> bounded is_user_authorized()
-> bounded enabled source channel resolution
-> bounded disconnect
-> release flock
-> desensitized result
```

本阶段不注册 `NewMessage` handler，不启动 Monitor，不访问数据库，不修改 watchlist，不自动登录 Telegram。

## 2. 实施范围

允许实现：

- `telethon-session.lock` 安全 exclusive flock；
- MonitorBootstrap 跨 resolver 与 runtime 的 session ownership；
- online session preflight application service；
- session canonical path 与本地安全检查；
- `is_user_authorized()`；
- 分阶段 timeout 与 overall timeout；
- 全局失败、局部频道失败和未执行频道统计；
- 稳定、脱敏错误码；
- CLI `online-preflight` 子命令；
- Admin 独立 online preflight endpoint 与显式操作按钮；
- 单元、集成和 fake-client 生命周期测试。

明确禁止：

- rotation agent 安装、kickstart 或真实轮转；
- 自动发送验证码、二维码或密码请求；
- 调用 `client.start()`、`sign_in()` 或 `send_code_request()`；
- 创建或替换 session；
- 删除、修复或迁移损坏 session；
- 注册 handler、监听消息或长期运行；
- DB、Parser、Normalizer、Dedup、EventBus、Bot notification；
- 进入 3D-4、Deploy-4 或 Deploy-5。

## 3. Canonical Session Path

`TELEGRAM_SESSION_NAME` 当前是 Telethon SQLite session 的 base path。第一版只支持文件型 SQLite session，不支持 `StringSession` 或自定义 Storage。

canonical path 固定为：

```text
configured path suffix == .session
-> canonical = configured path

otherwise
-> canonical = configured path + ".session"
```

例如：

```text
~/.tg-hub/telethon
-> ~/.tg-hub/telethon.session
```

static preflight 增加本地字段：

```text
session_file_exists
session_file_regular
session_file_permissions
```

校验流程：

```text
lstat parent
-> reject symlink / non-directory / group-world permissions
-> lstat canonical session file
-> reject missing / symlink / non-regular / group-world permissions
```

session 文件不存在或路径不安全返回 `SESSION_PATH_INVALID`。online preflight 不允许依赖 Telethon 隐式创建新 session。

不得把 canonical path 输出到报告或日志。

### 3.1 TOCTOU 威胁边界

Telethon 接收的是 session path，不是本服务安全打开后的 fd，因此 `lstat` 与 Telethon 实际打开 SQLite 之间存在不可完全消除的 TOCTOU 窗口。

第一版固定威胁模型：

- session parent 必须位于 production private-root contract 内；
- parent 必须为非 symlink 私有目录，权限 `0700`；
- 第一次校验记录 session 文件的 device、inode、regular-file 类型和权限；
- 获取 session ownership flock 后、创建 client 前立即重复 `lstat` 并比较上述属性；
- 常见 symlink、路径误配和校验后的普通 inode 替换会被拒绝；
- 不承诺抵御同一 Unix 用户在第二次校验后恶意替换 session 文件。

报告不得宣称 session path 已实现无 TOCTOU 的原子绑定。

## 4. Session Ownership Lock

lock path 固定为：

```text
~/.tg-hub/runtime/telethon-session.lock
```

实际路径由 `Settings.HEARTBEAT_PATH.parent` 推导，保持 production 私有 runtime root 一致。

安全打开：

```text
lstat runtime parent
-> require regular directory, not symlink, mode & 0o077 == 0
-> os.open(O_CREAT | O_RDWR | O_NOFOLLOW, 0600)
-> fstat regular file
-> fchmod(fd, 0600)
-> flock(LOCK_EX | LOCK_NB)
```

获取失败立即返回 `SESSION_IN_USE`，不得等待、轮询、读取 PID 或 kill 进程。

lock 文件在 release 后保留，不得 unlink。删除 lock 文件会造成旧 inode 与新 inode 两套锁并存，破坏互斥语义。进程退出或 fd close 自动释放内核 flock，遗留文件本身不表示占用。

## 5. Monitor 所有权接入

当前 Monitor 启动链路是：

```text
MonitorBootstrap
-> resolver client connect/disconnect
-> runtime client connect/listen/reconnect/disconnect
```

锁必须由 `MonitorBootstrap` 持有，而不是只由 `MonitorRuntime` 持有。

固定生命周期：

```text
database/static assembly completed
-> acquire session ownership before resolver client creation/connect
-> resolve channels
-> create and run MonitorRuntime
-> runtime final disconnect completes or bounded shutdown settles
-> release ownership in Bootstrap finally
```

这样 resolver client 与 runtime client 之间不会出现 online preflight 插入窗口。

Admin Monitor 与 CLI Monitor 共用同一个 `MonitorBootstrap`，因此自动遵守同一锁契约。Monitor 重连 backoff 期间继续持锁，因为 runtime 仍拥有该 session 生命周期。

锁获取失败时 Monitor 启动返回稳定 `SESSION_IN_USE`，不得降级为 `CHANNEL_RESOLUTION_FAILED` 或通用 `CONNECT_FAILED`。

本阶段只允许为所有权接入调整 Bootstrap 资源管理，不改变 Monitor handler、重连、handoff、数据库或通知语义。

旧的 P6 验证 helper（listener dry-run、直接 resolver helper）不是生产入口，不得被 Admin 或生产 CLI 调用。若测试继续直接使用它们，必须显式注入 fake lease 或保持为纯测试路径，不得形成第二条生产 session 使用链路。

## 6. Online Preflight Result

```text
OnlineSessionPreflightResult
- status: pass | fail
- error_code: str | null
- session_authorized: yes | no | unknown
- channel_resolution: completed | partial | blocked_by_session | not_started
- enabled_channels: int
- resolved_channels: int
- failed_channels: int
- unattempted_channels: int
- telegram_api_accessed: yes | no
- database_accessed: no
- listener_started: no
- handler_registered: no
- session_created: no
- report_desensitized: yes
```

不输出：

- phone、用户 ID 或账号资料；
- API ID/hash；
- session path 或 auth key；
- channel numeric ID、username、title、ref；
- RPC message、exception text 或 traceback。

## 7. Frozen Watchlist Snapshot 与执行顺序

online preflight 开始时只读取一次 watchlist：

```text
load and schema-validate once
-> deep immutable snapshot
-> derive internal revision
-> static checks use this snapshot
-> channel loop uses this snapshot
```

本轮执行期间 watchlist 文件发生变化不影响当前 snapshot。`enabled_channels`、解析顺序和计数恒等式全部来自同一个 snapshot。revision 只用于内部一致性，不进入公共 DTO。

现有 `run_static_startup_preflight()` 会自行重读文件，3D-3 不得先调用它再重新加载。应抽出可接受已验证 snapshot 的纯构建函数，CLI、Admin online service 共用。

执行顺序：

1. 读取并冻结一个 watchlist snapshot；
2. 使用同一 snapshot 完成 static preflight；
3. 校验 canonical session 文件并记录 identity；
4. 非阻塞获取 session ownership flock；
5. 重复校验 session identity；
6. 获取成功后才创建 Telethon client；
7. bounded connect；
8. bounded `is_user_authorized()`；
9. 未授权立即返回，不解析频道；
10. 授权成功后逐个处理 snapshot 中的 enabled source channel；
11. finally 中 disconnect 并等待所有 owned task 收口；
12. client/task 全部收口后释放 flock。

static/path/lock 阶段失败时：

```text
telegram_api_accessed = no
session_authorized = unknown
channel_resolution = not_started or blocked_by_session
```

只有真正调用 connect 后，`telegram_api_accessed=yes`。

## 8. Timeout 预算

第一版固定：

```text
connect_timeout = 10s
authorization_timeout = 5s
channel_resolution_timeout_per_channel = 10s
disconnect_timeout = 5s
overall_timeout = 120s
operation_deadline = overall_timeout - disconnect_timeout = 115s
```

session flock 使用 `LOCK_NB`，不消耗 timeout 预算。

获取 lock 后建立绝对 overall deadline。connect、authorization 和每频道 resolve 使用：

```text
min(phase_timeout, operation_deadline_remaining)
```

操作阶段在第 115 秒请求停止，正常 cooperative 路径保留最多 5 秒给 disconnect。

整体预算耗尽返回 `ONLINE_PREFLIGHT_TIMEOUT`，不得误报为 `TELEGRAM_CONNECT_TIMEOUT`。

`120s` 是 cooperative upper bound，不是硬实时保证。`task.cancel()` 不等于底层网络或 Telethon task 已停止；若 active phase 或 disconnect 不响应取消，preflight 可以超过 120 秒，但 session flock 必须继续持有，直到所有 owned task 真正完成或确认取消。

第一版不采用独立子进程，因此不得同时承诺“严格 120 秒返回”和“锁释放后绝无后台 session task”。本阶段优先保证 session ownership 安全。

## 9. 取消与清理

`CancelledError` 不得转换为普通 preflight result。

固定语义：

```text
receive CancelledError or phase deadline
-> cancel active connect/authorization/resolve task
-> await active task settlement while retaining flock
-> start disconnect task
-> request cancellation after disconnect budget if needed
-> await disconnect task settlement while retaining flock
-> release session flock
-> re-raise CancelledError or return timeout result
```

cooperative cleanup 超过预算时记录稳定 `ONLINE_PREFLIGHT_CLEANUP_TIMEOUT`，但该错误码不授权提前释放仍被 active task 使用的 flock。

所有阶段 task 必须被 await、取消并收口，不留下无所有者 task。

## 10. 授权与错误映射

只有：

```text
await client.is_user_authorized() is False
```

才返回 `SESSION_UNAUTHORIZED`。

稳定全局错误码：

```text
STATIC_PREFLIGHT_FAILED
SESSION_PATH_INVALID
SESSION_IN_USE
SESSION_UNAUTHORIZED
SESSION_CORRUPTED
TELEGRAM_CONNECT_TIMEOUT
TELEGRAM_NETWORK_UNAVAILABLE
TELEGRAM_RPC_ERROR
TELEGRAM_DISCONNECT_FAILED
ONLINE_PREFLIGHT_TIMEOUT
ONLINE_PREFLIGHT_CLEANUP_TIMEOUT
ONLINE_PREFLIGHT_UNEXPECTED_ERROR
SESSION_STORAGE_BUSY
SESSION_STORAGE_ERROR
```

映射要求：

- connect `asyncio.TimeoutError` -> `TELEGRAM_CONNECT_TIMEOUT`；
- DNS、connection refused/reset、network unreachable 等 `OSError` -> `TELEGRAM_NETWORK_UNAVAILABLE`；
- SQLite `SQLITE_CORRUPT` / `SQLITE_NOTADB` -> `SESSION_CORRUPTED`；
- SQLite `SQLITE_BUSY` / `SQLITE_LOCKED` -> `SESSION_STORAGE_BUSY`；
- SQLite permission、readonly、cantopen -> `SESSION_PATH_INVALID`；
- SQLite 其他 I/O/storage error -> `SESSION_STORAGE_ERROR`；
- Telethon `RPCError` 及稳定子类 -> `TELEGRAM_RPC_ERROR`；
- 未知异常 -> `ONLINE_PREFLIGHT_UNEXPECTED_ERROR`；
- SQLite 分类使用异常类型与稳定 `sqlite_errorcode`，不使用 message 模糊匹配；
- 不使用任何 exception message 的自由文本作为公共分类依据。

现有 `TelethonControlledChannelResolver.disconnect()` 会吞掉异常，不适合作为 3D-3 lifecycle owner。3D-3 应新增专用 online adapter/service，或让 adapter 返回可判定 disconnect 结果；不得把 disconnect 失败静默当成功。

## 11. 频道解析

授权成功后按 frozen watchlist snapshot 的 enabled source channel 顺序处理。所有类型都必须证明当前 session 能生成 Monitor 可用的实体/access metadata：

- numeric ID：走批准的 numeric resolution path，例如 `get_input_entity(numeric_id)`，验证得到当前 session 可用的 input entity/access hash，并校验 canonical peer ID；不得仅 `int(ref)` 后计为 resolved；
- username/t.me URL：通过 resolver 获取 input entity/实体，并得到 canonical Monitor channel ID；
- invalid ref：应已被 static preflight 阻止；若仍出现，计为 failed；
- 单频道 timeout：计为 failed，稳定内部码 `CHANNEL_RESOLVE_TIMEOUT`，继续下一频道；
- not found/private/forbidden/flood wait：计为 failed，继续下一频道；
- client 全局断开、overall timeout 或全局 RPC 故障：停止循环，剩余计为 unattempted。

完成判定：

```text
completed
-> authorized
-> resolved + failed == enabled
-> unattempted == 0

partial
-> authorized
-> unattempted > 0

blocked_by_session
-> SESSION_IN_USE or SESSION_UNAUTHORIZED
-> no channel resolver calls

not_started
-> static/path/connect/authorization-exception failed before channel phase
```

计数恒等式：

```text
resolved_channels + failed_channels + unattempted_channels
== enabled_channels
```

`completed` 不表示全部频道成功；只有 `failed_channels == 0` 时顶层 `status=pass`。

## 12. CLI 与 Admin

CLI 新增显式命令：

```text
python -m app.modules.monitor.cli online-preflight --json
```

退出码：

```text
0 = pass
2 = fail
130 = SIGINT / cancellation
```

Admin 新增显式 mutation endpoint：

```text
POST /api/admin/v1/monitor/online-preflight
```

必须经过现有 local-origin、CSRF 和 JSON content-type guard。稳定业务失败仍返回 HTTP 200 + result DTO，避免把未授权、网络故障或 session in use 误当 Admin transport 错误。

概览页增加独立“在线预检”按钮和报告 dialog。现有“运行预检”继续只执行 static preflight，不能静默改成联网操作。

页面加载、Monitor start、rotation status polling 均不得自动触发 online preflight。

## 13. 并发与资源所有权

- 同一进程两个 online preflight：第二个立即 `SESSION_IN_USE`；
- Monitor 持锁时 online preflight：立即 `SESSION_IN_USE`；
- online preflight 持锁时 Monitor start：返回 `SESSION_IN_USE`；
- lock 文件存在但无 flock：允许获取；
- Monitor starting/stopping 状态不作为 lock 真值；
- 任何路径均不读取 lock 文件内容；
- client 仅在获取 lock 后创建；
- lock 仅在 disconnect 收口后释放。

## 14. Static Preflight 兼容

现有 static preflight 保持：

- 不访问 Telegram；
- 不创建 client；
- 不创建 session；
- 不获取 session flock；
- `telegram_api_accessed=no`。

新增 session 文件字段只做 `lstat` 和权限检查，不打开 SQLite 内容。原有 Admin static preflight dialog同步展示这些字段。

## 15. 测试矩阵

至少覆盖：

1. canonical `.session` suffix 解析；
2. session missing/symlink/non-regular/权限过宽返回 SESSION_PATH_INVALID；
3. static preflight 不访问 Telegram、不创建 lock/client；
4. lock 安全创建、0600、父目录 0700；
5. 遗留 lock 文件无 flock 时可获取；
6. 已持有 flock 时立即 SESSION_IN_USE；
7. lock release 后下一使用方可获取；
8. MonitorBootstrap 在 resolver connect 前持锁；
9. resolver 与 runtime client 之间锁不释放；
10. runtime reconnect/backoff 期间锁不释放；
11. runtime final disconnect 后释放；
12. resolution/startup failure 与 cancellation 均释放；
13. Monitor lock 冲突映射 SESSION_IN_USE；
14. client 只在 lock 成功后创建；
15. connect timeout/network/session corrupted/RPC 分别映射；
16. 只有 is_user_authorized false 映射 SESSION_UNAUTHORIZED；
17. 未授权时 resolver call count 为 0；
18. numeric channel 使用批准的 numeric resolution path，不因仅能解析为整数而计为 resolved；
19. 单频道 timeout 继续后续频道；
20. overall timeout 产生 partial 与正确 unattempted；
21. resolved + failed + unattempted == enabled；
22. disconnect 在成功、失败、timeout、cancellation 路径均执行；
23. cancellation 清理后重新抛出 CancelledError；
24. 不留下后台 task；
25. 报告不包含 session path、账号、numeric channel ID、ref 或异常文本；
26. CLI 退出码 0/2/130；
27. Admin endpoint 需要 CSRF 且业务失败返回 200；
28. static 与 online 两个按钮职责分离；
29. 页面加载和 polling 不自动 online preflight；
30. database/parser/dedup/notification 均未调用；
31. Monitor、Admin、Deploy 与完整回归通过。
32. numeric ID 语法合法但 session 无 entity/access metadata 时不得 resolved；
33. phase task 延迟响应 cancellation 时 flock 保持到 task 真正结束，且不留下后台 task；
34. watchlist 运行中发生 revision 变化时仍使用启动 snapshot，计数恒等式稳定；
35. SQLite busy/locked 与 corrupt/notadb 使用不同稳定错误码；
36. session 文件在首次校验后被替换时，第二次 identity 校验可检测常见 inode 变化，且不宣称完全消除同用户 TOCTOU。

## 16. 建议代码组织

```text
app/modules/monitor/session_ownership.py
  canonical path + safe flock

app/modules/monitor/online_preflight.py
  DTO + timeout orchestration + error mapping

app/modules/monitor/bootstrap.py
  production Monitor ownership integration

app/modules/monitor/cli.py
  explicit online-preflight command

app/modules/admin/router.py
  explicit online endpoint
```

不得把 online orchestration 塞入 `monitor/runtime.py`；runtime 继续只负责长期监听生命周期。

## 17. 完成标准

3D-3 完成时只能证明：

- 同一 Telethon session 在本机生产入口间具有可靠互斥所有权；
- session 授权状态可以受控检查；
- enabled source channels 可以在短生命周期内得到完整或明确 partial 结果；
- cooperative 路径在预算内完成；非 cooperative 清理优先保持 session ownership，所有结果脱敏且 lock 仅在 client/task 真正收口后释放。

不能证明：

- session 可以自动修复或自动登录；
- Monitor 已执行真实生产监听验收；
- rotation LaunchAgent 已安装或轮转成功；
- Telegram 网络长期稳定。

## 18. 当前评审结论

```text
P6-DEPLOY-3D-3_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_3D_3: yes
  allow_P6_Deploy_3D_4: no
  allow_P6_Deploy_4: no
  allow_P6_Deploy_5: no
```

设计已经独立复审批准。下一步仅允许进入 3D-3 实现和 fake/local 回归测试；不得访问真实 Telegram、安装 rotation agent、kickstart 或执行真实轮转。
