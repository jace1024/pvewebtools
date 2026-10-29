// 系统设置（从 app.js 抽出）：
// 系统设置弹窗（系统升级 / 帮助手册 分区切换）、系统升级（上传校验 → 升级 / 回滚）。
//
// 依赖方向：调用 js/ui.js（$、转义、Toast、appConfirm）与 js/api.js（api）；
// 由 index.html 的内联 onclick（loadUpgrade / openSettings / switchSettingsPane …）
// 与 app.js 的初始化流程调用。classic script 加载，函数仍在全局作用域。

// ---- 版本信息 ----

let editionInfo = null;   // /api/edition 的结果：{ runtime, can_join, max_nodes, enforced }

// 「+ 加入节点」入口状态：开源版无授权限制，can_join 恒为 true，入口始终展示。
function applyJoinEntryState() {
  const single = !!(editionInfo && editionInfo.can_join === false);
  document.querySelectorAll('[onclick="openJoinClusterModal()"]').forEach((b) => {
    b.style.display = single ? "none" : "";
    b.title = single ? "本版本为单机版，不支持加入节点" : "";
  });
  // 节点列表副标题
  const hint = $("nodeMgmtHint");
  if (hint) {
    hint.textContent = single
      ? "（本机节点概览；可重启节点、重跑共享存储接入、临时取得仲裁）"
      : "（集群节点概览；可加入集群、重启节点、重跑共享存储接入、临时取得仲裁）";
  }
}

// 由 app.js 的 loadEditionBadge() 在拿到 /api/edition 后回填（两处共用同一份结果）
function setEditionInfo(info) {
  editionInfo = info || null;
  applyJoinEntryState();
}

// ---- 系统设置弹窗（系统升级 / 帮助手册 分区切换）----

function openSettings(pane = "help") {
  const mask = $("settingsModal");
  if (!mask) return;
  mask.classList.remove("hidden");
  switchSettingsPane(pane);
}

function closeSettings() {
  const mask = $("settingsModal");
  if (mask) mask.classList.add("hidden");
}

// 分区切换：按钮与内容都按 data-pane 匹配，新增分区只需再加一个 .settings-pane 节点
function switchSettingsPane(name) {
  document.querySelectorAll("#settingsModal .stab").forEach((b) =>
    b.classList.toggle("active", b.dataset.pane === name));
  document.querySelectorAll("#settingsModal .settings-pane").forEach((p) =>
    p.classList.toggle("hidden", p.dataset.pane !== name));
  if (name === "upgrade") {
    loadUpgrade();
  }
}

// ---- 系统升级（编译版自助升级：上传编译包 → 安装前校验 → 升级 / 回滚）----
// 升级由后端交给独立的 systemd 任务执行，期间服务会 stop + start，本页请求会短暂失败，
// 因此轮询对失败「只当服务重启中」继续重试，恢复后再显示最终结果。

let upgradeStatus = null;        // 最近一次 /upgrade/status 的响应
let upgradePollTimer = null;     // 升级进行中的轮询句柄
let upgradeRestarts = 0;         // 服务重启期间连续请求失败次数
let upgradeSubmitLost = false;   // 提交升级/回滚的回执丢失（服务重启打断了在飞的请求）
let upgradeEditionSyncedTs = 0;  // 已据此同步过版本胶囊的「完成」时间戳（避免重复拉取）

function scheduleUpgradePoll() {
  if (!upgradePollTimer) upgradePollTimer = setTimeout(pollUpgrade, 2000);
}

// 提交回执丢失后的收尾：服务恢复后用真实状态下结论 —— 有任务在跑就撤掉提示（当无事发生）；
// 没有则说明请求确实没送达，明确提示重试。不能像原来那样直接断言「未启动」：
// 服务重启会打断在飞的请求（Failed to fetch），但任务通常已经被受理
function resolveLostSubmit() {
  if (!upgradeSubmitLost) return;
  upgradeSubmitLost = false;
  const el = $("upgradeInspect");
  if (!el) return;
  el.innerHTML = ((upgradeStatus || {}).state || {}).running ? ""
    : '<div class="upgrade-check bad">服务已恢复，但没看到进行中的任务 —— 提交可能没有送达，请重试。</div>';
}

