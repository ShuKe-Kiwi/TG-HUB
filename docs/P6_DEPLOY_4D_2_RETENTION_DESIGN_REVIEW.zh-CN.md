# P6-Deploy-4D-2 Retention / Pin 独立设计复审

> 项目：tg-hub  
> 阶段：P6-Deploy-4D-2 design review  
> 日期：2026-07-16  
> 状态：implementation-approved / real-gates-closed

## 1. 复审范围

本次仅评审：

- deterministic daily/weekly/minimum/verified-floor selection；
- canonical inventory snapshot 与 stale 判定；
- plan/dry-run；
- pin/unpin 状态机与审计；
- temp-only apply engine、journal 和精确删除恢复性。

不批准真实 pin、生产 apply CLI、真实 package 删除、4D-3、4D-4 或生产恢复。

## 2. 前轮 blocker 关闭情况

### 2.1 实现与真实删除授权已拆开

4D-2 对外只安装 plan/dry-run。apply engine 仅允许通过测试注入的临时 root/fake adapter
运行；生产调用必须在读取 plan、获取 lock 或修改文件前 fail closed。真实 apply CLI 延后到
4D-3 单独授权。

结论：关闭。

### 2.2 时间窗口已有唯一锚点

plan 在 shared lock 内读取一次 injected UTC clock，持久化
`selection_reference_at_utc=created_at_utc`。daily/weekly slot 只依赖该锚点，apply/resume
不读取当前时间。

结论：关闭。

### 2.3 pin/unpin 状态机与审计已闭环

missing、exact-valid same reason、reason conflict、invalid/mismatch/unsupported/orphan 均有固定
结果；exact idempotent 不改时间或 mtime，异常 sidecar 不覆盖、不删除。审计使用 immutable
operation identity 和 mutable durable phase journal，动作事实与审计事实分离，审计失败不
反向修改已完成 sidecar。pin/unpin mutation 同样要求 temp-only capability，production factory
不暴露真实写入口。

结论：关闭。

### 2.4 destructive filesystem primitive 已收口

rename/delete 使用 dirfd-anchored 私有同设备目录；pending 已存在时禁止覆盖。合法 package
只有三个固定 regular file，因此不使用通用递归删除；任何额外条目、symlink、特殊文件、
device 或 identity 变化均进入 manual review。

复审期间进一步发现逐文件删除的 partial-pending 崩溃窗口，现已增加每文件 durable intent、
`file_index/file_phase` 和 `rmdir_started` 协调矩阵；同时要求平台 no-replace rename，禁止退化
为普通覆盖式 rename。

结论：关闭。

## 3. Clarification 关闭情况

- canonical snapshot 字段、排序、JSON 编码与 SHA-256 已固定；
- plan stale 矩阵已覆盖 package、pin、verification、hold、policy、root 和 plan 外 pending；
- plan/journal/audit 自动清理明确不属于 4D-2/4D-3；
- pin identity 使用受控 sidecar 内容 identity，不依赖 mtime；
- pin audit 已改为动作前 durable intent journal，可协调 mutation 后崩溃；
- 当前真实目录只有一个 valid restore-verified package，plan candidate 为零属于预期结果。

## 4. 实现准入边界

允许：

```text
retention contracts and DTOs
canonical frozen inventory snapshot
deterministic selection with injected clock
plan create/read and dry-run report
pin/unpin service with fake/temp root tests
pin audit records
temp-only apply engine and crash recovery tests
```

禁止：

```text
write real ~/.tg-hub/backups/.pins
install or enable production retention apply CLI
rename/delete any real backup package
modify real verification sidecar or recovery hold
P6-Deploy-4D-3
P6-Deploy-4D-4
production restore
P6-Deploy-5
```

## 5. 复审结论

```text
P6-DEPLOY-4D-2_INDEPENDENT_REVIEW:
  result: approved_for_implementation
  architecture_direction: aligned
  prerequisite_4D_1B_2: pass
  blockers: 0
  required_clarifications: 0
  allow_P6_Deploy_4D_2_implementation: yes
  allow_retention_apply_implementation: yes_temp_only
  allow_real_pin_write: no
  allow_production_retention_apply_cli: no
  allow_real_retention_delete: no
  allow_P6_Deploy_4D_3: no
  allow_P6_Deploy_4D_4: no
  allow_production_restore: no
  allow_P6_Deploy_5: no
```

下一步只能进入 P6-Deploy-4D-2 代码实现和 fake/temp 测试。真实目录 dry-run、真实 pin 和
真实删除均需实现完成、代码复审并提交后再次单独授权。
