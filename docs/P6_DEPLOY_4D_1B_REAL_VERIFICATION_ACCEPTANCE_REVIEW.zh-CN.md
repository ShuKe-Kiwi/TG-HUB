# P6-Deploy-4D-1B 真实恢复验证 Sidecar 签发评审

> 项目：tg-hub
> 阶段：P6-Deploy-4D-1B
> 状态：implementation-patch-approved
> 前置：P6-Deploy-4D-1 已完成并提交（`59033b7`）
> ALLOW_IMPLEMENTATION_PATCH：yes
> ALLOW_REAL_C2_RERUN：no
> ALLOW_REAL_SIDECAR_WRITE：no
> ALLOW_P6_DEPLOY_4D_2：no

## 1. 评审目标

4D-1B 不是根据历史 C2 报告补写 sidecar，而是对选定真实 4B package 重新执行完整 C2：

```text
read-only package validation
-> generated isolated database
-> pg_restore
-> schema / constraint / integrity verification
-> guarded DROP
-> recovery record cleanup
-> verification sidecar durable commit
-> inventory read-back
```

本阶段只允许签发：

```text
<BACKUP_DIR>/.verifications/<backup_id>.json
```

禁止 pin、retention、生产恢复、生产数据库覆盖、watchlist 修改或 Monitor 生命周期变更。

## 2. 当前实现已通过部分

以下实现方向成立：

- C2 持有 backup shared lock；
- sidecar 使用固定 schema、canonical backup ID 和三项 checksum identity；
- sidecar 目录 `0700`、文件 `0600`；
- 写入使用 no-follow、atomic replace、file/directory fsync；
- sidecar 只在 restore、三类 verification、guarded DROP 全部成功后签发；
- recovery record 删除失败时不会提前签发 sidecar；
- sidecar 写失败返回 `BACKUP_VERIFICATION_WRITE_FAILED`；
- sidecar 写失败不伪造 `restore_verified=yes`，也不要求数据库 cleanup；
- inventory 能区分 missing、valid、invalid，并将 mismatch 放入 manual review；
- 历史 C2 结果不会自动反向生成 sidecar；
- 4D-1 fake/temp 与全量回归已通过。

## 3. Blocker 1：C2 未冻结完整 package identity

当前顺序是：

```text
validator validates manifest/dump/watchlist
-> restore and verify isolated database
-> guarded DROP
-> recovery record delete
-> recompute manifest SHA-256 only
-> write sidecar using old parsed manifest + final manifest hash
```

问题在于 backup shared lock 是协作锁，不能阻止同一用户绕过应用直接修改 package。
validator 完成后至 sidecar 签发前，manifest、dump 或 watchlist 都可能变化。

当前代码只在最后计算 manifest hash，没有保存初始 manifest hash，也没有最终复核 dump 与
watchlist。可能形成混合 identity：

```text
old parsed manifest fields
+ final changed manifest SHA-256
+ unverified final dump/watchlist filesystem state
```

此时 sidecar 可能为当前 package 签发，但隔离恢复实际验证的是较早版本。

### 必须修正

C2 在第一次 validator 通过后、创建数据库前，冻结：

```text
initial_backup_root_device + inode + type
initial_canonical_backup_id
initial_manifest_sha256
initial_database_dump_sha256 + size
initial_watchlist_sha256 + size
initial_parsed_manifest_identity
```

这与 4D 总设计一致：verification identity 仍然只由 canonical `backup_id` 与三项 SHA-256
构成，不在 4D-1B 中新增 package directory inode 作为 hard identity。backup root identity
用于确认整个操作始终位于同一个受控根；package directory device/inode/type 可以记录为
rename-replace 观测证据，但其变化本身不能在内容 identity 完全一致时单独导致拒绝签发。

root 与 package 必须 no-follow，package 必须始终是同一 canonical backup root 的直接子目录，
不得接受 symlink、traversal 或挂载点切换。三个文件的值必须来自 safe-open、exact-N、双
fstat 的流式读取，并与 parsed manifest 完全一致。每个文件的读取顺序固定为：

```text
first fstat identity/size
-> stream exactly N bytes to EOF
-> checksum
-> final fstat identity/type/size unchanged
```

在 guarded DROP 和 recovery record cleanup 后、sidecar 写入前，再次流式读取三文件：

```text
final backup root identity == initial backup root identity
AND final canonical backup_id == initial canonical backup_id
AND final three-file identity == initial three-file identity
AND final parsed manifest identity == initial parsed manifest identity
```

package 目录 inode/device 变化但上述 hard identity 完全一致时允许继续，同时记录
`package_directory_replaced_observed=yes` 作为私有审计事实；该字段不得进入 verification
identity。backup root identity、canonical backup ID、三文件内容、文件安全类型或 parsed
manifest identity 任一变化，才判定 package 已变化。