// 升级/回滚完成后当前版本就变了，而版本胶囊只在页面加载时取过一次（init 里的 loadEditionBadge），
// 页面不会自动重载 → 不补拉就会一直显示旧值（表现为「胶囊 LITE / 版本形态 PRO」这样自相矛盾）。
// 按状态的 ts 去重：同一次完成只同步一次，轮询期间不会每 2 秒重拉
function syncEditionAfterUpgrade() {
  const st = (upgradeStatus || {}).state || {};
  if (st.state !== "done" || !st.ts || st.ts === upgradeEditionSyncedTs) return;
  upgradeEditionSyncedTs = st.ts;
  loadEditionBadge();
}

async function loadUpgrade() {
  const sum = $("upgradeSummary");
  if (!sum) return;
  try {
    upgradeStatus = await api("/upgrade/status");
  } catch (e) {
    sum.innerHTML = `<p class="muted">读取升级状态失败: ${escapeHtml(e.message)}</p>`;
    return;
  }
  renderUpgradeSummary();
  renderUpgradePackages();
  resolveLostSubmit();
  syncEditionAfterUpgrade();
  if (((upgradeStatus || {}).state || {}).running) scheduleUpgradePoll();
}

async function pollUpgrade() {
  upgradePollTimer = null;
  try {
    upgradeStatus = await api("/upgrade/status");
    upgradeRestarts = 0;
  } catch (e) {
    // 升级过程中 pvewebtools 会被停掉再拉起：请求失败属预期，继续等待
    upgradeRestarts += 1;
    const sum = $("upgradeSummary");
    if (sum) {
      sum.innerHTML = `<p style="margin:0;color:var(--accent)">服务正在重启，等待恢复…`
        + `（已重试 ${upgradeRestarts} 次）</p>`;
    }
    if (upgradeRestarts < 90) scheduleUpgradePoll();
 return;
 }
 renderUpgradeSummary();
 renderUpgradePackages();
 resolveLostSubmit();
 syncEditionAfterUpgrade();
 if (((upgradeStatus || {}).state || {}).running) scheduleUpgradePoll();
}

function renderUpgradeSummary() {
 const st = upgradeStatus || {};
 const cur = st.current || {};
 const dep = st.deployment || {};
 const us = st.state || {};
 const badge = $("upgradeBadge");
 const ed = cur.edition ? cur.edition.toUpperCase() : "源码版";
 let html = "";
 // 编译版保留构建串；源码版用工作区 git 最近提交作为构建参考
 if (cur.edition || cur.build || cur.source === "source") {
 const buildTxt = cur.build
 ? `　构建：<span class="mono">${escapeHtml(cur.build)}</span>` : "";
    // 升级时间：本机把当前版本升上来/部署上的时刻（回答「这版何时上线」）
    let installedTxt = "";
    const it = cur.installed_at ? new Date(cur.installed_at) : null;
    if (it && !isNaN(it.getTime())) installedTxt = `　升级时间：<span class="mono">${escapeHtml(it.toLocaleString())}</span>`;
    html += `<div>当前版本：<b>${escapeHtml(cur.version || "未知")}</b>`
      + `　版本形态：<b>${escapeHtml(ed)}</b>`
      + installedTxt
      + buildTxt
      + `</div>`;
  }

  if (!dep.can_self_upgrade) {
    // reason 本身已是完整句子（含原因与 install.sh 部署指引），不再加前缀、也不再补
    // 第二段说明：同一件事说一遍就够（上传区另有「只做校验」提示，帮助手册有完整流程）
    html += `<p style="margin:${html ? "10px" : "0"} 0 0;color:#ff8c1a">`
      + `${escapeHtml(dep.reason || "未知原因")}</p>`;
    if (badge) badge.textContent = "不可自升级";
  } else if (us.running) {
    html += `<p style="margin:10px 0 0;color:var(--accent)">`
      + `${escapeHtml(us.message || "操作进行中")}`
      + `（${escapeHtml(us.from || "")} → ${escapeHtml(us.to || "")}）</p>`;
    if (badge) badge.textContent = "进行中";
  } else if (us.state === "error") {
    html += `<p class="u-err-msg">上次`
      + `${us.mode === "rollback" ? "回滚" : "升级"}未成功：${escapeHtml(us.message || "")}</p>`;
    if (badge) badge.textContent = "有异常";
  } else if (us.state === "done") {
    html += `<p class="u-ok-msg">上次操作已完成：`
      + `${escapeHtml(us.message || "")}</p>`;
    if (badge) badge.textContent = "";
  } else if (badge) {
    badge.textContent = "";
  }

  if (us.rollback_available && !us.running) {
    html += `<div class="u-flex-row">`
      + `<button class="btn" onclick="rollbackUpgrade()">回滚到 `
      + `${escapeHtml(us.rollback_version || "上一版本")}</button>`
      + `<span class="muted u-text13" >回滚同样会重启服务</span></div>`;
  }
  $("upgradeSummary").innerHTML = html;

  const btn = $("upgradeUploadBtn");
  if (btn) {
    // 能否上传取决于暂存目录可写（can_upload），与能否自助升级解耦
    btn.disabled = !!us.running || !dep.can_upload;
    btn.title = us.running ? "有升级任务进行中"
      : (dep.can_upload ? "" : (dep.upload_reason || "当前环境无法接收升级包"));
  }
}

