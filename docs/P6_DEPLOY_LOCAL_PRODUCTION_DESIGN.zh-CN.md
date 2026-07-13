# P6-Deploy 本机生产化交付设计

## 1. 阶段定义

```text
PROJECT: tg-hub
PHASE: P6-Deploy
MODE: design-lock
TARGET: single-user macOS local production
STATUS: design-locked
ALLOW_IMPLEMENTATION: yes
ALLOW_REMOTE_ADMIN: no
ALLOW_MULTI_INSTANCE: no
```

P6-Deploy 的目标是把已经可运行的 tg-hub 收口为可重复安装、启动、停止、升级、备份和排障的本机服务。

本阶段不新增业务能力，不改变 Monitor、ingestion、Parser、Normalizer、Dedup、EventBus 或 Bot 的职责。

## 2. 完成定义

P6-Deploy 通过后，管理员应能：

- 重启 Mac 后自动启动 tg-hub 管理服务；
- 在管理台明确看到服务和 Monitor 状态；
- 选择是否自动启动 Monitor；
- 进程异常退出后由 `launchd` 有界重启；
- 使用固定命令安装、升级、停止和卸载服务；
- 查看经过轮转且不含密钥的日志；
- 定期备份 PostgreSQL、watchlist 和必要运行配置；
- 在独立数据库中验证恢复流程；
- 通过最终 smoke test 判断项目是否可交付。

## 3. 固定部署拓扑

```text
macOS launchd (one service)
        |
        v
uvicorn app.main:app --host 127.0.0.1 --port 8010
        |
        +-> local Admin UI / API
        +-> MonitorControlService (single task owner)
        +-> Telethon client
        +-> ingestion / processing / EventBus / Bot
        |
        v
local PostgreSQL
```

固定约束：

- 只绑定 `127.0.0.1`，禁止 `0.0.0.0`。
- 只运行一个应用进程和一个 Uvicorn worker。
- 不使用 `--reload`。
- 不同时运行 CLI Monitor 与 Web 管理台 Monitor。
- `launchd` 只管理应用进程，不直接管理第二个 Monitor 进程。
- 不使用 Docker 作为本阶段默认部署方式。
- PostgreSQL 继续作为独立本机服务，不由 tg-hub 启停。

## 4. 项目结构

```text
backend/
├── app/
│   ├── main.py
│   ├── config.py
│   └── infra/
│       └── logger.py
├── deploy/
│   ├── com.tghub.service.plist.template
│   ├── install_launchd.sh
│   ├── uninstall_launchd.sh
│   ├── start.sh
│   ├── stop.sh
│   ├── status.sh
│   ├── backup.sh
│   ├── restore_verify.sh
│   ├── rotate_logs.py
│   └── preflight.py
├── tests/
│   └── deploy/
│       ├── test_launchd_template.py
│       ├── test_deploy_scripts.py
│       ├── test_health_endpoints.py
│       └── test_backup_manifest.py
└── .env.production.example

docs/
├── P6_DEPLOY_LOCAL_PRODUCTION_DESIGN.zh-CN.md
├── DEPLOYMENT_GUIDE.zh-CN.md
└── OPERATIONS_RUNBOOK.zh-CN.md
```

脚本必须支持 `--dry-run` 或等价只读检查，路径不得写死为某位开发者的 home。模板安装时再注入绝对路径。Linux 风格 `logrotate.conf` 只可作为可选文档，不是 macOS 主轮转方案。

## 5. 配置和密钥

生产环境文件建议固定为：

```text
~/.tg-hub/
├── production.env
├── watchlist.json
├── telethon.session
├── runtime/
│   ├── heartbeat.jsonl
│   └── deploy-state.json
├── logs/
│   ├── app.log
│   └── app.error.log
└── backups/
```

权限：

- `~/.tg-hub/`：`0700`
- `production.env`：`0600`
- `watchlist.json`：`0600`
- Telethon session：`0600`
- 日志和备份目录：`0700`
- launch agent plist：不得包含 API hash、Bot token、数据库密码或 session 内容。

`production.env` 至少包含：

```text
APP_ENV=production
CONFIG_SCHEMA_VERSION=1
LOG_LEVEL=INFO
DATABASE_URL=...
WATCHLIST_PATH=~/.tg-hub/watchlist.json
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_NAME=~/.tg-hub/telethon
TELEGRAM_BOT_TOKEN=...
TELEGRAM_NOTIFY_CHAT_IDS=...
ADMIN_BIND_HOST=127.0.0.1
ADMIN_PORT=8010
MONITOR_AUTO_START=false
HEARTBEAT_PATH=~/.tg-hub/runtime/heartbeat.jsonl
LOG_DIR=~/.tg-hub/logs
BACKUP_DIR=~/.tg-hub/backups
```

规则：

