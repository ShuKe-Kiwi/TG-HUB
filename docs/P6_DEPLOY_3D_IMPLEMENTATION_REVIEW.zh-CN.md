# P6-Deploy-3D 实施评审：轮转调度、状态投影与在线 Session 预检

> 项目：tg-hub
> 阶段：P6-Deploy-3D
> 状态：design-approved / P6-Deploy-3D-1 implementation allowed
> BLOCKERS：0
> ALLOW_IMPLEMENTATION：P6-Deploy-3D-1 only
> 前置阶段：P6-Deploy-3A、3B、3C 已完成

## 1. 阶段目标

P6-Deploy-3D 只把已经完成的一次性轮转引擎接入本机 `launchd` 调度，并补齐只读状态投影、在线 Telegram session 授权预检和真实安装验收。

固定链路：

```text
launchd hourly schedule
-> backend/.venv/bin/python -m app.deploy.rotate_logs
-> RotationEngine.run()
-> rotation-status.json
-> CLI / Admin read-only projection
```

在线预检链路：

```text
static preflight
-> session path validation
-> bounded Telegram connect
-> is_user_authorized()
-> resolve enabled source_channels
-> disconnect in finally
```

本阶段不重新设计轮转状态机，不修改 Monitor、ingestion、Parser、Normalizer、Dedup、EventBus 或 Bot 业务链路。

## 2. 交付范围

允许实现：

- `com.tghub.rotate-logs` LaunchAgent template；
- rotation agent 的安装、卸载、状态检查和 dry-run；
- 每小时调用一次 3C 一次性轮转引擎；
- `rotation-status.json` 的安全只读解析与脱敏状态投影；
- CLI 与管理台共同使用的 rotation status DTO；
- online session authorization preflight；
- session、网络、RPC、损坏文件的稳定错误分类；
- 真实 LaunchAgent 安装和至少一次受控真实轮转验收；
- 部署与使用说明更新。

明确禁止：

- 修改 3C 的 pending phase、retention、budget 或 copy-truncate 语义；
- 为追求零丢失而自动停止主服务；
- 自动登录 Telegram、请求验证码或创建新 session；
- 自动修改、替换或删除现有 session；
- 在轮转 agent 中访问数据库或业务服务；
- 在管理台增加手工删除 archive、修改预算或编辑路径；
- 静默删除、重写或补脱敏历史日志；
- Deploy-4 备份恢复与 Deploy-5 最终交付内容。

## 3. Rotation LaunchAgent 契约

Label 固定为：

```text
com.tghub.rotate-logs
```

执行参数固定为绝对路径：

```text
<backend>/.venv/bin/python
-m
app.deploy.rotate_logs
```

plist 必须设置：

- `WorkingDirectory = <backend>`；
- `TG_HUB_ENV_FILE = ~/.tg-hub/production.env`，或安装时显式传入的同一生产配置文件；
- `StartInterval = 3600`；
- `RunAtLoad = false`；
- `ProcessType = Background`；
- 不设置 `KeepAlive`；
- 不包含 Telegram、数据库或 Bot 的任何敏感值；
- stdout/stderr 均指向 `/dev/null`，运行结果以 `rotation-status.json` 为准。

不使用 shell profile、`PATH`、调用者 `PWD` 或 `PYTHONPATH`。agent 的一次执行必须有界，完成后退出；不得变成长驻进程。

退出码固定沿用 3C CLI：

```text
0 = pass
1 = partial
2 = fail
```

`launchd` 的非零退出只表示本轮需要检查状态文件，不触发 `KeepAlive` 重试。下一次重试由下一小时调度承担，避免失败风暴。

首次执行语义固定为：

- 安装和 `bootstrap` 完成后不自动执行真实轮转；
- 第一次系统调度可能最多等待一个 `StartInterval`；
- 首次真实轮转必须在用户单独批准后通过 3C CLI 显式执行；
- 调度验收允许使用 `launchctl kickstart gui/$UID/com.tghub.rotate-logs`；
- `kickstart` 只触发 agent 进程，不绕过 `rotate.lock`，也不改变后续周期；
- 如果刚完成真实轮转且未再达到阈值，agent 返回 `pass/not_modified` 也是合法验收结果；
- 不为验收伪造文件大小、修改 generation time 或缩短生产阈值。

## 4. 安装与卸载

新增文件职责：

```text
backend/deploy/com.tghub.rotate-logs.plist.template
backend/deploy/install_rotation.sh
backend/deploy/uninstall_rotation.sh
backend/deploy/rotation_status.sh
```

状态解析职责固定为：

