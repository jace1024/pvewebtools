// PVE WebTools 前端逻辑（原生 JS，无框架依赖；入口，最后加载）
//
// M3 起基础设施已拆到 js/ 目录（classic script 按序加载，**不是** ES module —— 内联 onclick
// 依赖全局函数名，改 module 会让它们全部失效）：
//   js/state.js  全局可变状态
//   js/ui.js     展示层：DOM 辅助 / 格式化 / 阈值 / 转义 / Toast / 应用内确认框
//   js/api.js    请求层：统一超时 + 有限重试 + 错误清洗
//   js/overview.js  概览页：节点卡片 / 虚机容器列表 / 分页与筛选 / 批量操作 / 数据加载
//   js/settings.js  系统设置：设置弹窗（帮助手册） / 版本档位
//   js/toolbox.js   工具箱：工具执行弹窗 / 定时开关机 / 虚机快照
//   js/transfer.js  传输中心：上传 / 云镜像 / LXC 模板 / 存储内容 / 缓存加速 / 可搜索下拉
//   js/passthrough.js  直通工具：IOMMU 重启提示 / 使直通生效
//   js/logs.js      执行日志页：任务历史 / 筛选 / 日志详情 / 清理
// 本文件只剩页签切换与初始化入口。

// ---- 页签切换 ----

function switchView(view) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view));
  $("viewOverview").classList.toggle("hidden", view !== "overview");
  $("viewNode").classList.toggle("hidden", view !== "node");
  $("viewToolbox").classList.toggle("hidden", view !== "toolbox");
  $("viewTransfer").classList.toggle("hidden", view !== "transfer");
  $("viewLogs").classList.toggle("hidden", view !== "logs");
  if (view === "node") loadNodeMgmt();
  if (view === "toolbox" && !toolboxData) loadToolbox();
  if (view === "logs") loadJobs();
}

async function loadToolbox() {
  // 加载前先铺卡片骨架屏
  $("toolGrid").innerHTML = Array(6).fill(
    `<div class="tool-card skeleton-card">
       <div class="sk-line sk-w60"></div><div class="sk-line"></div>
     </div>`).join("");
  try {
    toolboxData = await api("/tools");
    // categories 为空（后端异常/配置为空）时兜底，避免 categories[0].id 抛 TypeError，
    // 导致骨架屏残留、工具箱无法恢复
    if (!toolboxData.categories || !toolboxData.categories.length) {
      $("catList").innerHTML = "";
      $("toolGrid").innerHTML = `<div class="empty-state"><div class="empty-icon"><svg class="ico"><use href="#ico-cone"></use></svg></div><div>暂无可用工具</div></div>`;
      toast("工具箱数据为空", true);
      return;
    }
    currentCat = toolboxData.categories[0].id;
    renderToolbox();
  } catch (e) {
    toast("工具箱加载失败: " + e.message, true);
  }
}

