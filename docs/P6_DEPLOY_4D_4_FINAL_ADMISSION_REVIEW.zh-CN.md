# P6-Deploy-4D-4 最终准入复审

> 项目：tg-hub
> 阶段：P6-Deploy-4D-4
> 日期：2026-07-16
> 结果：approved_for_4D_4A

## 1. 最终结论

第三轮唯一 blocker 已关闭。Rollback application stop 现在具备独立 durable intent、外部事实
协调和完成 phase，整个 lifecycle 链不再依赖“stop 可盲目重试”的隐含假设。

```text
P6-DEPLOY-4D-4_FINAL_ADMISSION_REVIEW:
  result: approved_for_4D_4A
  architecture_direction: aligned
  first_review_blockers_resolved: 4/4
  second_review_blockers_resolved: 3/3
  third_review_blockers_resolved: 1/1
  blockers: 0
  required_clarifications: 0
  recommendations: 4
  allow_P6_Deploy_4D_4A_implementation: yes
  allow_P6_Deploy_4D_4B_implementation: no
  allow_P6_Deploy_4D_4C_temp_postgres_rehearsal: no
  allow_P6_Deploy_4D_4D_runbook_archive: no
  allow_real_production_config_read: no
  allow_real_service_lifecycle_change: no
  allow_real_production_restore: no
  allow_P6_Deploy_5: no
```

## 2. Rollback Stop 复核

最终链路：

```text
rollback_started
-> stop Monitor
-> rollback_monitor_stopped
-> rollback_application_stop_started
-> stop exact replacement application
-> verify application stopped and connections drained
-> rollback_application_stopped
-> rollback_env_started
```

协调规则：

- exact application running：重试 stop/drain；
- application stopped 且 connections drained：补写 result phase；
- wrong/duplicate process：manual reconciliation；
- intent write 失败：不得调用 lifecycle adapter；
- result write 失败：不重复假设，按外部事实协调。

该链与正向 `services_stop_started`、rollback env/watchlist、rollback original application start 和
rollback readiness 的 intent/result 契约一致。

## 3. 全状态机复核

以下高风险动作均已有 durable intent 与崩溃协调：

- protection backup create/skip；
- replacement CREATE 与 identity COMMENT；
- pg_restore 与 read-only verification；
- 正向 Monitor/application stop 与 connection drain；
- env/watchlist protection、switch 和 read-back；
- replacement application start 与 readiness；
- Monitor independent authorization、start 与 write fence；
- rollback Monitor stop、application stop、env/watchlist restore；
- rollback original application start 与 readiness；
- production child cleanup 与 main/child 双 record 收敛。

可选分支均有合法转换：protection backup skipped、watchlist not switched、rollback without
watchlist restore、Monitor write observed yes/no。

## 4. 非阻塞建议

实施时仍建议：

1. 为 protection-skip authorization 建立严格 fixture DTO；
2. 为 monitor-start authorization 建立严格 fixture DTO；
3. 将 `cleanup_required` truth table 编码为纯函数；
4. 从 transition table 参数化生成 forbidden-transition 测试。

这些建议不改变 4D-4A 的 fake/temp-only 边界，不阻止契约与纯状态机实现。

## 5. 实施边界

现在只批准 P6-Deploy-4D-4A：

- DTO 与 phase transition table；
- temp-only stable record/lock；
- production child cleanup contract；
- fake adapters 与纯 orchestrator；
- production adapter fail closed；
- 单元测试与完整回归。

仍然禁止读取真实 production config、访问真实 backup root、连接 PostgreSQL、操作 LaunchAgent、
访问 Telegram 或执行任何真实恢复动作。
