# P6-Deploy-4D-3 真实 Retention 验收 Gate 设计与评审

> 项目：tg-hub
> 阶段：P6-Deploy-4D-3
> 日期：2026-07-16
> 状态：final-admission-approved-for-4D-3A
> 前置：P6-Deploy-4D-1/1B-1/1B-2/2 已完成
> ALLOW_REAL_BACKUP_DIR_READ：no
> ALLOW_REAL_BACKUP_CREATE：no
> ALLOW_REAL_C2_VERIFY：no
> ALLOW_REAL_PIN_WRITE：no
> ALLOW_P6_DEPLOY_4D_3A_IMPLEMENTATION：yes
> ALLOW_PRODUCTION_APPLY_EXECUTION：no
> ALLOW_REAL_RETENTION_DELETE：no

## 1. 阶段目标

P6-Deploy-4D-3 只验收已经锁定的 retention 选择、保护和可恢复删除机制在真实备份目录上
是否成立。它不建立长期自动清理任务，也不授权生产恢复。

完整链路固定为：

```text
只读真实目录基线
-> 新建生产备份
-> 对新备份执行 C2 隔离恢复并签发 verification sidecar
-> 真实目录 plan/dry-run
-> 人工确认 protected / candidate / verified floor / budget
-> 可选真实 pin Gate
-> 再生成全新 plan
-> 单次 apply 明确授权
-> 逐项删除与 journal/pending 收敛验收
-> 归档脱敏报告
```

阶段通过只能证明：在本次冻结快照和人工确认范围内，明确候选包可被受控删除，且至少一个
restore-verified 包继续受到保护。不能证明未来自动 retention 永远安全。

## 2. 当前实现事实

P6-Deploy-4D-2 当前状态：

- `python -m app.deploy.backup_retention plan` 是唯一生产可达命令；
- pin/unpin service 必须持有绑定临时目录的不可序列化 capability；
- apply engine 同样必须持有临时目录 capability；
- 生产 factory 不构造上述 capability；
- 生产 `pin`、`unpin`、`apply` CLI 尚不存在；
- 未执行真实 plan、pin、rename、unlink 或 retention delete。

因此 4D-3 必须先拆出生产装配实现 Gate，不能通过测试专用 capability 绕过边界，也不能在
验收脚本中直接实例化内部 mutation service。

## 2.1 一次性生产授权契约

生产 mutation 不接受普通布尔开关、环境变量或可重复使用的 capability。每次真实动作由独立
授权 owner 签发 create-once authorization record：

```text
schema_version: 1
authorization_id: random opaque id
authorization_nonce: random 256-bit value
operation: pin | unpin | pin_reconcile | retention_apply | retention_resume
backup_root_identity:
  configured_logical_root_id
  resolved_device
  resolved_inode
issued_at_utc
expires_at_utc
single_use: true
phase: issued | consumed | cancelled
consumed_at_utc: null | UTC datetime
payload: strict operation-specific discriminated union
```

`payload` 必须是严格判别联合：

```text
pin:
  backup_id
  reason_code
  expected_pin_identity

unpin:
  backup_id
  expected_pin_identity
  expected_reason_code

pin_reconcile:
  predecessor_authorization_id
  predecessor_consumed_identity
  original_operation: pin | unpin
  pin_operation_id
  pin_journal_digest
  pin_journal_phase
  backup_id
  intended_reason_code
  observed_before_pin_identity
  intended_result_pin_identity
  expected_current_pin_identity
  reconcile_scope: settle_current_only
  acknowledge_current_partial_state

retention_apply:
  plan_id
  plan_digest
  ordered_candidate_backup_ids
  expected_candidate_count
  expected_reclaim_bytes
  acknowledge_non_transactional_multi_package_delete
  acknowledge_terminal_metadata_retained

retention_resume:
  predecessor_authorization_id
  predecessor_consumed_identity
  plan_id
  plan_digest
  journal_digest
  journal_generation
  already_deleted_backup_ids
  earliest_unfinished_backup_id
  earliest_unfinished_phase
  remaining_ordered_candidate_ids
  resume_scope
  already_reclaimed_bytes
  remaining_expected_reclaim_bytes
  acknowledge_current_partial_state
```

