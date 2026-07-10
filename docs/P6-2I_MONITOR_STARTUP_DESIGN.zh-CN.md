# P6-2I Monitor Startup / CLI / 使用说明设计

> 状态：设计锁定
> 范围：提供长期 monitor 的受控启动入口、退出语义、summary 输出和使用说明
> 不包含：Web UI、Outbox、retry queue、history reconcile、订阅系统、媒体下载、生产部署守护进程

## 阶段位置

| 阶段 | 目标 | 状态 |
|------|------|------|
| P6-2E | Monitor -> RawMessage ingestion boundary | 已完成 |
| P6-2F | RawMessage -> Parser / Normalizer / Dedup 编排 | 已完成 |
| P6-2G | EventBus / Bot 查询通知接入 | 已完成 |
| P6-2H | Monitor -> ingest -> process handoff 编排 | 已完成 |
| P6-2I | runtime start command / 使用说明 | 本文档定义 |

P6-2I 只能回答：

> 如何从终端启动长期 monitor，并让它按既定边界装配 watchlist、Telethon client、ingestion boundary、processing boundary、EventBus 和 Bot notification handler？

P6-2I 不能回答：

> 如何做 Web 可视化、可靠队列、失败补偿、多实例调度、Outbox 投递、用户订阅或生产级进程管理。

## 固定目标

P6-2I 的核心目标是把已完成链路包装为一个可受控运行入口：

```text
CLI command
-> load Settings
-> preflight / config validation
-> assemble EventBus
-> assemble Bot notification handlers when configured
-> assemble IncomingMessageIngestionBoundary
-> assemble RawMessageProcessingBoundary(event_bus)
-> run_monitor_runtime_from_settings(...)
-> handle SIGINT / SIGTERM
-> print final summary
-> return stable exit code
```

CLI 入口只做装配和生命周期，不做业务处理。

## Composition Root

P6-2I 固定唯一 composition root：

```text
app.modules.monitor.bootstrap
```

CLI 层只负责参数解析、输出格式、exit code 映射和进程信号处理；具体依赖装配放入 bootstrap 层。

建议文件组织：

```text
app/modules/monitor/
├── cli.py
├── bootstrap.py
├── runtime.py
├── preflight.py
└── heartbeat.py
```

职责固定：

- `cli.py`：parse args、map exit code、print stdout/stderr、signal handling
- `bootstrap.py`：create EventBus、create boundaries、register optional notification handlers、create runtime
- `preflight.py`：static startup checks
- `heartbeat.py`：heartbeat sink implementations

不得把业务构造、信号处理、输出格式、preflight 检查全部堆入 `cli.py`。

## 建议入口

