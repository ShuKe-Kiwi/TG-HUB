# P6-Deploy-3D-4 真实安装与受控验收评审

> 项目：tg-hub
> 阶段：P6-Deploy-3D-4
> 状态：design-locked
> ALLOW_REAL_ACTIONS：no
> 前置：P6-Deploy-3D-1/2/3 已实现并分别提交

## 1. 阶段目标

P6-Deploy-3D-4 不增加新的业务能力，只验证已经实现的部署链路在当前本机真实环境中成立：

```text
rotation LaunchAgent install
-> launchd visible
-> launchd-managed one-shot execution
-> controlled real rotation
-> active logs continue growing
-> rotation status observable

explicit online preflight
-> session ownership
-> authorization
-> controlled channel resolution
```

本阶段通过后，只能证明本机部署和当前 Telegram session 在验收时可用，不能证明网络、频道权限或服务永远稳定。

## 2. 固定边界

允许验收：

- rotation install `--dry-run`；
- plist lint、字段和敏感值检查；
- rotation agent 真实安装；
- `launchctl print` 只读确认；
- 3C rotation engine `--dry-run`；
- 一次用户明确批准的真实轮转；
- 一次用户明确批准且归入 Gate D 的 rotation agent kickstart；
- 一次用户明确批准的 online session preflight；
- 主服务 PID、health、日志增长和状态文件的前后对比；
- 验收报告归档。

禁止：

- 自动登录 Telegram、索要或处理验证码；
- 注册额外 `NewMessage` handler；
- 停止主 Monitor 来规避 `SESSION_IN_USE`；
- 修改 watchlist、数据库或业务处理链；
- 修改轮转阈值、generation time 或制造假日志来强制命中；
- 删除 active、archive、pending、session 或历史状态文件；
- 把真实轮转与 online Telegram 访问串成一个不可中断脚本；
- 进入 Deploy-4 或 Deploy-5。

## 3. 外部动作分级

### Gate A：只读基线

无需改变外部状态：

1. `git status --short`；
2. 完整测试结果确认；
3. `install_rotation.sh --dry-run`；
4. `plutil -lint` template；
5. 检查 template 无 secret、无 `KeepAlive`、`RunAtLoad=false`、`StartInterval=3600`；
6. 读取主服务 `launchctl print`、PID、HTTP health；
7. 读取 rotation status；
8. 记录 active 文件 inode、size、mode，不读取或输出日志正文。

Gate A 不安装 agent、不联网、不轮转。

### Gate B：rotation agent 安装

属于本机 launchd 状态变更，必须单独批准：

```text
backend/deploy/install_rotation.sh
-> launchctl bootstrap
-> launchctl verification
-> rotation-agent.json atomic commit
```

安装后必须确认：

- plist mode 为 `0600`；
- job 可由 `launchctl print gui/$UID/com.tghub.rotate-logs` 读取；
- `RunAtLoad=false`，安装本身没有执行轮转；
- `rotation-agent.json` schema、label、UTC `installed_at` 正确；
- 主服务 PID、health/readiness 和 active 日志未受影响；
- rotation status 对当前 installation generation 必须投影为 `never_run`，且 `stale=false`。

旧 `rotation-status.json` 可以继续保留为历史文件，但 `check_completed_at < installed_at` 时不得投影为当前 generation 的执行状态。若安装过程中出现当前 generation 的新执行记录，视为未授权执行，立即停止验收。

安装失败应使用 3D-1 已实现的回滚，不手工删除预先存在的 plist。

### Gate C：LaunchAgent 可执行性静态验收

Gate C 不执行 production agent kickstart，只静态确认：

- plist lint；
- `launchctl print` 可见；
- `ProgramArguments` 使用绝对 Python、固定 `-m app.deploy.rotate_logs`；
- `WorkingDirectory` 与 `EnvironmentVariables` 正确；
- `RunAtLoad=false`；
- `StartInterval=3600`；
- job 已配置但当前未因验收而执行；
- 主服务持续健康。

3C `--dry-run` 仍可执行，但只用于记录 `rotation_dry_run_prediction`，不构成 kickstart 的权限隔离保证。dry-run 与后续进程之间存在 TOCTOU：size 或 generation age 可能跨过阈值。

因此任何 production rotation agent kickstart 都必须进入 Gate D。Gate C 不能证明 one-shot 真实启动成功，也不能证明自然 `StartInterval` 调度已发生。

