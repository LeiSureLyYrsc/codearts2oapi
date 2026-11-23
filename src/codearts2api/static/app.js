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

  // One authorization at a time. `gen` invalidates every late async callback
  // (poll response, start response, paste-submit) so a closed or restarted
  // dialog never writes into a newer flow.
  const auth = {
    gen: 0,
    pollSeq: 0,
    mode: "create",
    accountId: null,
    loginId: null,
    draft: null,
    submitting: false,
    finished: false,
  };

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
      body.innerHTML = '<tr><td colspan="7" class="muted">还没有账号。点击「连接 OAuth 账号」走华为云授权，或添加 AK/SK 账号、导入 Auth 文件。</td></tr>';
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
          ${account.kind === "oauth" ? '<button type="button" class="btn small" data-act="login">重新授权</button>' : ""}
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
        openAuthDialog(account);
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

  function openAccountDialog(account) {
    const form = $("account-form");
    form.reset();
    $("account-error").classList.add("hidden");
    const isEdit = Boolean(account);
    // OAuth credentials come from the browser flow, so this dialog only edits
    // the label/remark/enabled flag for those accounts.
    const isOauth = isEdit && account.kind === "oauth";
    form.kind.value = isEdit ? account.kind : "aksk";
    form.id.value = account ? account.id : "";
    form.name.value = account ? account.name : "";
    form.note.value = account ? account.note : "";
    form.enabled.checked = account ? account.enabled : true;
    form.project_id.value = account && account.project_id ? account.project_id : "";
    $("account-dialog-title").textContent = isEdit ? `编辑账号 · ${account.name}` : "添加 AK/SK 账号";
    $("aksk-fields").hidden = isOauth;
    $("secret-hint").hidden = !isEdit || isOauth;
    form.ak.required = !isEdit && !isOauth;
    form.sk.required = !isEdit && !isOauth;
    form.ak.placeholder = isEdit ? "留空表示保持原值" : "";
    form.sk.placeholder = isEdit ? "留空表示保持原值" : "";
    accountDialog.showModal();
  }

  $("btn-add-aksk").addEventListener("click", () => openAccountDialog(null));
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

  /* ---------- OAuth: authorize first, then save the account ---------- */

  const POLL_INTERVAL_MS = 1500;

  function setAuthStep(step) {
    $("login-step-details").classList.toggle("hidden", step !== "details");
    $("login-step-auth").classList.toggle("hidden", step !== "auth");
    $("login-step-done").classList.toggle("hidden", step !== "done");
  }

  function setAuthStatus(kind, text) {
    const label = $("oauth-status");
    label.textContent = text;
    const wrap = label.parentElement;
    if (wrap) wrap.className = `auth-status ${kind}`;
  }

  function showAuthError(message) {
    $("auth-error").textContent = message || "";
    $("auth-error").classList.toggle("hidden", !message);
  }

  function showCallbackError(message) {
    $("callback-error").textContent = message || "";
    $("callback-error").classList.toggle("hidden", !message);
  }

  function resetAuthError() {
    $("login-start-error").classList.add("hidden");
    showAuthError("");
    showCallbackError("");
  }

  // The paste box is useless before a login_id exists: submitting then would
  // look like a failure even though startup is merely still in flight.
  function setCallbackReady(ready) {
    $("btn-submit-callback").disabled = !ready;
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    // Invalidates any poll request that is already awaiting a response.
    auth.pollSeq += 1;
  }

  async function copyText(text, message) {
    const value = String(text || "").trim();
    if (!value) return;
    // navigator.clipboard needs a secure context. A gateway reached over plain
    // http:// (LAN address or forwarded port) has none, so keep a manual path.
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(value);
        toast(message);
        return;
      } catch {
        /* fall through */
      }
    }
    const field = $("authorize-url");
    field.focus();
    field.select();
    field.setSelectionRange(0, value.length);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    toast(copied ? message : "已选中地址，请按 Ctrl+C 复制");
  }

  function applyAuthPayload(data) {
    if (!data) return;
    if (data.login_id) auth.loginId = data.login_id;
    if (!data.authorize_url) {
      if (auth.loginId) setCallbackReady(true);
      return;
    }
    $("authorize-url").value = data.authorize_url;
    $("btn-copy-url").disabled = false;
    const link = $("oauth-open-link");
    link.href = data.authorize_url;
    link.classList.remove("hidden");
    if (auth.loginId) setCallbackReady(true);
  }

  // Clear everything that belongs to one authorization attempt. Called on open,
  // on start, and on retry so a stale URL/paste can never carry over.
  function resetFlowUi() {
    $("callback-url-input").value = "";
    $("authorize-url").value = "";
    $("oauth-open-link").classList.add("hidden");
    $("btn-copy-url").disabled = true;
    setCallbackReady(false);
    $("btn-login-retry").hidden = true;
  }

  function openAuthDialog(account) {
    // Bumping the generation abandons any previous flow and its in-flight work.
    const previous = !auth.finished ? auth.loginId : null;
    auth.gen += 1;
    stopPolling();
    auth.mode = account ? "reauth" : "create";
    auth.accountId = account ? account.id : null;
    auth.loginId = null;
    if (previous) cancelLogin(previous);
    auth.draft = account
      ? { name: account.name, note: account.note || "", enabled: account.enabled }
      : null;
    auth.submitting = false;
    auth.finished = false;

    const form = $("login-start-form");
    form.reset();
    resetAuthError();
    resetFlowUi();

    $("login-title").textContent = account ? "重新授权 CodeArts 账号" : "连接 CodeArts 账号";
    $("login-subtitle").textContent = account
      ? `为「${account.name}」重新登录，成功后替换旧凭证。`
      : "先在浏览器完成华为云授权，成功后再写入账号池。";

    if (account) {
      setAuthStatus("pending", "正在启动授权…");
      setAuthStep("auth");
    } else {
      setAuthStatus("pending", "准备中");
      setAuthStep("details");
    }
    loginDialog.showModal();
    if (account) startAuthFlow(auth.gen);
    else form.name.focus();
  }

  async function startAuthFlow(gen) {
    auth.finished = false;
    auth.submitting = false;
    resetAuthError();
    resetFlowUi();
    setAuthStep("auth");
    setAuthStatus("pending", "正在启动授权…");
    try {
      let data;
      if (auth.mode === "reauth") {
        data = await api(`admin/accounts/${encodeURIComponent(auth.accountId)}/login`, { method: "POST" });
      } else {
        data = await api("admin/login/start", {
          method: "POST",
          body: JSON.stringify(auth.draft || {}),
        });
      }
      if (gen !== auth.gen) {
        // The dialog was closed or restarted while this request was in flight.
        if (data && data.login_id) cancelLogin(data.login_id);
        return;
      }
      applyAuthPayload(data);
      if (data && data.status === "logged_in") {
        await finishAuth();
        return;
      }
      setAuthStatus("pending", "等待浏览器完成授权…");
      schedulePoll(gen);
    } catch (err) {
      if (gen !== auth.gen) return;
      if (auth.mode === "create") {
        // Name conflicts and validation errors belong on the form so they can be fixed.
        setAuthStep("details");
        const form = $("login-start-form");
        if (auth.draft) {
          form.name.value = auth.draft.name || "";
          form.note.value = auth.draft.note || "";
          form.enabled.checked = auth.draft.enabled !== false;
        }
        $("login-start-error").textContent = err.message;
        $("login-start-error").classList.remove("hidden");
        form.name.focus();
        return;
      }
      setAuthStatus("error", "启动失败");
      showAuthError(err.message);
      $("btn-login-retry").hidden = false;
    }
  }

  function schedulePoll(gen) {
    stopPolling();
    if (gen !== auth.gen || auth.finished || auth.submitting) return;
    if (!auth.loginId) {
      setAuthStatus("error", "授权会话已丢失");
      showAuthError("没有拿到授权会话，请重新开始。");
      $("btn-login-retry").hidden = false;
      return;
    }
    const loginId = auth.loginId;
    const seq = auth.pollSeq;
    pollTimer = setTimeout(() => {
      pollTimer = null;
      if (gen !== auth.gen || seq !== auth.pollSeq || auth.finished || auth.submitting) return;
      void pollLogin(gen, loginId, seq);
    }, POLL_INTERVAL_MS);
  }

  async function pollLogin(gen, loginId, seq) {
    try {
      const status = await api(`admin/login/status?login_id=${encodeURIComponent(loginId)}`);
      if (gen !== auth.gen || seq !== auth.pollSeq || auth.finished || auth.submitting) return;
      applyAuthPayload(status);
      if (status.status === "logged_in") {
        await finishAuth();
        return;
      }
      if (status.status === "pending") {
        setAuthStatus("pending", "等待浏览器完成授权…");
        schedulePoll(gen);
        return;
      }
      if (status.status === "error") {
        setAuthStatus("error", "授权失败");
        showAuthError(status.message || "授权未完成，请重新开始。");
      } else {
        setAuthStatus("idle", "授权会话已结束");
        showAuthError(status.message || "本次授权会话已结束（可能已超时），请重新开始。");
      }
      $("btn-login-retry").hidden = false;
    } catch (err) {
      if (gen !== auth.gen || seq !== auth.pollSeq) return;
      setAuthStatus("error", "无法获取授权状态");
      showAuthError(err.message);
      $("btn-login-retry").hidden = false;
    }
  }

  async function finishAuth() {
    if (auth.finished) return;
    auth.finished = true;
    stopPolling();
    auth.loginId = null;
    const name = (auth.draft && auth.draft.name) || "账号";
    setAuthStep("done");
    $("login-done-name").textContent = name;
    $("login-done-detail").textContent = "之后如果提示凭证过期或失效，点账号列表里这个账号的「重新授权」即可。";
    toast(auth.mode === "reauth" ? "已重新授权" : "OAuth 账号已连接");
    $("btn-login-done").focus();
    try {
      await loadAll();
    } catch {
      /* the success view is still valid; the table refreshes on the next load */
    }
  }

  async function cancelLogin(loginId) {
    if (!loginId) return;
    try {
      await api(`admin/login?login_id=${encodeURIComponent(loginId)}`, { method: "DELETE" });
    } catch {
      /* best effort: the backend expires abandoned flows anyway */
    }
  }

  function closeAuthDialog(options) {
    const cancel = !options || options.cancel !== false;
    const pending = cancel && !auth.finished && auth.loginId;
    auth.gen += 1;
    stopPolling();
    auth.loginId = null;
    auth.finished = false;
    auth.submitting = false;
    if (loginDialog.open) loginDialog.close();
    if (pending) cancelLogin(pending);
  }

  $("btn-add-oauth").addEventListener("click", () => openAuthDialog(null));

  $("login-start-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const name = form.name.value.trim();
    if (!name) {
      $("login-start-error").textContent = "请填写账号名称，用来在账号池里区分。";
      $("login-start-error").classList.remove("hidden");
      form.name.focus();
      return;
    }
    auth.draft = { name, note: form.note.value.trim(), enabled: form.enabled.checked };
    startAuthFlow(auth.gen);
  });

  $("btn-login-retry").addEventListener("click", async () => {
    // A retry is a fresh attempt: invalidate the old poll before touching state.
    auth.gen += 1;
    const gen = auth.gen;
    stopPolling();
    const stale = auth.loginId;
    auth.loginId = null;
    if (stale) await cancelLogin(stale);
    if (gen !== auth.gen || !loginDialog.open) return;
    await startAuthFlow(gen);
  });

  $("btn-login-cancel-start").addEventListener("click", () => closeAuthDialog({ cancel: false }));
  $("btn-login-cancel").addEventListener("click", () => closeAuthDialog());
  $("btn-login-close").addEventListener("click", () => closeAuthDialog());
  $("btn-login-done").addEventListener("click", () => closeAuthDialog({ cancel: false }));
  loginDialog.addEventListener("cancel", (event) => {
    // Esc must go through the same cancellation path as the buttons.
    event.preventDefault();
    closeAuthDialog();
  });

  $("btn-copy-url").addEventListener("click", () => copyText($("authorize-url").value, "已复制授权地址"));
  $("authorize-url").addEventListener("focus", (event) => event.target.select());
  $("authorize-url").addEventListener("click", (event) => event.target.select());

  function looksLikeCallback(raw) {
    let url;
    try {
      url = new URL(raw);
    } catch {
      return false;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return false;
    if (!url.pathname.replace(/\/+$/, "").endsWith("/oauth/callback")) return false;
    return url.search.length > 1;
  }

  $("callback-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (auth.submitting) return;
    const gen = auth.gen;
    const raw = $("callback-url-input").value.trim();
    const button = $("btn-submit-callback");
    showCallbackError("");
    if (!raw) {
      showCallbackError("请粘贴浏览器地址栏里的完整回调地址。");
      return;
    }
    if (!looksLikeCallback(raw)) {
      showCallbackError("需要一个完整的回调地址：http(s)://主机/oauth/callback?…，并且带上 ? 后面的 code 或 secret 参数。");
      return;
    }
    if (!auth.loginId) {
      showCallbackError("当前授权会话不可用，请重新开始授权。");
      $("btn-login-retry").hidden = false;
      return;
    }
    // The submit owns the next status read; drop the queued poll first.
    stopPolling();
    auth.submitting = true;
    button.disabled = true;
    let resumePoll = true;
    setAuthStatus("pending", "正在校验回调地址…");
    try {
      const status = await api("admin/login/callback", {
        method: "POST",
        body: JSON.stringify({ login_id: auth.loginId, callback_url: raw }),
      });
      if (gen !== auth.gen) return;
      applyAuthPayload(status);
      if (status.status === "logged_in") {
        $("callback-url-input").value = "";
        await finishAuth();
        return;
      }
      $("callback-url-input").value = "";
      if (status.status === "pending") {
        setAuthStatus("pending", "回调已收到，正在换取凭证…");
        return;
      }
      resumePoll = false;
      if (status.status === "error") {
        setAuthStatus("error", "授权失败");
        showCallbackError(status.message || "回调地址无效，请重新复制地址栏里的完整地址。");
      } else {
        setAuthStatus("idle", "授权会话已结束");
        showCallbackError(status.message || "本次授权会话已结束，请重新开始。");
      }
      $("btn-login-retry").hidden = false;
    } catch (err) {
      if (gen !== auth.gen) return;
      // A rejected URL keeps the flow pending, so the user can paste again.
      showCallbackError(err.message || "回调地址无效，请重新复制地址栏里的完整地址。");
      setAuthStatus("pending", "等待浏览器完成授权…");
    } finally {
      // A newer dialog may already own these flags; only the same generation clears them.
      if (gen === auth.gen) {
        auth.submitting = false;
        button.disabled = false;
        if (resumePoll && !auth.finished && auth.loginId) schedulePoll(gen);
      }
    }
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