function renderToolbox() {
  const { categories, tools } = toolboxData;
  // 隐藏工具（hidden:True，如云镜像的两个底层工具）不渲染为卡片，仅合成入口可见
  const visibleTools = tools.filter((t) => !t.hidden);
  $("catList").innerHTML = categories.map((c) => {
    const count = visibleTools.filter((t) => t.category === c.id).length;
    return `<button class="cat-item ${c.id === currentCat ? "active" : ""}" onclick="selectCat(${jsStr(c.id)})">
      <span><i class="cat-ico">${escapeHtml(c.icon)}</i>${escapeHtml(c.name)}</span><span class="muted">${count}</span>
    </button>`;
  }).join("");

  const catTools = visibleTools.filter((t) => t.category === currentCat)
    .sort((a, b) => (a.order ?? 999) - (b.order ?? 999));
  const cardHtml = catTools.map((t) => {
    const head = `<span><span class="tool-name">${escapeHtml(t.name)}</span>${t.tag ? `<span class="tool-tag">${escapeHtml(t.tag)}</span>` : ""}</span>`;
    const desc = `<p class="tool-card-desc" title="${escapeHtml(t.desc)}">${escapeHtml(t.desc)}</p>`;
    // 占位卡片（后端 disabled:True，如「高级玩法 → 即将开放」）：置灰、点不进执行弹窗，
    // 点击只给一句提示；不渲染风险标签，避免看起来像能跑的只读工具
    if (t.disabled) {
      return `<div class="tool-card is-disabled" title="即将开放" onclick="toast('该模块即将开放，敬请期待')">
        <div class="tool-card-head">${head}</div>${desc}</div>`;
    }
    return `<div class="tool-card" onclick="openTool(${jsStr(t.id)})">
      <div class="tool-card-head">${head}${RISK_TAG[t.risk] || ""}</div>${desc}</div>`;
  }).join("");
  const grid = $("toolGrid");

  // 存储与磁盘模块：存储管理面板置顶，磁盘工具卡片列于其后
  if (currentCat === SM_CAT_ID) {
    grid.innerHTML = buildStorageModuleHtml() + cardHtml;
    bindStorageModule();
  } else {
    grid.innerHTML = cardHtml;
  }
}

function selectCat(id) {
  currentCat = id;
  renderToolbox();
}

// 「系统设置」弹窗标题右侧的版本胶囊（描边风格）：开源版统一显示「开源版 <版本号>」
function showEditionBadge(version = "") {
  const el = $("editionBadge");
  if (!el) return;
  // 「未知」是后端取不到版本号时的兜底文案，拼进胶囊只是噪声，此时只显示形态
  const ver = version && version !== "未知" ? version : "";
  el.textContent = ver ? `开源版 ${ver}` : "开源版";
  el.style.display = "";
}

// 加载版本胶囊：轻量 /api/edition，不依赖工具箱 tab 的 /tools 重型接口；
// 弹窗未打开时也先取好，打开即可见（版本号回退读包内 VERSION 文件）
async function loadEditionBadge() {
  try {
    const data = await api("/edition");
    showEditionBadge(data.version || "");
    // 同一份结果顺带决定「+ 加入节点」入口（见 settings.js applyJoinEntryState）
    if (typeof setEditionInfo === "function") setEditionInfo(data);
  } catch (e) {
    // 获取失败不影响主流程，胶囊保持隐藏即可
  }
}

// ---- 初始化 ----

// 任一弹窗（工具执行 / 配置详情 / 日志详情 / 应用内确认框）是否打开
function isModalOpen() {
  return !$("toolModal").classList.contains("hidden") ||
    !$("modal").classList.contains("hidden") ||
    !$("appConfirmModal").classList.contains("hidden") ||
    !$("jobLogModal").classList.contains("hidden");
}

let _loginTfaToken = null;
let _mustChangePassword = false;