```text
app.deploy.rotation_status.read_rotation_status()
-> RotationStatusProjection
-> shared by CLI and Admin
```

`rotation_status.sh` 只能调用 Python status CLI，不得使用 `jq`、`grep` 或 shell 自行解析 schema。Admin 必须调用同一个 Python reader/service，禁止存在第二套 status 校验和 stale 逻辑。

安装流程固定为：

```text
validate production env
-> validate python/module import
-> validate runtime/log directories
-> render plist to sibling temp
-> plutil -lint
-> chmod 0600
-> atomic rename into ~/Library/LaunchAgents
-> launchctl bootstrap gui/$UID
-> launchctl print verification
```

安装脚本必须：

- 支持 `--dry-run`，且 dry-run 不创建文件、不 bootstrap；
- 已安装时返回稳定 `ROTATION_AGENT_ALREADY_INSTALLED`，不得覆盖活动 plist；
- bootstrap 失败时只回滚本次创建的 plist；
- 不启动、停止或重启 `com.tghub.service`；
- 不直接执行真实轮转。

安装回滚边界固定为：

```text
target plist already exists
-> ROTATION_AGENT_ALREADY_INSTALLED
-> do not overwrite
-> do not delete

target absent before install
-> render + atomic rename succeeded
-> bootstrap/verification failed
-> bootout when launchctl may have partially loaded the job
-> confirm launchctl print is absent
-> delete only the plist created by this invocation
```

若 `bootout` 或不存在确认失败，不得删除 plist 并声称回滚成功，必须返回 `ROTATION_AGENT_ROLLBACK_FAILED`。安装成功后安全原子写入 `~/.tg-hub/runtime/rotation-agent.json`，只包含 `schema_version`、`installed_at` 和 `label`，权限 `0600`；它是 `never_run` grace period 的时间依据，不包含路径或敏感配置。

卸载脚本只执行 rotation agent 的 `bootout` 和 plist 删除；不得删除 archive、active log、heartbeat、lock、pending、pending.meta 或 rotation status。

## 5. 状态投影

3D 只读现有 `~/.tg-hub/runtime/rotation-status.json`，不扩展 3C 持久化 schema。新增内部投影 DTO：

```text
RotationStatusProjection
- available: bool
- status: never_run | pass | partial | fail | invalid
- error_code: str | null
- last_started_at: datetime | null
- last_completed_at: datetime | null
- rotated_files: int
- cleaned_archives: int
- archive_bytes: int
- active_bytes: int
- archive_budget_status: within_budget | cleaned | exceeded_unrecoverable | unknown
- active_oversize: bool
- legacy_content_possible: bool
- stale: bool | not_applicable
```

读取规则：

- 不存在时返回 `never_run`，不是故障；
- symlink、非普通文件、权限过宽、JSON 损坏或 schema 不支持时返回 `invalid`；
- 读取失败不得使 `/health/live` 或 `/health/ready` 返回失败；
- 不返回绝对路径、archive 文件名、run ID、异常文本或文件内容；
- 管理台读取不得触发轮转或文件写入；
- `stale` 仅在 rotation agent 已安装时判断，阈值固定为 2 小时 15 分钟；未安装时为 `not_applicable`。

`stale` 判定固定为：

```text
agent_installed == false
-> stale = not_applicable

agent_installed == true AND status == never_run
-> now <= installed_at + 2h15m: stale = false
-> now > installed_at + 2h15m: stale = true

agent_installed == true AND status file valid
-> now - check_completed_at > 2h15m: stale = true
-> otherwise: stale = false
```

禁止使用 `rotation-status.json`、plist 或日志文件的 mtime 判定 stale。`status=fail` 与 `stale` 相互独立：刚执行失败是 `fail + stale=false`，长期没有新结果才是 `stale=true`。

管理台第一版只展示：最近轮转状态、最近完成时间、归档占用、预算状态和 active oversize。不得提供“立即轮转”按钮。

## 6. 首次真实轮转与历史日志

当前 active application log 可能包含 3A 迁移前的非 JSON 历史行。3D 固定选择：

```text
first_real_rotation_strategy = legacy_content_possible
```

规则：

- 不迁移、不删除、不重写现有 active 日志；
- 首次涉及已有 application log 的 archive 必须保留 `legacy_content_possible = true`；
- 首次 archive 不以“逐行全部为 JSON”作为通过门槛；
- 仍必须验证 gzip 可读、archive 非空时字节可恢复、active inode 不变且主服务继续写入；
- 后续由纯 3A structured logging generation 产生的 archive 才执行逐行 JSON 验证；
- 验收报告必须明确区分 legacy archive 与 clean JSON archive。

不得为获得绿色验收结论而清空真实日志。

