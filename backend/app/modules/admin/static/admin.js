const API = "/api/admin/v1";
const csrf = document.querySelector('meta[name="tg-hub-csrf"]')?.content ?? "";
const page = document.body.dataset.page;

class ApiError extends Error {
  constructor(code, status) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

async function api(path, options = {}) {
  const method = options.method ?? "GET";
  const headers = new Headers(options.headers ?? {});
  if (method !== "GET") {
    headers.set("Content-Type", "application/json");
    headers.set("X-TG-Hub-CSRF", csrf);
  }
  const response = await fetch(`${API}${path}`, { ...options, method, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(payload?.error?.code ?? "ADMIN_REQUEST_FAILED", response.status);
  }
  return payload.data;
}

function setGlobalStatus(state, text) {
  const dot = document.querySelector("#global-status-dot");
  const label = document.querySelector("#global-status-text");
  if (!dot || !label) return;
  const style = state === "running" ? "running" : ["failed"].includes(state) ? "failed" : ["starting", "stopping", "degraded"].includes(state) ? "warning" : "neutral";
  dot.className = `status-dot ${style}`;
  label.textContent = text;
}

function updateRefreshTime() {
  const target = document.querySelector("#last-refresh");
  if (target) target.textContent = `刷新于 ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
}

function showPageError(message) {
  const alert = document.querySelector("#page-alert");
  if (!alert) return;
  alert.textContent = message;
  alert.classList.remove("hidden");
}

function clearPageError() {
  document.querySelector("#page-alert")?.classList.add("hidden");
}

function toast(message, tone = "default") {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const item = document.createElement("div");
  item.className = `toast ${tone}`;
  item.textContent = message;
  region.append(item);
  window.setTimeout(() => item.remove(), 3600);
}

function humanError(error) {
  const labels = {
    ADMIN_CSRF_REJECTED: "页面安全令牌已失效，请刷新页面。",
    MONITOR_ALREADY_ACTIVE: "Monitor 已在运行或启动中。",
    MONITOR_PRECHECK_FAILED: "启动前检查未通过，请先查看预检结果。",
    MONITOR_STOP_TIMEOUT: "停止请求超时，任务仍由系统持有。",
    MONITOR_TASK_STILL_RUNNING: "上一个 Monitor task 尚未结束。",
    WATCHLIST_REVISION_CONFLICT: "配置已被外部修改，请重新加载后再编辑。",
    WATCHLIST_SCHEMA_INVALID: "配置内容未通过校验。",
    WATCHLIST_CURRENT_FILE_INVALID: "当前文件无效，需要确认恢复后才能保存。",
    ADMIN_REQUEST_FAILED: "管理服务暂时不可用。",
  };
  return labels[error?.code] ?? `操作失败：${error?.code ?? "UNKNOWN"}`;
}

function setLoading(button, loading) {
  if (!button) return;
  button.classList.toggle("loading", loading);
  button.disabled = loading;
}

function closeDialog(id) {
  document.querySelector(`#${id}`)?.close();
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-close-dialog]");
  if (target) closeDialog(target.dataset.closeDialog);
});

function confirmAction(title, message, actionLabel = "确认") {
  const dialog = document.querySelector("#confirm-dialog");
  document.querySelector("#confirm-title").textContent = title;
  document.querySelector("#confirm-message").textContent = message;
  document.querySelector("#confirm-action").textContent = actionLabel;
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

const stateLabels = {
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  degraded: "降级",
  failed: "失败",
};

function formatFlag(value) {
  return value === "yes" || value === true ? "是" : "否";
}

function formatUptime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value % 60;
  return hours ? `${hours}时 ${minutes}分` : minutes ? `${minutes}分 ${rest}秒` : `${rest}秒`;
}

