# P6-Deploy-3B：Heartbeat 持久化实施评审

> STATUS：implementation-review-approved
> ALLOW_P6_DEPLOY_3B_IMPLEMENTATION：yes
> ALLOW_P6_DEPLOY_3C_IMPLEMENTATION：no
> ALLOW_P6_DEPLOY_3D_IMPLEMENTATION：no

## 1. 评审结论

P6-Deploy-3B 可以进入实现。范围严格限定为：

```text
MonitorHeartbeat
-> CompositeHeartbeatSink
   -> ControlHeartbeatSink
   -> JsonlHeartbeatSink
-> HeartbeatPersistenceStatus
```

本阶段不实现 heartbeat rename、日志 copy-truncate、retention、archive budget、rotation LaunchAgent 或 online Telethon preflight。

## 2. 当前代码差异

实现必须处理以下现状：

1. `JsonlHeartbeatSink` 长期持有文本文件句柄，必须改为 short-open。
2. `MonitorControlService` 只装配 `ControlHeartbeatSink`，管理台启动不会持久化。
3. CLI 只有显式传入 `--heartbeat-jsonl` 才写文件，默认使用 `NullHeartbeatSink`。
4. `BootstrapFactory` 参数类型被固定为 `ControlHeartbeatSink`，需要收口到 `HeartbeatSink` protocol。
5. `MonitorControlSnapshot` 尚无 persistence status 投影。
6. 当前同步文件操作直接发生在 async emit 中，可能阻塞 Monitor event loop。

## 3. 固定接口

### 3.1 HeartbeatSink

协议保持：

```python
class HeartbeatSink(Protocol):
    async def emit(self, heartbeat: MonitorHeartbeat) -> None: ...
    async def aclose(self) -> None: ...
```

不把状态查询强加给所有 sink；只有支持状态的 sink 实现额外只读 `status()`。

### 3.2 HeartbeatPersistenceStatus

固定为 frozen DTO：

```text
enabled: bool
last_attempt_at: datetime | null
last_success_at: datetime | null
status: disabled | idle | ok | write_failed | permission_denied | path_invalid | lock_timeout | short_write | closed
error_code: str | null
```

稳定错误码：

```text
HEARTBEAT_WRITE_FAILED
HEARTBEAT_PERMISSION_DENIED
HEARTBEAT_PATH_INVALID
HEARTBEAT_LOCK_TIMEOUT
HEARTBEAT_SHORT_WRITE
HEARTBEAT_SINK_CLOSED
HEARTBEAT_PAYLOAD_TOO_LARGE
```

状态对象不包含路径、payload、异常原文或 inode。

### 3.3 CompositeHeartbeatSink

构造参数使用有序 sink tuple。`emit()` 按注册顺序调用全部 sink，单个 sink 抛错不得阻断后续 sink；`aclose()` 幂等并尝试关闭全部 sink。

`statuses()` 只调用已有的内存 `status()`，不得触发文件 I/O。组合 sink 自己不复制 persistence 状态，也不把错误折叠成一个自由文本。

## 4. JsonlHeartbeatSink 写入语义

每个 sink 持有一个 `asyncio.Lock`，覆盖：

```text
closed check
-> last_attempt_at
-> payload serialization and size check
-> heartbeat flock
-> safe open / write-all / close
-> success or failure status
```

实际 flock 和文件系统操作通过 `asyncio.to_thread()` 执行，不在事件循环中进行阻塞系统调用。整个 blocking helper 在 worker thread 内拥有和释放 fd，不把 fd 跨线程交还 async 层。

`to_thread()` 的取消语义必须显式收口。取消等待它的 async task 不会停止已经运行的 worker thread，因此实现必须在持有 sink `asyncio.Lock` 的期间创建 worker task，并使用 `asyncio.shield()` 等待。若外层收到 `CancelledError`，必须继续等待 worker 真正结束、按 worker 结果完成本次内部状态更新，然后释放锁并重新抛出 `CancelledError`。

固定顺序为：