function setupLogin() {
  const mask = $("loginMask");
  const card = mask.querySelector(".login-card");
  const btn = $("btnLogin");
  const userInp = $("loginUser");
  const pwInp = $("loginPass");
  const otpWrap = $("loginOtpWrap");
  const otpInp = $("loginOtp");
  const newPwWrap = $("loginNewPwWrap");
  const newPw2Wrap = $("loginNewPw2Wrap");
  const newPwInp = $("loginNewPw");
  const newPw2Inp = $("loginNewPw2");
  const pwHint = $("loginPwHint");
  const errEl = $("loginErr");

  const idleText = () => (_mustChangePassword ? "设置新密码并登录" : "登录");

  function showErr(msg) {
    errEl.textContent = msg || "登录失败";
    pwInp.classList.add("bad");
    card.classList.remove("shake");
    void card.offsetWidth;           // 重置动画
    card.classList.add("shake");
  }

  function clearErr() {
    errEl.textContent = "";
    pwInp.classList.remove("bad");
    card.classList.remove("shake");
  }

  async function submit() {
    clearErr();
    const user = userInp.value.trim();
    const pw = pwInp.value;
    if (!user || !pw) { showErr("请输入用户名和密码"); return; }
    if (_mustChangePassword) {
      const np = newPwInp.value;
      if (np.length < 8) { showErr("新密码至少 8 位"); return; }
      if (np !== newPw2Inp.value) { showErr("两次输入的新密码不一致"); return; }
    }
    btn.disabled = true;
    btn.textContent = "处理中…";
    try {
      let data;
      if (_mustChangePassword) {
        const cp = { username: user, oldPassword: pw, newPassword: newPwInp.value };
        if (_loginTfaToken) { cp.otp = otpInp.value.trim(); cp.tfaToken = _loginTfaToken; }
        data = await api("/auth/change-password", "POST", cp);
      } else {
        const lp = { username: user, password: pw };
        if (_loginTfaToken) { lp.otp = otpInp.value.trim(); lp.tfaToken = _loginTfaToken; }
        data = await api("/login", "POST", lp);
      }
      if (data && data.tfa) {
        _loginTfaToken = data.tfaToken;
        otpWrap.classList.remove("hidden");
        otpInp.focus();
        errEl.textContent = "请输入二次验证动态码";
        btn.disabled = false;
        btn.textContent = idleText();
        return;
      }
      if (data && data.mustChangePassword) {
        // 默认密码首次登录：切到「设置新密码」模式（改密时会重新校验旧密码/TFA）
        _mustChangePassword = true;
        _loginTfaToken = null;
        otpWrap.classList.add("hidden");
        otpInp.value = "";
        newPwWrap.classList.remove("hidden");
        newPw2Wrap.classList.remove("hidden");
        pwHint.classList.remove("hidden");
        newPwInp.focus();
        errEl.textContent = "首次登录，请先设置新密码";
        btn.disabled = false;
        btn.textContent = idleText();
        return;
      }
      // 登录或改密成功
      pwInp.value = ""; otpInp.value = ""; newPwInp.value = ""; newPw2Inp.value = "";
      _loginTfaToken = null; _mustChangePassword = false;
      mask.classList.add("hidden");
      runApp({ authenticated: true, enabled: true, user: data && data.user, expiresAt: data && data.expiresAt });
    } catch (e) {
      showErr(e.message || "登录失败");
      _loginTfaToken = null;
      otpWrap.classList.add("hidden");
      otpInp.value = "";
      btn.disabled = false;
      btn.textContent = idleText();
      pwInp.focus();
      pwInp.select();
    }
  }

  btn.addEventListener("click", submit);
  [userInp, pwInp, otpInp, newPwInp, newPw2Inp].forEach((el) => {
    el.addEventListener("input", clearErr);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  });
}

async function init() {
  setupLogin();
  let st = null;
  try { st = await api("/auth/status"); } catch (e) { st = { authenticated: false, enabled: true }; }
  if (st && st.authenticated) { runApp(st); }
  else { hideToast(); $("loginMask").classList.remove("hidden"); }
}

