# P6-Deploy-4D-4C-2 Dedicated PostgreSQL Role 设计与授权

> 日期：2026-07-16
> 状态：execution-authorized
> 适用范围：本机 loopback PostgreSQL，仅 4C generated database 演练

## 1. 目标

建立长期保留的本机测试角色 `tg_hub_4c_rehearsal`，替代当前 superuser 执行 4C generated
source/replacement 的 CREATE、dump、restore、verify 和 guarded DROP。

该角色不是生产应用角色，不写入 `production.env`，不供 LaunchAgent、Monitor、Bot 或业务服务使用。

## 2. 固定属性

```text
rolname: tg_hub_4c_rehearsal
LOGIN: yes
CREATEDB: yes
SUPERUSER: no
CREATEROLE: no
INHERIT: no
REPLICATION: no
BYPASSRLS: no
CONNECTION LIMIT: 4
COMMENT: tg-hub-4c-dedicated-role:v1
password: none managed by tg-hub
```

连接仅允许使用显式 `127.0.0.1:5432/postgres` spec。tg-hub 不保存或生成该角色密码；当前本机
`pg_hba.conf` 若不允许无密码 loopback 登录，则本 Gate 失败，不能自动修改 HBA 或写入口令。

## 3. 创建协议

```text
read-only inspect role/comment
-> absent only
-> transaction + advisory xact lock
-> CREATE ROLE with exact attributes
-> COMMENT ON ROLE
-> commit
-> read-only exact verification
-> login read-only verification as dedicated role
-> rerun 4C-2 read-only runtime preflight
```

同名角色已存在时：

- 属性和 COMMENT 全部精确一致：复用，不执行 ALTER；
- 任一字段不一致：`TEMP_REHEARSAL_ROLE_IDENTITY_CONFLICT`，停止；
- 禁止 `ALTER ROLE` 接管未知角色。

## 4. 权限边界

允许：

- 创建上述固定角色；
- 为该角色写入固定 COMMENT；
- 只读查询 `pg_roles`、`pg_authid` 可公开投影、`pg_stat_activity`、`pg_prepared_xacts`；
- 以该角色连接 maintenance database 并执行只读 preflight。

禁止：

- 修改任何现有角色；
- 授予 superuser、createrole、replication、bypassrls 或成员角色；
- 修改 `pg_hba.conf`；
- 设置、输出或保存密码；
- 修改 production database/schema/table privilege；
- 创建、恢复、COMMENT 或 DROP generated database，直到 read-only preflight 通过；
- 访问生产配置、生产备份包、LaunchAgent 或 Telegram。

## 5. 授权结论

```text
P6-DEPLOY-4D-4C-2_DEDICATED_ROLE_GATE:
  result: approved
  allow_role_read_only_inspection: yes
  allow_create_exact_dedicated_role_if_absent: yes
  allow_comment_exact_dedicated_role: yes
  allow_alter_existing_role: no
  allow_password_or_hba_change: no
  allow_read_only_preflight_as_dedicated_role: yes
  allow_generated_database_create: no_before_preflight_pass
  allow_generated_database_drop: no_before_preflight_pass
  allow_production_recovery: no
```

## 6. 回滚与保留

角色创建成功后默认保留，供后续 4C 重复验收。当前 Gate 不授权自动 `DROP ROLE`。若创建事务失败，
PostgreSQL 应回滚整个事务；若提交结果不确定，只允许重新只读检查，不重复盲目创建或 ALTER。

## 7. 执行结果

```text
status: pass
role_created: yes
role_identity_exact: yes
login_verified: yes
createdb: yes
superuser: no
createrole: no
inherit: no
replication: no
bypassrls: no
connection_limit: 4
comment_exact: yes
password_changed: no
hba_changed: no
production_privileges_changed: no
```
