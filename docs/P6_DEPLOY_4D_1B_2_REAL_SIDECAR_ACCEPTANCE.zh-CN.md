# P6-Deploy-4D-1B-2 真实 Sidecar 签发验收

> 项目：tg-hub  
> 阶段：P6-Deploy-4D-1B-2 real restore verification sidecar acceptance  
> 日期：2026-07-16  
> 状态：complete  
> ALLOW_P6_DEPLOY_4D_2：no  
> ALLOW_PRODUCTION_RESTORE：no  
> ALLOW_P6_DEPLOY_5：no

## 1. 验收范围

本次仅对已通过 4B 静态验证的真实备份包执行：

```text
read-only baseline
-> isolated database create
-> pg_restore from recovery-owned snapshot
-> schema / constraint / integrity verification
-> guarded isolated database DROP
-> recovery and snapshot cleanup
-> verification sidecar durable commit
-> same-lock inventory projection
```

未修改生产数据库、生产 watchlist 或生产配置；未执行 pin、retention、生产恢复或
P6-Deploy-5。

## 2. 执行前基线

```text
app_env_production: yes
backup_inventory: pass
candidate_package_count: 1
candidate_restore_verified: no
candidate_verification_status: missing
legacy_restore_database_count: 0
restore_recovery_record_count: 0
restore_snapshot_count: 0
migration_revision_present: yes
```

候选 package 的 manifest、database dump、watchlist snapshot 和 catalog 均通过；三个
package 文件权限均为 `0600`。

## 3. 首次执行与受控清理

首次真实执行在 `pg_restore` 阶段稳定返回：

```text
status: fail
error_code: RESTORE_FAILED
target_created: yes
cleanup_required: yes
sidecar_written: no
```

系统生成 opaque cleanup handle。验收严格使用该 handle 执行 guarded cleanup，结果为：

```text
cleanup_status: pass
target_dropped: yes
record_deleted: yes
```

未使用裸 `dropdb`，未手工删除 recovery record、snapshot 或异常 Sidecar。

## 4. 契约修正

排查确认 `pg_restore` 在 argv 同时缺少 `-d/--dbname` 和 `-f/--file` 时不会连接
`PGDATABASE` 执行恢复，而是直接拒绝执行。现有 4C 文档已经锁定正确契约：

```text
argv:
  pg_restore
  --dbname=<generated isolated target>
  --no-owner
  --no-acl
  --exit-on-error
  <recovery-owned snapshot>

env:
  PGDATABASE=<same generated isolated target>
```

实现恢复为该契约，并继续保证 argv 不包含 production database、连接 URL、用户或密码。
定向回归为 `38 passed, 1 skipped`，完整回归为 `619 passed, 3 skipped`。

## 5. 真实重跑结果

```text
status: pass
target_created: yes
restore_completed: yes
schema_verified: yes
constraints_verified: yes
integrity_verified: yes
target_dropped: yes
cleanup_required: no
cleanup_handle: null
error_code: null
report_desensitized: yes
```

只有在隔离恢复、三类验证、guarded DROP、snapshot cleanup 和 recovery record cleanup
全部成功后，才原子签发 verification Sidecar。

## 6. 验收后状态

```text
inventory_status: pass
restore_verified: yes
verification_status: valid
verification_version: 1
restore_verified_count: 1
manual_review_count: 0
remaining_restore_database_count: 0
remaining_recovery_record_count: 0
remaining_restore_snapshot_count: 0
sidecar_mode: 0600
```

Sidecar 与当前 package identity 精确绑定；inventory 在相同 shared backup lock 生命周期内
回读目标 package，并投影为 `restore_verified=yes`。

## 7. 结论

```text
P6-DEPLOY-4D-1B-2_REAL_SIDECAR_ACCEPTANCE:
  result: pass
  real_C2_rerun: pass
  recovery_owned_snapshot: pass
  guarded_cleanup_after_initial_failure: pass
  sidecar_durable_commit: pass
  inventory_projection: pass
  residual_restore_database: no
  residual_recovery_record: no
  residual_restore_snapshot: no
  production_database_modified: no
  allow_P6_Deploy_4D_2: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

本阶段只能证明当前真实备份包能够在隔离数据库中恢复并通过验证，且可信 verification
Sidecar 已签发。下一步必须单独评审 P6-Deploy-4D-2 retention / pin；生产恢复继续禁止。
