# P6-Deploy-3D-2 实施评审：Rotation Status 只读投影

> 项目：tg-hub
> 阶段：P6-Deploy-3D-2
> 状态：approved
> BLOCKERS：0
> ALLOW_IMPLEMENTATION：P6-Deploy-3D-2 only
> 前置提交：`8c105c1 feat: add rotation agent lifecycle`

## 1. 评审结论

3D-2 的架构方向成立：由唯一 Python reader 安全读取 3C 的 `rotation-status.json`，生成稳定脱敏 DTO，供 CLI 与 Admin 共用。该观察结果不得反向修改轮转状态，不得影响 `/health/live` 或 `/health/ready`。

复审指出的 installation generation、投影层错误码、unknown 字段和 CLI 当前 generation 优先级已写入正式契约。3D-1 metadata 最小补丁已完成并通过专项、Deploy 与完整回归，`never_run` 现在具有可信 `installed_at`。

```text
P6-DEPLOY-3D-2_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_3D_2: yes
  allow_P6_Deploy_3D_3: no
  allow_P6_Deploy_3D_4: no
```

## 2. 阶段目标

固定链路：

```text
rotation-agent.json + rotation-status.json
-> app.deploy.rotation_status.read_rotation_status()
-> RotationStatusProjection
-> rotation status CLI
-> Admin read-only endpoint and overview display
```

3D-2 只回答：

- rotation agent 是否处于已安装观察态；
- 最近一次轮转检查是否成功、失败或部分成功；
- 最近结果是否 stale；
- archive budget 和 active oversize 是否需要关注；
- 是否存在 legacy content 标记。

3D-2 不执行轮转，不安装 agent，不访问 Telegram，不读取 archive 内容。

## 3. 已完成前置补丁：安装元数据

3D-1 正式契约要求安装成功后安全原子写入：

```text
~/.tg-hub/runtime/rotation-agent.json
```

`backend/deploy/install_rotation.sh` 已在 bootstrap 与 launchctl verification 成功后原子生成该文件；`uninstall_rotation.sh` 已只删除 metadata 并保留历史 status/archive。

已完成的 3D-1 最小修正：

```text
successful bootstrap + launchctl verification
-> atomic write rotation-agent.json
-> schema_version = 1
-> label = com.tghub.rotate-logs
-> installed_at = timezone-aware UTC datetime
-> chmod 0600
-> fsync file and parent
```

安装元数据写失败时，安装不能返回成功。必须：

```text
metadata write failed
-> bootout rotation agent
-> confirm launchctl job absent
-> remove only plist created by this install
-> remove incomplete metadata temp
-> ROTATION_AGENT_METADATA_WRITE_FAILED
```

回滚失败返回既有 `ROTATION_AGENT_ROLLBACK_FAILED`。

卸载成功后删除 `rotation-agent.json`，但不得删除 `rotation-status.json`。这样卸载后：

- `agent_installed = false`；
- 历史最近轮转结果仍可展示；
- `stale = not_applicable`。

不得使用 plist mtime、status mtime 或当前进程启动时间替代 `installed_at`。

## 4. 唯一读取模块

新增模块固定为：

```text
backend/app/deploy/rotation_status.py
```

公开接口建议：

```python
def read_rotation_status(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> RotationStatusProjection:
    ...
```

CLI 入口使用同一模块：

```text
python -m app.deploy.rotation_status
```

`backend/deploy/rotation_status.sh` 只负责选择 backend venv 并调用该模块，不得自行解析 JSON。Admin service 也必须调用同一个 reader，禁止复制 schema、stale 或错误映射逻辑。

## 5. 安全只读语义

3D-2 不得直接复用当前 `rotation_fs.open_regular()`，因为该 helper 会执行 `fchmod(fd, 0600)`，违反状态投影的只读承诺。

必须新增只读 helper，固定流程：