function initOverview() {
  let polling = false;
  let pollTimer = null;
  let latest = null;
  const startButton = document.querySelector("#start-monitor");
  const stopButton = document.querySelector("#stop-monitor");
  const preflightButton = document.querySelector("#run-preflight");

  function render(snapshot) {
    latest = snapshot;
    const status = snapshot.control_state;
    setGlobalStatus(status, stateLabels[status] ?? status);
    const badge = document.querySelector("#runtime-badge");
    badge.textContent = stateLabels[status] ?? status;
    badge.className = `badge ${status === "running" ? "running" : status === "failed" ? "failed" : ["starting", "stopping", "degraded"].includes(status) ? "warning" : "neutral"}`;

    const values = {
      control_state: stateLabels[status] ?? status,
      connected: formatFlag(snapshot.connected),
      handler_registered: formatFlag(snapshot.handler_registered),
      uptime_seconds: formatUptime(snapshot.uptime_seconds),
      watchlist_status: snapshot.watchlist_status,
      restart_required: snapshot.restart_required ? "需要" : "不需要",
      liveness: formatFlag(snapshot.liveness),
      readiness: formatFlag(snapshot.readiness),
      task_owned: formatFlag(snapshot.task_owned),
      heartbeat_persistence: ({
        idle: "空闲",
        ok: "正常",
        closed: "已关闭",
        disabled: "未启用",
      })[snapshot.heartbeat_persistence?.status] ?? "失败",
      last_error_code: snapshot.last_error_code ?? "无",
    };
    for (const [key, value] of Object.entries(values)) {
      document.querySelectorAll(`[data-status="${key}"]`).forEach((node) => { node.textContent = value; });
    }
    startButton.disabled = !snapshot.allowed_actions.can_start;
    stopButton.disabled = !snapshot.allowed_actions.can_stop;
    preflightButton.disabled = !snapshot.allowed_actions.can_preflight;
    document.querySelector("#restart-notice").classList.toggle("hidden", !snapshot.restart_required);

    const heartbeat = snapshot.heartbeat ?? {};
    const summary = snapshot.last_summary ?? {};
    document.querySelectorAll("[data-metric]").forEach((node) => {
      const key = node.dataset.metric;
      node.textContent = heartbeat[key] ?? summary[key] ?? 0;
    });
    renderErrors(snapshot.last_errors ?? []);
    renderMatches(heartbeat.recent_matches ?? []);
    updateRefreshTime();
  }

  function renderMatches(matches) {
    const body = document.querySelector("#match-list");
    if (!matches.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-cell">暂无标题命中</td></tr>';
      return;
    }
    body.innerHTML = matches.slice(-10).reverse().map((item) => {
      const label = item.source_label || (item.source_username ? `@${item.source_username}` : item.source_ref);
      const identity = item.source_username ? `@${item.source_username}` : item.source_ref;
      const secondary = identity !== label ? `<div class="row-subtle">${escapeHtml(identity)}</div>` : "";
      return `<tr><td><div class="row-title">${escapeHtml(label)}</div>${secondary}</td><td><div class="match-titles">${(item.matched_titles ?? []).map((title) => `<span class="match-title">${escapeHtml(title)}</span>`).join("")}</div></td><td>${escapeHtml(item.source_message_id)}</td><td>${formatDate(item.matched_at)}</td></tr>`;
    }).join("");
  }

  function renderErrors(errors) {
    const body = document.querySelector("#error-list");
    if (!errors.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无运行错误</td></tr>';
      return;
    }
    body.innerHTML = errors.slice(-10).reverse().map((item) => `<tr><td class="row-title">${escapeHtml(item.error_code)}</td><td>${escapeHtml(item.error_phase)}</td><td>${escapeHtml(item.recoverability)}</td><td>${formatDate(item.occurred_at)}</td><td>${formatFlag(item.retry_scheduled)}</td></tr>`).join("");
  }

  async function poll(immediate = false) {
    if (polling || document.hidden) return;
    polling = true;
    if (immediate) clearPageError();
    try {
      render(await api("/monitor/status"));
    } catch (error) {
      setGlobalStatus("failed", "管理服务不可用");
      showPageError(humanError(error));
    } finally {
      polling = false;
      window.clearTimeout(pollTimer);
      const active = latest && ["starting", "running", "stopping", "degraded"].includes(latest.control_state);
      pollTimer = window.setTimeout(poll, active ? 3000 : 10000);
    }
  }

  startButton.addEventListener("click", async () => {
    setLoading(startButton, true);
    try {
      await api("/monitor/start", { method: "POST", body: JSON.stringify({ expected_watchlist_revision: latest?.current_watchlist_revision ?? null }) });
      toast("启动请求已接受，正在连接频道。", "success");
    } catch (error) { toast(humanError(error), "error"); }
    finally { setLoading(startButton, false); await poll(true); }
  });

  stopButton.addEventListener("click", async () => {
    if (!await confirmAction("停止 Monitor", "系统会等待正在处理的消息完成，并优雅断开 Telegram 连接。", "停止")) return;
    setLoading(stopButton, true);
    try {
      await api("/monitor/stop", { method: "POST", body: "{}" });
      toast("Monitor 已停止。", "success");
    } catch (error) { toast(humanError(error), "error"); }
    finally { setLoading(stopButton, false); await poll(true); }
  });

  preflightButton.addEventListener("click", async () => {
    setLoading(preflightButton, true);
    try {
      const report = await api("/monitor/preflight", { method: "POST", body: "{}" });
      const passed = report.status === "pass";
      const blockers = Array.isArray(report.blockers) ? report.blockers : [];
      const result = document.querySelector("#preflight-result");
      result.className = `preflight-result ${passed ? "passed" : "failed"}`;
      result.innerHTML = `<span class="preflight-result-icon" aria-hidden="true">${passed ? "✓" : "!"}</span><div><strong>${passed ? "预检通过，可以启动" : "预检未通过"}</strong><p>${passed ? "运行环境和监听配置均已满足启动条件。" : escapeHtml(blockers.length ? `存在 ${blockers.length} 个阻塞项，请处理后重新预检。` : "请检查下方未通过项目后重新预检。")}</p></div>`;
      const content = document.querySelector("#preflight-content");
      content.innerHTML = Object.entries(report).filter(([key]) => !["status", "report_desensitized"].includes(key)).map(([key, value]) => `<div class="report-item"><span>${escapeHtml(preflightLabels[key] ?? key)}</span><strong class="${preflightValueTone(value)}">${escapeHtml(formatPreflightValue(value))}</strong></div>`).join("");
      document.querySelector("#preflight-dialog").showModal();
    } catch (error) { toast(humanError(error), "error"); }
    finally { setLoading(preflightButton, false); }
  });

  document.querySelector("#refresh-status").addEventListener("click", () => poll(true));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });
  poll(true);
}