function renderUpgradePackages() {
  const el = $("upgradePackages");
  if (!el) return;
  const list = (upgradeStatus || {}).packages || [];
  if (!list.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px;margin:0">暂无已上传的升级包。</p>';
    return;
  }
  el.innerHTML = list.map((p) =>
    '<div style="display:flex;gap:10px;align-items:center;padding:6px 0;font-size:13px;flex-wrap:wrap">'
    + `<span class="mono">${escapeHtml(p.name)}</span>`
    + `<span class="muted">${fmtBytes(p.size)}</span>`
    + `<button class="btn btn-sm" onclick="inspectUpgrade(${jsStr(p.name)})">校验</button>`
    + `<button class="btn btn-sm" onclick="deleteUpgradePackage(${jsStr(p.name)})">删除</button>`
    + '</div>').join("");
}

function uploadUpgrade() {
  const input = $("upgradeFile");
  const file = input && input.files && input.files[0];
  if (!file) { toast("请先选择升级包文件", true); return; }
  const btn = $("upgradeUploadBtn");
  if (btn.disabled) { toast(btn.title || "当前环境无法接收升级包", true); return; }
  const bar = $("upgradeUploadBar");
  const fill = $("upgradeUploadFill");
  const msg = $("upgradeUploadMsg");
  btn.disabled = true;
  bar.classList.remove("hidden");
  fill.style.width = "0%";
  msg.textContent = "上传中…";
  // 用 XHR 而非 fetch：需要上传进度（升级包几十 MB，无进度用户会以为卡死）
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upgrade/upload?name=" + encodeURIComponent(file.name));
  xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");  // CSRF 防护
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) {
      fill.style.width = Math.round((ev.loaded / ev.total) * 100) + "%";
      msg.textContent = `上传中… ${fmtBytes(ev.loaded)} / ${fmtBytes(ev.total)}`;
    }
  };
  xhr.onload = () => {
    btn.disabled = false;
    let data = null;
    try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { data = null; }
    if (xhr.status >= 200 && xhr.status < 300 && data && data.data) {
 fill.style.width = "100%";
 msg.textContent = `已上传（${fmtBytes(data.data.size)}），开始校验…`;
 inspectUpgrade(file.name);
 } else {
 const err = (data && data.error) || ("HTTP " + xhr.status);
 bar.classList.add("hidden");
 msg.textContent = "";
 toast("上传失败: " + err, true);
 }
 };
 xhr.onerror = () => {
    btn.disabled = false;
    bar.classList.add("hidden");
    msg.textContent = "";
    toast("上传失败：网络中断（可重新选择同一文件重试）", true);
  };
  xhr.send(file);
}