```text
acquire asyncio.Lock
-> create task for asyncio.to_thread(blocking helper)
-> await asyncio.shield(worker task)
-> if cancelled, await worker task to completion
-> apply this emit result exactly once
-> release asyncio.Lock
-> propagate CancelledError
```

由此锁定以下保证：

- `asyncio.Lock` 在 worker thread 完成前不得释放；
- `aclose()` 必须等待正在进行的 emit 收口；
- `CancelledError` 最终必须向上传播，不得转换为 persistence status；
- 首次或重复取消均不得留下无所有者的 blocking helper，等待 worker 收口期间仍须屏蔽对 worker 的取消；
- worker 结果只更新一次，不得在后续 emit 后乱序覆盖状态；
- 第一版接受有界取消延迟，上界由 flock timeout 和不超过 64 KiB 的 bounded write duration 共同约束。

写入固定为：

```text
serialize utf8 JSON + newline
-> reject len > 64 KiB
-> acquire heartbeat flock with bounded timeout
-> safe open O_APPEND | O_CREAT | O_WRONLY | O_NOFOLLOW
-> fstat regular file
-> fchmod fd to 0600
-> write-all loop
-> close
-> release flock
```

第一版 `heartbeat.lock` 使用 `O_CREAT | O_RDWR | O_NOFOLLOW` 打开，随后 `fstat` 验证 regular file、`fchmod(fd, 0o600)`，再以 `LOCK_EX | LOCK_NB` 轮询获取 flock。轮询使用固定短 sleep 和总 timeout，不得在 event loop 中 sleep；lock 文件存在不表示锁已占用。

父目录必须位于已经通过 production private-root contract 的 `~/.tg-hub/runtime/`。实现仍须 `lstat` immediate parent、拒绝 symlink，并验证它是 directory，且 group/other 权限位均为零（`mode & 0o077 == 0`）。标准安装结果为 `0700`；运行时允许更严格权限，但实际不可写时返回稳定权限错误。文件打开后只通过 fd 执行 `fchmod`，不得再次使用路径 `chmod`。3B 不扩大为通用安全文件系统库。

短写后继续 write-all，只有最终无法写满才返回 `HEARTBEAT_SHORT_WRITE`。active 文件在 crash 情况下仍允许 partial tail，3B 不实现 reader 或修复器。

`fsync_each_emit=no`。成功表示完整 payload 已交给内核，不表示断电持久。

## 5. Admin 装配

`MonitorControlService.start()` 固定创建：

```text
control_sink = ControlHeartbeatSink(_on_heartbeat)
persistence_sink = JsonlHeartbeatSink(settings.HEARTBEAT_PATH)
composite_sink = CompositeHeartbeatSink(control_sink, persistence_sink)
bootstrap_factory(composite_sink)
```

Service 分别保留：

- `ControlHeartbeatSink`：读取 latest heartbeat；
- `JsonlHeartbeatSink`：读取 persistence status；
- `CompositeHeartbeatSink`：交给 bootstrap 并由 bootstrap 关闭。

不得让 `MonitorControlService` 和 `MonitorBootstrap` 双重关闭同一个 sink。现有所有权保持：bootstrap owns `heartbeat_sink.aclose()`。

所有权转移点固定为 bootstrap 成功构造并接收 composite sink 之后。`start()` 的幂等/运行态检查必须发生在创建任何新 sink 之前，避免重复启动产生无人关闭的 sink。

若 bootstrap 构造在 task 创建前失败，ControlService 必须显式关闭尚未移交所有权的 composite sink。

`BootstrapFactory` 参数类型改为通用 `HeartbeatSink`。现有测试 fake 不得依赖具体 Control sink 类型。

## 6. CLI 装配

CLI `run` 默认持久化到 `Settings.HEARTBEAT_PATH`，不再默认 `NullHeartbeatSink`。

`--heartbeat-jsonl` 只允许作为测试或显式运维覆盖路径。生产环境中的覆盖路径继续受 private-root/path safety 校验，不能绕过生产路径边界；测试环境可以使用受测试控制的临时目录，但仍须覆盖 symlink、文件类型和权限安全测试。`NullHeartbeatSink` 只用于测试注入或未来明确的 disabled flag，本阶段不新增 heartbeat disabled 配置。