## 7. Online Session Preflight

static preflight 保持纯本地，不访问 Telegram。online preflight 是独立显式操作，不并入应用启动 preflight，也不阻塞管理台服务启动。

新增结果契约：

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
- telegram_api_accessed: yes
- database_accessed: no
- listener_started: no
- report_desensitized: yes
```

执行顺序必须固定：

1. 运行 static preflight；
2. 校验 session 路径存在、父目录安全且 session 文件为普通文件；
3. 使用有限超时连接 Telegram；
4. 调用 `is_user_authorized()`；
5. 未授权则立即返回 `blocked_by_session`；
6. 仅在授权成功后解析 enabled source channels；
7. `finally` 中有界 disconnect。

超时预算固定为独立阶段配置，第一版默认值：

```text
connect_timeout = 10s
authorization_timeout = 5s
channel_resolution_timeout_per_channel = 10s
overall_timeout = 120s
disconnect_timeout = 5s
```

`overall_timeout` 覆盖获取 session lock 之后到 disconnect 收口的整个 online preflight。connect 超时返回 `TELEGRAM_CONNECT_TIMEOUT`；单频道超时计为该频道稳定失败并继续；整体预算耗尽返回独立 `ONLINE_PREFLIGHT_TIMEOUT`，未执行频道计入 `unattempted_channels`。

稳定错误码至少包含：

```text
SESSION_UNAUTHORIZED
TELEGRAM_CONNECT_TIMEOUT
TELEGRAM_NETWORK_UNAVAILABLE
TELEGRAM_RPC_ERROR
SESSION_CORRUPTED
SESSION_PATH_INVALID
SESSION_IN_USE
ONLINE_PREFLIGHT_TIMEOUT
```

错误映射要求：

- connect 超时不能映射成 `SESSION_UNAUTHORIZED`；
- DNS、拒绝连接、网络不可达映射为 `TELEGRAM_NETWORK_UNAVAILABLE`；
- SQLite/session 解码或 auth key 损坏映射为 `SESSION_CORRUPTED`；
- Telegram RPC 异常映射为 `TELEGRAM_RPC_ERROR`；
- 只有 `is_user_authorized() == false` 才返回 `SESSION_UNAUTHORIZED`；
- 未授权时不得逐频道 resolver，避免生成重复频道错误；
- 不输出 phone、session 路径、API ID/hash、频道 numeric ID、原始异常文本。

频道完成判定固定为：

- `completed`：授权通过，所有 enabled channel 均得到成功或稳定失败结果，`resolved + failed == enabled`；
- `partial`：整体超时或全局故障导致部分频道未执行，`unattempted > 0`；
- `blocked_by_session`：session 未授权或 ownership lock 被占用，未调用 resolver；
- `not_started`：static/path/connect 阶段失败，未进入授权后的频道阶段。

online preflight 不注册 `NewMessage` handler，不进入长期监听，不写数据库，不写 watchlist。

## 8. 并发与生命周期

- rotation agent 与人工 CLI 可并发启动，但由 `rotate.lock` 保证最多一个实际执行；
- `ROTATION_ALREADY_RUNNING` 是稳定可观察结果，不做忙循环重试；
- heartbeat 轮转继续遵守 `rotate.lock -> heartbeat.lock`；
- 所有 Telethon session 使用方通过 `~/.tg-hub/runtime/telethon-session.lock` 获取同一个 exclusive flock；
- Monitor 在创建/连接 client 前获取锁，并持续持有到最终 disconnect 完成；
- CLI Monitor 和 Admin Monitor 遵守同一锁契约；
- online preflight 在创建 client 前非阻塞尝试获取同一锁，失败返回 `SESSION_IN_USE`；
- online preflight 使用独立短生命周期 Telethon client；
- online preflight 不复用正在运行 Monitor 的 client，也不停止 Monitor；
- lock 文件存在本身不表示占用，只以内核 flock 结果为准；
- 不读取 PID 文本、不 kill 其他进程、不使用 Monitor 状态猜测所有权；
- 所有 connect、authorization、resolve 和 disconnect 均必须有界。

session lock 获取顺序固定为：

```text
static/path validation
-> acquire telethon-session.lock exclusive flock
-> create/connect client
-> authorization check
-> channel resolution or long-running monitor ownership
-> disconnect
-> release flock
```

本阶段允许对 Monitor 做最小 session ownership lock 接入，但不得修改重连、handler、handoff 或业务语义。进程崩溃后内核自动释放 flock；遗留 lock 文件不构成占用。

## 9. 真实安装验收顺序

真实验收必须分步执行，不得一次脚本完成全部不可逆操作：

1. 完整测试和静态检查通过；
2. rotation install `--dry-run` 通过；
3. `plutil -lint` 通过且 plist 无敏感值；
4. 安装 rotation LaunchAgent；
5. `launchctl print gui/$UID/com.tghub.rotate-logs` 可见；
6. 确认主 `com.tghub.service` PID 和健康状态未受影响；
7. 先执行 3C `--dry-run` 并归档报告；
8. 用户明确批准后执行一次真实轮转；
9. 验证 active inode、后续日志增长、gzip、权限、pending 清理与 status；
10. 等待或受控触发一次 LaunchAgent 调度，验证状态更新；
11. 执行 online session preflight，验证授权与频道解析分类；
12. 保持主 Monitor 原运行状态，不因验收擅自停止。

真实轮转和 online Telegram 访问都属于外部状态变更，执行时必须单独获得批准。

## 10. 测试矩阵

至少覆盖：

1. rotation plist 使用绝对 python、固定 `-m` 模块和 WorkingDirectory；
2. plist 无敏感值、无 KeepAlive、StartInterval 为 3600；
3. install dry-run 只读；
4. 重复安装稳定拒绝；
5. bootstrap 失败回滚本次 plist；
6. uninstall 不删除任何运行数据；
7. rotation status 不存在返回 never_run；
8. status symlink、权限过宽、非普通文件、非法 JSON、未知 schema 返回 invalid；
9. status projection 不泄漏路径、run ID、archive 名或异常文本；
10. rotation status 失败不影响 live/ready；
11. stale 在未安装时为 not_applicable，在安装后按阈值判定；
12. legacy 首次 archive 正确标记，不要求逐行 JSON；
13. clean generation archive 逐行 JSON 可解析；
14. static preflight 不访问 Telegram；
15. online preflight 未授权返回 SESSION_UNAUTHORIZED 且不 resolve channels；
16. connect timeout、network、RPC、corrupted session、invalid path 分别映射；
17. online preflight finally disconnect；
18. online preflight 不注册 handler、不监听、不访问数据库；
19. Monitor 运行时 session 并发策略稳定；
20. rotation lock 冲突稳定返回，不重试风暴；
21. 主 LaunchAgent 在 rotation agent 安装、运行和卸载后持续健康；
22. Deploy、Monitor、Admin 相关回归与完整测试通过；
23. 真实 LaunchAgent 至少一次调度与一次真实轮转通过。
24. Monitor 持有 session flock 时 online preflight 返回 SESSION_IN_USE；遗留 lock 文件不误报，结束后正确释放；
25. 安装不自动轮转，kickstart 遵守 rotate.lock，not_modified 也产生合法状态；
26. stale 基于 installed_at/check_completed_at，不基于 mtime，fail 与 stale 独立；
27. bootstrap 部分成功时先 bootout 再删除本次 plist，绝不删除预先存在 plist；
28. CLI 与 Admin 共用 `app.deploy.rotation_status` reader；
29. online overall timeout 保证总时长有界，未执行频道只计入 unattempted。

## 11. 建议实施拆分

```text
P6-Deploy-3D-1
rotation LaunchAgent template + install/uninstall/status scripts

