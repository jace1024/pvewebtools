// 系统设置：设置弹窗分区切换（帮助手册）+ 版本胶囊。
//
// 依赖方向：调用 js/ui.js（$、转义、Toast）与 js/api.js（api）；
// 由 index.html 的内联 onclick（openSettings / switchSettingsPane …）与 app.js 的初始化流程调用。
// classic script 加载，函数仍在全局作用域。

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