- 生产默认 `LOG_LEVEL=INFO`，不得使用 Telethon DEBUG。
- `.env.production.example` 只能包含占位值。
- 环境检查和报告只输出 configured/pass/fail，不输出值。
- `DATABASE_URL`、Bot token、API hash、session path 和完整 chat ID 不进入日志。
- 配置层通过 `TG_HUB_ENV_FILE=~/.tg-hub/production.env` 显式读取 env 文件。
- 禁止 `source production.env`、`eval` 或 `export $(cat ...)`。
- env 文件由 Python 严格 parser 加载，shell 不解释空格、引号、变量展开或命令替换。
- `~` 只由 Python 使用 `Path(value).expanduser().resolve()` 展开。
- watchlist、session、logs、backups、heartbeat 和 runtime state 的解析后路径必须位于 `~/.tg-hub/` 下。

## 6. launchd 生命周期

建议安装为用户级 LaunchAgent：

```text
~/Library/LaunchAgents/com.tghub.service.plist
```

plist 固定要求：

- `ProgramArguments` 调用 `start.sh`，最终使用项目 venv 的 Python 执行 `python -m uvicorn`；
- `WorkingDirectory` 为 `backend/` 绝对路径；
- `RunAtLoad=true`；
- `KeepAlive` 固定为 `SuccessfulExit=false`，只对非零退出重启；
- `ThrottleInterval=10`；
- stdout/stderr 写入 `~/.tg-hub/logs/`；
- 不使用 shell 拼接命令；
- 不在 plist 中内嵌敏感环境变量。

plist 结构固定包含：

```xml
<key>RunAtLoad</key>
<true/>
<key>KeepAlive</key>
<dict>
  <key>SuccessfulExit</key>
  <false/>
</dict>
<key>ThrottleInterval</key>
<integer>10</integer>
```

正常 stop 必须退出 `0`，不得被 launchd 立即拉起；非零崩溃由 launchd 节流后重启。

稳定退出码：

```text
0  normal stop
10 config error
11 permission error
12 port occupied
13 database unavailable
14 migration not at head
15 invalid watchlist
16 static preflight failed
```

`start.sh` 不解析敏感 env，也不充当 supervisor。它只解析参数、调用共享 Python preflight，然后执行：

```text
exec "$VENV/bin/python" -m uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8010 \
  --workers 1 \
  --no-access-log
```

如保留 Uvicorn access log，必须证明 URL 不含 secret、管理 API 不接受 query secret 且 request body 永不记录。

启动脚本负责验证：

- 当前目录与 venv 存在；
- 配置文件权限不宽于 `0600`；
- 端口未被其他进程占用；
- 数据库可连接；
- Alembic `current == head`；
- env contract 可由共享 Python preflight 加载；
- 数据库基础连通和 migration head。

应用 lifespan 负责：

- application assembly；
- watchlist load；
- `MonitorControlService` 初始化；
- 可选 Monitor auto-start。

shell 和 lifespan 不各自实现一套 static preflight；共用 `deploy/preflight.py` 或生产配置模块中的稳定检查函数。

任何前置失败都应以稳定退出码结束，让 launchd 日志明确记录 blocker；不得进入半启动状态。

## 7. Monitor 自动启动

生产默认：

```text
MONITOR_AUTO_START=false
```

首次部署由管理员打开管理台、运行预检并手动启动。确认稳定后才允许设为 `true`。

自动启动链路：

```text
FastAPI lifespan startup
-> application assembly
-> watchlist load
-> MonitorControlService initialization
-> watchlist revision snapshot
-> MonitorControlService.start(expected_revision)
```

失败规则：

- 自动启动失败不能导致管理台退出；
- 管理台保持可用，并显示 `AUTO_START_FAILED` 和脱敏 blocker；
- 不在 lifespan 中无限重试；
- runtime 自身继续使用已锁定的 bounded reconnect；
- 应用 shutdown 只触发一次 graceful stop。

防抖和状态要求：

- `MonitorControlService.start()` 必须保持幂等；
- `running/starting -> already_running`；
- `stopping -> reject start`；
- `failed/stopped -> allow start`；
- lifespan 对同一 app 实例只尝试一次自动启动；
- 状态公开 `auto_start_attempted`、`auto_start_status`、`auto_start_error_code`；
- 不保存或返回敏感 blocker 原文。

## 8. 健康检查

健康状态分为 process liveness、application readiness、monitor operational readiness。保留 `/health` 作为兼容入口，并增加：

```text
GET /health/live
GET /health/ready
GET /health/monitor
```

`/health/live`：

- 只证明 FastAPI event loop 可响应；
- 不访问 Telegram 或数据库；
- 成功返回 `200`。

`/health/ready`：

