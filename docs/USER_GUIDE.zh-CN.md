# tg-hub 项目使用说明书

> 适用对象：本地开发、阶段验收、长期 monitor 启停与排障。
> 当前状态：长期 monitor、入库、处理、EventBus/Bot 通知链路和 CLI 启动入口已实现。

## 1. 项目用途

`tg-hub` 是一个 Telegram 资源聚合后端，用于：

- 接收 Telegram 资源消息
- 解析资源标题、剧集、网盘链接等信息
- 归一化资源元数据
- 对资源进行去重和合并
- 提供查询和 Telegram Bot 通知所需的只读模型
- 分阶段接入 Telegram monitor

当前 monitor 已完成：

- watchlist 过滤
- source channel 预检
- 一次性频道身份解析
- 短时真实监听 dry-run
- 长期运行 monitor 生命周期、重连、心跳和优雅停机
- Monitor -> ingestion -> processing handoff
- EventBus 与可选 Bot 资源通知
- CLI `preflight` / `run` 启动入口

尚未完成：可视化管理台、history backfill、Outbox/可靠通知、生产 supervisor 和多实例运行语义。

## 2. 目录说明

```text
tg-hub/
  README.md
  README.zh-CN.md
  backend/
    app/
      modules/
        monitor/       # monitor 配置、过滤、频道解析、dry-run listener
        parser/        # 资源解析
        normalizer/    # 资源归一化
        resource/      # 资源 registry、查询、dedup
        rawmessage/    # 原始消息入库服务
        bot/           # Telegram Bot 查询与通知适配器
      infra/           # EventBus 等基础设施
    tests/
    requirements.txt
  docs/
```

## 3. 本地环境准备

进入 backend 目录：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
```

安装依赖：

```bash
./.venv/bin/pip install -r requirements.txt
```

如果 `backend/.venv` 不存在：

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

确认 Python 环境：

```bash
./.venv/bin/python -c "import sys; print(sys.executable)"
```

确认 Telethon：

```bash
./.venv/bin/python -c "import telethon; print(telethon.__version__)"
```

## 4. 配置文件

后端读取环境变量和 `backend/.env`。

建议从示例文件复制：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
cp .env.example .env
```

基础配置：

```text
DATABASE_URL
TEST_DATABASE_URL
APP_NAME
APP_ENV
APP_HOST
APP_PORT
LOG_LEVEL
```

Telegram Bot 配置：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
TELEGRAM_ALLOWED_CHAT_IDS
TELEGRAM_NOTIFY_CHAT_IDS
```

Telegram monitor 配置：

```text
WATCHLIST_PATH=~/.tg-hub/watchlist.json
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_NAME=~/.tg-hub/telethon
```

说明：

- `TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH` 用于 Telethon 用户账号会话。
- `TELEGRAM_SESSION_NAME` 是 Telethon session 文件名，不是 Bot token。
- 默认 session 父目录是 `/Users/kiwishook/.tg-hub`。

检查 session 目录权限：

```bash
test -w /Users/kiwishook/.tg-hub && echo writable
```

如果目录不存在：

```bash
mkdir -p /Users/kiwishook/.tg-hub
chmod 700 /Users/kiwishook/.tg-hub
```

## 5. Monitor 配置文件

运行时配置：

```text
/Users/kiwishook/.tg-hub/watchlist.json
```

离线验收样本：

```text
/Users/kiwishook/.tg-hub/p6_2b_samples.json
```

二者不能混用。

`watchlist.json` 只表达：

```text
source_channels
watch_titles
```

`p6_2b_samples.json` 只表达：

```text
输入样本
预期匹配结果
```

示例 `watchlist.json` 结构：

```json
{
  "source_channels": [
    {
      "ref": "https://t.me/example_channel",
      "enabled": true
    }
  ],
  "watch_titles": [
    {
      "title": "家业",
      "enabled": true,
      "aliases": []
    }
  ]
}
```

## 6. 常用测试命令

运行 monitor 测试：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pytest tests/monitor
```