const preflightLabels = {
  watchlist_loaded: "监听配置已加载",
  watchlist_schema: "监听配置格式",
  telethon_dependency: "Telethon 依赖",
  telegram_api_id_configured: "Telegram API ID",
  telegram_api_hash_configured: "Telegram API Hash",
  session_configured: "Telegram Session",
  session_parent_exists: "Session 目录存在",
  session_parent_writable: "Session 目录可写",
  database_url_configured: "数据库连接配置",
  enabled_source_channels: "启用频道数",
  invalid_source_channels: "无效频道数",
  enabled_watch_titles: "启用资源名数",
  blockers: "阻塞项",
};

function formatPreflightValue(value) {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "无";
  const labels = { pass: "通过", fail: "未通过", yes: "是", no: "否" };
  return labels[value] ?? String(value ?? "--");
}

function preflightValueTone(value) {
  if (["pass", "yes"].includes(value)) return "value-pass";
  if (["fail", "no"].includes(value) || (Array.isArray(value) && value.length)) return "value-fail";
  return "";
}

function initWatchlist() {
  let config = { source_channels: [], watch_titles: [] };
  let original = "";
  let revision = null;
  let status = "missing";
  let loading = false;
  const saveButton = document.querySelector("#save-watchlist");

  async function load() {
    if (loading) return;
    loading = true;
    clearPageError();
    try {
      const snapshot = await api("/watchlist");
      status = snapshot.status;
      revision = snapshot.revision;
      config = structuredClone(snapshot.config ?? { source_channels: [], watch_titles: [] });
      original = JSON.stringify(config);
      render();
      setGlobalStatus("running", "配置服务可用");
      updateRefreshTime();
    } catch (error) {
      showPageError(humanError(error));
      setGlobalStatus("failed", "配置读取失败");
    } finally { loading = false; }
  }

  function isDirty() { return JSON.stringify(config) !== original; }
  function render() {
    renderChannels();
    renderTitles();
    document.querySelector("#channel-count").textContent = config.source_channels.length;
    document.querySelector("#title-count").textContent = config.watch_titles.length;
    document.querySelector("#config-summary").textContent = `${config.source_channels.length} 个频道 · ${config.watch_titles.length} 个资源名 · ${status}`;
    document.querySelector("#revision-label").textContent = `revision: ${revision ? revision.slice(0, 12) : "none"}`;
    saveButton.disabled = !isDirty();
    const warning = document.querySelector("#watchlist-warning");
    warning.classList.toggle("hidden", status === "valid");
    warning.textContent = status === "invalid" ? "当前 watchlist 无效。保存有效配置时需要确认恢复。" : status === "missing" ? "watchlist 文件不存在。首次保存将以 0600 权限创建。" : status === "unsafe" ? "watchlist 路径不安全，管理台拒绝写入。" : "";
  }

  function renderChannels() {
    const body = document.querySelector("#channel-list");
    if (!config.source_channels.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-cell">还没有监听频道</td></tr>';
      return;
    }
    body.innerHTML = config.source_channels.map((item, index) => `<tr><td><div class="row-title">${escapeHtml(item.ref)}</div></td><td>${channelType(item.ref)}</td><td><span class="state-pill ${item.enabled ? "enabled" : ""}">${item.enabled ? "启用" : "停用"}</span></td><td><div class="row-actions"><button class="icon-button" data-toggle-channel="${index}" title="${item.enabled ? "停用" : "启用"}" aria-label="${item.enabled ? "停用" : "启用"}">${item.enabled ? "Ⅱ" : "▶"}</button><button class="icon-button" data-edit-channel="${index}" title="编辑" aria-label="编辑">✎</button><button class="icon-button" data-delete-channel="${index}" title="删除" aria-label="删除">×</button></div></td></tr>`).join("");
  }

  function renderTitles() {
    const body = document.querySelector("#title-list");
    if (!config.watch_titles.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-cell">还没有关注资源名</td></tr>';
      return;
    }
    body.innerHTML = config.watch_titles.map((item, index) => `<tr><td><div class="row-title">${escapeHtml(item.title)}</div></td><td><div>${item.aliases.length}</div><div class="row-subtle">${escapeHtml(item.aliases.slice(0, 2).join(" · ") || "无别名")}</div></td><td><span class="state-pill ${item.enabled ? "enabled" : ""}">${item.enabled ? "启用" : "停用"}</span></td><td><div class="row-actions"><button class="icon-button" data-toggle-title="${index}" title="${item.enabled ? "停用" : "启用"}" aria-label="${item.enabled ? "停用" : "启用"}">${item.enabled ? "Ⅱ" : "▶"}</button><button class="icon-button" data-edit-title="${index}" title="编辑" aria-label="编辑">✎</button><button class="icon-button" data-delete-title="${index}" title="删除" aria-label="删除">×</button></div></td></tr>`).join("");
  }

  function changed() { render(); }

  document.querySelector(".tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[role=tab]");
    if (!tab) return;
    document.querySelectorAll("[role=tab]").forEach((item) => { item.classList.toggle("active", item === tab); item.setAttribute("aria-selected", String(item === tab)); });
    document.querySelector("#channels-panel").classList.toggle("hidden", tab.id !== "channels-tab");
    document.querySelector("#titles-panel").classList.toggle("hidden", tab.id !== "titles-tab");
  });

  document.addEventListener("click", async (event) => {
    const add = event.target.closest("[data-add]");
    if (add) openEditor(add.dataset.add);
    const editChannel = event.target.closest("[data-edit-channel]");
    if (editChannel) openChannel(Number(editChannel.dataset.editChannel));
    const editTitle = event.target.closest("[data-edit-title]");
    if (editTitle) openTitle(Number(editTitle.dataset.editTitle));
    const toggleChannel = event.target.closest("[data-toggle-channel]");
    if (toggleChannel) { const item = config.source_channels[Number(toggleChannel.dataset.toggleChannel)]; item.enabled = !item.enabled; changed(); }
    const toggleTitle = event.target.closest("[data-toggle-title]");
    if (toggleTitle) { const item = config.watch_titles[Number(toggleTitle.dataset.toggleTitle)]; item.enabled = !item.enabled; changed(); }
    const deleteChannel = event.target.closest("[data-delete-channel]");
    if (deleteChannel && await confirmAction("删除监听频道", "该频道会从 watchlist 中移除，保存后生效。", "删除")) { config.source_channels.splice(Number(deleteChannel.dataset.deleteChannel), 1); changed(); }
    const deleteTitle = event.target.closest("[data-delete-title]");
    if (deleteTitle && await confirmAction("删除资源名", "该标题及其 aliases 会从 watchlist 中移除。", "删除")) { config.watch_titles.splice(Number(deleteTitle.dataset.deleteTitle), 1); changed(); }
  });

  function openEditor(kind) { kind === "channel" ? openChannel(-1) : openTitle(-1); }
  function openChannel(index) {
    const item = index >= 0 ? config.source_channels[index] : { ref: "", enabled: true };
    document.querySelector("#channel-dialog-title").textContent = index >= 0 ? "编辑频道" : "添加频道";
    document.querySelector("#channel-index").value = index;
    document.querySelector("#channel-ref").value = item.ref;
    document.querySelector("#channel-enabled").checked = item.enabled;
    document.querySelector("#channel-error").textContent = "";
    document.querySelector("#channel-dialog").showModal();
    document.querySelector("#channel-ref").focus();
  }
  function openTitle(index) {
    const item = index >= 0 ? config.watch_titles[index] : { title: "", aliases: [], enabled: true };
    document.querySelector("#title-dialog-title").textContent = index >= 0 ? "编辑资源名" : "添加资源名";
    document.querySelector("#title-index").value = index;
    document.querySelector("#watch-title").value = item.title;
    document.querySelector("#watch-aliases").value = item.aliases.join("\n");
    document.querySelector("#title-enabled").checked = item.enabled;
    document.querySelector("#title-error").textContent = "";
    document.querySelector("#alias-error").textContent = "";
    document.querySelector("#title-dialog").showModal();
    document.querySelector("#watch-title").focus();
  }

  document.querySelector("#channel-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const index = Number(document.querySelector("#channel-index").value);
    const ref = document.querySelector("#channel-ref").value.trim();
    if (!ref) { document.querySelector("#channel-error").textContent = "频道引用不能为空。"; return; }
    const duplicate = config.source_channels.some((item, itemIndex) => itemIndex !== index && normalizeChannelRef(item.ref) === normalizeChannelRef(ref));
    if (duplicate) { document.querySelector("#channel-error").textContent = "该频道已存在，请勿重复添加。"; return; }
    const item = { ref, enabled: document.querySelector("#channel-enabled").checked };
    index >= 0 ? config.source_channels.splice(index, 1, item) : config.source_channels.push(item);
    closeDialog("channel-dialog");
    changed();
  });

  document.querySelector("#title-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const index = Number(document.querySelector("#title-index").value);
    const title = document.querySelector("#watch-title").value.trim();
    const aliases = [...new Set(document.querySelector("#watch-aliases").value.split("\n").map((item) => item.trim()).filter(Boolean))];
    if (!title) { document.querySelector("#title-error").textContent = "资源标题不能为空。"; return; }
    const normalized = normalizeName(title);
    if (aliases.some((alias) => normalizeName(alias) === normalized)) { document.querySelector("#alias-error").textContent = "Alias 不能与标题相同。"; return; }
    const candidateNames = new Set([normalized, ...aliases.map(normalizeName)]);
    const conflict = config.watch_titles.some((item, itemIndex) => itemIndex !== index && [item.title, ...item.aliases].some((name) => candidateNames.has(normalizeName(name))));
    if (conflict) { document.querySelector("#alias-error").textContent = "标题或 Alias 与其他条目冲突。"; return; }
    const item = { title, aliases, enabled: document.querySelector("#title-enabled").checked };
    index >= 0 ? config.watch_titles.splice(index, 1, item) : config.watch_titles.push(item);
    closeDialog("title-dialog");
    changed();
  });

  saveButton.addEventListener("click", async () => {
    if (!isDirty()) return;
    const recovery = ["missing", "invalid"].includes(status);
    const message = recovery ? "当前 watchlist 不可用。将以这份有效配置执行显式恢复。" : `将保存 ${config.source_channels.length} 个频道和 ${config.watch_titles.length} 个资源名。`;
    if (!await confirmAction(recovery ? "恢复 watchlist" : "保存监听配置", message, recovery ? "确认恢复" : "保存")) return;
    setLoading(saveButton, true);
    try {
      const result = await api("/watchlist", { method: "PUT", body: JSON.stringify({ expected_revision: revision, recovery_confirmed: recovery, config }) });
      revision = result.revision;
      status = "valid";
      original = JSON.stringify(config);
      toast("监听配置已保存。运行中的 Monitor 需要重启后生效。", "success");
      render();
    } catch (error) {
      toast(humanError(error), "error");
      if (error.code === "WATCHLIST_REVISION_CONFLICT") showPageError("配置已被外部修改。请点击重新加载，确认最新内容后再编辑。");
    } finally {
      setLoading(saveButton, false);
      saveButton.disabled = !isDirty();
    }
  });

  document.querySelector("#reload-watchlist").addEventListener("click", async () => {
    if (isDirty() && !await confirmAction("放弃未保存更改", "重新加载会覆盖当前页面中的编辑内容。", "重新加载")) return;
    await load();
  });

  load();
}

function channelType(ref) {
  if (/^-?\d+$/.test(ref)) return "numeric id";
  if (ref.startsWith("@")) return "username";
  if (/^https?:\/\/(www\.)?t\.me\//i.test(ref)) return "t.me URL";
  return "待校验";
}

function normalizeName(value) {
  return value.normalize("NFKC").trim().toLocaleLowerCase().replace(/\s+/g, " ");
}

function normalizeChannelRef(value) {
  const ref = value.normalize("NFKC").trim();
  if (ref.startsWith("@")) return `username:${ref.slice(1).toLocaleLowerCase()}`;
  if (/^-?\d+$/.test(ref)) return `numeric:${String(Number(ref))}`;
  try {
    const parsed = new URL(ref);
    if (["t.me", "www.t.me"].includes(parsed.hostname.toLocaleLowerCase())) {
      const username = parsed.pathname.split("/").filter(Boolean)[0];
      if (username) return `username:${username.toLocaleLowerCase()}`;
    }
  } catch (_) {
    // The server remains authoritative for malformed references.
  }
  return `raw:${ref.toLocaleLowerCase()}`;
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

function formatDate(value) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "--" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" }).format(date);
}

if (page === "overview") initOverview();
if (page === "watchlist") initWatchlist();