### Gate D：受控真实轮转

这是可能执行不可逆 active 文件变更的真实动作，必须单独批准。执行前必须明确二选一：

```text
direct_cli_rotation:
  backend/.venv/bin/python -m app.deploy.rotate_logs

launchagent_triggered_rotation:
  launchctl kickstart gui/$UID/com.tghub.rotate-logs
```

两种方式都必须按“可能真实轮转”授权。`launchagent_kickstart` 即使 dry-run 预测 `not_modified`，也可能在实际执行时跨过 size/age 阈值并轮转；用户授权必须同时接受 `not_modified` 与 `rotated` 两种合法结果。

不得通过修改阈值或 generation metadata 强制触发。若选择 direct CLI，`launchagent_kickstart_verified=no`，阶段最多为 partial，直到后续另行批准 LaunchAgent 真实触发。若选择 LaunchAgent kickstart，则验收：

- job 按 one-shot 语义启动并退出；
- `check_started_at`、`check_completed_at` 属于当前 installation generation；
- `rotation-status.json` 可由唯一 reader 解析；
- rotate lock 冲突时稳定失败，不并发执行第二个 rotator；
- `launchagent_kickstart_verified=yes`；
- 主服务持续健康。

kickstart 仍不能证明系统已经自然执行 `StartInterval=3600`；第一版无需等待一小时观察自然周期。

执行前快照：

- target active inode、size、mode；
- archive 数量与总字节数；
- pending/pending.meta 集合；
- rotation status 摘要；
- 主服务 PID、health/readiness；
- heartbeat writer 是否活跃。

执行后验证：

- application copy-truncate 的 active inode 不变；
- heartbeat locked-rename 按设计产生新 generation；
- active 在有限观察窗口内继续增长，或稳定记录 `not_observed`；
- final archive 为 gzip、mode `0600`；
- clean generation 的完整行逐行可解析；
- legacy 首次 archive 明确标记 `legacy_content_possible`；
- 无遗留 pending/pending.meta，或稳定报告 `ROTATION_RECOVERY_REQUIRED`；
- archive budget 只清理 final archive；
- status 与实际统计一致；
- 主服务 PID、health/readiness 无非预期变化。

不宣称 copy-truncate 零丢失。报告必须保留：

```text
rotation_consistency: best_effort_copy_truncate
concurrent_write_loss_possible: true
writer_paused: false
```

增长观察窗口固定为：

- application logs：只允许通过正常 health/status 请求触发既有应用访问日志，不注入伪造日志内容；
- heartbeat：最多等待 `max(2 * heartbeat_interval, 120s)`；
- 窗口内没有自然写入证据时记录 `active_growth_continued=not_observed`，不写成 `no`；
- `no` 只用于明确发现 writer 仍写旧 generation/inode 或出现写入错误；
- 未轮转 target 记录 `not_applicable`。

### Gate E1：session ownership contention 验收

Monitor 正在运行时显式执行：

```text
backend/.venv/bin/python -m app.modules.monitor.cli online-preflight --json
```

预期结果必须为：

```text
status=fail
error_code=SESSION_IN_USE
telegram_api_accessed=no
session_authorized=unknown
```

E1 通过只证明 session ownership isolation 成立，不能证明 Telegram 网络、session authorization 或频道解析可用。验收不得为了 E1 停止 Monitor。

### Gate E2：完整 online session 验收

这是显式 Telegram 网络访问，必须单独批准，并且只在以下前提成立时执行：

```text
Monitor 已由用户在本次验收之外正常停止
and
telethon-session.lock 当前空闲
```

验收流程不得主动停止 Monitor。执行命令与 E1 相同。

执行前只读确认：

- 主 Monitor 是否持有 `telethon-session.lock`；
- session 文件和父目录权限符合契约；
- watchlist revision 已记录；
- 不输出 session path、账号、username、频道 ID 或异常文本。

成功门槛：

- `session_authorized=yes`；
- `resolved + failed + unattempted == enabled`；
- 全部成功时顶层 `status=pass`；
- 部分失败时稳定为 `partial/fail`，不得伪造 pass；
- finally disconnect 完成后 session flock 可重新获取；
- 未注册 handler、未监听、未访问数据库、未通知 Bot。

阶段结论：

