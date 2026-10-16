const adminLogBox = document.querySelector("#adminLogBox");
const loginPanel = document.querySelector("#adminLoginPanel");
const dashboard = document.querySelector("#adminDashboard");
const refreshButton = document.querySelector("#adminRefresh");
const logoutButton = document.querySelector("#adminLogout");

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  })[ch]);
}

function statusLabel(status) {
  const map = {
    queued: "排队中",
    running: "运行中",
    stopping: "停止中",
    completed: "已完成",
    no_result: "正常结束（无可用结果）",
    failed: "失败",
    cancelled: "已取消",
    deleting: "删除中"
  };
  return map[status] || status;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    credentials: "same-origin",
    ...options
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `HTTP ${res.status}`, res.status);
  return data;
}

function showLogin(message = "") {
  loginPanel.classList.remove("hidden");
  dashboard.classList.add("hidden");
  refreshButton.classList.add("hidden");
  logoutButton.classList.add("hidden");
  document.querySelector("#adminLoginMessage").textContent = message;
}

function showDashboard() {
  loginPanel.classList.add("hidden");
  dashboard.classList.remove("hidden");
  refreshButton.classList.remove("hidden");
  logoutButton.classList.remove("hidden");
  document.querySelector("#adminLoginMessage").textContent = "";
  document.querySelector("#adminToken").value = "";
}

function handleError(err) {
  if (err instanceof ApiError && [401, 403].includes(err.status)) {
    showLogin(err.message);
    return;
  }
  document.querySelector("#adminLine").textContent = err.message;
}

function metric(label, value) {
  return `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`;
}

function renderTaskRow(task) {
  const div = document.createElement("div");
  div.className = "task-row admin-task-row";
  div.innerHTML = `
    <strong>#${task.id}</strong>
    <div>
      <strong>${escapeHtml(task.herbs)}</strong>
      <p>${escapeHtml(task.disease)} · ${escapeHtml(task.username || "")}</p>
      ${task.warning ? `<p class="warning-text">${escapeHtml(task.warning)}</p>` : ""}
      ${task.cleanup_error ? `<p class="warning-text">清理失败，可重试：${escapeHtml(task.cleanup_error)}</p>` : ""}
    </div>
    <span class="status ${task.status}">${statusLabel(task.status)}</span>
    <p>${escapeHtml(task.cpu_range || "-")}</p>
    <p>${task.pid ? `PID ${task.pid}` : "-"}</p>
    <p>${task.cleaned ? "已清理" : (task.expires_at ? `到期 ${escapeHtml(task.expires_at)}` : "-")}</p>
    <div class="task-actions">
      <button class="small-btn" data-log="${task.id}" type="button">日志</button>
      ${["running", "queued", "stopping"].includes(task.status) ? `<button class="small-btn danger-btn" data-stop="${task.id}" type="button">停止</button>` : ""}
      <button class="small-btn" data-restart="${task.id}" type="button">重跑</button>
      ${task.download_url ? `<button class="small-btn download-btn" data-download="${task.id}" type="button">下载</button>` : ""}
      <button class="small-btn danger-btn" data-delete="${task.id}" type="button">删除</button>
    </div>
  `;
  return div;
}

async function loadOverview() {
  const data = await api("/api/admin/overview");
  showDashboard();
  const counts = data.counts || {};
  const settings = data.settings || {};
  document.querySelector("#metrics").innerHTML = [
    metric("磁盘剩余", `${data.free_gb} GB`),
    metric("运行中", counts.running || 0),
    metric("排队中", counts.queued || 0),
    metric("并发/资源", `${settings.max_concurrency} 任务 · ${settings.task_resource_label || `${settings.task_cpu_limit} 线程 · 内存不做硬限制`}`),
    metric("保留期", `${settings.retention_days} 天`),
    metric("CPU 槽位", (settings.cpu_slots || []).join(" / ")),
    metric("注册码", settings.invite_code || "-")
  ].join("");
  document.querySelector("#adminLine").textContent =
    `并发 ${settings.max_concurrency} · 每任务 ${settings.task_resource_label || "12 核 / 24 线程 / 内存不做硬限制"} · 结果保留 ${settings.retention_days} 天`;
  const list = document.querySelector("#adminTasks");
  list.innerHTML = "";
  (data.tasks || []).forEach(task => list.appendChild(renderTaskRow(task)));
  bindActions();
}

function bindActions() {
  document.querySelectorAll("[data-log]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-log");
      document.querySelector("#adminSelectedTask").textContent = `#${id}`;
      const data = await api(`/api/admin/tasks/${id}/log`);
      const events = (data.events || []).map(e => `[${e.created_at}] ${e.level}: ${e.message}`).join("\n");
      adminLogBox.textContent = `${events}\n\n${data.log || ""}`.trim() || "暂无日志。";
    };
  });
  document.querySelectorAll("[data-stop]").forEach(btn => {
    btn.onclick = async () => {
      await api(`/api/admin/tasks/${btn.getAttribute("data-stop")}/stop`, {method: "POST", body: "{}"});
      await loadOverview();
    };
  });
  document.querySelectorAll("[data-restart]").forEach(btn => {
    btn.onclick = async () => {
      await api(`/api/admin/tasks/${btn.getAttribute("data-restart")}/restart`, {method: "POST", body: "{}"});
      await loadOverview();
    };
  });
  document.querySelectorAll("[data-download]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-download");
      const res = await fetch(`/download/${id}`, {
        credentials: "same-origin"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `SushanAIDD_task_${id}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    };
  });
  document.querySelectorAll("[data-delete]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-delete");
      if (!confirm(`确定删除任务 #${id} 吗？服务器上的结果文件、压缩包和日志会同步删除，且不可恢复。`)) {
        return;
      }
      btn.disabled = true;
      await api(`/api/admin/tasks/${id}`, {method: "DELETE"});
      document.querySelector("#adminSelectedTask").textContent = "-";
      adminLogBox.textContent = "选择一个任务查看日志摘要。";
      await loadOverview();
    };
  });
}

document.querySelector("#adminLoginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const message = document.querySelector("#adminLoginMessage");
  message.textContent = "正在验证…";
  try {
    await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({token: document.querySelector("#adminToken").value})
    });
    await loadOverview();
  } catch (err) {
    showLogin(err.message);
  }
});

refreshButton.addEventListener("click", () => loadOverview().catch(handleError));
logoutButton.addEventListener("click", async () => {
  try {
    await api("/api/admin/logout", {method: "POST", body: "{}"});
  } finally {
    showLogin("已退出管理员会话。");
  }
});
document.querySelector("#cleanupBtn").addEventListener("click", async () => {
  try {
    await api("/api/admin/cleanup", {method: "POST", body: "{}"});
    await loadOverview();
  } catch (err) {
    handleError(err);
  }
});

loadOverview().catch(handleError);
setInterval(() => {
  if (!dashboard.classList.contains("hidden")) {
    loadOverview().catch(handleError);
  }
}, 10000);