- 检查数据库 `SELECT 1`，设置短 timeout；
- 检查 Alembic revision 是否为 head；
- 检查 watchlist 可读取且 schema 有效；
- 检查应用装配完成；
- 返回稳定检查项和错误码，不返回连接字符串或路径。

application readiness 不把 Monitor running 作为硬条件。数据库、migration、watchlist 或 assembly 失败返回 `503`，但管理页面仍保持可访问。Monitor 失败通过 ready 响应的 `monitor` 段和 `/health/monitor` 展示。

固定成功响应：

```json
{
  "status": "ready",
  "checks": {
    "database": "pass",
    "migration": "pass",
    "watchlist": "pass",
    "assembly": "pass"
  },
  "monitor": {
    "enabled": true,
    "state": "failed",
    "error_code": "AUTO_START_FAILED"
  }
}
```

失败只返回稳定错误码；禁止 DB host/user/password、绝对路径、频道 username、session path、token 和异常栈。

## 9. 日志与轮转

主方案固定为单一写入路径：Python 只输出结构化日志到 stdout/stderr，launchd 分别重定向到 `app.stdout.log` 和 `app.stderr.log`。Python 不得再用 FileHandler 写这两个文件。

stdout 承载 normal logs，stderr 只承载 startup/fatal；日志采用结构稳定的单行文本或 JSON，至少包含：

- timestamp
- level
- logger/module
- event/error_code
- request_id（适用时）
- runtime state（适用时）

禁止记录：

- Telegram 消息正文、caption 和 raw payload
- API hash、Bot token、session 内容
- 完整 `DATABASE_URL`
- 完整 Telegram entity
- 通知完整 chat ID

默认轮转策略：

- `app.stdout.log`：10 MB，保留 7 份；
- `app.stderr.log`：10 MB，保留 7 份；
- heartbeat JSONL：10 MB，保留 3 份；
- 轮转后压缩；
- 文件权限保持 `0600`。

macOS 主轮转方案为 `deploy/rotate_logs.py`，由独立每日 LaunchAgent 或管理员命令执行。不得依赖 Linux `logrotate`。

heartbeat writer 当前如果长期持有文件句柄，不能直接 rename 后假定写入新文件。P6-Deploy-3 必须选择并测试一种固定语义：

- writer 每次 emit 短生命周期打开 append 文件；或
- rotation 发出主动 reopen；或
- 使用受控 copy-truncate。

验收必须证明轮转期间持续写入不会让旧 inode 无限增长或静默丢失 heartbeat。

## 10. PostgreSQL 迁移

部署和升级流程固定为：

```text
stop tg-hub
-> backup
-> install dependencies
-> alembic current / heads
-> alembic upgrade head
-> start tg-hub
-> readiness / smoke test
```

规则：

- 应用启动时不自动执行 migration；
- migration 必须是显式运维动作；
- upgrade 前必须完成可验证备份；
- migration 失败不得启动新版本；
- 禁止自动执行 downgrade；
- 回滚优先恢复应用版本，涉及不兼容 schema 时按备份恢复 runbook 操作。

## 11. 备份与恢复

备份范围：

- PostgreSQL `pg_dump` custom format；
- `watchlist.json`；
- 当前 git commit、Alembic revision、时间和 SHA-256 manifest。

第一版默认明确排除：

- `production.env`：`included=false, reason=secret_material_excluded`；
- Telethon session：`included=false, reason=authentication_session_excluded`。

P6-Deploy 不自创加密格式。未来如需备份敏感文件，单独设计 age、macOS Keychain 或加密磁盘镜像及密钥托管。

建议频率：

- 每日数据库备份；
- watchlist 保存后可追加一次配置备份；
- 升级前强制备份；
- 默认保留最近 7 日和最近 4 周。

`backup.sh` 必须：

- `set -euo pipefail`；
- 使用临时目录，成功后原子 rename；
- 任一步失败不留下“成功”标记；
- 生成 manifest 和 checksum；
- 不把密码打印到命令行或日志。

manifest 固定字段：

```text
backup_id
created_at_utc
app_git_commit
python_version
dependency_lock_sha256
alembic_revision
config_schema_version
database_dump_filename
database_dump_sha256
watchlist_filename
watchlist_sha256
production_env_included=false
telethon_session_included=false
backup_status=complete
```

完整且 checksum 可验证的 `manifest.json` 是唯一成功标准，并且必须最后原子写入；不使用额外空 `SUCCESS` 文件。

恢复验证必须在独立测试数据库完成：

```text
create isolated restore database
-> pg_restore
-> alembic current
-> integrity smoke queries
-> success: delete isolated database after report
-> failure: retain isolated database and print cleanup command
```

恢复数据库名必须以 `tg_hub_restore_verify_` 开头，且不得等于 production DB。创建、连接和 DROP 前都要再次验证前缀、owner 和目标连接；DROP 使用严格名称 guard。禁止直接在生产库上“试恢复”。