`operation` 与 payload variant 必须严格一致。缺失字段、未知字段、未知 variant 或跨 operation
字段全部拒绝；不得从 CLI 参数补齐 authorization record。模糊的 `journal_identity` 不再存在，
resume identity 只由 `journal_digest + journal_generation` 和完整 resume payload 表达。

`pin_reconcile` 只能处理 predecessor 为 exact consumed `pin`/`unpin` authorization，且只允许
收敛绑定的单个 pin journal；不得创建新的独立 pin 意图、改变 reason、切换 backup ID 或批量
处理多个 journal。journal digest/phase 或当前 sidecar identity 变化使授权失效。

`retention_resume` 必须引用立即前一条 consumed `retention_apply` 或 `retention_resume`
authorization，并同时绑定当前 journal。resume 链不能跳过 predecessor、改换 plan 或拼接另一条
authorization lineage。

`authorization_nonce` 只保存在 `0600` 私有 record 中，不进入报告。授权目录必须是私有真实
目录、非 symlink。record 的每次 phase 更新使用 atomic replace、file/directory fsync。

每个 authorization 使用独立、稳定的 lock inode：

```text
<authorization-root>/<authorization_id>.lock
<authorization-root>/<authorization_id>.json
```

lock file 在 record 签发前 create-once，固定 `0600`，使用 safe-open、`O_NOFOLLOW`、fstat regular
file 和 flock；lock inode 永不 rename、replace 或 unlink。JSON record 可以 atomic replace，但其
全部读取、验证和 phase 更新必须持有对应 stable lock fd。lock filename 只能由通过
`OPAQUE_ID_PATTERN` 的 authorization ID 推导，不接受路径。

签发失败留下的 orphan lock 不代表授权存在，也不自动删除；保留并进入 metadata manual review。
record 存在但 stable lock 缺失、类型/权限错误或映射不一致时稳定拒绝。lock/audit metadata 的
清理不属于 4D-3。

`authorization_id` 只用于定位 record，不构成消费凭据。`authorization_nonce` 必须由独立安全
输入提供，并与 record 内值执行恒定时间比较；仅知道 authorization ID 不足以消费授权。3A
只实现 issuer/consumer 抽象与 fake/temp issuer，不接通真实人工 authorization owner。

消费顺序固定为：

```text
safe-open authorization record
-> safe-open stable authorization lock file and acquire flock
-> 在 stable lock 内重新 safe-open authorization record
-> 校验 operation、expiry、root、plan/journal/sidecar binding
-> 获取 backup mutation exclusive lock
-> 在锁内重新 safe-open authorization 并校验全部 binding
-> create/read-back durable planned mutation journal
-> durable phase=consumed
-> 执行对应 mutation
-> release backup exclusive lock
-> release stable authorization lock
```

锁顺序唯一固定为 `stable authorization lock -> backup exclusive lock`；任何代码不得反向获取。
两个 lock 均持有到 mutation owner 完成 operation journal 更新；先释放 backup lock，再释放
stable authorization lock。进入 backup lock 后必须重新读取 authorization record；已经 consumed
或 cancelled 的 record 稳定拒绝。执行期间不 atomic replace/unlink stable lock file。

reconcile/resume 需要读取新授权和 predecessor 授权时，先按 canonical authorization ID 字节序
升序获取全部 stable authorization locks，再获取 backup exclusive lock；释放顺序完全相反。
锁内重新读取每个 record，并要求新授权为 `issued`、predecessor 为 exact `consumed` 且 identity
匹配。不得先持有 backup lock 再获取任一 authorization lock。

规则：

- 同一 `authorization_id` 只能从 `issued` 进入一次 `consumed`；
- `consumed/cancelled` 均不可作为新 mutation 授权重放；
- 两个进程并发消费时只有一个可以完成 durable phase 转换，另一个稳定返回
  `BACKUP_MUTATION_AUTHORIZATION_ALREADY_CONSUMED`；
- 授权过期、operation/root/plan/journal/sidecar 不匹配均在 mutation 前拒绝；
- `pin`、`unpin`、pin reconcile、initial apply、resume 使用不同 operation，不得跨命令复用；
- 每个 mutation 的 `planned` operation journal 必须在 authorization `consumed` 前
  durable；journal 创建失败不消费授权且不得执行 mutation；
