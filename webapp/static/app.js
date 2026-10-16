let authMode = "login";
let currentUser = null;

const activeStatuses = ["queued", "running", "stopping", "deleting"];
const authView = document.querySelector("#authView");
const dashboardView = document.querySelector("#dashboardView");
const authForm = document.querySelector("#authForm");
const taskForm = document.querySelector("#taskForm");
const authMsg = document.querySelector("#authMsg");
const taskMsg = document.querySelector("#taskMsg");
const logBox = document.querySelector("#logBox");
const submitBtn = document.querySelector("#submitBtn");
const activeNotice = document.querySelector("#activeNotice");

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
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function setView(loggedIn) {
  authView.classList.toggle("hidden", loggedIn);
  dashboardView.classList.toggle("hidden", !loggedIn);
}

async function loadMe() {
  try {
    const data = await api("/api/me");
    currentUser = data.user;
    document.querySelector("#userLine").textContent = `账号：${currentUser.username}`;
    setView(true);
    await loadTasks();
  } catch {
    setView(false);
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

function queueNotice(task) {
  if (task.status !== "queued" || task.queue_position == null) return "";
  const ahead = Number(task.queue_ahead || 0);
  const position = Number(task.queue_position || 1);
  const text = ahead <= 0
    ? "排队中：你是队列第 1 位，前方没有等待任务。"
    : `排队中：前面还有 ${ahead} 个任务，当前队列第 ${position} 位。`;
  return `<div class="queue-hint">${escapeHtml(text)}</div>`;
}

function renderTaskCard(task) {
  const div = document.createElement("article");
  div.className = "task-card";
  div.innerHTML = `
    <div class="task-title">
      <strong>#${task.id} ${escapeHtml(task.herbs)} / ${escapeHtml(task.disease)}</strong>
      <span class="status ${task.status}">${statusLabel(task.status)}</span>
    </div>
    <div class="task-meta">
      <span>pH ${escapeHtml(task.ph)}</span>
      <span>${escapeHtml(task.created_at || "")}</span>
      ${task.expires_at ? `<span>有效期至 ${escapeHtml(task.expires_at)}</span>` : ""}
    </div>
    ${queueNotice(task)}
    ${task.warning ? `<div class="warning-text">${escapeHtml(task.warning)}</div>` : ""}
    ${task.cleanup_error ? `<div class="warning-text">清理失败，可重试：${escapeHtml(task.cleanup_error)}</div>` : ""}
    <div class="task-actions">
      <button class="small-btn" data-log="${task.id}" type="button">日志</button>
      <button class="small-btn danger-btn" data-delete="${task.id}" data-delete-label="任务 #${task.id}" type="button">删除任务</button>
      ${task.download_url ? `<a class="download" href="${task.download_url}">下载结果</a>` : ""}
    </div>
  `;
  return div;
}

function renderTaskRow(task) {
  const div = document.createElement("div");
  div.className = "task-row";
  div.innerHTML = `
    <strong>#${task.id}</strong>
    <div>
      <strong>${escapeHtml(task.herbs)}</strong>
      <p>${escapeHtml(task.disease)} · pH ${escapeHtml(task.ph)}</p>
      ${task.warning ? `<p class="warning-text">${escapeHtml(task.warning)}</p>` : ""}
      ${task.cleanup_error ? `<p class="warning-text">清理失败，可重试：${escapeHtml(task.cleanup_error)}</p>` : ""}
    </div>
    <span class="status ${task.status}">${statusLabel(task.status)}</span>
    <p>${escapeHtml(task.finished_at || task.created_at || "")}</p>
    <p>${task.cleaned ? "已清理" : (task.expires_at ? `有效期至 ${escapeHtml(task.expires_at)}` : "-")}</p>
    <div class="task-actions">
      <button class="small-btn" data-log="${task.id}" type="button">日志</button>
      ${task.download_url ? `<a class="download" href="${task.download_url}">下载</a>` : ""}
      <button class="small-btn danger-btn" data-delete="${task.id}" data-delete-label="历史记录 #${task.id}" type="button">删除记录</button>
    </div>
  `;
  return div;
}

function setSubmitState(activeTask) {
  const locked = Boolean(activeTask);
  submitBtn.disabled = locked;
  submitBtn.textContent = locked ? "等待当前任务结束" : "提交任务";
  activeNotice.classList.toggle("hidden", !locked);
  activeNotice.textContent = locked
    ? `当前账号已有任务 #${activeTask.id} ${statusLabel(activeTask.status)}，完成后才能提交下一个。`
    : "";
}

async function loadTasks() {
  const data = await api("/api/tasks");
  const tasks = data.tasks || [];
  const queueSummary = data.queue_summary || {};
  const counts = tasks.reduce((acc, task) => {
    acc[task.status] = (acc[task.status] || 0) + 1;
    return acc;
  }, {});
  document.querySelector("#runningCount").textContent = queueSummary.running_count ?? counts.running ?? 0;
  document.querySelector("#queuedCount").textContent = queueSummary.queued_count ?? counts.queued ?? 0;
  document.querySelector("#completedCount").textContent = counts.completed || 0;
  document.querySelector("#runningBadge").textContent = `${tasks.length} 个任务`;
  document.querySelector("#taskCount").textContent = `${tasks.length} 个任务`;
  const current = tasks.filter(t => activeStatuses.includes(t.status));
  const history = tasks.filter(t => !activeStatuses.includes(t.status));
  setSubmitState(current[0]);
  const currentEl = document.querySelector("#currentTasks");
  const historyEl = document.querySelector("#historyTasks");
  currentEl.innerHTML = "";
  historyEl.innerHTML = "";
  current.forEach(t => currentEl.appendChild(renderTaskCard(t)));
  if (!current.length) currentEl.innerHTML = `<p class="muted">暂无运行或排队任务。</p>`;
  (history.length ? history : tasks).forEach(t => historyEl.appendChild(renderTaskRow(t)));
  bindTaskActions();
}

function bindTaskActions() {
  document.querySelectorAll("[data-log]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-log");
      document.querySelector("#selectedTask").textContent = `#${id}`;
      const data = await api(`/api/tasks/${id}/log`);
      const events = (data.events || []).map(e => `[${e.created_at}] ${e.level}: ${e.message}`).join("\n");
      logBox.textContent = `${events}\n\n${data.log || ""}`.trim() || "暂无日志。";
    };
  });
  document.querySelectorAll("[data-delete]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-delete");
      const label = btn.getAttribute("data-delete-label") || `任务 #${id}`;
      if (!confirm(`确定删除${label}吗？服务器上的结果文件、压缩包和日志会同步删除，且不可恢复。`)) {
        return;
      }
      btn.disabled = true;
      try {
        await api(`/api/tasks/${id}`, {method: "DELETE"});
        taskMsg.textContent = `${label} 已删除。`;
        document.querySelector("#selectedTask").textContent = "-";
        logBox.textContent = "选择一个任务查看日志摘要。";
        await loadTasks();
      } catch (err) {
        taskMsg.textContent = err.message;
        btn.disabled = false;
      }
    };
  });
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    authMode = tab.dataset.mode;
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === tab));
    document.querySelector("#inviteField").classList.toggle("hidden", authMode !== "register");
    authMsg.textContent = "";
  });
});

authForm.addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(authForm);
  authMsg.textContent = "";
  try {
    await api(authMode === "register" ? "/api/register" : "/api/login", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(form.entries()))
    });
    authForm.reset();
    await loadMe();
  } catch (err) {
    authMsg.textContent = err.message;
  }
});

taskForm.addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(taskForm);
  taskMsg.textContent = "";
  try {
    const data = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(form.entries()))
    });
    taskMsg.textContent = `任务 #${data.task_id} 已进入队列。`;
    taskForm.reset();
    await loadTasks();
  } catch (err) {
    taskMsg.textContent = err.message;
    await loadTasks().catch(() => {});
  }
});

document.querySelector("#refreshBtn").addEventListener("click", loadTasks);
document.querySelector("#logoutBtn").addEventListener("click", async () => {
  await api("/api/logout", {method: "POST", body: "{}"});
  currentUser = null;
  setView(false);
});

loadMe();
setInterval(() => {
  if (currentUser) loadTasks().catch(() => {});
}, 10000);
