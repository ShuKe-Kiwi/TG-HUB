# tg-hub

[English](README.md)

Telegram 资源聚合后端。

`tg-hub` 用于接收 Telegram 资源消息，解析并归一化资源元数据，完成资源去重，提供查询和 Bot 通知所需的只读模型。目前项目正在以严格分阶段方式接入 Telegram monitor。

## 当前状态

项目按阶段推进。近期 monitor 相关工作刻意拆分，避免把 dry-run 验证误升级成生产监听器。

| 范围 | 状态 |
|------|------|
| P1-P5 后端主链路 | 已实现 |
| Parser / Normalizer / Dedup | 已实现 |
| Telegram Bot 查询与通知适配器 | 已实现 |
| P6-2B watchlist 过滤边界 | 已实现 |
| P6-2C-0 source channel 预检 | 已实现 |
| P6-2C-0B 受控频道解析 | 已实现 |
| P6-2C-1 监听 dry-run 设计 | 已锁定 |
| P6-2C-2 短时真实监听 dry-run | 已实现 |
| P6-2D 长期 monitor | 设计已锁定，未实现 |

相关文档：

- [项目使用说明书](docs/USER_GUIDE.zh-CN.md)
- [架构文档](docs/ARCHITECTURE.md)
- [实施进度](docs/IMPLEMENTATION_STATUS.md)
- [P6-2C-1 monitor dry-run 设计](docs/P6-2C-1_MONITOR_DRY_RUN_DESIGN.md)
- [P6-2D monitor runtime 设计](docs/P6-2D_MONITOR_RUNTIME_DESIGN.zh-CN.md)

## 目录结构

```text
backend/
  app/
    modules/
      monitor/       # watchlist、source 解析、dry-run listener
      parser/        # 资源解析 pipeline
      normalizer/    # 归一化资源模型与 fingerprint
      resource/      # registry、查询服务、dedup 服务
      rawmessage/    # Telegram 原始消息存储服务
      bot/           # Telegram Bot 查询与通知适配器
    infra/           # event bus 与基础设施 helper
  tests/
docs/
```

## 安装

在 `backend` 目录中使用 backend 自己的虚拟环境。

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/pip install -r requirements.txt
```

如果虚拟环境不存在：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## 配置

后端配置来自环境变量和 `backend/.env`。

核心应用和数据库变量：

```text
DATABASE_URL
TEST_DATABASE_URL
APP_NAME
APP_ENV
APP_HOST
APP_PORT
LOG_LEVEL
```

Telegram Bot 变量：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
TELEGRAM_ALLOWED_CHAT_IDS
TELEGRAM_NOTIFY_CHAT_IDS
```

Telegram monitor 变量：

```text
WATCHLIST_PATH=~/.tg-hub/watchlist.json
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_NAME=~/.tg-hub/telethon
```

Monitor 运行时文件：

```text
~/.tg-hub/watchlist.json       # 运行时 watchlist 配置
~/.tg-hub/p6_2b_samples.json   # P6-2B 离线验收样本
```

`watchlist.json` 只表达运行时配置：

```text
source_channels
watch_titles
```

`p6_2b_samples.json` 只表达离线验收样本。

## 测试

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

数据库相关测试需要配置 PostgreSQL 测试库，不属于 monitor dry-run 验证闭环。

## Monitor 阶段边界

### P6-2B

只验证：

```text
IncomingMessage -> content_text 选择 -> watchlist 标题过滤
```

不验证 Telegram 自动采集、Parser、provider 识别、数据库入库、Dedup 或通知。

### P6-2C-0 / P6-2C-0B

验证 source channel 引用和一次性频道身份解析：

```text
source_channels -> numeric channel ids
```

不注册 `NewMessage` handler，也不监听消息。

### P6-2C-2

验证短时监听 dry-run：

```text
resolved channel ids
-> 一个 NewMessage handler
-> IncomingMessage
-> watchlist filter
-> 脱敏报告
-> 清理并退出
```

明确不做：

- 写数据库
- 调用 `RawMessageService`
- 运行 Parser / Normalizer / Dedup
- 发送 Bot 通知
- 下载媒体
- 回溯历史消息
- 无限期运行

### P6-2D

P6-2D 目前只是已锁定的生产运行时设计，覆盖：

- 重连
- 心跳
- 错误恢复
- 可观测性
- 优雅停机
- 生产生命周期

长期运行 monitor 尚未开始实现。

## 常用验证命令

运行时 preflight：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -c "from app.modules.monitor.runtime_preflight import run_runtime_preflight; import json; print(json.dumps(run_runtime_preflight().model_dump(), ensure_ascii=False, indent=2))"
```

Telethon 依赖检查：

```bash
cd /Users/kiwishook/nova_projects/tg-hub/backend
./.venv/bin/python -c "import telethon; print(telethon.__version__)"
```

Session 目录写权限检查：

```bash
test -w /Users/kiwishook/.tg-hub && echo writable
```

## Git 注意事项

当前工作区里 `backend/uv.lock` 是未跟踪文件。除非某个阶段明确批准 lockfile 变更，否则不要把它带进阶段提交。