- `consumed` 只表示授权已不可逆消费，不表示 mutation 成功；mutation 结果唯一由 pin/retention
  operation journal 表达；
- consumed 后、首次 mutation 前崩溃时，原授权不得重放；pin/unpin 必须签发绑定 predecessor
  和 pin journal 的 `pin_reconcile`，retention 必须签发 `retention_resume`；
- mutation 已发生但 operation journal terminal phase 未写入时，不反向撤销，只按 operation
  journal 与文件事实协调；
- `issued -> cancelled` 是唯一允许的 cancel 转换；`consumed` 后不能 cancel；
- authorization 不存在 mutation 后 terminal update，因此 mutation 成功后不会出现
  authorization terminal write failure；
- CLI candidate list 只用于 equality confirmation，不是 mutation 数据源。

production adapter 最小输入固定为：

```text
ValidatedProductionAuthorization
SafeOpenedImmutablePlan
RootBoundExclusiveLock
```

adapter 不接受任意路径、package object、policy 或调用方构造的 candidate list。

## 3. 阶段拆分

### 4D-3A：生产装配实现

仅允许在单独代码评审后实现：

- production pin/unpin adapter；
- production apply adapter；
- `pin`、`unpin`、`apply --plan-id` CLI；
- 独立的显式授权 token/confirmation contract；
- authorization create/consume/reconcile audit；
- plan reader、journal reader 和脱敏结果 DTO；
- fake/temp 测试、取消测试、崩溃恢复测试和完整回归。

3A 不得读取或修改真实 `BACKUP_DIR`，不得执行真实 pin 或删除。实现不能复用、导出或伪造
`TempMutationCapability`；生产 adapter 应使用独立、不可由普通参数构造的启动时授权对象。
3A 可以实现 production command surface 和拒绝路径，但真实 authorization issuer 必须不可达；
没有后续单独装配授权时，生产命令不得获得可消费的真实 authorization。

### 4D-3B：只读真实基线与 plan

单独授权后只允许：

- 读取真实 inventory；
- 读取 verification、pin、hold、pending 和 audit 状态；
- 运行真实目录 `plan`；
- 输出脱敏 plan 报告；
- 不写 pin、不 apply、不删除。

### 4D-3C：真实 pin 验收（可选）

只有存在确需额外保护的明确 `backup_id` 时才执行。pin 和 unpin 必须分别授权；不得为了
“测试功能”随意改变生产保护状态。pin 成功后旧 plan 必须作废，并重新执行 3B。

### 4D-3D：单次真实 apply 验收

仅在候选集合非空、人工确认完成且用户明确批准 plan ID 和候选 ID 集合后执行。一次批准只
覆盖一个 immutable plan，不形成长期授权，不安装 LaunchAgent、cron 或自动清理任务。

### 4D-3E：验收归档

只读核对 apply 结果、inventory、verified floor、journal/pending 和 budget，生成脱敏报告。
不得在归档阶段补删其他 package 或自动清理 terminal metadata。

### 4D-3R：同一 plan 的受控恢复

partial、进程崩溃或 `cleanup_required` 后，不返回 3B/C 重新选 candidate。恢复只能进入独立
Gate E-R，使用新的 `retention_resume` 一次性授权和 resume 验证矩阵。

## 4. Gate 顺序

### Gate A：实现与环境只读基线

允许：

1. 确认 4D-2 commit、完整回归和工作区状态；
2. 检查 `BACKUP_DIR` logical identity、device/inode、mode 和非 symlink；
3. inventory shared/non-blocking 读取；
4. 确认 `.retention-pending` 为空；
5. 确认无未终结 pin audit、orphan sidecar、invalid 或 manual-review package；
6. 确认当前 restore-verified 包数量和 verified floor；
7. 不输出路径、checksum、文件正文、数据库 URL 或业务数据。

任一 unsafe 状态停止后续 Gate，不自动修复。

### Gate B1：新建生产备份

这是生产数据库读取和真实 package 写入，必须单独授权。复用已验收的 4B 服务，不在 4D-3
重新实现备份算法。成功后只得到 valid package，不能自动视为 restore-verified。