```text
lstat runtime parent
-> reject symlink / non-directory / group-world permissions
-> os.open(O_RDONLY | O_NOFOLLOW)
-> fstat opened fd
-> require regular file
-> require mode & 0o077 == 0
-> bounded read
-> close
```

只读 reader 禁止：

- `chmod` / `fchmod`；
- 创建 runtime 目录；
- 创建缺失文件；
- 更新 mtime；
- 获取 `rotate.lock`；
- 修复或重写无效 JSON；
- 跟随 symlink；
- 输出原始异常文本。

文件大小上限固定为 1 MiB。超限、短读后内容增长、非 UTF-8、非单一 JSON object、schema 不支持均返回稳定 `invalid`，不得继续解析不受限内容。

## 6. 输入文件与真值边界

输入固定为：

```text
runtime/rotation-agent.json
runtime/rotation-status.json
```

`rotation-agent.json` 只证明安装脚本完成过受控安装。3D-2 不调用 `launchctl`，避免 Admin 每次轮询创建子进程；实际 launchd job 在线状态留给 3D-4 安装验收和部署 status command 判断。

因此字段名称固定为：

```text
agent_configured
```

不得命名为：

```text
agent_running
agent_loaded
```

避免把安装元数据误表述为实时 launchd 状态。

若 metadata 存在但 schema/path/permission 无效，投影返回 `agent_status=invalid`，而不是 `agent_configured=false`。

### 6.1 Installation generation

卸载保留历史 `rotation-status.json`，但历史结果不得自动代表下一次安装。

当前 installation generation 的边界固定为：

```text
status.check_completed_at >= agent.installed_at
-> status belongs to current installation generation

status.check_completed_at < agent.installed_at
-> status belongs to previous installation generation
-> current status = never_run
-> current last_started_at = null
-> current last_completed_at = null
-> stale uses new installed_at grace period
```

等于边界属于当前 generation。第一版不在公共 DTO 中展示历史结果；历史文件继续保留在磁盘，但不影响当前状态、stale 或 CLI 退出码。

## 7. 投影 DTO

```text
RotationStatusProjection
- agent_status: not_configured | configured | invalid
- status: never_run | pass | partial | fail | invalid
- error_code: str | null
- installed_at: datetime | null
- last_started_at: datetime | null
- last_completed_at: datetime | null
- rotated_files: int | null
- cleaned_archives: int | null
- archive_bytes: int | null
- active_bytes: int | null
- archive_budget_status: within_budget | cleaned | exceeded_unrecoverable | unknown
- active_oversize: bool | null
- legacy_content_possible: bool | null
- stale: true | false | not_applicable | unknown
- report_desensitized: yes
```

映射规则：

- metadata 不存在：`agent_status=not_configured`；
- metadata 合法：`agent_status=configured`；
- metadata 无效：`agent_status=invalid`，`stale=unknown`；
- status 不存在：`status=never_run`；
- status 合法：保留 `pass | partial | fail`；
- status 无效：`status=invalid`；
- `legacy_content_possible` 为所有 file status 的逻辑 OR；
- `never_run` 的 counts/bytes 为 `0`，布尔观察值为 `false`；
- 合法 status 使用文件中的 counts/bytes/bool；
- status invalid、unreadable 或 schema unsupported 时相关 counts/bytes/bool 为 `null`，不得伪造 `0` 或 `false`；
- `last_started_at` 只映射 `check_started_at`，`last_completed_at` 只映射 `check_completed_at`，不得兼容或猜测其他字段名；
- 投影不得暴露 `run_id`、`archive_name`、目标路径、单文件 copied/truncated bytes 或原始异常。

### 7.1 投影层错误码

3D-2 reader 错误与 3C execution 错误使用不同命名空间。稳定投影错误码至少包括：