CLI 和 Admin 必须调用同一个 sink factory/helper，避免两套装配规则。

## 7. 状态投影

`MonitorControlSnapshot` 增加：

```text
heartbeat_persistence: HeartbeatPersistenceStatus
```

规则：

- Monitor 从未启动：`enabled=true, status=idle`；
- 正常持久化：`status=ok`；
- 写失败：展示稳定 status/error_code，但不改变 application readiness；
- Monitor stopped 后保留最后 persistence status，不伪造新的 heartbeat；
- sink 已关闭可显示 `closed`，但 last_success_at 保留；
- Admin API 继续 no-store 和脱敏。

3B 只做 API/status DTO 投影。管理页面若已有通用 heartbeat 状态区域可最小接入；不得扩展为日志或轮转页面。

## 8. 异常隔离

- Control sink 失败不阻止 persistence sink；
- persistence sink 失败不阻止 Control sink 和 MonitorRuntime；
- Composite 捕获普通 `Exception`，但不吞 `CancelledError`；
- persistence 状态更新失败不得递归写 heartbeat；
- `aclose()` 与 emit 由 sink 内部 lock 排序；
- close 幂等；close 后 emit 不打开文件；
- payload 序列化失败映射为 `HEARTBEAT_WRITE_FAILED`，不输出 payload。

`JsonlHeartbeatSink.emit()` 的外部异常语义固定为：

- 可预期持久化失败，包括 permission denied、invalid path、flock timeout、short write 和 payload too large：更新稳定 persistence status，不向 `MonitorRuntime` 抛出，正常返回；
- 编程错误或未知异常：先将状态更新为 `write_failed`，随后原样抛出，由 `CompositeHeartbeatSink` 隔离并继续调用其他 sink；
- `CancelledError`：不得转换成 persistence status，按第 4 节等待 worker 收口后重新抛出。

## 9. 测试门槛

必须覆盖：

1. short-open，每次 emit 后无长期文件句柄；
2. 文件和 lock 权限为 `0600`，标准安装目录为 `0700`，运行时接受 group/other 权限位为零的更严格目录；
3. symlink parent 和 symlink target 拒绝；
4. non-regular target 拒绝；
5. payload 超过 64 KiB 拒绝且不写文件；
6. write-all 和最终 short-write 错误码；
7. flock timeout 错误码；
8. 崩溃遗留 lock 文件不阻塞新 flock；
9. emit 并发串行，状态时间不乱序；
10. emit 与 aclose 并发稳定；
11. close 后 emit 返回 closed 状态且不写文件；
12. Composite 单 sink 失败不阻断其他 sink；
13. Composite close 尝试所有 sink 且幂等；
14. Admin start 同时更新内存 heartbeat 和 JSONL；
15. CLI 默认写 `HEARTBEAT_PATH`；
16. persistence 失败不改变 Monitor/application readiness；
17. status/API 不泄漏路径和 payload；
18. 既有 Monitor、Admin、CLI 和完整回归通过。
19. emit 在 `to_thread()` 执行期间被取消：worker 最终完成或稳定失败、`asyncio.Lock` 不提前释放、`aclose()` 等待 emit 收口、`CancelledError` 最终传播、不发生 close 后写入，且后续 emit 状态不乱序。

## 10. 禁止范围

3B 不得实现：

- heartbeat reader、partial-tail repair；
- heartbeat rename 或 gzip；
- rotate.lock 或 rotation engine；
- application log rotation；
- retention、archive budget；
- rotation LaunchAgent；
- online Telegram session authorization；
- 新业务指标或修改 `MonitorHeartbeat` DTO。

## 11. 最终授权

```text
P6-DEPLOY-3B_REVIEW:
  result: approved
  blockers: 0
  allow_P6_Deploy_3B: yes
  allow_P6_Deploy_3C: no
  allow_P6_Deploy_3D: no
```

下一步只允许实现 P6-Deploy-3B。