任一变化：

```text
status=fail
error_code=BACKUP_PACKAGE_CHANGED_DURING_VERIFY
target_dropped=yes
cleanup_required=no
sidecar_written=no
```

不得将 package identity 变化映射成普通 sidecar 写失败。

## 4. Blocker 2：已有 sidecar 会被无条件覆盖

`BackupVerificationStore.write_passed()` 当前直接 atomic replace 目标路径。若目标已存在：

- valid 且 identity 相同：会无意义刷新 `verified_at_utc`；
- invalid：异常证据会被覆盖；
- valid 但 identity 不同：旧签发证据会被覆盖；
- unsupported newer version：会被当前版本静默替换。

这与设计中的规则冲突：invalid、orphan 或 identity mismatch 必须进入 manual review，不得
由普通流程自动清理或覆盖。

### 必须修正

sidecar commit 固定为 create-or-confirm：

```text
target missing
  -> create new sidecar atomically

target valid and exact same identity
  -> idempotent success
  -> preserve original verified_at_utc
  -> do not rewrite

target exists but invalid / unsupported / identity mismatch
  -> BACKUP_VERIFICATION_IDENTITY_INVALID
  -> do not replace
  -> manual_review
```

首次创建不能使用可覆盖目标的普通 `os.replace()` 作为唯一 existence guard。应在持有 backup
shared lock 的前提下使用安全的 create-if-absent 协议；并明确并发两个 C2 签发者时只有一个
创建者，另一方只能 read-back exact identity 后幂等成功。

create-or-confirm 的成功实现必须完成 durable commit，而不是以 `write()` 返回为准：

```text
exclusive create temp/final candidate
-> write-all
-> file fsync
-> create-if-absent commit
-> verification directory fsync
-> return success
```

file fsync 与 directory fsync 是 creator 的实现要求，不是其他进程可观察的协议状态，也不引入
`durable_pending` 一类外部状态。并发 loser 遇到 target exists 时总是 safe-open read-back：

```text
exact-valid identity
  -> idempotent success
invalid / unsupported / mismatch / unreadable
  -> BACKUP_VERIFICATION_IDENTITY_INVALID
```

loser 不能推断或等待另一进程的 directory fsync，也不能覆盖、touch 或重新签发。creator 在
directory fsync 前失败必须返回稳定写入失败；后续调用只根据磁盘上可 safe-open 的 sidecar
执行 create-or-confirm，不依赖不可观察的 winner 状态。

## 5. Inventory read-back 约束（非 Blocker）

若 sidecar commit 后先释放 backup shared lock，再让 inventory 扫描整个 `BACKUP_DIR`，扫描结果
可能混入并发创建、删除或状态变化的其他 package。此时全局 `verified_count`、
`manual_review_count` 或 candidate 集合不能严格证明刚签发的目标 package 已被正确投影。

sidecar 签发、safe-open read-back 和目标 inventory 投影必须位于同一 backup shared lock
生命周期内，并绑定同一 frozen package identity：

```text
sidecar durable commit
-> safe-open sidecar read-back
-> exact schema/identity/mode verification
-> inventory reads target backup_id under the same shared lock
-> require target restore_verified=yes and verification_status=valid
-> release shared lock
```

inventory 可以同时生成全局统计，但 Gate 的成功判定只能依赖目标 `backup_id` 与本轮 frozen
identity，不能依赖可能变化的全局计数。第一版在同一 shared lock 内完成目标读取已经足够；
inventory generation ID 仅在未来引入缓存或持久化 snapshot 时再设计，不作为本轮 blocker。

## 6. Required clarification 1：签发后必须 read-back

`write_passed()` 返回不等于 4D-1B Gate 完成。真实验收必须在同一 backup shared lock
生命周期内：

```text
sidecar commit
-> safe-open read-back
-> schema validate
-> exact identity compare
-> mode 0600
-> parent mode 0700
```

随后在同一 shared lock 内运行目标 package 的只读 inventory 投影，要求：

```text
status=pass
target package restore_verified=yes
verification_status=valid
verification_version=1
target manual_review=no
```

全局 `manual_review_count` 可作为报告信息，但不得作为本次目标签发的 generation 证明。
inventory 输出不得包含 checksum、绝对路径或 package 内容。

## 7. Required clarification 2：真实命令环境

4D-1B 必须显式使用：

```text
TG_HUB_ENV_FILE=~/.tg-hub/production.env
```

禁止依赖当前 shell 的 `.env`、隐式 `DATABASE_URL` 或临时覆盖连接串。执行前应只读确认：