async function inspectUpgrade(name, force = false, allowDowngrade = false) {
  const el = $("upgradeInspect");
  if (!el) return;
  el.innerHTML = '<div class="upgrade-check muted">校验中…</div>';
  let res;
  try {
    res = await api("/upgrade/inspect", "POST",
      { name, force: !!force, allow_downgrade: !!allowDowngrade });
  } catch (e) {
    el.innerHTML = `<div class="upgrade-check bad">校验失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  renderUpgradeInspect(res, force, allowDowngrade);
}

function renderUpgradeInspect(res, force, allowDowngrade) {
  const m = res.manifest || {};
  const lines = (res.checks || []).map((c) => {
    const cls = (!c.ok) ? "bad" : (c.level === "warn" ? "warn" : "ok");
    const mark = (!c.ok) ? "✕" : (c.level === "warn" ? "!" : "✓");
    return `<div class="${cls}">${mark} ${escapeHtml(c.name)}：${escapeHtml(c.detail || "")}</div>`;
  }).join("");
  const pkgBuild = m.build ? `　构建 ${escapeHtml(m.build)}` : "";
  let html = '<div class="upgrade-check">'
    + `<div style="font-weight:600">包信息：${escapeHtml(m.version || "未知")}`
    + `（${escapeHtml((m.edition || "").toUpperCase())}）`
    + pkgBuild
    + `　架构 ${escapeHtml(m.arch || "-")}`
    + `　机器上限 ${escapeHtml(String(m.hard_max || "-"))}`
    + `　${fmtBytes(res.size || 0)}</div>${lines}`;
  if ((res.warnings || []).length) {
    html += `<div class="warn" style="margin-top:6px">`
      + (res.warnings || []).map(escapeHtml).join("<br>") + "</div>";
  }
  if ((res.errors || []).length) {
    html += `<div class="bad" style="margin-top:6px">`
      + (res.errors || []).map(escapeHtml).join("<br>") + "</div>";
  }
  html += "</div>";

  if (res.ok && res.can_apply) {
    html += '<div class="u-flex-row">'
      + `<button class="btn run" onclick="applyUpgrade(${jsStr(res.pkg)}, ${!!force}, ${!!allowDowngrade})">`
      + "开始升级并重启服务</button>"
      + '<span class="muted u-text13" >升级期间本页面会短暂断开，恢复后自动刷新</span></div>';
  } else if (res.ok) {
    // 源码版 / 开发环境：包本身没问题，但本机不能自助替换程序，只给结论不做误导性的操作入口
    html += '<div class="u-flex-row">'
      + '<span class="u-ok-text">校验通过：该包可用于安装。</span>'
      + '<span class="muted u-text13" >但当前环境不支持自助升级'
      + `${res.apply_blocked_reason ? "（" + escapeHtml(res.apply_blocked_reason) + "）" : ""}，`
      + "请用发行包内的 install.sh 手工部署。</span></div>";
  } else {
    const versionIssue = (res.errors || []).some((x) =>
      x.indexOf("同版本") >= 0 || x.indexOf("低于本机") >= 0);
    html += '<div class="u-flex-row">'
      + '<span class="bad u-text13" >校验未通过，未做任何改动</span>';
    if (versionIssue) {
      html += `<button class="btn" onclick="inspectUpgrade(${jsStr(res.pkg)}, true, true)">`
        + "允许重装/降级并重新校验</button>";
    }
    html += "</div>";
  }
  $("upgradeInspect").innerHTML = html;
}

async function applyUpgrade(name, force, allowDowngrade) {
  if (!(await appConfirm("确认升级到该版本？\n\n"
    + "· 升级过程会停止并重启 pvewebtools 服务，网页会短暂不可用\n"
    + "· 启动失败会自动回滚到当前版本\n"
    + "· 授权不受影响（授权码与本机指纹绑定）",
    { title: "系统升级", okText: "开始升级" }))) return;
  const el = $("upgradeInspect");
  el.innerHTML = '<div class="upgrade-check muted">正在提交升级任务…</div>';
  try {
    await api("/upgrade/apply", "POST",
      { name, force: !!force, allow_downgrade: !!allowDowngrade });
  } catch (e) {
    // 服务重启会打断在飞的请求（连接中断 → Failed to fetch），但任务多半已被受理，
    // 据此断言「升级未启动」是误报 —— 记下回执丢失，交给轮询用真实状态下结论
    el.innerHTML = '<div class="upgrade-check">提交请求被中断（服务可能已开始重启），正在确认任务状态…</div>';
    upgradeSubmitLost = true;
    upgradeRestarts = 0;
    scheduleUpgradePoll();
    return;
  }
  toast("升级任务已启动，服务将短暂重启");
  upgradeRestarts = 0;
  scheduleUpgradePoll();
}

async function rollbackUpgrade() {
  if (!(await appConfirm("确认回滚到升级前的版本？服务会重启，网页短暂不可用。",
                         { title: "回滚版本", okText: "确认回滚", danger: true }))) return;
  try {
    await api("/upgrade/rollback", "POST", {});
  } catch (e) {
    // 同上：回执丢失不等于没提交
    toast("提交请求被中断（服务可能已在重启），正在确认任务状态…");
    upgradeSubmitLost = true;
    upgradeRestarts = 0;
    scheduleUpgradePoll();
    return;
  }
  toast("回滚任务已启动，服务将短暂重启");
  upgradeRestarts = 0;
  scheduleUpgradePoll();
}

async function deleteUpgradePackage(name) {
  if (!(await appConfirm("删除已上传的升级包 " + name + " ？", { danger: true }))) return;
  try {
    await api("/upgrade/package", "DELETE", { name });
    toast("已删除");
    loadUpgrade();
  } catch (e) {
    toast("删除失败: " + e.message, true);
  }
}