如果 B1 成功但 B2 未执行或失败，结果必须固定为：

```text
new_backup_created: yes
new_backup_restore_verified: no
retention_apply_allowed: no
```

该 package 不自动删除，不复制旧 verification sidecar，也不为了继续验收伪造
restore-verified 状态。

### Gate B2：新包 C2 隔离恢复验证

这是 PostgreSQL 创建/restore/验证/guarded DROP 和 verification sidecar 写入，必须再次单独
授权。必须完整执行 C2，不允许复制旧 sidecar 或根据历史报告伪造验证状态。

成功门槛：

```text
restore/schema/constraint/integrity: pass
guarded_drop: pass
recovery_record_cleanup: pass
snapshot_cleanup: pass
verification_sidecar: exact-valid
inventory.restore_verified: yes
```

### Gate C：真实 plan/dry-run

获取 shared backup lock，冻结一次 UTC reference 和 canonical inventory snapshot，生成 immutable
plan。报告必须至少包含：

```text
plan_id
selection_reference_at_utc
valid_package_count
restore_verified_count
protected_backup_ids
candidate_backup_ids
verified_floor_backup_id
daily / weekly / minimum / pin / hold protection counts
total_package_bytes
post_plan_package_bytes
budget_status
manual_review_count
unrecognized_entry_count
```

报告不得包含绝对路径、checksum、inode、用户名、数据库连接信息、watchlist 正文或 dump 内容。

Gate C 通过不授权 apply。人工必须逐项确认：

- candidate 全部是预期旧包；
- 最新通过 C2 的包受到保护；
- verified floor 不在 candidate；
- pinned/held 包不在 candidate；
- 删除后仍至少有一个 restore-verified 包；
- budget 结果合理；
- plan 没有 manual-review 或 unknown 状态。

候选为空是合法结果：记录 `nothing_to_delete`，不得制造候选或降低保护策略来完成验收。

### Gate D：可选真实 pin/unpin

真实 pin 写入必须单独批准以下精确输入：

```text
operation: pin | unpin
backup_id: canonical id
reason_code: approved enum (pin only)
expected_pin_identity: exact content identity or missing
expected_reason_code: exact current reason (unpin only)
```

执行后必须 read-back sidecar 和 audit terminal phase。`unknown`、audit 未终结、reason conflict、
orphan 或 identity mismatch 均停止验收。任何 pin/unpin 后 Gate C 生成的旧 plan 永久失效，必须
重新 inventory 并创建新 plan。

### Gate E：单次真实 apply

这是不可逆删除动作，必须在执行前单独明确授权：

```text
plan_id: exact opaque id
candidate_backup_ids: exact ordered list
expected_candidate_count
expected_reclaim_bytes
acknowledge_non_transactional_multi_package_delete: yes
acknowledge_terminal_metadata_retained: yes
```

授权必须绑定经人工查看的 plan digest。CLI 不接受 package path、通配符、`--all`、任意候选
追加、重新选择或临时 policy 覆盖。

initial apply 获取 exclusive backup lock 后必须重新验证：

- root identity；
- plan schema/policy/algorithm version；
- canonical package 集合和所有 package identity；
- candidate/protected/verified-floor identity；
- verification、pin、hold sidecar identity；
- 无 plan 外 pending、未终结 audit、manual-review 或 unrecognized entry；
- 用户确认的 ordered candidate list 与 plan 完全一致；
- execution journal 尚未进入 mutation phase；
- 不存在 plan-owned pending。

initial apply 在授权消费前 create-once execution journal，全部 candidate 均为 `planned`；已存在
exact planned journal 可在未消费授权下 read-back 复用，任何非 planned phase 必须改走 Gate E-R。

任一变化返回 `BACKUP_RETENTION_PLAN_STALE`，零新增删除。不得自动生成新 plan 并继续。

### Gate E-R：同一 plan 的受控 resume

resume 与 initial apply 使用不同授权。人工必须查看当前 journal 和 pending 摘要，授权至少绑定：

