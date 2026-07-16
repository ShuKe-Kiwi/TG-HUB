# P6-Deploy-4D-4C-2 Generated PostgreSQL Happy-Path 验收结果

> 项目：tg-hub
> 日期：2026-07-16
> 状态：complete
> 模式：本机 generated PostgreSQL；不读取生产配置、生产备份包或生产数据

## 验收结论

```text
P6-DEPLOY-4D-4C-2_HAPPY_PATH_RESULT:
  status: pass
  generated_source_created: yes
  alembic_head_applied: yes
  bounded_synthetic_rows_seeded: yes
  custom_dump_created: yes
  dump_catalog_validated: yes
  generated_replacement_created: yes
  restore_completed: yes
  read_only_verification_completed: yes
  temp_config_switch_completed: yes
  real_monitor_started: no
  rollback_to_generated_source_completed: yes
  replacement_guarded_cleanup_completed: yes
  source_guarded_cleanup_completed: yes
  global_generated_database_residue: 0
  production_config_accessed: no
  production_package_accessed: no
  production_database_modified: no
  launch_agent_modified: no
  telegram_accessed: no
  fault_injection_used: no
  full_regression: 762 passed, 4 skipped
  allow_4C_3: no
  allow_production_recovery: no
```

## 执行记录

首次执行在 PostgreSQL 强制 READ ONLY verifier 入口安全停止。原因是 generated rehearsal 数据库
名称与旧 restore-verify 名称白名单属于两个独立命名空间。停止发生在配置切换前，未访问或修改
生产对象。

实现随后增加显式 `generated_rehearsal_only` 验证策略；默认 restore-verify 策略保持不变，并增加
严格名称拒绝测试。第二次执行完成：

```text
generated source
-> Alembic head
-> synthetic rows
-> custom dump + catalog validation
-> generated replacement restore
-> READ ONLY schema/constraint/integrity verification
-> temp switch/readiness
-> rollback source/readiness
-> replacement child cleanup
-> source cleanup
-> rehearsal_terminal
```

首次安全停止遗留的两个 generated database 没有通过无条件 teardown 删除。清理前从 durable record
读取身份，并逐项验证 generated name、owner OID、精确 COMMENT token、active connections=0、
prepared transactions=0；验证通过后才执行 guarded DROP。最终只读 inventory 确认全局 residue 为 0。

## 边界结论

本次验收只能证明 4D-4 durable state machine 能在本机自动生成的 PostgreSQL 数据库上完成合成
dump、restore、验证、临时切换、回滚和受控清理。它不证明真实生产备份恢复成功，也不授权生产
数据库切换、4C-3 故障注入或 Deploy-5。
