# P6-2J-5 最小管理台集成验收

## 1. 验收结论

```text
PROJECT: tg-hub
PHASE: P6-2J-5
MODE: integration-acceptance
STATUS: pass
```

P6-2J-1 至 P6-2J-4 的服务、控制边界、管理 API 与页面已完成集成验收。验收没有访问真实 Telegram API 或 Bot API，没有启动无限监听，也没有修改运行时 `~/.tg-hub/watchlist.json`。

## 2. 验收环境

- 管理台绑定：`127.0.0.1`
- watchlist：`/tmp` 独立临时夹具
- 浏览器：Playwright Chromium
- 桌面页面：运行概览、监听配置
- 移动视口：`390 x 844`
- Monitor：未启动真实 runtime
- Telegram / Bot：未访问

## 3. 自动化验收

### WatchlistApplicationService

- 有效、缺失、无效与不安全快照
- schema、标题、alias 与频道引用唯一性校验
- revision 冲突和外部修改检测
- 原子替换和并发单一胜者
- 写入失败保留旧文件
- symlink 拒绝
- 新文件 `0600` 与现有权限保留
- 无效文件显式恢复

### MonitorControlService

- 单 task 所有权
- start / stop 幂等和竞态串行化
- preflight 失败不创建 task
- stop timeout 保留 task 所有权并禁止二次启动
- task 完成后的状态协调
- runtime 失败、取消与 revision drift 状态保留
- lifespan shutdown 不重复创建停止流程

### Admin API

- 本机来源、Host、Origin、CSRF 和 JSON content type 防护
- GET 无副作用且响应 `Cache-Control: no-store`
- start 立即返回 `202 starting`
- stop timeout 返回稳定冲突结果
- revision、schema 和控制错误映射稳定
- 异常堆栈和敏感配置不进入响应
- lifespan 持有 control service shutdown

## 4. 浏览器验收

- 概览页能渲染 stopped 状态、健康信号、计数器和空错误状态
- 按服务端 `allowed_actions` 控制启停和预检按钮
- 监听配置页能读取临时 watchlist 和 revision
- 频道与资源名标签页可切换
- 频道添加、编辑、启停和删除入口完整
- `t.me URL` 与大小写不同的 `@username` 在添加时被识别为重复
- 保存按钮只在候选配置变化后启用
- 桌面和 `390 x 844` 移动视口无控件重叠或不可达操作
- 浏览器控制台：`0 errors / 0 warnings`
- 网络请求仅访问 `127.0.0.1` 管理页面、静态资源和 `/api/admin/v1/*`
- DOM 和响应未出现 API hash、Bot token、session path 或 `DATABASE_URL`

## 5. 回归结果

```text
pytest: 447 passed
git_diff_check: pass
telegram_api_accessed: no
bot_api_accessed: no
database_accessed_by_browser_acceptance: no
infinite_monitor_started: no
runtime_watchlist_changed: no
```

## 6. 阶段边界

P6-2J 通过只证明本机管理员可以通过浏览器管理 watchlist、查看脱敏状态，并经 application control boundary 控制单个 monitor task。

本阶段不证明：

- 已完成生产部署或系统重启自动恢复
- 支持远程、多管理员或多实例选主
- 通知可靠投递
- 历史补偿或消息重放
- watchlist 已迁移到 PostgreSQL

## 7. 最终判定

```text
P6-2J-1: complete
P6-2J-2: complete
P6-2J-3: complete
P6-2J-4: complete
P6-2J-5: complete
P6-2J: complete
```