```text
ROTATION_AGENT_METADATA_INVALID
ROTATION_AGENT_METADATA_READ_FAILED
ROTATION_STATUS_INVALID
ROTATION_STATUS_READ_FAILED
ROTATION_STATUS_SCHEMA_UNSUPPORTED
ROTATION_STATUS_TIME_INVALID
ROTATION_STATUS_ERROR_UNKNOWN
```

错误优先级固定为：

```text
agent metadata invalid/unreadable
-> ROTATION_AGENT_*

agent metadata valid + status invalid/unreadable
-> ROTATION_STATUS_*

agent metadata and status valid + current generation
-> approved 3C execution error_code
```

3C execution `error_code` 只允许 `ROTATION_ERROR_CODES`；文件中出现未知 execution code 时映射为 `ROTATION_STATUS_ERROR_UNKNOWN`。任何层级都不得把解析异常或系统异常原文放入 DTO。

## 8. 时间与 stale 契约

所有输入时间必须：

- timezone-aware；
- 转换为 UTC；
- `check_started_at <= check_completed_at`；
- 不得晚于 reader 当前时间 5 分钟以上。

否则对应文件视为 `invalid`。

stale 阈值固定为：

```text
2h15m
```

判定：

```text
agent_status == not_configured
-> stale = not_applicable

agent_status == invalid
-> stale = unknown

agent_status == configured AND status == never_run
-> now <= installed_at + 2h15m: stale = false
-> now > installed_at + 2h15m: stale = true

agent_status == configured AND status in pass|partial|fail
-> now - check_completed_at > 2h15m: stale = true
-> otherwise: stale = false

status == invalid
-> stale = unknown
```

`fail != stale`。刚失败可显示 `status=fail, stale=false`；长期没有新结果才显示 `stale=true`。禁止使用任何文件 mtime。

## 9. 一致性读取

3C 使用 atomic replace 写 status，因此 reader 打开的 fd 对应一个完整 inode generation。reader 不要求 `rotate.lock`，也不因轮转正在运行而阻塞 Admin。

两个输入文件不是同一事务更新，允许短暂观察到：

```text
agent configured + status never_run
```

这是合法状态，不应重试或报错。

reader 每次调用只读取每个文件一次；不得为了“追求一致”循环读取或无限重试。

有界读取算法固定为：

```text
open fd with O_RDONLY | O_NOFOLLOW
-> first fstat: inode + size N
-> reject N > 1 MiB
-> read exactly N bytes
-> second fstat
-> require same inode and size == N
-> decode strict UTF-8
-> parse exactly one JSON object
```

读取不足 N bytes、第二次 size 变化或 inode 异常均返回 `ROTATION_STATUS_READ_FAILED`（metadata 对应 `ROTATION_AGENT_METADATA_READ_FAILED`）。不得继续追读增长内容。

## 10. CLI 契约

`python -m app.deploy.rotation_status` 输出单行脱敏 JSON。

退出码：

```text
2 = agent_status invalid OR status invalid
0 = agent_status not_configured
0 = agent_status configured AND current status never_run
1 = agent_status configured AND current status partial|fail
0 = agent_status configured AND current status pass
```

判断严格按上述顺序执行。CLI 只评价当前 installation generation，不评价卸载后保留的历史 status，也不让重装前的旧 fail 产生退出码 1。CLI 不调用 `launchctl`，不执行轮转，不创建缺失目录或文件。

## 11. Admin 接入边界

新增只读 endpoint：

```text
GET /api/admin/v1/observability/rotation
```

规则：

- 使用现有 local-origin guard 和 envelope；
- 通过 `asyncio.to_thread()` 调用同步 reader，避免阻塞 event loop；
- 正常观察态始终返回 HTTP 200，包括 `never_run`、`partial`、`fail`、`invalid`；
- reader 未知编程错误降级为脱敏 `status=invalid`，不得返回异常文本；
- endpoint 不执行轮转，不调用 `launchctl`，不修改文件；
- 不将 rotation DTO 塞入 `MonitorControlSnapshot`，保持 Deploy observability 与 Monitor ownership 分离。