P6-Deploy-3D-2
rotation status read-only projection + Admin display

P6-Deploy-3D-3
online session authorization preflight

P6-Deploy-3D-4
real installed acceptance + controlled real rotation
```

每个子阶段单独测试和提交。3D-4 之前不得对真实 active 文件执行轮转。

## 12. 完成标准

P6-Deploy-3D 完成时只能得出：

- 本机 rotation LaunchAgent 可以按小时有界调用 3C engine；
- 轮转状态能被安全、脱敏地观察；
- session 授权与频道解析前置错误可稳定区分；
- 主服务在真实轮转后仍持续写入和保持健康；
- legacy 首次 archive 被明确标记，没有静默删除历史。

不能得出：

- copy-truncate 零丢失；
- 整个日志目录存在 250 MiB 绝对硬上限；
- Telegram session 可自动修复或自动登录；
- 已具备备份恢复或跨机器部署能力。

## 13. 当前评审结论

```text
P6-DEPLOY-3D_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_3D_implementation: yes
  allow_P6_Deploy_3D_1: yes
  allow_P6_Deploy_3D_2: no
  allow_P6_Deploy_3D_3: no
  allow_P6_Deploy_3D_4: no
  allow_P6_Deploy_4: no
  allow_P6_Deploy_5: no
```

独立复审已批准。当前只允许进入 P6-Deploy-3D-1，不得直接执行真实轮转，不得提前进入状态投影、online session preflight 或真实安装验收。
