# P6-Deploy-4D-4 第二轮独立设计复审

> 项目：tg-hub
> 阶段：P6-Deploy-4D-4
> 日期：2026-07-16
> 评审对象：P6_DEPLOY_4D_4_PRODUCTION_RECOVERY_REHEARSAL_DESIGN.zh-CN.md
> 结果：revise_required

## 1. 总体结论

第一轮提出的 4 个 blocker 已在主体设计中得到实质响应：正向 durable intent、可选分支、
rollback-entry matrix 和 cleanup 单一 owner 方向均已补入。总体架构继续对齐，真实生产恢复与
fake/temp 演练没有混淆。

但当前仍有 3 个可编码性 blocker，不能批准进入 4D-4A。

```text
P6-DEPLOY-4D-4_SECOND_REVIEW:
  result: revise_required
  architecture_direction: aligned
  previous_review_blockers_resolved: 4/4
  previous_review_recommendations_resolved: 5/5
  new_blockers: 3
  recommendations: 4
  allow_P6_Deploy_4D_4A_implementation: no
  allow_P6_Deploy_4D_4B_implementation: no
  allow_P6_Deploy_4D_4C_temp_postgres_rehearsal: no
  allow_P6_Deploy_4D_4D_runbook_archive: no
  allow_real_production_config_read: no
  allow_real_service_lifecycle_change: no
  allow_real_production_restore: no
  allow_P6_Deploy_5: no
```

## 2. Blocker 1：Rollback application/readiness 生命周期仍不唯一

正向链路已拆为：

```text
application_start_started
-> application_started
-> readiness_started
-> readiness_passed
```

但 rollback 链仍为：

```text
rollback_application_started
-> rollback_readiness_passed
-> rolled_back
```

这里无法判断 `rollback_application_started` 是启动旧应用前的 intent，还是启动成功后的结果；
也没有 `rollback_application_start_completed` 或 `rollback_readiness_started`。

崩溃窗口：

```text
rollback_application_started durable
-> old application start success
-> process crashes before readiness
```

恢复后实现者无法区分应用未启动、已正确连接 original database、使用错误 database 或存在重复
进程。直接重试 start 可能产生重复实例，直接 readiness 又可能验证错误进程。

必须固定一种可编码模型。建议：

```text
rollback_application_start_started
-> reconcile exact single process/original DB identity
-> rollback_application_started
-> rollback_readiness_started
-> rollback_readiness_passed
```

并为 stopped、exact-running、wrong/duplicate 三种事实分别定义 retry、补 phase 和 manual
reconciliation。`rolled_back` 只能从 `rollback_readiness_passed` 进入。

## 3. Blocker 2：Child cleanup 与现有 4C primitive 不兼容

修订设计声明 child record 持有：

```text
cleanup_started
cleanup_completed
```

主 record 随后 read-back child terminal result。

但当前已验收的 4C cleanup primitive 使用 `RestoreRecoveryRecord` 原 phase，并在 guarded DROP
成功后直接删除 recovery record。它不存在 `cleanup_started/cleanup_completed` terminal record。
因此“复用现有完整 primitive”和“read-back child completed record”不能同时成立。

还有一个未覆盖窗口：

```text
main cleanup_requested=yes + cleanup_record_id durable
-> child record 尚未 create
-> crash
```

现有协调矩阵没有 `child absent` 分支。

必须选择并锁定：

1. 新增 production-recovery cleanup child schema/adapter，内部复用 4C database guard/drop helper，
   但由 child schema 自己持久化 intent 与 terminal result；或
2. 完整复用现有 4C record/cleanup service，并将“child record success 后不存在”定义为合法终态，
   由 main 同时验证 target absent、child ownership evidence 和 record absence后收敛。

不能只复用 guard 条件，也不能让 main 和 child 都可能调用 DROP。还必须补齐：child absent、
child create 失败、DROP 成功但 child terminal/删除未收口、main update 失败四个窗口。

## 4. Blocker 3：`replacement_activated` 没有单调协调规则

record 已增加 `replacement_activated: yes | no`，但正向转换、env 崩溃协调和 rollback 中没有
规定何时由 `no` 变为 `yes`，也没有明确它是否可在 rollback 后改回 `no`。

安全事实应固定为：只要 active env 的 database component 曾被可靠观察为 replacement，
`replacement_activated` 必须单调写为 `yes`，即使 `env_switched` phase 尚未落盘；之后不得改回
`no`。Rollback 只改变 active env，不抹除 replacement 曾被激活的历史事实。

至少补齐：

```text
env_switch_started + active env == staged replacement
  -> reconcile replacement_activated=yes
  -> env_switched

rollback completed + active env == original
  -> replacement_activated remains yes
  -> cleanup eligibility additionally requires durable rolled_back

replacement_activated=no + active env points replacement
  -> first persist yes; never infer cleanup-safe
```

否则 cleanup、报告和 resume 会对同一外部事实产生不同结论。

## 5. 非阻塞建议

1. `skip authorization` 与 `monitor-start authorization` 增加明确 fixture DTO、identity 和
   single-use 语义，不继续放在模糊的 `authorization observations` 中。
2. rollback transition table 应像正向表一样列出每个 `from -> to`，而不是只给流程箭头。
3. `cleanup_required` 投影应固定 truth table，明确 main/child phase、target existence、active env
   与 manual reconciliation 的优先级。
4. 测试矩阵增加：main 已请求 cleanup 但 child 尚未创建；replacement 曾激活后已 rollback；
   rollback old application 已启动但 phase 未写入。

## 6. 已确认通过

- 真实 Production Recovery 与 4D-4 fake/temp 演练隔离；
- protection backup skip 和 watchlist no-switch 分支不伪造 phase；
- verification、service stop、正向 application start/readiness intent 完整；
- service stop 四项事实已拆分；
- `monitor_start_started` 后自动 rollback 永久禁止；
- env/watchlist 第三种内容 fail closed；
- Temp capability minting authority 与 Alembic revision 语义明确；
- production adapter 和全部真实 Gate 保持关闭。

## 7. 最终判定

当前准确状态：

```text
result: revise_required
blockers: 3
ALLOW_P6_DEPLOY_4D_4A_IMPLEMENTATION: no
ALLOW_REAL_PRODUCTION_CONFIG_READ: no
ALLOW_REAL_SERVICE_LIFECYCLE_CHANGE: no
ALLOW_REAL_PRODUCTION_RESTORE: no
ALLOW_P6_DEPLOY_5: no
```

下一步应修订上述 3 个契约，再进行第三轮独立设计复审；不得进入代码实现。