运行当前非数据库回归集合：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pytest tests/bot/test_bot_p5d.py tests/infra/test_eventbus.py tests/monitor tests/normalizer tests/parser
```

数据库相关测试需要 PostgreSQL 测试库。`preflight` 不连接数据库；`run` 启动时会执行一次只读数据库可用性检查。

## 7. P6-2B：watchlist 离线验收

P6-2B 只验证：

```text
IncomingMessage
-> content_text 选择
-> watchlist normalize
-> 标题匹配
```

不验证：

- Telegram 自动采集
- Parser
- Normalizer
- Dedup
- Provider 识别
- DB 入库
- Bot 通知

运行 monitor 测试即可覆盖 P6-2B：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pytest tests/monitor
```

## 8. P6-2C-0：source_channels 预检

P6-2C-0 只做引用分类：

```text
t.me URL / @username / numeric id
-> input_type
-> status
-> numeric id，如果已是 numeric
```

不做：

- Telegram API 访问
- username 真实解析
- DB 写入
- Parser/Dedup
- 消息监听

相关模块：

```text
backend/app/modules/monitor/source_channels.py
```

## 9. P6-2C-0B：一次性频道解析

P6-2C-0B 将 enabled `source_channels` 解析为 numeric channel id。

边界：

- 可以访问 Telegram API
- 可以创建短生命周期 Telethon client
- 解析完成后立即 disconnect
- 不注册 `NewMessage` handler
- 不监听消息
- 不写数据库
- 不跑 Parser/Dedup

相关模块：

```text
backend/app/modules/monitor/telethon_resolver.py
```

## 10. Runtime Preflight

preflight 用于确认真实环境是否准备好。

运行：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -c "from app.modules.monitor.runtime_preflight import run_runtime_preflight; import json; print(json.dumps(run_runtime_preflight().model_dump(), ensure_ascii=False, indent=2))"
```

目标：

```text
ready_for_one_shot_resolve: yes
blockers: []
```

preflight 不会：

- 访问 Telegram API
- 创建 Telethon client
- 连接 Telegram
- 注册 handler
- 写 DB

## 11. P6-2C-2：短时真实监听 dry-run

P6-2C-2 用于验证真实目标频道消息是否能在有限窗口内进入 handler。

它允许：

- 使用已解析的 numeric channel ids
- 创建 Telethon client
- 连接 Telegram
- 注册一个 `NewMessage` handler
- 将 event 转为 `IncomingMessage`
- 执行 watchlist filter
- 输出脱敏报告
- 达到 timeout 或 max_messages 后清理并退出

它禁止：

- 长期运行
- `run_until_disconnected()`
- DB 写入
- `RawMessageService`
- Parser / Normalizer / Dedup
- Bot 通知
- 媒体下载
- history backfill
- 保存 raw event

短时 dry-run 的结果中应关注：

```text
handler_registered
listener_started
exit_reason
implementation_pass
traffic_observed
events_received
dto_conversion_success_count
filter_pass_count
filter_reject_count
handler_removed
client_disconnected_cleanly
blockers
```

`traffic_observed=no` 不一定表示失败。它可能只是测试窗口内没有真实消息到达。

## 12. 长期 monitor runtime

P6-2D 至 P6-2H 已实现长期 runtime 及受控业务 handoff：

文档：

```text
docs/P6-2D_MONITOR_RUNTIME_DESIGN.zh-CN.md
```

它支持：

- runtime 状态机
- startup lifecycle
- reconnect / backoff
- heartbeat
- liveness / readiness
- error code
- graceful shutdown
- observability
- backpressure
- 匹配消息经 application boundary 写入 RawMessage
- 对新入库消息执行 Parser / Normalizer / Dedup 编排
- 通过 EventBus 触发可选 Bot 通知

Monitor 仍然只是传输适配和 handoff 层，不直接访问 Session、Repository、Parser、Dedup 或 Bot transport。

## 13. 启动前静态检查

进入后端目录：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
```

执行纯本地检查：

```bash
./.venv/bin/python -m app.modules.monitor.cli preflight
```

机器可读 JSON：

```bash
./.venv/bin/python -m app.modules.monitor.cli preflight --preflight-json
```

该命令检查 Telethon 依赖、Telegram API 配置、session 父目录、watchlist schema 和数据库连接字符串。它不会连接 Telegram、解析频道、连接数据库或调用 Bot API。