第一版固定为 Python module 入口，不引入新的 CLI 框架依赖：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -m app.modules.monitor.cli run
```

原因：

- 项目当前没有 `typer` / `click` 依赖
- `backend/pyproject.toml` 当前没有 console scripts 配置
- `python -m` 入口最小、明确、便于测试
- 后续可再包装成 `tg-hub-monitor run`

P6-2I 不要求新增 console script。

## CLI 子命令

P6-2I 第一版只要求两个子命令：

```text
run
preflight
```

### run

启动长期 monitor：

```bash
./.venv/bin/python -m app.modules.monitor.cli run
```

职责：

- 读取 `backend/.env` 与环境变量
- 加载 `WATCHLIST_PATH`
- 执行 static startup preflight
- 执行 bounded DB readiness check
- 装配 ingestion boundary
- 装配 processing boundary
- 装配 EventBus
- 按 Bot 配置注册 Resource notification handler
- 启动 `run_monitor_runtime_from_settings()`
- 捕获 SIGINT / SIGTERM 并调用 `runtime.stop()`
- 输出 final summary
- 按 summary 返回 exit code

`run` 启动阶段允许做一次有限 DB readiness check。该检查只验证数据库连接是否可用，不写入业务数据，不调用 Parser / Normalizer / Dedup，不发布 EventBus 事件。

### preflight

只做启动前检查，不连接 Telegram、不注册 handler、不长期运行：

```bash
./.venv/bin/python -m app.modules.monitor.cli preflight
```

职责：

- 调用 `StaticStartupPreflight`
- 检查 Telethon 依赖
- 检查 Telegram API 配置
- 检查 session parent directory
- 检查 watchlist 文件存在与 schema
- 检查数据库配置字符串存在
- 输出脱敏 JSON report

`preflight` 必须是纯本地静态检查。如果现有 runtime preflight 包含 Telegram connect、channel resolve 或 DB connect，CLI 的 `preflight` 不得直接复用这部分逻辑，只能复用其中的静态检查函数。

`preflight` 不做：

- Telegram API 连接
- channel resolve
- DB 连接
- RawMessage 写入
- Parser / Dedup
- Bot 通知

## 运行参数

第一版 CLI 参数保持很少：

```text
--summary-json
--preflight-json
--heartbeat-jsonl <path>
--no-bot-notify
```

固定含义：

- `--summary-json`：final summary 以 JSON 输出到 stdout
- `--preflight-json`：preflight report 以 JSON 输出到 stdout
- `--heartbeat-jsonl <path>`：把 heartbeat 逐行 JSON 写入指定文件
- `--no-bot-notify`：仍创建 EventBus，但不注册 Bot notification handler

不做：

- 不允许 CLI 参数覆盖 watchlist 内容
- 不在 CLI 中增删频道
- 不在 CLI 中增删 watch_titles
- 不在 CLI 中修改数据库
- 不在 CLI 中执行 backfill

频道和资源名第一版仍来自：

```text
~/.tg-hub/watchlist.json
```

## 配置来源

固定配置来源：

```text
backend/.env
environment variables
~/.tg-hub/watchlist.json
```

必需配置：

```text
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION_NAME
DATABASE_URL
```

Watchlist 路径规则：

```text
WATCHLIST_PATH 可配置
默认值为 ~/.tg-hub/watchlist.json
```

因此 `WATCHLIST_PATH` 不是强制必填项；但最终展开后的 watchlist 文件必须存在且 schema 有效。

`TELEGRAM_SESSION_NAME` 在 P6-2I 中按 logical session name 或 session file path 兼容处理，但输出中只能报告：

```text
session_configured: yes/no
session_parent_writable: yes/no
```

不得输出完整 session path。

Bot 通知相关配置：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_NOTIFY_CHAT_IDS
```

Bot webhook 查询相关配置：

```text
TELEGRAM_WEBHOOK_SECRET
TELEGRAM_ALLOWED_CHAT_IDS
```

P6-2I 启动 monitor 不要求 webhook 必须可用；monitor notification 只需要 Bot token 与 notify chat ids。

## Preflight Report 与 Runtime Summary

P6-2I 固定使用两个独立模型：

```text
MonitorStartupPreflightReport
MonitorRuntimeSummary
```

不得让 preflight 伪造 runtime summary。

原因：

- preflight 没有 runtime
- preflight 没有 handler
- preflight 没有 uptime
- preflight 没有 shutdown reason
- preflight 没有事件计数

CLI 输出层可以共享脱敏序列化器，但模型语义必须分开。

## 装配边界

CLI 可以装配具体 application boundary：

```text
InMemoryEventBus
IncomingMessageIngestionBoundary
RawMessageProcessingBoundary(event_bus)
ResourceNotifyHandler
TelegramBotTransport
```

但 MonitorRuntime 仍只接收协议对象：

```text
MonitorRuntime(
  ingestion_boundary=ingestion_boundary,
  processing_boundary=processing_boundary,
)
```

CLI 不得让 MonitorRuntime 直接持有：

- `AsyncSession`
- Repository
- `RawMessageService`
- Parser
- Normalizer
- Dedup
- Bot handler
- Bot transport

## Bot Notification 装配

如果 Bot 通知配置完整：

```text
TELEGRAM_BOT_TOKEN present
TELEGRAM_NOTIFY_CHAT_IDS non-empty
--no-bot-notify not set
```

CLI 可以：

```text
event_bus.subscribe(ResourceCreated, ResourceNotifyHandler.handle_created)
event_bus.subscribe(ResourceMerged, ResourceNotifyHandler.handle_merged)
```

固定规则：

- 每个 notification handler 自己创建短生命周期 DB session
- 通知失败由 P5/P6-2G 既有隔离逻辑处理
- CLI 不统计 notification_sent
- Monitor summary 不反推通知结果
- `--no-bot-notify` 优先于完整 Bot 配置

如果 Bot 配置缺失：

- monitor 仍可启动
- EventBus 仍可注入 processing boundary
- 不注册 Bot notification handler
- summary 不把缺失 Bot 配置视为 startup failure

Bot notification 状态固定为三种：

```text
enabled
disabled_by_flag
disabled_config_missing
```

summary 可以输出 `bot_notification_status`，但不得输出 Bot token、完整 chat id 或 handler 目标详情。

## Heartbeat Sink

