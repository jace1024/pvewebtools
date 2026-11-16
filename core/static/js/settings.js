// 系统设置：设置弹窗分区切换（帮助手册）+ 版本胶囊 + 「+ 加入节点」入口状态。
//
// 依赖方向：调用 js/ui.js（$、转义、Toast）与 js/api.js（api）；
// 由 index.html 的内联 onclick（openSettings / switchSettingsPane …）与 app.js 的初始化流程调用。
// classic script 加载，函数仍在全局作用域。

let editionInfo = null;   // /api/edition 的结果：{ runtime, can_join, max_nodes, enforced }

// 「+ 加入节点」入口状态：按 /api/edition 的 can_join 显隐（服务端返回不能加机时置灰并提示）。
function applyJoinEntryState() {
  const single = !!(editionInfo && editionInfo.can_join === false);
  document.querySelectorAll('[onclick="openJoinClusterModal()"]').forEach((b) => {
    b.style.display = single ? "none" : "";
    b.title = single ? "本版本为单机版，不支持加入节点" : "";
  });
  const hint = $("nodeMgmtHint");
  if (hint) {
    hint.textContent = single
      ? "（本机节点概览；可用节点操作、一键修复共享存储、临时取得仲裁）"
      : "（集群节点概览；可加入集群、逐节点操作、一键修复共享存储、临时取得仲裁）";
  }
}

// 由 app.js 的 loadEditionBadge() 在拿到 /api/edition 后回填（两处共用同一份结果）
function setEditionInfo(info) {
  editionInfo = info || null;
  applyJoinEntryState();
}

// ---- 系统设置弹窗（帮助手册分区）----

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
}