```text
operation: retention_resume
plan_id
plan_digest
journal_digest
journal_generation
already_deleted_backup_ids
earliest_unfinished_backup_id
earliest_unfinished_phase
remaining_ordered_candidate_ids
resume_scope: settle_current_only | settle_and_continue_remaining
already_reclaimed_bytes
remaining_expected_reclaim_bytes
acknowledge_current_partial_state: yes
```

执行器必须先处理最早 unfinished/cleanup-required candidate；调用方不能跳过它选择后续项。
`settle_current_only` 收敛当前项后立即停止；`settle_and_continue_remaining` 只有当前项成功收敛
后才可按原 plan 顺序继续。journal digest/generation 在人工查看后发生变化，旧授权立即失效。

resume 验证矩阵：

```text
journal=deleted:
  final absent + pending absent -> expected
journal=pending/delete_started/cleanup_required:
  final absent + exact plan-owned pending/progress -> expected resumable state
journal=rename_started:
  final exact + pending absent
  OR final absent + exact plan-owned pending -> expected coordinatable state
journal=planned:
  final package and all identities exactly equal immutable plan
```

以下仍必须停止：

- plan 外 pending；
- deleted 项重新出现；
- planned 项缺失或 identity 变化；
- protected、verified-floor、pin 或 hold identity 变化；
- journal 与文件系统事实不匹配；
- candidate 顺序、plan digest、journal digest/generation 变化。

selection inputs 被外部修改返回 `BACKUP_RETENTION_PLAN_STALE`；plan-owned pending 与 journal
不一致返回 `BACKUP_RETENTION_EXECUTION_IDENTITY_INVALID` 并进入 manual review；合法但未收敛
的删除故障返回 `BACKUP_RETENTION_CLEANUP_REQUIRED`。三者不得合并。

## 5. Apply 停止与恢复规则

多 package apply 不是事务。每个 candidate 只能按既有状态机执行：

```text
planned
-> rename_started
-> pending
-> delete_started
-> deleted

failure after rename/delete intent
-> cleanup_required
```

出现以下任一情况立即停止后续 candidate：

- no-replace 不可用或目标已存在；
- final/pending identity 不匹配；
- symlink、额外文件、特殊文件或跨 device；
- journal 写入或 fsync 失败；
- plan stale；
- `cleanup_required`；
- lock、权限、I/O 或空间异常；
- 进程取消或 shutdown。

失败后不得重新 plan 跳过异常项。只能进入 Gate E-R 对同一 plan 执行受控 resume；initial
apply 的全量 snapshot equality 不用于 resume，resume 必须使用 phase-aware 验证矩阵。
terminal journal、plan、authorization audit、pin audit 和异常 pending 不由 4D-3 自动删除。

## 6. 真实删除后的验收

Gate E 完成后，在 shared lock 下重新 inventory，并核对：

- 只有计划 candidate 缺失；
- protected、pinned、held 和 verified floor 全部存在且 identity 未变；
- 至少一个 restore-verified package 存在；
- deleted candidate 的 final 与 pending 均缺失；
- execution journal 每项为 `deleted`，或阶段稳定失败并明确列出 cleanup requirement；
- `.retention-pending` 无非预期遗留；
- budget 统计与实际 final package 总量一致；
- 备份创建、C2、主服务和数据库业务链路没有被 retention 代码调用；
- 无生产恢复、watchlist 修改、session 或 Telegram API 访问。

如果 apply 部分完成，阶段结论只能为 `partial` 或 `cleanup_required`，不能因为部分空间已经
回收就写成 pass。

## 7. 报告契约

建议归档：

```text
P6_DEPLOY_4D_3_RESULT:
  status: pass | partial | fail | blocked
  gate_a_read_only_baseline: pass | fail | not_run
  gate_b1_new_backup: pass | fail | not_run
  gate_b2_restore_verify: pass | fail | not_run
  gate_c_real_plan: pass | fail | nothing_to_delete | not_run
  gate_d_pin_operation: pass | fail | not_needed | not_run
  gate_e_real_apply: pass | partial | fail | not_authorized | not_run
  gate_e_r_resume: pass | partial | fail | not_needed | not_run
  plan_id:
  plan_digest:
  selection_reference_at_utc:
  valid_package_count_before:
  restore_verified_count_before:
  protected_count:
  candidate_count:
  deleted_count:
  cleanup_required_count:
  restore_verified_count_after:
  verified_floor_preserved: yes | no | unknown
  budget_status_before:
  budget_status_after:
  bytes_before:
  bytes_after:
  real_pin_written: yes | no
  real_packages_deleted: yes | no
  new_backup_created: yes | no
  new_backup_restore_verified: yes | no
  retention_apply_allowed: yes | no
  production_restore_executed: no
  database_modified_by_retention: no
  watchlist_modified_by_retention: no
  telegram_api_accessed: no
  report_desensitized: yes
  recommend_allow_P6_Deploy_4D_4: yes | no
  blockers: []
```