返回码：`0` 表示通过，`2` 表示存在配置 blocker。

## 14. 启动长期 monitor

先确认 PostgreSQL 已启动、数据库迁移已完成，并且 watchlist 中的频道已在 Channel 表登记。然后运行：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -m app.modules.monitor.cli run
```

输出机器可读 summary：

```bash
./.venv/bin/python -m app.modules.monitor.cli run --summary-json
```

把 heartbeat 写入 JSONL：

```bash
./.venv/bin/python -m app.modules.monitor.cli run \
  --heartbeat-jsonl ~/.tg-hub/monitor-heartbeat.jsonl
```

禁用 Bot 通知但保留 EventBus：

```bash
./.venv/bin/python -m app.modules.monitor.cli run --no-bot-notify
```

按 `Ctrl+C` 会请求 graceful stop，等待 handler drain、移除 handler、断开 Telethon client、关闭 Bot HTTP client 和 heartbeat 文件，再输出 final summary。`SIGTERM` 使用相同停机路径。

退出码：

| code | 含义 |
|------|------|
| `0` | 正常启动并优雅停止 |
| `1` | runtime 启动或运行失败 |
| `2` | static preflight 未通过 |
| `130` | SIGINT 导致无法生成 final summary |

Bot 通知只有在 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_NOTIFY_CHAT_IDS` 同时有效时启用。缺少 Bot 配置不会阻止 monitor 启动；summary 会显示 `disabled_config_missing`。

频道和资源名仍以 `watchlist.json` 为事实来源，也可以通过本机可视化管理台安全编辑。

## 15. 启动可视化管理台与 FastAPI

管理台只允许本机访问。启动时必须绑定 loopback：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000/admin/
```

管理台提供：

- Monitor 状态、运行统计、最近错误和 static preflight
- 受控启动与优雅停止
- `source_channels` 增删改、启停和本地引用分类
- `watch_titles` 与 aliases 增删改、启停和冲突校验
- watchlist revision 冲突提示与 missing/invalid 文件显式恢复

Web 管理台和 CLI 是两种互斥的 runtime owner。使用管理台启动 Monitor 时，不要同时执行：

```bash
./.venv/bin/python -m app.modules.monitor.cli run
```

管理台不提供远程访问认证，不能把服务绑定到 `0.0.0.0` 或直接暴露到局域网/公网。

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

Telegram Bot webhook 需要额外配置 Bot token、webhook secret 和路由，不属于 monitor dry-run 必需项。

## 16. Git 提交注意事项

阶段提交前建议：

```bash
git status --short
git diff --cached --stat
```

## 17. 常见问题

### `../.venv/bin/pip: no such file or directory`

你可能在 `backend` 目录下用了错误路径。

正确路径：

```bash
./.venv/bin/pip
```

不是：

```bash
../.venv/bin/pip
```

### `telethon_dependency_missing`

当前 backend venv 中没有 Telethon。

解决：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pip install -r requirements.txt
```

### `telegram_session_parent_not_writable`

`TELEGRAM_SESSION_NAME` 的父目录不可写。

解决：

```bash
mkdir -p /Users/kiwishook/.tg-hub
chmod 700 /Users/kiwishook/.tg-hub
test -w /Users/kiwishook/.tg-hub && echo writable
```

### dry-run 超时没有消息

如果报告中：

```text
implementation_pass: yes
traffic_observed: no
exit_reason: timeout
```

说明实现路径正常，只是窗口内没有真实目标频道消息到达。

### `database_unavailable`

`run` 的有限数据库 readiness check 失败。确认 PostgreSQL 正在运行、`DATABASE_URL` 正确并且当前用户可连接。该检查只执行 `SELECT 1`，不写业务数据。

### `channel_not_registered`

Telegram numeric channel id 没有对应 Channel 记录。P6-2E 明确禁止自动创建 Channel，需要先通过既有 Channel 管理流程登记 canonical Telethon marked peer id。

### Bot 通知显示 `disabled_config_missing`

同时配置以下两项后重新启动：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_NOTIFY_CHAT_IDS
```

多个通知 chat id 使用英文逗号分隔。不要把 token、API hash、session 文件或 `DATABASE_URL` 放进日志和验收报告。