```text
E1 pass + E2 not_run
-> P6-Deploy-3D-4 partial
-> ownership isolation passed
-> Telegram availability not verified

E2 pass or stable partial channel result
-> full online acceptance complete

E2 fail
-> phase fail/blocked according to stable error_code
```

## 4. 顺序与停止条件

固定执行顺序：

```text
Gate A read-only baseline
-> review result
-> explicit approval for Gate B
-> install and verify
-> Gate C static LaunchAgent verification only
-> 3C dry-run prediction for reporting only
-> explicit approval for Gate D trigger choice
-> controlled real rotation and verify when authorized
-> explicit approval for Gate E1 ownership contention
-> E1 expected SESSION_IN_USE
-> E2 only after user independently stops Monitor and separately approves Telegram access
-> final report
```

出现以下任一情况立即停止，不继续下一 Gate：

- 主服务 PID 非预期变化；
- health/readiness 下降；
- install rollback 不完整；
- metadata/status 无法安全读取；
- pending recovery required；
- active/archive 权限异常；
- lock 所有权不明确；
- 验收结果需要删除数据或停止主服务才能继续。

## 5. 脱敏验收报告

```text
P6-DEPLOY-3D-4_RESULT:
- baseline_status: pass/fail
- full_regression: pass/fail/not_run
- rotation_install_dry_run: pass/fail
- plist_lint: pass/fail
- secrets_exposed: no
- main_service_before: healthy/unhealthy/unknown
- rotation_agent_installed: yes/no
- rotation_agent_visible: yes/no/not_checked
- install_metadata: valid/invalid/not_present
- rotation_kickstarted: yes/no
- rotation_dry_run_prediction: rotation/not_modified/blocked
- rotation_trigger: direct_cli/launchagent_kickstart/not_run
- launchagent_kickstart_verified: yes/no
- natural_interval_execution_observed: yes/no/not_required
- rotation_execution: rotated/not_modified/fail/not_run
- active_inode_preserved: yes/no/not_applicable
- active_growth_continued: yes/no/not_observed/not_applicable
- archive_valid: yes/no/not_applicable
- pending_recovery_required: yes/no
- rotation_status_valid: yes/no/not_checked
- session_ownership_acceptance: pass/fail/not_run
- full_online_preflight: pass/partial/fail/not_run
- session_authorized: yes/no/unknown
- enabled_channels:
- resolved_channels:
- failed_channels:
- unattempted_channels:
- telegram_api_accessed: yes/no
- database_modified: no
- watchlist_modified: no
- monitor_stopped_by_acceptance: no
- recommend_allow_P6_Deploy_4: yes/no
- blockers:
  - ...
```

报告不得包含路径、PID、inode 原值、日志正文、频道引用、numeric channel ID、session 内容、Telegram 账号信息或 exception text。PID/inode 只比较 `same/changed`。

## 6. 完成标准

3D-4 通过必须同时满足：

- rotation agent 安装与 launchd 可见性通过；
- 至少一次经 Gate D 授权的 launchd kickstart one-shot 执行有合法状态；
- 至少一次自然触发的真实轮转通过；
- 主服务在安装、调度和轮转前后保持健康并继续写日志；
- rotation status 对当前 installation generation 可读；
- session ownership contention 验收通过；
- full online session preflight 已在 session 空闲条件下完成；
- 验收过程未停止主 Monitor、未改数据库/watchlist、未自动登录。

若 Gate D 只执行 direct CLI 而未验证 LaunchAgent kickstart、当前日志没有自然达到轮转条件，或只有 E1 `SESSION_IN_USE` 而 E2 未执行，3D-4 只能标记 `partial`，且 `recommend_allow_P6_Deploy_4=no`。不得为了完成阶段伪造轮转条件或主动停止 Monitor。

## 7. 当前评审结论

```text
P6-DEPLOY-3D-4_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 0
  allow_read_only_baseline: yes
  allow_rotation_agent_install: no
  allow_rotation_kickstart: no
  allow_real_rotation: no
  allow_real_telegram_access: no
  allow_P6_Deploy_4: no
  allow_P6_Deploy_5: no
```

上一轮 Gate C TOCTOU blocker 已按方案 B 收口：Gate C 只做静态检查，任何 production kickstart 均属于 Gate D 真实动作。设计已锁定；当前仍只允许执行 Gate A 只读基线，Gate B、D、E1、E2 均未授权。