报告只给出后续建议，不自行授权 4D-4、生产恢复或 Deploy-5。

## 8. 明确禁止

- 不安装定时 retention agent、cron 或 LaunchAgent；
- 不自动运行 plan 后立即 apply；
- 不根据 budget 超限自动扩大 candidate；
- 不删除 protected、pinned、held、manual-review 或 verified-floor package；
- 不修改 final package 内容或 verification sidecar；
- 不自动 unpin；
- 不删除 terminal plan、journal、audit 或异常 pending；
- 不使用通用递归删除；
- 不执行生产数据库恢复；
- 不修改 `production.env`、watchlist、Telegram session 或 Monitor 生命周期；
- 不把一次真实授权保存为长期 capability。

## 9. 测试与实施准入

4D-3A 实现前至少补齐：

1. production adapter 无授权对象时在读取 plan/获取 lock 前拒绝；
2. temp capability 不能传给 production adapter；
3. CLI 不接受 path、glob、`--all` 或 policy override；
4. authorization 精确绑定 plan ID、digest 和 ordered candidates；
5. authorization nonce、expiry、root、operation 和 single-use durable consumption；
6. 同一 authorization 并发消费只有一个 winner，重放稳定拒绝；
7. consumed 后崩溃不允许重放 initial authorization；
8. planned mutation journal 在 authorization consumption 前 durable，失败时不消费授权；
9. stable authorization lock inode 不因 JSON atomic replace 失效；
10. orphan lock、missing lock、symlink lock 和错误 inode/type/mode 均 fail closed；
11. 单 authorization 和多 authorization locks 均按 canonical ID 全序后再获取 backup lock；
12. nonce 缺失或错误稳定拒绝，并使用恒定时间比较；
13. 仅允许 `issued -> cancelled`，consumed 后 cancel 稳定拒绝；
14. consumed 后 mutation 结果只读取 operation journal，不存在 mutation 后 authorization update；
15. pin authorization 缺 backup_id/reason/identity 任一字段时拒绝；
16. apply authorization 缺 count/bytes/acknowledgement 任一字段时拒绝；
17. resume authorization 缺 journal generation/scope/partial acknowledgement 时拒绝；
18. operation 与 payload variant 不一致、未知 variant 或未知字段时拒绝；
19. pin_reconcile 严格绑定 predecessor consumed identity、pin journal 和 sidecar identity；
20. retention_resume 严格绑定 predecessor authorization lineage 和当前 journal；
21. 真实 authorization issuer 在 3A 中不可达；
22. plan stale 时零新增删除；
23. pin 后旧 plan 稳定 stale；
24. unpin 授权绑定当前 sidecar identity 和 reason；
25. candidate 为空时 apply 拒绝或返回 nothing-to-do，不制造删除；
26. verified floor 永远不可授权为 candidate；
27. partial apply 不报告 pass；
28. initial apply 使用完整 snapshot equality；
29. resume 接受 journal 可证明的 deleted/pending 变化，但拒绝外部变化；
30. resume 授权绑定 journal digest/generation、最早未完成项和 scope；
31. `settle_current_only` 不继续后续 candidate；
32. `settle_and_continue_remaining` 不能跳过最早异常项；
33. stale、execution identity invalid、cleanup required 错误码分离；
34. unknown/audit/pending/manual-review 状态 fail closed；
35. 结果报告脱敏；
36. fake/temp 和完整回归通过。

这些实现和测试通过代码复审前，不允许真实 Gate A-E。

## 10. 设计评审

### 已确认