`--heartbeat-jsonl <path>` 由 CLI 持有文件生命周期，Runtime 不直接感知文件路径。

固定协议：

```text
class HeartbeatSink(Protocol):
    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        ...
```

CLI 装配：

```text
NullHeartbeatSink
JsonlHeartbeatSink
```

固定规则：

- 默认不启用 heartbeat 文件
- CLI 负责打开、flush、关闭 heartbeat 文件
- Runtime 只调用 `HeartbeatSink.emit()`
- 写入失败不应导致业务 handler 崩溃
- 写入错误计入 runtime / CLI error summary
- heartbeat 输出不得包含敏感内容

## 信号与退出

P6-2I 必须处理：

```text
SIGINT
SIGTERM
```

固定语义：

```text
signal received
-> set shutdown_requested event
-> schedule runtime.stop()
-> wait runtime.run() final summary
-> unsubscribe handlers if needed
-> close Bot transport / HTTP client if owned
-> close heartbeat sink
-> print final summary
-> exit 0 if graceful
```

Python signal handler 必须是同步 callable。实现时应通过 event loop 调度异步 stop：

```text
loop.add_signal_handler(
  signal.SIGINT,
  lambda: asyncio.create_task(request_shutdown("sigint")),
)
```

重复信号防护：

```text
shutdown_requested: asyncio.Event
shutdown_reason: first signal wins
```

第二次信号到达时不得重复创建多个 stop task。

Bot transport ownership：

- CLI 创建的 `TelegramBotTransport` 由 CLI 关闭
- 外部注入的 transport 不由 CLI 关闭
- P6-2I 第一版实际只支持 CLI 创建，因此 CLI owns `TelegramBotTransport` lifecycle

不做：

- 不强杀 Telethon client
- 不吞掉 final summary
- 不在 signal handler 内做 DB 写入
- 不做 supervisor restart

## Exit Code

第一版固定：

| exit code | 含义 |
|-----------|------|
| `0` | graceful stop / operator stop / clean summary |
| `1` | runtime startup failed or runtime failed |
| `2` | config / preflight failed |
| `130` | cancelled by SIGINT before final summary can be produced |

映射规则：

```text
summary.startup_status == "pass"
and summary.final_state == "stopped"
-> 0

summary.startup_status == "fail"
or summary.final_state == "failed"
-> 1

preflight blockers present
-> 2

shutdown_reason == "sigint"
and CancelledError before summary
-> 130
```

Ctrl+C 不等于必然返回 `130`。只有 SIGINT 导致 CLI 无法完成 graceful stop、无法拿到 summary 时，才返回 `130`。

普通 `asyncio.CancelledError` 不得一律映射为 `130`，必须结合 `shutdown_reason == "sigint"` 判断。

## 输出格式

默认输出 human-readable 文本：

```text
TG-HUB_MONITOR_RUN_RESULT:
- startup_status:
- final_state:
- shutdown_reason:
- uptime_seconds:
- preflight_status:
- watchlist_loaded:
- enabled_channel_count:
- resolved_channel_count:
- ingestion_boundary_ready:
- processing_boundary_ready:
- event_bus_ready:
- bot_notification_status:
- heartbeat_status:
- events_seen_total:
- events_matched_total:
- ingest_stored_total:
- ingest_duplicate_total:
- ingest_rejected_total:
- ingest_failed_total:
- process_success_total:
- process_already_done_total:
- process_failed_total:
- last_error_code:
- blockers:
```

`--summary-json` 输出：

```text
MonitorRuntimeSummary.model_dump(mode="json")
```

`preflight --preflight-json` 输出：

```text
MonitorStartupPreflightReport.model_dump(mode="json")
```

stdout / stderr 规则固定：

human-readable mode:

- final summary -> stdout
- warnings / logs -> stderr

JSON mode:

- JSON report / JSON summary -> stdout
- all logs -> stderr
- stdout 必须只输出一个合法 JSON 文档

输出必须脱敏：

- 不输出 message text
- 不输出 raw_payload
- 不输出 Telegram API hash
- 不输出 Bot token
- 不输出完整 session path
- 不输出 DATABASE_URL
- 不输出资源链接完整 URL 作为 monitor summary 字段

## 使用说明更新

P6-2I 实现时必须更新：

```text
docs/USER_GUIDE.zh-CN.md
```

至少增加：

- 如何安装依赖
- 如何准备 `.env`
- 如何准备 `watchlist.json`
- 如何执行 `preflight`
- 如何执行 `run`
- 如何 Ctrl+C 停止
- 如何看 final summary
- 常见 exit code
- 常见 blockers