async function runApp(info) {
  $("btnRefresh").addEventListener("click", () => {
    refresh(true);
    restartAutoRefresh();
  });
  renderSkeleton();  // 首次请求前先展示骨架屏
  $("searchBox").addEventListener("input", () => { guestPage = 1; renderGuests(); });
  $("typeFilter").addEventListener("change", () => { guestPage = 1; renderGuests(); });
  $("statusFilter").addEventListener("change", () => { guestPage = 1; renderGuests(); });
  $("modalClose").addEventListener("click", () => $("modal").classList.add("hidden"));
  $("modal").addEventListener("click", (e) => {
    if (e.target === $("modal")) $("modal").classList.add("hidden");
  });
  // 系统设置弹窗：关闭按钮 / 点遮罩关闭 / 分区切换（帮助手册）
  $("settingsClose").addEventListener("click", closeSettings);
  $("settingsModal").addEventListener("click", (e) => {
    if (e.target === $("settingsModal")) closeSettings();
  });
  document.querySelectorAll("#settingsModal .stab").forEach((b) =>
    b.addEventListener("click", () => switchSettingsPane(b.dataset.pane)));

  // 工具箱
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchView(t.dataset.view)));
  $("toolModalClose").addEventListener("click", closeToolModal);
  $("btnRunTool").addEventListener("click", runTool);
  // 应用内确认框：确定 / 取消 / 关闭按钮 / 点遮罩空白处，一律走同一个收口
  $("appConfirmOk").addEventListener("click", () => _appConfirmDone(true));
  $("appConfirmCancel").addEventListener("click", () => _appConfirmDone(false));
  $("appConfirmClose").addEventListener("click", () => _appConfirmDone(false));
  $("appConfirmModal").addEventListener("click", (e) => {
    if (e.target === $("appConfirmModal")) _appConfirmDone(false);
  });
  // 高危确认词输入即时反馈（匹配/不一致/未输入），绑定一次即可
  const confirmInp = $("confirmInput");
  if (confirmInp) confirmInp.addEventListener("input", syncConfirmState);
  const confirmCopy = $("confirmCopy");
  if (confirmCopy) confirmCopy.addEventListener("click", copyConfirmWord);
  // 点依赖提示里的工具名直接跳到前置工具
  $("depHint").addEventListener("click", (e) => {
    const a = e.target.closest(".dep-link");
    if (a) openTool(a.dataset.tool);
  });
  // 点遮罩空白处关闭工具弹窗
  $("toolModal").addEventListener("click", (e) => {
    if (e.target === $("toolModal")) closeToolModal();
  });
  // 执行日志
  $("jobSearchBox").addEventListener("input", renderJobs);
  $("jobStatusFilter").addEventListener("change", renderJobs);
  $("btnReloadJobs").addEventListener("click", loadJobs);
  $("btnClearJobs").addEventListener("click", clearJobs);
  $("jobLogClose").addEventListener("click", closeJobLogModal);
  $("jobLogModal").addEventListener("click", (e) => {
    if (e.target === $("jobLogModal")) closeJobLogModal();
  });
  // ESC 关闭任意弹窗
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // 确认框叠在最上层：先收它，避免 ESC 把底下的工具弹窗一起关掉
    if (!$("appConfirmModal").classList.contains("hidden")) { _appConfirmDone(false); return; }
    if (!$("toolModal").classList.contains("hidden")) closeToolModal();
    else if (!$("jobLogModal").classList.contains("hidden")) closeJobLogModal();
    else if (!$("settingsModal").classList.contains("hidden")) closeSettings();
    $("modal").classList.add("hidden");
  });

  loadEditionBadge();    // 首屏预取版本胶囊（轻量接口，不等 /tools；设置弹窗打开即可见）
  const logoutBtn = $("btnLogout");
  if (logoutBtn) {
    // 仅开启登录门禁（enabled）时显示退出按钮；--no-auth 下无意义
    if (info && info.enabled) {
      logoutBtn.classList.remove("hidden");
      logoutBtn.addEventListener("click", doLogout);
    } else {
      logoutBtn.classList.add("hidden");
    }
  }
  // 会话信息：当前登录用户 + 到期时间（--no-auth 或未取到则隐藏）
  const sessionInfo = $("sessionInfo");
  if (sessionInfo) {
    if (info && info.enabled && info.user) {
      let text = info.user;
      if (info.expiresAt) {
        const t = new Date(info.expiresAt * 1000);
        text += " · 会话至 " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }
      sessionInfo.textContent = text;
      sessionInfo.classList.remove("hidden");
    } else {
      sessionInfo.classList.add("hidden");
    }
  }
  await refresh();
  restartAutoRefresh();  // 每 10 秒自动刷新
}

async function doLogout() {
  // 先停自动刷新，避免退出瞬间还在途的 /api/nodes 401 又弹错误提示
  clearInterval(refreshTimer);
  refreshTimer = null;
  try { await api("/logout", "POST"); } catch (e) {}
  hideToast();
  location.reload();
}

init();