- `APP_ENV=production`；
- `DATABASE_URL` 包含显式 PostgreSQL user；
- `BACKUP_DIR` 是预期私有根；
- PostgreSQL major 与 package 一致；
- 当前 migration 为 head；
- package validator 通过。

报告只输出布尔状态，不输出 URL、user、路径或凭据。

## 8. Required clarification 3：真实 Gate 停止条件

执行前必须确认：

- Git worktree clean；
- 4D-1 完整回归通过；
- 选定 package 当前没有 sidecar，或 existing sidecar exact-valid；
- 无 `tg_hub_restore_verify_*` 遗留数据库；
- 无 restore recovery record；
- 无 backup exclusive operation；
- 磁盘空间足够创建隔离数据库。

执行中出现以下任一情况立即停止，不签发 sidecar：

- package validator 失败；
- package identity 变化；
- restore/schema/constraint/integrity 失败；
- guarded DROP 未完成；
- recovery record cleanup 未完成；
- sidecar path 已存在但不是 exact-valid；
- read-back 或 inventory 投影失败。

失败时只允许使用既有 opaque cleanup handle；禁止裸 `dropdb` 或手工删除异常 sidecar。

## 9. 非阻塞实施建议

- sidecar schema 可增加稳定的 `issued_by_version`，用于区分签发工具版本；该字段不得替代
  `verification_version`。
- exact-valid sidecar 幂等确认必须同时保留 `verified_at_utc` 与文件 mtime，禁止 touch。
- inventory 回读是只读文件系统操作，不打开数据库；shared lock 所有权必须与 C2 使用同一
  backup lock factory。
- package directory device/inode/type 可作为私有审计 evidence，但不得加入 verification
  identity 或单独触发拒绝签发。
- creator 的 file/directory fsync 属于耐久性实现要求，不得暴露成 loser 必须观察的协议状态。
- sidecar 可在后续 schema 版本增加 `issuer_instance_uuid`；本轮不得为此扩大既有 schema。
- safe-open read-back 应收敛为固定 helper：stream exact bytes、schema validate、identity compare、
  mode verify。
- `verification_version` 的 major/minor 演进留待后续版本设计，本轮继续使用整数版本 `1`。

## 10. 测试门槛

实施补丁至少覆盖：

1. 初始与最终 manifest identity 相同才签发；
2. manifest 在 C2 生命周期内变化时拒绝签发；
3. dump 在 C2 生命周期内变化时拒绝签发；
4. watchlist 在 C2 生命周期内变化时拒绝签发；
5. package 目录 inode 变化但 canonical ID、root identity、三项 checksum 与 parsed manifest
   identity 相同时允许签发并记录替换 evidence；
6. backup root device/inode、canonical ID 或三项内容 identity 变化时拒绝签发；
7. identity 变化映射 `BACKUP_PACKAGE_CHANGED_DURING_VERIFY`；
8. 流式读取期间文件 size/inode/type 变化时拒绝签发；
9. missing sidecar 首次创建成功；
10. 首次创建完成 file fsync 与 directory fsync 后才报告成功；
11. directory fsync 失败不得报告签发成功；
12. loser 遇到 exists 时直接 safe-open exact read-back，不依赖 winner 状态；
13. exact-valid sidecar 幂等成功且不改 verified_at 或 mtime；
14. invalid sidecar 不覆盖；
15. mismatched sidecar 不覆盖；
16. unsupported newer sidecar 不覆盖；
17. 并发签发只有一个 creator，另一方 exact read-back；
18. sidecar 写入后 read-back schema/identity/mode 通过；
19. C2 成功但 sidecar commit 失败时 target 已 DROP、无 cleanup handle；
20. inventory 在同一 shared lock 内投影目标 sidecar 为 restore_verified=yes；
21. 并发新增其他 package 不改变目标 backup_id 的 Gate 判定；
22. 完整回归；
23. 真实 package/C2/sidecar 仍需补丁提交后的再次明确授权。

## 11. 当前评审结论

```text
P6-DEPLOY-4D-1B_FINAL_INDEPENDENT_REVIEW:
  result: approved_for_patch
  architecture_direction: pass
  consistency_with_4C: pass
  consistency_with_4D_1: pass
  consistency_with_4D: pass
  implementation_readiness: ready
  blockers: 0
  recommendations: 4
  allow_implementation_patch: yes
  allow_real_C2_rerun: no
  allow_real_sidecar_write: no
  allow_P6_Deploy_4D_2: no
  allow_real_pin_write: no
  allow_real_retention_delete: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

当前只允许开发 4D-1B 最小实现补丁：冻结并最终复验 package identity、sidecar
create-or-confirm、安全 read-back 与同锁目标 inventory 投影。补丁通过测试、代码复审并提交后，
才能再次请求真实 C2 重跑与 sidecar 写入授权。