第一版仍然说明：

```text
频道和资源名通过 watchlist.json 管理
可视化管理台属于后续阶段
```

## 测试验收

建议新增：

```text
backend/tests/monitor/test_cli.py
```

必测：

| 场景 | 预期 |
|------|------|
| preflight pass | exit code 0，输出脱敏 report |
| preflight blockers | exit code 2 |
| run startup fail | exit code 1，输出 summary |
| run graceful stop | exit code 0，输出 summary |
| SIGINT | 调用 runtime.stop()，尽量输出 summary |
| SIGTERM | graceful stop 返回 0 |
| 重复 SIGINT | 只触发一次 stop |
| summary-json | 输出 JSON |
| summary-json stdout | stdout 可被 `json.loads()` 解析 |
| summary-json stderr | 日志只写 stderr |
| heartbeat-jsonl | 写入 heartbeat JSON lines |
| heartbeat write fail | 不导致 runtime 业务异常 |
| heartbeat close | 退出时 flush + close |
| no-bot-notify | 不注册 ResourceNotifyHandler |
| no-bot-notify priority | 完整 Bot 配置下仍不注册 |
| missing bot config | monitor 仍可启动 |
| bot token only | 不注册 notification handler |
| chat ids only | 不注册 notification handler |
| Bot token masked | 输出中不含 token |
| API hash masked | 输出中不含 hash |
| session path masked | 输出中不含完整 session path |
| DATABASE_URL masked | 输出中不含 DATABASE_URL |
| preflight no Telegram connect | 不调用 `TelegramClient.connect()` |
| preflight no DB connect | 不连接数据库 |
| run assembly order | 装配顺序稳定 |
| session factory separation | ingestion / processing 使用短生命周期 session factory |
| bot transport close order | runtime 完成后关闭 transport |
| CLI import | import 不产生副作用 |
| CLI does not mutate watchlist | watchlist 文件不被修改 |

测试中禁止：

- 不访问真实 Telegram API
- 不连接真实 Telethon
- 不发送真实 Bot HTTP
- 不启动真实长期无限循环
- 不依赖真实 OS signal，除非使用可控 fake signal path

## 建议验收报告

```text
P6-2I_MONITOR_STARTUP_RESULT:
- cli_module_entrypoint: pass/fail
- preflight_command: pass/fail
- run_command: pass/fail
- runtime_assembly: pass/fail
- static_preflight_only: pass/fail
- db_readiness_check: pass/fail
- ingestion_boundary_injected: pass/fail
- processing_boundary_injected: pass/fail
- eventbus_injected: pass/fail
- bot_notify_optional: pass/fail
- bot_notification_status: enabled/disabled_by_flag/disabled_config_missing
- heartbeat_sink_lifecycle: pass/fail
- graceful_stop_supported: pass/fail
- repeated_signal_guard: pass/fail
- final_summary_printed: pass/fail
- exit_code_mapping: pass/fail
- stdout_stderr_separated: pass/fail
- summary_json_supported: pass/fail
- heartbeat_jsonl_supported: pass/fail
- user_guide_updated: pass/fail
- telethon_api_accessed_in_tests: no
- telegram_bot_api_accessed_in_tests: no
- watchlist_mutated_by_cli: no
- outbox_used: no
- worker_started: no
- retry_queue_enabled: no
- history_backfill_called: no
- blockers:
  - ...
```

## 完成标准

P6-2I 通过时，可以得出：

- 项目具备明确 composition root 和终端启动长期 monitor 的入口
- 启动前可以执行纯本地 static preflight
- runtime 能装配 ingest / process / EventBus / optional Bot notify
- Ctrl+C / SIGTERM 可触发 graceful stop
- final summary 脱敏、稳定且机器可解析
- stdout / stderr 分离
- exit code 稳定
- 用户知道如何通过 `.env` 与 `watchlist.json` 使用项目

P6-2I 通过时，不能得出：

- 有 Web 可视化管理台
- 配置可通过 UI 修改
- 有 Outbox
- 有 retry queue
- 有历史补偿扫描
- 有生产 supervisor / launchd / systemd 配置
- 多实例运行语义已经稳定

## 下一阶段

P6-2I 后续可单独设计：

```text
P6-2J:
最小可视化管理台设计

P6-Reconcile:
扫描 parse_pending / dedup_pending 的历史补偿

P6-Outbox:
持久化事件与可靠通知

P6-Deploy:
launchd / systemd / supervisor 生产运行配置
```

不得把这些内容混入 P6-2I。
