# P6-Deploy-4B 真实生产备份验收

> 项目：tg-hub  
> 阶段：P6-Deploy-4B real backup acceptance  
> 日期：2026-07-14  
> 状态：complete  
> ALLOW_P6_DEPLOY_4C：no  
> ALLOW_RESTORE_VERIFY：no  
> ALLOW_PRODUCTION_RESTORE：no

## 1. 验收范围

本次仅执行：

```text
read-only baseline
-> static backup preflight
-> one production pg_dump
-> final package commit
-> read-only package validation
-> post-backup health/readiness
```

未执行 restore database、`pg_restore` 数据恢复、retention、scheduler、生产恢复或
P6-Deploy-4C。

## 2. 基线

- 主 LaunchAgent：running；
- production env：存在，权限保持私有；
- `pg_dump`：PostgreSQL 16.14；
- `pg_restore`：PostgreSQL 16.14；
- 磁盘可用空间：约 1.7 TiB；
- backup static preflight：pass；
- health live：pass；
- readiness：database/migration/watchlist/assembly 全部 pass；
- Git worktree：clean。

## 3. 配置收口

首次真实执行被稳定拒绝：

```text
DATABASE_URL_UNSUPPORTED
package_state=not_created
```

原因是生产 URL 依赖隐式 PostgreSQL 用户。只读确认数据库当前角色与本机角色一致
后，使用结构化 URL 解析为现有 URL 补充显式用户字段；未改变 host、database、
port、密码或 query，未输出 URL/用户名/凭据。`production.env` 以原子替换更新并保持
`0600`，随后主 LaunchAgent 重启且 readiness 恢复通过。

首次失败未创建 final/temp package。

## 4. 真实备份结果

```text
status: pass
backup_id: 20260714T100055.207160Z-49502732533cd7470f883488ed0a6131
package_state: final_committed
backup_bytes: 42685
manifest_valid: yes
database_dump_valid: yes
watchlist_snapshot_valid: yes
error_code: null
report_desensitized: yes
```

备份期间未停止主服务，未修改生产数据库或 watchlist，未访问 Telegram API。

## 5. 独立只读验证

```text
validator_status: pass
required_catalog_objects_present: yes
file_set_exact: yes
package_mode_private: yes
files_mode_private: yes
production_env_excluded: yes
telethon_session_excluded: yes
pending_temp_count: 0
```

final package 只包含：

- `database.dump`；
- `watchlist.json`；
- `manifest.json`。

## 6. 验收后状态

- 主 LaunchAgent：running；
- health live：pass；
- readiness：pass；
- Monitor：running/listening，12/12 个频道已解析；
- 数据库、迁移、watchlist、assembly：pass；
- restore database：未创建；
- retention：未执行；
- 生产恢复：未执行。

## 7. 结论

```text
P6-DEPLOY-4B_REAL_BACKUP_ACCEPTANCE:
  result: pass
  production_backup_created: yes
  final_package_committed: yes
  package_validation: pass
  core_catalog_validation: pass
  sensitive_material_excluded: yes
  application_health_after_backup: pass
  monitor_state_restored: yes
  restore_verified: no
  allow_P6_Deploy_4C: no
  allow_production_restore: no
```

本阶段只能证明生产数据库和 watchlist 已生成一个通过静态验证的真实备份包，不能
证明该包可成功恢复。下一步必须独立评审 P6-Deploy-4C 隔离恢复验证，批准后才能在
非生产隔离数据库中执行恢复。
