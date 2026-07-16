# P6-Deploy-4D-4 第三轮独立设计复审

> 项目：tg-hub
> 阶段：P6-Deploy-4D-4
> 日期：2026-07-16
> 结果：revise_required

## 1. 总体结论

第二轮提出的 3 个 blocker 已实质关闭：

- rollback application start/readiness 已拆成 intent/result；
- cleanup 已改为 production child record + 共享 guarded DROP primitive；
- `replacement_activated` 已定义为单调历史事实。

但沿 rollback 全链继续检查，发现 1 个新的 durable-intent blocker。当前仍不能进入 4D-4A。

```text
P6-DEPLOY-4D-4_THIRD_REVIEW:
  result: revise_required
  architecture_direction: aligned
  first_review_blockers_resolved: 4/4
  second_review_blockers_resolved: 3/3
  new_blockers: 1
  remaining_recommendations: 4
  allow_P6_Deploy_4D_4A_implementation: no
  allow_P6_Deploy_4D_4B_implementation: no
  allow_P6_Deploy_4D_4C_temp_postgres_rehearsal: no
  allow_P6_Deploy_4D_4D_runbook_archive: no
  allow_real_production_config_read: no
  allow_real_service_lifecycle_change: no
  allow_real_production_restore: no
  allow_P6_Deploy_5: no
```

## 2. 已关闭问题复核

### 2.1 Rollback application/readiness

当前已明确：

```text
rollback_application_start_started
-> rollback_application_started
-> rollback_readiness_started
-> rollback_readiness_passed
```

stopped、exact original process、wrong/duplicate process 三态也有协调规则。该 blocker 关闭。

### 2.2 Child cleanup

当前选择了独立 production child schema：

```text
planned -> cleanup_started -> cleanup_completed
```

4C 与 production child 只共享无 record 所有权的 `GuardedDatabaseCleanupPrimitive`。main requested
但 child absent、DROP 完成但 terminal write 失败、child terminal 但 main update 失败均有收敛规则。
该 blocker 关闭。

### 2.3 Replacement activation

active env 首次被可靠观察为 replacement 后，`replacement_activated` 单调写为 `yes`，rollback
后也不改回 `no`。cleanup 仍需 durable `rolled_back` 和 active env original。该 blocker 关闭。

## 3. 新 Blocker：Rollback application stop 缺少 intent

当前 rollback 链起始为：

```text
rollback_started
-> rollback_monitor_stopped
-> rollback_application_stopped
```

`rollback_started` 可以作为“停止 Monitor”的 durable intent，`rollback_monitor_stopped` 是该动作
的完成事实。但随后停止 application 前没有独立 intent。

崩溃窗口：

```text
phase = rollback_monitor_stopped
-> application stop 成功
-> process crashes before rollback_application_stopped
```

恢复后仅凭 phase 无法区分 application 仍运行、已停止、停止命令部分失败或错误实例仍运行。
虽然 stop 通常可重试，但设计已经要求 lifecycle 动作必须结合 exact process/database identity
协调，不能由实现者自行假设 stop 幂等。

必须增加：

```text
rollback_monitor_stopped
-> rollback_application_stop_started durable
-> stop exact application
-> verify stopped/connections drained
-> rollback_application_stopped
```

协调矩阵至少覆盖：

```text
rollback_application_stop_started + exact app running
  -> retry stop
rollback_application_stop_started + app stopped/connections drained
  -> write rollback_application_stopped
rollback_application_stop_started + wrong/duplicate process
  -> manual reconciliation
```

并把该 phase 纳入合法转换表与失败注入测试。

## 4. 非阻塞建议

1. Protection-skip authorization 增加 fixture DTO、nonce/expiry/single-use 与 source-unreadable
   evidence binding。
2. Monitor-start authorization 增加独立 fixture DTO 与 consumed identity，而不是只写 observation。
3. `cleanup_required` 增加完整 truth table。
4. rollback 全表增加 forbidden transitions，便于状态机测试生成。

## 5. 最终判定

当前准确状态：

```text
result: revise_required
blockers: 1
ALLOW_P6_DEPLOY_4D_4A_IMPLEMENTATION: no
ALLOW_REAL_PRODUCTION_CONFIG_READ: no
ALLOW_REAL_SERVICE_LIFECYCLE_CHANGE: no
ALLOW_REAL_PRODUCTION_RESTORE: no
ALLOW_P6_DEPLOY_5: no
```

下一步只需补齐 rollback application stop intent，再进行最终准入复审。不得进入代码实现。
