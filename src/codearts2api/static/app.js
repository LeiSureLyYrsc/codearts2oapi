(() => {
  const KEY = "ca2a_api_key";
  const $ = (id) => document.getElementById(id);

  const loginView = $("login-view");
  const consoleView = $("console-view");
  const headerActions = $("header-actions");
  const headerStatus = $("header-status");
  const loginError = $("login-error");
  const toastEl = $("toast");
  const accountDialog = $("account-dialog");
  const loginDialog = $("login-dialog");

  let pollTimer = null;

  function getKey() {
    return sessionStorage.getItem(KEY) || "";
  }

  function setKey(value) {
    sessionStorage.setItem(KEY, value);
  }

  function clearKey() {
    sessionStorage.removeItem(KEY);
  }

  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.remove("hidden");
    setTimeout(() => toastEl.classList.add("hidden"), 3200);
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    const key = getKey();
    if (key) headers.Authorization = `Bearer ${key}`;
    if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, Object.assign({}, options, { headers }));
    if (response.status === 401) {
      clearKey();
      showLogin("密钥无效，请重新输入。");
      const error = new Error("未授权");
      error.status = 401;
      throw error;
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
    }
    if (!response.ok) {
      const detail = data && (data.detail || data.message || (data.error && data.error.message));
      const error = new Error(typeof detail === "string" ? detail : `请求失败 HTTP ${response.status}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  function showLogin(message) {
    loginView.classList.remove("hidden");
    consoleView.classList.add("hidden");
    headerActions.classList.add("hidden");
    loginError.classList.toggle("hidden", !message);
    loginError.textContent = message || "";
  }

  function showConsole() {
    loginView.classList.add("hidden");
    consoleView.classList.remove("hidden");
    headerActions.classList.remove("hidden");
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = $("api-key").value.trim();
    if (!value) return;
    setKey(value);
    try {
      await loadAll();
      showConsole();
    } catch (err) {
      if (err.status !== 401) showLogin(err.message);
    }
  });

  $("btn-logout").addEventListener("click", () => {
    clearKey();
    showLogin("");
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("hidden", panel.id !== `tab-${tab.dataset.tab}`);
      });
    });
  });

  function formatTime(value) {
    if (!value) return "—";
    return String(value).replace("T", " ").replace("+00:00", "Z");
  }

  function statusPill(status) {
    if (!status) return '<span class="pill">—</span>';
    const kind = ["claimed", "already_claimed", "ok"].includes(status) ? "ok"
      : ["error", "confirm_failed"].includes(status) ? "err" : "warn";
    return `<span class="pill ${kind}">${escapeHtml(status)}</span>`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function credentialLabel(account) {
    if (account.kind === "oauth") {
      const oauth = account.oauth || {};
      if (!oauth.has_access_key) return "未登录";
      const expiry = oauth.expires_at ? formatTime(oauth.expires_at) : "无过期时间";
      return oauth.expired ? `已过期 / ${expiry}` : `有效至 ${expiry}`;
    }
    return account.ak_masked || "未设置 AK";
  }

  function renderAccounts(accounts) {
    const body = $("account-rows");
    if (!accounts.length) {
      body.innerHTML = '<tr><td colspan="7" class="muted">还没有账号。可添加 AK/SK，或添加 OAuth 账号后走官方浏览器登录。</td></tr>';
      return;
    }
    body.innerHTML = accounts.map((account) => {
      const last = account.last_checkin || {};
      const cool = account.in_cooldown
        ? `<span class="pill warn">冷却至 ${escapeHtml(formatTime(account.cooldown_until))}</span>`
        : "";
      const err = account.last_error ? `<div class="error">${escapeHtml(account.last_error)}</div>` : "";
      return `<tr data-id="${escapeHtml(account.id)}">
        <td><strong>${escapeHtml(account.name)}</strong><div class="muted">${escapeHtml(account.id)}</div></td>
        <td>${escapeHtml(account.kind)}</td>
        <td>${escapeHtml(credentialLabel(account))}</td>
        <td><label class="check"><input type="checkbox" class="toggle-enabled" ${account.enabled ? "checked" : ""}></label></td>
        <td>${statusPill(last.status)}<div class="muted">${escapeHtml(formatTime(account.last_checkin_at))}</div></td>
        <td>${cool || '<span class="pill ok">就绪</span>'}${err}</td>
        <td class="row-actions">
          <button type="button" class="btn small" data-act="export">导出 Auth</button>
          <button type="button" class="btn small" data-act="edit">编辑</button>
          <button type="button" class="btn small" data-act="test">探测</button>
          ${account.kind === "oauth" ? '<button type="button" class="btn small" data-act="login">OAuth 登录</button>' : ""}
          <button type="button" class="btn small" data-act="checkin">签到</button>
          <button type="button" class="btn small danger" data-act="delete">删除</button>
        </td>
      </tr>`;
    }).join("");

    body.querySelectorAll("tr[data-id]").forEach((row) => {
      const id = row.getAttribute("data-id");
      const account = accounts.find((item) => item.id === id);
      row.querySelector(".toggle-enabled").addEventListener("change", async (event) => {
        try {
          await api(`admin/accounts/${id}`, {
            method: "PATCH",
            body: JSON.stringify({ enabled: event.target.checked }),
          });
          toast("已更新启用状态");
          await loadAccounts();
        } catch (err) {
          toast(err.message);
          event.target.checked = !event.target.checked;
        }
      });
      row.querySelectorAll("[data-act]").forEach((button) => {
        button.addEventListener("click", () => handleAccountAction(button.dataset.act, account));
      });
    });
  }

  async function handleAccountAction(act, account) {
    try {
      if (act === "export") {
        if (!confirm("导出文件包含完整密钥及 OAuth 会话，请仅保存在可信设备。是否继续？")) return;
        const payload = await api(`admin/accounts/${encodeURIComponent(account.id)}/export`);
        const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = `account-${account.id.replace(/[^A-Za-z0-9_-]/g, "_")}.json`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        return;
      }
      if (act === "edit") {
        openAccountDialog(account);
        return;
      }
      if (act === "delete") {
        if (!confirm(`确定删除账号「${account.name}」吗？此操作不可撤销。`)) return;
        await api(`admin/accounts/${account.id}`, { method: "DELETE" });
        toast("账号已删除");
        await loadAll();
        return;
      }
      if (act === "test") {
        const result = await api(`admin/accounts/${account.id}/test`, { method: "POST" });
        toast(result.ok ? `探测成功：${result.account_type || "ok"}` : `探测失败：${result.message || "error"}`);
        return;
      }
      if (act === "login") {
        startOauthLogin(account);
        return;
      }
      if (act === "checkin") {
        await api("admin/checkin", { method: "POST", body: JSON.stringify({ account_id: account.id }) });
        toast(`已为 ${account.name} 执行签到`);
        await loadAll();
      }
    } catch (err) {
      toast(err.message);
    }
  }

  $("btn-import-auth").addEventListener("click", () => $("auth-files").click());
  $("auth-files").addEventListener("change", async (event) => {
    try {
      const selected = Array.from(event.target.files);
      if (!selected.length) return;
      if (selected.length > 100 || selected.reduce((n, f) => n + f.size, 0) > 4 * 1024 * 1024) {
        throw new Error("最多导入 100 个文件，总计不超过 4 MiB");
      }
      const files = await Promise.all(selected.map(async (file) => JSON.parse(await file.text())));
      await api("admin/accounts/import", { method: "POST", body: JSON.stringify({ files }) });
      toast("Auth 文件已导入");
      await loadAll();
    } catch (err) { toast(err.message); }
    finally { event.target.value = ""; }
  });

  function openAccountDialog(account, kind) {
    const form = $("account-form");
    form.reset();
    $("account-error").classList.add("hidden");
    const isEdit = Boolean(account);
    const accountKind = account ? account.kind : (kind || "aksk");
    form.kind.value = accountKind;
    form.id.value = account ? account.id : "";
    form.name.value = account ? account.name : "";
    form.note.value = account ? account.note : "";
    form.enabled.checked = account ? account.enabled : true;
    form.project_id.value = account && account.project_id ? account.project_id : "";
    $("account-dialog-title").textContent = isEdit ? `编辑账号 · ${account.name}` : (accountKind === "oauth" ? "添加 OAuth 账号" : "添加 AK/SK 账号");
    $("aksk-fields").hidden = accountKind === "oauth" && !isEdit ? true : accountKind === "oauth";
    if (accountKind === "aksk") $("aksk-fields").hidden = false;
    $("secret-hint").hidden = !isEdit || accountKind !== "aksk";
    if (accountKind === "aksk") {
      form.ak.required = !isEdit;
      form.sk.required = !isEdit;
      form.ak.placeholder = isEdit ? "留空表示保持原值" : "";
      form.sk.placeholder = isEdit ? "留空表示保持原值" : "";
    } else {
      form.ak.required = false;
      form.sk.required = false;
    }
    accountDialog.showModal();
  }

  $("btn-add-aksk").addEventListener("click", () => openAccountDialog(null, "aksk"));
  $("btn-add-oauth").addEventListener("click", () => openAccountDialog(null, "oauth"));
  $("btn-account-cancel").addEventListener("click", () => accountDialog.close());

  $("account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const id = form.id.value;
    const kind = form.kind.value;
    const payload = {
      name: form.name.value.trim(),
      kind,
      enabled: form.enabled.checked,
      note: form.note.value,
    };
    if (kind === "aksk") {
      if (form.ak.value) payload.ak = form.ak.value;
      if (form.sk.value) payload.sk = form.sk.value;
      if (form.security_token.value) payload.security_token = form.security_token.value;
      payload.project_id = form.project_id.value;
    }
    try {
      if (id) {
        await api(`admin/accounts/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
        toast("账号已更新");
      } else {
        await api("admin/accounts", { method: "POST", body: JSON.stringify(payload) });
        toast("账号已添加");
      }
      accountDialog.close();
      await loadAll();
    } catch (err) {
      $("account-error").textContent = err.message;
      $("account-error").classList.remove("hidden");
    }
  });

  async function startOauthLogin(account) {
    $("oauth-account-label").textContent = `${account.name} (${account.id})`;
    $("oauth-status").textContent = "正在启动…";
    $("oauth-url-wrap").hidden = true;
    $("oauth-callback").textContent = "";
    loginDialog.showModal();
    try {
      const data = await api(`admin/accounts/${account.id}/login`, { method: "POST" });
      showOauthStatus(data);
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        try {
          const status = await api(`admin/accounts/${account.id}/login`);
          showOauthStatus(status);
          if (status.status === "logged_in" || status.status === "error" || status.status === "idle") {
            clearInterval(pollTimer);
            pollTimer = null;
            if (status.status === "logged_in") {
              toast("OAuth 登录成功");
              await loadAll();
            }
          }
        } catch (err) {
          $("oauth-status").textContent = err.message;
        }
      }, 1500);
    } catch (err) {
      $("oauth-status").textContent = err.message;
    }
  }

  function showOauthStatus(data) {
    $("oauth-status").textContent = data.status || "unknown";
    if (data.authorize_url) {
      $("oauth-url-wrap").hidden = false;
      const link = $("oauth-url");
      link.href = data.authorize_url;
      link.textContent = data.authorize_url;
    }
    $("oauth-callback").textContent = data.callback_url ? `回调地址：${data.callback_url}` : (data.message || "");
  }

  $("btn-copy-url").addEventListener("click", async () => {
    const url = $("oauth-url").textContent;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      toast("已复制授权地址");
    } catch {
      toast("复制失败，请手动选择地址");
    }
  });

  $("btn-login-close").addEventListener("click", () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    loginDialog.close();
  });

  function renderStatus(data) {
    const scheduler = data.scheduler || {};
    $("scheduler-info").innerHTML = [
      ["启用", scheduler.enabled ? "是" : "否"],
      ["Cron", scheduler.cron || "—"],
      ["时区", scheduler.timezone || "—"],
      ["上次运行", formatTime(scheduler.last_run_at)],
      ["下次运行", formatTime(scheduler.next_run_at)],
      ["调度错误", scheduler.last_error || "无"],
    ].map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");
    $("last-checkin").textContent = data.last_checkin
      ? JSON.stringify(data.last_checkin, null, 2)
      : "暂无";
    const accounts = data.pool && data.pool.accounts ? data.pool.accounts : [];
    if (!accounts.length) {
      $("checkin-accounts").innerHTML = '<p class="muted">账号池为空。</p>';
      return;
    }
    $("checkin-accounts").innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>账号</th><th>上次签到</th><th>时间</th><th>权益</th><th></th></tr></thead>
      <tbody>${accounts.map((account) => `<tr>
        <td>${escapeHtml(account.name)}</td>
        <td>${statusPill((account.last_checkin || {}).status)} <span class="muted">${escapeHtml((account.last_checkin || {}).message || "")}</span></td>
        <td>${escapeHtml(formatTime(account.last_checkin_at))}</td>
        <td>${escapeHtml((account.last_benefit || {}).status || "—")}<div class="muted">${escapeHtml(formatTime(account.last_benefit_at))}</div></td>
        <td class="row-actions">
          <button type="button" class="btn small" data-checkin="${escapeHtml(account.id)}">签到</button>
          <button type="button" class="btn small" data-benefit="${escapeHtml(account.id)}">领取权益</button>
        </td>
      </tr>`).join("")}</tbody></table></div>`;
    $("checkin-accounts").querySelectorAll("[data-checkin]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await api("admin/checkin", { method: "POST", body: JSON.stringify({ account_id: button.dataset.checkin }) });
          toast("签到已执行");
          await loadAll();
        } catch (err) { toast(err.message); }
      });
    });
    $("checkin-accounts").querySelectorAll("[data-benefit]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await api("admin/benefit-claim", { method: "POST", body: JSON.stringify({ account_id: button.dataset.benefit }) });
          toast("权益领取已执行");
          await loadAll();
        } catch (err) { toast(err.message); }
      });
    });
  }

  $("btn-checkin-all").addEventListener("click", async () => {
    try {
      await api("admin/checkin", { method: "POST", body: JSON.stringify({}) });
      toast("已为全部启用账号签到");
      await loadAll();
    } catch (err) { toast(err.message); }
  });

  $("btn-benefit-all").addEventListener("click", async () => {
    try {
      await api("admin/benefit-claim", { method: "POST", body: JSON.stringify({}) });
      toast("已为全部启用账号领取权益");
      await loadAll();
    } catch (err) { toast(err.message); }
  });

  function fillSettings(config) {
    $("readonly-settings").innerHTML = [
      ["监听地址", `${config.host}:${config.port}`],
      ["数据目录", config.data_dir],
      ["会话目录", config.auth_dir],
      ["API Key", `${config.api_key_configured ? "已配置" : "未配置"}（来源：${config.api_key_source || "env"}）`],
    ].map(([k, v]) => `<div class="readonly-item"><b>${escapeHtml(k)}</b>${escapeHtml(v)}</div>`).join("");
    const form = $("settings-form");
    const names = [
      "timezone", "checkin_cron", "campaign_type", "model_cache_ttl", "pool_strategy",
      "cooldown_seconds", "log_level", "credential_source", "http_timeout",
      "http_stream_timeout", "oauth_callback_port", "oauth_locale", "snap_base_url",
      "opengw_base_url", "oauth_portal_url", "oauth_sts_url", "oauth_client_id", "x_language",
    ];
    names.forEach((name) => {
      if (!(name in form)) return;
      let value = config[name];
      if (name === "credential_source" && value === "env") value = "aksk";
      form[name].value = value == null ? "" : value;
    });
    form.scheduler_enabled.checked = Boolean(config.scheduler_enabled);
    form.checkin_run_on_startup.checked = Boolean(config.checkin_run_on_startup);
    form.checkin_claim_all.checked = Boolean(config.checkin_claim_all);
    form.api_key.value = "";
    $("settings-message").textContent = "host / port 只能通过环境变量 CA2A_HOST / CA2A_PORT 修改，需重启进程。";
  }

  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      timezone: form.timezone.value,
      checkin_cron: form.checkin_cron.value,
      campaign_type: form.campaign_type.value,
      model_cache_ttl: Number(form.model_cache_ttl.value),
      pool_strategy: form.pool_strategy.value,
      cooldown_seconds: Number(form.cooldown_seconds.value),
      log_level: form.log_level.value,
      credential_source: form.credential_source.value,
      http_timeout: Number(form.http_timeout.value),
      http_stream_timeout: Number(form.http_stream_timeout.value),
      oauth_callback_port: Number(form.oauth_callback_port.value),
      oauth_locale: form.oauth_locale.value,
      snap_base_url: form.snap_base_url.value,
      opengw_base_url: form.opengw_base_url.value,
      oauth_portal_url: form.oauth_portal_url.value,
      oauth_sts_url: form.oauth_sts_url.value,
      oauth_client_id: form.oauth_client_id.value,
      x_language: form.x_language.value,
      scheduler_enabled: form.scheduler_enabled.checked,
      checkin_run_on_startup: form.checkin_run_on_startup.checked,
      checkin_claim_all: form.checkin_claim_all.checked,
    };
    if (form.api_key.value) payload.api_key = form.api_key.value;
    try {
      const saved = await api("admin/config", { method: "PUT", body: JSON.stringify(payload) });
      if (payload.api_key) setKey(payload.api_key);
      fillSettings(saved);
      $("settings-message").textContent = "配置已保存，可立即生效的项已应用。";
      toast("配置已保存");
      await loadStatus();
    } catch (err) {
      $("settings-message").textContent = err.message;
    }
  });

  async function loadAccounts() {
    const data = await api("admin/accounts");
    renderAccounts(data.accounts || []);
  }

  async function loadStatus() {
    const data = await api("admin/status");
    renderStatus(data);
    const enabled = data.pool ? data.pool.enabled : 0;
    const count = data.pool ? data.pool.count : 0;
    headerStatus.textContent = `账号 ${enabled}/${count}`;
    headerStatus.className = "pill " + (enabled ? "ok" : "warn");
  }

  async function loadSettings() {
    const data = await api("admin/config");
    fillSettings(data);
  }

  async function loadAll() {
    await Promise.all([loadAccounts(), loadStatus(), loadSettings()]);
  }

  (async () => {
    if (!getKey()) {
      showLogin("");
      return;
    }
    try {
      await loadAll();
      showConsole();
    } catch (err) {
      if (err.status !== 401) showLogin(err.message);
    }
  })();
})();