## 12. 升级与回滚

P6-Deploy 第一版采用固定仓库工作树，不实现 release directory 或原子 symlink 切换。因此：

- 升级前必须 `git status --short` 为空；
- 记录精确 git commit；
- 校验 dependency lock/hash；
- 存在未提交业务改动时拒绝升级；
- 明确不承诺原子 release rollback。

每次部署记录：

- git commit
- Python 版本
- dependency lock/hash
- Alembic revision
- 配置 schema version
- backup manifest
- 部署时间和结果

升级失败：

1. 保持服务停止；
2. 保留失败日志；
3. 在干净工作树恢复上一个精确代码 commit；
4. 如 schema 兼容则直接启动旧版本；
5. 如 schema 不兼容，按受控恢复流程恢复数据库；
6. 重新执行 readiness 和 smoke test。

## 13. 运维命令

交付后固定提供：

```text
deploy/install_launchd.sh
deploy/uninstall_launchd.sh
deploy/start.sh --check
deploy/status.sh
deploy/stop.sh
deploy/backup.sh
deploy/restore_verify.sh <backup>
```

所有命令必须幂等或明确拒绝重复操作，并输出稳定结果：

```text
status: pass | fail
service_state: running | stopped | failed | unknown
pid: int | null
http_ready: yes | no
monitor_state: ...
error_code: str | null
```

## 14. 测试与验收

### 静态验收

- plist 可被 `plutil -lint` 校验；
- plist 不包含密钥；
- host 固定 loopback；
- worker 数固定为 1；
- 脚本通过 shell syntax check；
- 示例配置不含真实凭据；
- 文件权限检查有测试。
- `production.env` 含 shell 特殊字符时由 Python 安全加载，不执行内容。
- 示例配置扫描无真实 token、session 或 chat ID。

### 集成验收

- install -> start -> ready；
- 重复 install/start 不产生第二实例；
- plist 重复安装幂等或稳定拒绝；
- 同端口已有非 tg-hub 进程时启动拒绝；
- stale PID/state 不被 `status.sh` 误报为 running；
- SIGTERM 优雅停止 Monitor；
- 正常 stop 后 launchd 不立即重启；
- 异常退出后 launchd 有界重启；
- 数据库断开时 readiness 为 503、liveness 仍为 200；
- readiness DB timeout 在固定短时间内返回；
- Monitor auto-start 失败时 UI 仍可用且状态可见；
- migration 非 head 时拒绝生产启动；
- 无效 watchlist 时管理台可用但 Monitor 不启动；
- 日志不包含 seeded secrets；
- 日志轮转期间持续写入不写旧文件无限增长；
- backup 生成可校验 manifest；
- backup 中断没有完整 manifest，不视为成功；
- restore_verify 在隔离数据库成功；
- 非法 restore 目标名被拒绝；
- restore 失败时隔离库默认保留；
- dirty worktree 时升级拒绝；
- Mac 重启后的 RunAtLoad 由人工最终验收。

### 真实链路 smoke test

- preflight pass；
- Monitor running / connected / handler registered；
- 所有启用频道已解析；
- 观察到消息；
- 测试标题命中；
- RawMessage stored 或 duplicate disposition 正确；
- processing 完成；
- Bot 测试通知到达；
- graceful stop 成功。

## 15. 禁止范围

P6-Deploy 不实现：

- 远程公网管理
- Nginx、TLS 或域名
- Docker/Kubernetes 编排
- 多实例 leader election
- 云数据库或对象存储
- Outbox、消息队列或历史补偿
- P6-2K 热播目录
- 新 Parser、Provider 或通知业务能力

## 16. 实施拆分

```text
P6-Deploy-1
production config contract + shared preflight + health/live + health/ready

P6-Deploy-2
launchd plist + install/start/stop/status/uninstall + single-instance guard

P6-Deploy-3
structured logging + redaction + heartbeat writer + macOS-compatible rotation

P6-Deploy-4
backup + manifest + restore verification + migration workflow

P6-Deploy-5
deployment guide + operations runbook + full smoke acceptance + Mac reboot manual acceptance
```

每个子阶段单独提交，不把系统安装动作与代码实现混在同一 commit。当前只批准进入 P6-Deploy-1；Deploy-2 至 5 不自动批准，必须逐阶段评审。

## 17. 完成标准

P6-Deploy 完成时可以得出：

- tg-hub 已具备单用户 macOS 本机长期运行和恢复能力；
- 部署、升级、备份、恢复和排障流程可重复执行；
- 管理台和 Monitor 生命周期仍保持既有架构边界；
- 项目达到当前定义的本机可交付完成状态。

不能得出：

- 已达到互联网 SaaS 或多租户生产标准；
- 支持远程管理或高可用；
- 通知具有 exactly-once 保证；
- 外部平台热播目录已经实现。