- 4D-3 没有改变 4D 总设计的 selection、stale、verified-floor 或删除状态机；
- 真实读取、真实备份、真实 C2、真实 pin 和真实删除已拆成独立授权；
- 当前生产入口缺口被显式放入 4D-3A，没有通过测试 capability 绕过；
- dry-run 不是删除授权，pin 后旧 plan 必须作废；
- candidate 为空被视为合法验收结果；
- apply 授权绑定 immutable plan 和完整有序候选集合；
- 一次性授权已定义 nonce、root/operation binding、expiry、durable consumption 和防重放；
- initial apply 与 resume 使用不同验证矩阵；
- resume 已绑定当前 journal identity、最早未完成项和明确 scope；
- authorization 使用严格 operation-specific payload，不依赖 CLI 参数补齐事实；
- authorization JSON 与 stable lock inode 已分离；
- authorization `consumed` 是 mutation 前消费终态，结果仅由 operation journal 表达；
- pin reconcile 与 retention resume 均绑定 predecessor authorization lineage；
- 部分删除、cleanup_required 和 resume 语义没有被简化；
- 不引入自动 retention 或生产恢复。

### 前轮评审结论（历史）

```text
P6-DEPLOY-4D-3_DESIGN_REVIEW:
  result: rereview_v2_changes_applied_final_rereview_required
  architecture_direction: aligned
  previous_review_blockers_addressed: 3
  previous_review_recommendations_addressed: 5
  rereview_v2_blockers_addressed: 2
  rereview_v2_recommendations_addressed: 3
  blockers: pending_final_rereview
  required_clarifications: 0
  allow_P6_Deploy_4D_3A_implementation: no
  allow_real_backup_dir_read: no
  allow_real_backup_create: no
  allow_real_C2_verify: no
  allow_real_pin_write: no
  allow_production_retention_apply_cli: no
  allow_real_retention_delete: no
  allow_P6_Deploy_4D_4: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

以下状态保留为进入第 11 节最终准入复审前的历史记录；当前有效结论以第 11 节为准。

## 11. 最终准入复审

### Blocker 关闭确认

1. **Stable authorization lock inode：关闭。** JSON record 与 create-once `.lock` 已分离；stable
   lock 永不 replace/unlink，全部 record 读取和更新均在该 lock 下完成。canonical 映射、权限、
   no-follow、orphan lock 和 metadata 保留策略已固定。
2. **Post-consumption reconciliation：关闭。** Authorization `consumed` 现在是 mutation 前消费
   终态，不再有 mutation 后 terminal update。Pin/unpin 使用严格 `pin_reconcile`；retention 使用
   lineage-bound `retention_resume`。两者均绑定 predecessor consumed identity、operation journal
   和当前文件事实。

### 一致性复核

- planned operation journal 在 authorization consumed 前 durable；
- consumed 只表示权限已使用，不表示 mutation 成功；
- mutation 结果唯一来自 operation journal；
- 普通 initial authorization 不可重放；
- reconcile/resume 使用新的 single-use authorization；
- 多 authorization locks 按 canonical ID 全序，再获取 backup lock，不存在反向锁顺序；
- retention 全部 deleted 时 journal 已是 terminal，无 authorization terminal reconciliation；
- 真实 issuer 仍不可达，真实目录和真实 mutation Gate 未开启。

### 最终结论

```text
P6-DEPLOY-4D-3_FINAL_ADMISSION_REVIEW:
  result: approved_for_4D_3A_implementation
  architecture_direction: aligned
  stable_authorization_lock: pass
  authorization_consumption_model: pass
  pin_reconciliation_contract: pass
  retention_resume_contract: pass
  multi_lock_ordering: pass
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_4D_3A_implementation: yes
  allow_real_backup_dir_read: no
  allow_real_backup_create: no
  allow_real_C2_verify: no
  allow_real_pin_write: no
  allow_production_retention_apply_cli_execution: no
  allow_real_retention_delete: no
  allow_P6_Deploy_4D_4: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

最终准入复审通过。下一步只允许 4D-3A authorization schema/store/consumer、stable lock、
pin reconcile、retention resume、production adapter/CLI contract 与 fake/temp 测试实现。真实
authorization issuer、真实目录读取、真实 pin/apply/delete 继续禁止。
