# P6-Deploy-3D-4 Gate A 只读基线结果

> 状态：pass-after-remediation
> 执行范围：Gate A only
> 外部状态变更：no

```text
P6-DEPLOY-3D-4_GATE_A_RESULT:
- baseline_status: pass
- full_regression: pass (531 tests, confirmed from 3D-3 acceptance)
- gate_a_targeted_regression: pass (21 tests)
- rotation_install_dry_run: pass
- plist_lint: pass
- plist_contract: pass
- secrets_exposed: no
- main_service: running
- liveness: pass
- readiness: pass
- database_readiness: pass
- migration_readiness: pass
- watchlist_readiness: pass
- assembly_readiness: pass
- monitor_state: running
- monitor_error: none
- active_files_regular: yes
- active_files_mode: 0600
- active_files_nonempty: yes
- rotation_agent_installed: no
- rotation_status: never_run
- rotation_status_stale: not_applicable
- rotation_agent_kickstarted: no
- real_rotation_executed: no
- telegram_api_accessed: no
- database_modified: no
- watchlist_modified: no
- monitor_stopped_by_acceptance: no
- allow_gate_b_without_explicit_approval: no
- blockers: []
```

## 验收过程发现并修复的问题

首次从仓库根目录执行 `backend/deploy/rotation_status.sh` 时，脚本未切换到 `backend/`，Python 无法解析 `app.deploy.rotation_status`。

最小修复：

- wrapper 在执行 backend venv Python 前显式 `cd "$BACKEND"`；
- 增加脚本契约回归断言；
- Python reader、状态 schema 与 Admin 投影均未修改。

修复后 wrapper 返回合法脱敏状态：rotation agent 未配置，当前状态为 `never_run`。

## 阶段结论

Gate A 只读基线通过。当前没有安装 rotation agent，没有 kickstart，没有执行真实轮转，也没有访问 Telegram。

下一步 Gate B 属于本机 launchd 状态变更，必须获得用户单独明确批准后才能执行。