管理台仅新增紧凑观察区：

- Agent 配置状态；
- 最近轮转状态；
- 最近完成时间；
- stale；
- archive 占用与预算状态；
- active oversize；
- legacy archive 提示。

不得增加立即轮转、安装、卸载、删除 archive、修改预算或编辑路径操作。

## 12. Health 语义

rotation projection 是观察信息：

- 不参与 `/health/live`；
- 不参与 `/health/ready`；
- status `fail`、`invalid` 或 `stale=true` 均不返回 503；
- Admin rotation endpoint 故障不得影响 Monitor status endpoint；
- 管理台轮转状态请求失败时只显示“状态不可用”。

## 13. 测试矩阵

至少覆盖：

1. metadata/status 均不存在时返回 not_configured + never_run + not_applicable；
2. configured + never_run 在 grace period 内外的 stale；
3. 合法 pass、partial、fail 映射；
4. fail 与 stale 独立；
5. stale 只基于 installed_at/check_completed_at，修改 mtime 不影响结果；
6. symlink parent、symlink file、非普通文件、权限过宽均 invalid；
7. reader 不 chmod、不创建、不写入、不更新时间；
8. 超过 1 MiB、非 UTF-8、非法 JSON、数组根、未知 schema 均 invalid；
9. naive、逆序或过度未来时间 invalid；
10. legacy_content_possible 使用所有目标 OR；
11. error_code allowlist，未知错误码不原样透出；
12. run_id、archive_name、绝对路径和异常文本不出现在投影 JSON；
13. atomic replace 并发读取只得到旧完整 generation 或新完整 generation；
14. CLI 与 Admin 使用同一 reader；
15. CLI 退出码 0/1/2 稳定；
16. endpoint 对 never_run/fail/invalid 均返回 200；
17. projection fail/stale 不影响 live/ready；
18. 管理台无轮转、安装、卸载或删除按钮；
19. 3D-1 metadata 写失败触发完整 agent 回滚；
20. uninstall 删除 metadata、保留 rotation-status 和历史 archive；
21. Deploy、Admin 和完整非数据库回归通过。
22. uninstall 后保留旧 fail status：not_configured、stale not_applicable、CLI exit 0；
23. reinstall 后旧 status 早于 installed_at：当前 never_run，时间字段为空，使用新 grace；
24. metadata 与 status reader 错误使用不同命名空间且不泄漏异常；
25. invalid/unreadable status 的 counts、bytes 和 bool 为 null；
26. 当前 generation 的 partial/fail 才产生 CLI exit 1；
27. `check_completed_at == installed_at` 属于当前 generation。

## 14. 禁止范围

P6-Deploy-3D-2 不得实现：

- rotation LaunchAgent 真实安装、kickstart 或轮转；
- online session authorization preflight；
- `telethon-session.lock`；
- Monitor 生命周期修改；
- archive 内容读取、下载或删除；
- retention/budget/state machine 修改；
- health 503 联动；
- Deploy-4/5。

## 15. 最终授权

安装 metadata 的最小补丁已写入 3D-1 实现并通过：

- 安装成功 metadata 原子持久化测试；
- metadata 写失败完整回滚测试；
- uninstall 只删除 metadata、保留 status/archive 测试。

以下 3D-2 契约已经完成锁定：

- installation generation 判断；
- projection-level error codes 与优先级；
- invalid/unreadable 状态的 nullable unknown；
- CLI 当前 generation 退出码优先级；
- 固定时间字段映射与双 `fstat` 有界读取。

```text
P6-DEPLOY-3D-2_REVIEW:
  result: approved
  architecture_direction: approved
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_3D_2: yes
  allow_P6_Deploy_3D_3: no
  allow_P6_Deploy_3D_4: no
```

当前允许下一步进入 3D-2 实现，但仍不得真实安装 rotation agent，不得进入 3D-3 或 3D-4。
