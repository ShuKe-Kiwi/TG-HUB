# P6-Deploy-4D-4C-2 Read-Only Runtime Preflight 结果

> 日期：2026-07-16
> 模式：read-only
> 当前结果：pass（第二次预检）

## 首次预检（历史）

```text
error_code: TEMP_REHEARSAL_CONNECTION_UNSAFE
loopback_target: yes
maintenance_database: postgres
pg_dump: PostgreSQL 16.14
pg_restore: PostgreSQL 16.14
role_can_create_database: yes
role_is_superuser: yes
blocker: dedicated_non_superuser_createdb_role_missing
database_writes_attempted: no
generated_database_created: no
generated_database_dropped: no
production_config_accessed: no
production_package_accessed: no
```

### 首次判定

当前本机角色不满足 4C-2 的最小权限模型。真实 generated PostgreSQL 演练继续禁止。

下一步只能先建立 dedicated、`CREATEDB=true`、`rolsuper=false` 的本机测试角色，并确保其只能连接
loopback maintenance database。创建角色属于 PostgreSQL 状态修改，需要用户另行明确授权；本次
只读 preflight 未执行该动作。

## 第二次预检

完成 dedicated role 创建后，以 `tg_hub_4c_rehearsal` 重新执行：

```text
status: pass
loopback_target: yes
maintenance_database: postgres
server_major: 16
pg_dump_major: 16
pg_restore_major: 16
role_can_create_database: yes
role_is_superuser: no
prepared_xacts_readable: yes
activity_inventory_readable: yes
system_identifier_readable: yes
generated_names_absent: yes
database_writes_attempted: no
generated_database_created: no
generated_database_dropped: no
production_config_accessed: no
production_package_accessed: no
```

当前 runtime preflight Gate 已通过。该结论只解除 4C-2 generated PostgreSQL happy-path 演练的
前置阻塞，不代表演练已经执行，也不授权 4C-3 故障注入或生产恢复。
