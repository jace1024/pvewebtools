// 执行日志页（M3/T3.4 第 6 块，从 app.js 抽出）：任务历史列表、状态/工具筛选、日志详情弹窗、
// 批量清理。
//
// 依赖方向：调用 js/ui.js（$、转义、Toast、appConfirm）与 js/api.js（api）；由页签切换
// （app.js 的 switchView）与 index.html 的内联 onclick（loadJobs / clearJobs …）调用。
// classic script 加载，函数仍在全局作用域。

// ==================== 执行日志 ====================

let allJobs = [];           // 最近一次拉取的任务列表
let jobsPollTimer = null;   // 日志页自动轮询（有运行中任务时）
let jobLogPollTimer = null; // 详情弹窗轮询
let currentJobId = null;    // 详情弹窗打开的任务
let currentJobLines = [];   // 详情弹窗当前任务的日志原文（「复制日志」用）

const JOB_STATUS_TEXT = { running: "执行中", success: "成功", failed: "失败" };

function jobStatusBadge(status) {
  const cls = { running: "running", success: "success", failed: "failed" }[status] || "stopped";
  return `<span class="job-badge ${cls}">${JOB_STATUS_TEXT[status] || status}</span>`;
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDuration(started, ended) {
  if (!started) return "-";
  const sec = (ended || Date.now() / 1000) - started;
  if (sec < 60) return sec.toFixed(1) + " 秒";
 return Math.floor(sec / 60) + " 分 " + Math.round(sec % 60) + " 秒";
}

async function loadJobs() {
 try {
 allJobs = await api("/jobs");
 renderJobs();
 // 有执行中的任务时每 2 秒刷一次状态
 clearInterval(jobsPollTimer);
 if (allJobs.some((j) => j.status === "running")) {
      jobsPollTimer = setInterval(loadJobs, 2000);
    }
  } catch (e) {
    toast("日志加载失败: " + e.message, true);
  }
}

// 本地节点名：跑本服务这台。优先用后端在 /api/nodes 里标的 local 标记 ——
// 不能拿 allNodes[0] 猜：pvesh 返回顺序不保证本机在前，猜错会让「重启节点」这类操作指向错误节点。
function localNodeName() {
  const me = allNodes.find((n) => n.local);
  if (me) return String(me.node || "");
  // 兜底：后端未标记时退回原行为，宁可不准也不要返回空
  return allNodes.length ? String(allNodes[0].node || "") : "本机";
}

function renderJobs() {
  const kw = $("jobSearchBox").value.trim().toLowerCase();
  const st = $("jobStatusFilter").value;
  const rows = allJobs
    .filter((j) => !st || j.status === st)
    .filter((j) => !kw || j.title.toLowerCase().includes(kw));

  $("jobsCount").textContent = `共 ${rows.length} 条`;
  $("jobEmptyTip").classList.toggle("hidden", rows.length > 0);
  $("jobBody").innerHTML = rows.map((j) => `
    <tr>
      <td data-label="状态">${jobStatusBadge(j.status)}${j.status === "running" && j.pid ? `<div class="muted" style="font-size:11px">PID ${j.pid}</div>` : ""}</td>
      <td class="name" data-label="工具" onclick="openJobLog(${jsStr(j.id)})">${escapeHtml(j.title)}</td>
      <td data-label="执行节点">${escapeHtml(j.node || localNodeName())}</td>
      <td data-label="开始时间">${fmtTime(j.started_at)}</td>
      <td data-label="耗时">${fmtDuration(j.started_at, j.ended_at)}</td>
      <td data-label="操作">
        <button class="btn btn-sm" onclick="openJobLog(${jsStr(j.id)})">查看日志</button>
        ${j.status === "running"
          ? `<button class="btn btn-sm danger" onclick="killJob(${jsStr(j.id)})">强制结束</button>`
          : `<button class="btn btn-sm danger" onclick="deleteJob(${jsStr(j.id)})">删除</button>`}
      </td>
    </tr>`).join("");
}

async function killJob(jobId) {
  if (!(await appConfirm("确定强制结束这个任务吗？脚本及其拉起的子进程（如 apt）会被立即杀死，已执行的部分可能已生效且不会回滚。",
                         { title: "强制结束任务", okText: "强制结束", danger: true }))) return;
  try {
    await api(`/jobs/${jobId}/kill`, "POST", {});
    toast("强杀信号已发送");
    if (currentJobId === jobId) openJobLog(jobId);
    loadJobs();
  } catch (e) {
    toast("强杀失败: " + e.message, true);
  }
}

async function deleteJob(jobId) {
  if (!(await appConfirm("确定删除这条执行日志吗？", { danger: true }))) return;
  try {
    await api(`/jobs/${jobId}`, "DELETE");
    toast("日志已删除");
    loadJobs();
  } catch (e) {
    toast("删除失败: " + e.message, true);
  }
}

// 批量清理日志：按下拉选的时间范围删（7/30/90 天前）或全部清空，运行中的任务不受影响
async function clearJobs() {
  const days = $("jobClearDays").value;
  const scope = days === "all" ? "全部已结束日志" : `${days} 天前的日志`;
  if (!(await appConfirm(`确定删除${scope}吗？执行中的任务不受影响。`, { danger: true }))) return;
  try {
    const payload = days === "all" ? {} : { before_days: Number(days) };
    const r = await api("/jobs/clear", "POST", payload);
    toast(`已清理 ${r.deleted} 条日志`);
    loadJobs();
  } catch (e) {
    toast("清理失败: " + e.message, true);
  }
}

// 一键复制详情弹窗里的完整日志：直接拼原文行，不复用 DOM 文本（卡片行的按钮文字等不会混进来）
async function copyJobLog() {
  if (!currentJobLines.length) { toast("暂无可复制的日志", true); return; }
  const ok = await copyTextToClipboard(currentJobLines.join("\n"));
  toast(ok ? `已复制 ${currentJobLines.length} 行日志` : "复制失败，请手动选择日志后复制", !ok);
}

function renderJobDetail(snap) {
  currentJobLines = snap.lines || [];   // 复制日志用原文（与直接渲染的 DOM 文本一致）
  $("jobLogTitle").textContent = snap.title + " - 执行日志";
  $("jobMeta").innerHTML = `
    <span>状态：${jobStatusBadge(snap.status)}</span>
    <span>节点：${escapeHtml(snap.node || localNodeName())}</span>
    ${snap.status === "running" && snap.pid ? `<span>PID：${snap.pid}</span>` : ""}
    <span>开始：${fmtTime(snap.started_at)}</span>
    <span>结束：${fmtTime(snap.ended_at)}</span>
    <span>耗时：${fmtDuration(snap.started_at, snap.ended_at)}</span>
    <button class="btn btn-sm" type="button" onclick="copyJobLog()" title="复制该任务的完整日志（纯文本），便于贴给他人排障">
      <svg class="ico"><use href="#ico-clipboard"></use></svg>复制日志</button>
    ${snap.status === "running" ? `<button class="btn btn-sm danger" onclick="killJob(${jsStr(snap.id)})">强制结束</button>` : ""}`;
  // 逐行包 .log-line（与工具弹窗同一渲染结构）：卡片行的块级样式才生效；
  // 不再用 "\n" 连接——<pre> 里 div 自带换行，避免卡片行之间多出空行
  $("jobFullLog").innerHTML = snap.lines.length
    ? snap.lines.map((l) => `<div class="log-line">${colorLog(l)}</div>`).join("")
    : '<span class="muted">暂无输出</span>';
  $("jobFullLog").scrollTop = $("jobFullLog").scrollHeight;
}

async function openJobLog(jobId) {
  currentJobId = jobId;
  clearTimeout(jobLogPollTimer);
  $("jobMeta").innerHTML = '<p class="muted">加载中...</p>';
  $("jobFullLog").textContent = "";
  currentJobLines = [];   // 加载中先清空，避免「复制日志」拷到上一个任务的残留
  $("jobLogModal").classList.remove("hidden");
  try {
    const snap = await api(`/jobs/${jobId}`);
    renderJobDetail(snap);
    // 运行中的任务持续轮询增量
    if (snap.status === "running") {
      jobLogPollTimer = setTimeout(() => openJobLog(jobId), 1500);
    }
  } catch (e) {
    $("jobMeta").innerHTML = `<p class="muted">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function closeJobLogModal() {
  clearTimeout(jobLogPollTimer);
  currentJobId = null;
  $("jobLogModal").classList.add("hidden");
  // 关掉详情后刷新列表状态（任务可能刚结束）
  if (!$("viewLogs").classList.contains("hidden")) loadJobs();
}
