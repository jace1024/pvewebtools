const $ = (id) => document.getElementById(id);


// ---- 格式化工具 ----

function fmtBytes(n) {
  if (!n) return "0";
  // 二进制换算（1 GiB = 1024 MiB），与 PVE 官方 WebUI 的 GiB 显示口径保持一致
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
 return n.toFixed(n >= 100 ? 0 : 1) + " " + units[i];
}

// 使用率百分比：统一一位小数，概览/节点列表/VM 表格共用，避免各处 toFixed 漂移
function fmtPct(v) {
  return ((+v || 0) * 100).toFixed(1);
}

function fmtUptime(sec) {
  if (!sec) return "-";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}天 ${h}小时`;
  if (h > 0) return `${h}小时 ${m}分`;
  return `${m}分钟`;
}

// ---- 全站阈值：集中一处维护，改这里即全站生效 ----
// 使用率（CPU / 内存 / 硬盘 / 存储池 / 各 VM）：≥70% 黄、≥80% 橙、≥90% 红；低于 70% 视为正常、不上色
const USAGE_THRESHOLDS = { warn: 0.7, high: 0.8, crit: 0.9 };
// 温度阈值（℃）：[警告, 危险]。机械盘容忍更高、NVMe 最敏感；CPU 沿用 60/80
const TEMP_THRESHOLDS = {
  cpu: [60, 80],
  hdd: [50, 60],
  ssd: [60, 75],
  nvme: [55, 70],
  default: [60, 80],
};
// 使用率分档 class：与 style.css 的 .pct.warn/.high/.crit、.bar > i.* 同源
function barClass(ratio) {
  if (ratio >= USAGE_THRESHOLDS.crit) return "crit";
  if (ratio >= USAGE_THRESHOLDS.high) return "high";
  if (ratio >= USAGE_THRESHOLDS.warn) return "warn";
  return "";
}

// 网络吞吐：字节/秒，十进制与 fmtBytes 口径一致
function fmtRate(n) {
  if (!n) return "0 B/s";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
 return n.toFixed(n >= 100 ? 0 : 1) + " " + units[i];
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 把字符串安全地嵌进 inline 事件处理器（onclick="fn('...')"）的 JS 单引号字面量里。
// 同时兼顾 HTML 双引号属性边界（" → &quot;）与 JS 字符串边界（' \ → 转义），
// 使 VM 名 / 节点名 / 快照名等用户可控内容既不会提前终结属性，也不会破坏 JS 字符串（避免 XSS / 语法错）。
function jsStr(s) {
  return "'" + String(s).replace(/[&<>"'\\]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "\\'", "\\": "\\\\" }[c])) + "'";
}

// 状态标签：运行中绿 / 已停止灰
function statusBadge(status) {
  const running = status === "running" || status === "online";
  const cls = running ? "running" : "stopped";
  const text = running ? "运行中" : "已停止";
  return `<span class="status-badge ${cls}"><span class="dot ${status}"></span>${text}</span>`;
}


// ---- Toast ----

let toastTimer = null;

// 登录遮罩可见时不弹业务提示：退出登录瞬间在途请求的 401 不该盖在登录页上
function loginMaskVisible() {
  const m = $("loginMask");
  return !!(m && !m.classList.contains("hidden"));
}

// 立即清除底部提示（退出登录、显示登录页时调用）
function hideToast() {
  clearTimeout(toastTimer);
  const el = $("toast");
  if (!el) return;
  el.classList.add("hidden");
  el.classList.remove("leaving");
}

// 底部提示：带状态图标，错误信息停留更久；退场带淡出动画
function toast(msg, isError = false) {
  if (loginMaskVisible()) return;   // 登录页不显示业务提示
  const el = $("toast");
  el.textContent = (isError ? "✕ " : "✓ ") + msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.add("leaving");   // 触发退场淡出，动画结束后真正隐藏
    setTimeout(() => el.classList.add("hidden"), 300);
  }, isError ? 7000 : 5000);
}


// ---- 应用内确认框（替代浏览器原生 confirm / prompt）----
// 原生框会显示页面地址、样式不可控，与产品风格割裂，故统一走应用内弹窗：
//   await appConfirm("确定…？")                          → 普通确认
//   await appConfirm("…", { requireText: "重启节点" })   → 须原样输入确认词（防误触）
// 返回 true/false。注意是异步的：调用处必须 await。
let _appConfirmResolve = null;

function appConfirm(message, opts = {}) {
  const box = $("appConfirmModal");
  // 兜底：元素缺失（老页面缓存等）时退回原生框，保证流程不中断
  if (!box) return Promise.resolve(window.confirm(message));
  const need = String(opts.requireText || "");
  $("appConfirmTitle").textContent = opts.title || "确认操作";
  $("appConfirmMsg").textContent = message;
  const okBtn = $("appConfirmOk");
  okBtn.textContent = opts.okText || "确定";
  // 破坏性操作用红色主按钮，其余用常规主按钮（与页面按钮语义一致）
  okBtn.classList.toggle("danger", !!opts.danger);
  okBtn.classList.toggle("run", !opts.danger);
  $("appConfirmCancel").textContent = opts.cancelText || "取消";
  const wrap = $("appConfirmInputWrap");
  const inp = $("appConfirmInput");
  const hint = $("appConfirmInputHint");
  wrap.classList.toggle("hidden", !need);
  if (need) {
    inp.value = "";
    inp.placeholder = `请原样输入：${need}`;
    hint.textContent = "";
    hint.className = "app-confirm-hint";
  }
  // requireText 模式：确认词未输对时「确定」保持禁用，并随输入实时反馈（与高危确认词同一套路）
  const syncOk = () => {
    const v = inp.value.trim();
    const hit = normConfirm(v) === normConfirm(need);
    okBtn.disabled = !hit;
    if (!v) { hint.textContent = ""; hint.className = "app-confirm-hint"; }
    else if (hit) { hint.textContent = "已确认"; hint.className = "app-confirm-hint ok"; }
    else { hint.textContent = "确认词不正确"; hint.className = "app-confirm-hint err"; }
  };
  if (need) { inp.oninput = syncOk; syncOk(); } else { okBtn.disabled = false; }
  box.classList.remove("hidden");
  setTimeout(() => (need ? inp : okBtn).focus(), 0);
  return new Promise((resolve) => { _appConfirmResolve = resolve; });
}

// 关闭确认框并把结果回给调用方（确定 true / 取消 false）
function _appConfirmDone(val) {
  const box = $("appConfirmModal");
  if (box) box.classList.add("hidden");
  const resolve = _appConfirmResolve;
  _appConfirmResolve = null;
  if (resolve) resolve(val);
}

// 拉取数据中心全部存储配置并渲染表格
async function tcLoadStorageCfg() {
  const body = $("smTableBody");
  if (!body) return;
  body.innerHTML = `<tr><td colspan="6" class="muted u-center-pad" >加载中…</td></tr>`;
  try {
    const list = await api("/storage-cfg");
    tcRenderStorageTable(list || []);
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="muted u-center-pad" >加载失败: ${escapeHtml(String(e))}</td></tr>`;
  }
}

function tcRenderStorageTable(items) {
  const body = $("smTableBody");
  if (!body) return;
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted u-center-pad" >暂无存储</td></tr>`;
    return;
  }
  body.innerHTML = items.map((s) => {
    const loc = s.path || s.server || s.portal || s.pool || s.vgname || s.datastore || s["fs-name"] || "—";
    const extra = s.export ? ` / ${escapeHtml(s.export)}` : (s.target ? ` / ${escapeHtml(s.target)}` : "");
    const content = s.content
      ? s.content.split(",").filter(Boolean).map((c) => (SM_CONTENT_TYPES.find((x) => x.v === c) || {}).l || c).join("、")
      : "—";
    const status = s.disable ? `<span class="sm-badge off">已禁用</span>` : `<span class="sm-badge on">启用</span>`;
    return `<tr>
      <td><b>${escapeHtml(s.storage)}</b></td>
      <td>${escapeHtml(SM_TYPE_LABEL[s.type] || s.type)}</td>
      <td>${escapeHtml(content)}</td>
      <td><span class="muted">${escapeHtml(loc)}${extra}</span></td>
      <td>${status}</td>
      <td>
        <button type="button" class="btn btn-sm" data-sm-toggle="${escapeHtml(s.storage)}" data-sm-target="${s.disable ? "0" : "1"}">${s.disable ? "启用" : "禁用"}</button>
        <button type="button" class="btn btn-sm" data-sm-edit="${escapeHtml(s.storage)}">编辑</button>
        <button type="button" class="btn btn-sm danger" data-sm-del="${escapeHtml(s.storage)}">移除</button>
      </td>
    </tr>`;
  }).join("");
  body.querySelectorAll("[data-sm-edit]").forEach((b) => b.addEventListener("click", () => tcOpenStorageForm(b.dataset.smEdit)));
  body.querySelectorAll("[data-sm-del]").forEach((b) => b.addEventListener("click", () => tcDeleteStorage(b.dataset.smDel)));
  body.querySelectorAll("[data-sm-toggle]").forEach((b) => b.addEventListener("click", () => tcToggleStorageDisable(b.dataset.smToggle, b.dataset.smTarget)));
}

// 启/禁用存储（原生 pvesh set --disable 0|1，与原生系统行为一致）
async function tcToggleStorageDisable(name, target) {
  if (!name || !target) return;
  const action = target === "1" ? "禁用" : "启用";
  if (!(await appConfirm(`确定${action}存储「${name}」？`))) return;
  try {
    await api(`/storage-cfg/${encodeURIComponent(name)}`, "PUT", { disable: target });
    toast(`已${action}`);
    tcLoadStorageCfg();
    if (typeof tcLoadContentStorages === "function") tcLoadContentStorages();
  } catch (e) {
    toast(`${action}失败: ` + e, true);
  }
}

// 打开新增/编辑存储的表单（复用 #modal）
async function tcOpenStorageForm(existing) {
  $("modalTitle").textContent = existing ? `编辑存储：${existing}` : "新增存储";
  const typeOptions = SM_TYPES.map((x) => `<option value="${x.v}">${escapeHtml(x.l)}</option>`).join("");
  $("modalBody").innerHTML = `<div class="sm-form">
    <div class="tc-row"><label>存储名</label>
      <input type="text" id="smFormName" ${existing ? "readonly" : ""} value="${existing ? escapeHtml(existing) : ""}" placeholder="字母数字下划线短横线">
    </div>
    <div class="tc-row"><label>类型</label>
      <select id="smFormType" ${existing ? "disabled" : ""}>
        <option value="">请选择…</option>
        ${typeOptions}
      </select>
    </div>
    <div id="smFormFields"></div>
    <div class="tc-row tc-actions u-justify-end" >
      <button type="button" class="btn" id="smFormCancel">取消</button>
      <button type="button" class="btn run" id="smFormOk">${existing ? "保存" : "创建"}</button>
    </div>
  </div>`;
  smShowModal();
  const typeSel = $("smFormType");
  const fieldsBox = $("smFormFields");
  const renderFields = (type, cfg) => {
    const def = smTypeOf(type);
    if (!def) { fieldsBox.innerHTML = ""; return; }
    fieldsBox.innerHTML = def.fields.map((f) => smFieldHtml(f, cfg ? cfg[f.n] : undefined, !!existing)).join("");
  };
  let initial = null;
  if (existing) {
    try {
      initial = await api(`/storage-cfg/${encodeURIComponent(existing)}`);
      typeSel.value = initial.type || "";
      renderFields(initial.type, initial);
    } catch (e) { toast("加载配置失败: " + e, true); }
  }
  typeSel.addEventListener("change", () => renderFields(typeSel.value, initial));
  $("smFormCancel").onclick = smHideModal;
  $("smFormOk").onclick = () => tcSubmitStorage(existing);
}

function smFieldHtml(f, val, editing) {
  const v = (val === undefined || val === null) ? (f.def || "") : (typeof val === "boolean" ? (val ? "1" : "0") : String(val));
  // 编辑态下，位置/身份类字段不可变：禁用输入（仅展示当前值）
  const ro = editing && SM_IMMUTABLE_FIELDS.has(f.n);
  const dis = ro ? " disabled" : "";
  if (f.t === "content") {
    const checks = SM_CONTENT_TYPES.map((c) => {
      const sel = v.split(",").includes(c.v) ? "checked" : "";
      return `<label style="margin-right:10px"><input type="checkbox" class="sm-content" value="${c.v}" ${sel}${dis}> ${escapeHtml(c.l)}</label>`;
    }).join("");
    return `<div class="tc-row"><label>${escapeHtml(f.l)}</label><div class="sm-content-box">${checks}</div></div>`;
  }
  if (f.t === "bool") {
    const checked = (v === "1" || v === "true" || v === true) ? "checked" : "";
    return `<div class="tc-row"><label>${escapeHtml(f.l)}</label>` +
      `<label class="sm-bool"><input type="checkbox" data-sm-field="${f.n}" ${checked}${dis} onchange="this.nextElementSibling.textContent=this.checked?'是':'否'"> <span>${checked ? "是" : "否"}</span></label></div>`;
  }
  return `<div class="tc-row"><label>${escapeHtml(f.l)}${f.req ? ' <span class="req">*</span>' : ""}</label>` +
         `<input type="${f.t || "text"}" data-sm-field="${f.n}" value="${escapeHtml(v)}" placeholder="${escapeHtml(f.ph || "")}"${dis}></div>`;
}

async function tcSubmitStorage(existing) {
  const name = $("smFormName").value.trim();
  const type = $("smFormType").value;
  if (!name) return toast("请填写存储名", true);
  if (!existing && !type) return toast("请选择存储类型", true);
  if (!/^[\w\-]+$/.test(name)) return toast("存储名仅允许字母、数字、下划线、短横线", true);
  const def = smTypeOf(existing ? type : type);
  const payload = existing ? {} : { storage: name, type };
  if (def) {
    for (const f of def.fields) {
      // 编辑态下跳过不可变的位置/身份字段，不回传给 pvesh set（会报 Unknown option）
      if (existing && SM_IMMUTABLE_FIELDS.has(f.n)) continue;
      if (f.t === "content") {
        const vals = Array.from(document.querySelectorAll(".sm-content:checked")).map((cb) => cb.value);
        if (vals.length) payload.content = vals.join(",");
        continue;
      }
      if (f.t === "bool") {
        const el = document.querySelector(`[data-sm-field="${f.n}"]`);
        if (!el) continue;
        payload[f.n] = el.checked ? "1" : "0";
        continue;
      }
      const el = document.querySelector(`[data-sm-field="${f.n}"]`);
      if (!el) continue;
      const v = el.value.trim();
      if (!v) {
        if (f.req) return toast(`「${f.l}」为必填项`, true);
        if (existing) continue;
        continue;
      }
      payload[f.n] = v;
    }
  }
  try {
    if (existing) {
      await api(`/storage-cfg/${encodeURIComponent(existing)}`, "PUT", payload);
      toast("已保存");
    } else {
      await api("/storage-cfg", "POST", payload);
      toast("已创建");
    }
    smHideModal();
    tcLoadStorageCfg();
    if (typeof tcLoadContentStorages === "function") tcLoadContentStorages();
  } catch (e) {
    toast("提交失败: " + e, true);
  }
}

async function tcDeleteStorage(name) {
  if (!name) return;
  if (!(await appConfirm(`确定移除存储「${name}」？\n\n此操作仅删除存储配置，不会清除实际数据。\n\n若该存储仍被 VM/备份/ISO 引用，请先解除引用再移除，否则相关功能会失效。`,
                         { danger: true }))) return;
  try {
    await api(`/storage-cfg/${encodeURIComponent(name)}`, "DELETE");
    toast("已移除");
    tcLoadStorageCfg();
    if (typeof tcLoadContentStorages === "function") tcLoadContentStorages();
  } catch (e) {
    toast("移除失败: " + e, true);
  }
}

// 存储管理面板归属的分类 id（工具箱「存储与磁盘」模块）
const SM_CAT_ID = "storage";

// 在工具箱「存储与磁盘」分类下渲染的存储管理模块（追加在磁盘工具卡片之后）
function buildStorageModuleHtml() {
  return `<section class="sm-module">
    <div class="sm-module-head">
      <div class="sm-module-title">存储管理 <span class="risk high">高危</span></div>
      <span class="sm-spacer"></span>
      <button type="button" class="btn" id="smRefresh">刷新</button>
      <button type="button" class="btn" id="smAdd" style="margin-left:6px">新增</button>
      <button type="button" class="btn-icon" id="smToggle" title="折叠/展开" style="margin-left:6px"><svg class="ico ico-chev"><use href="#ico-chevron"></use></svg></button>
    </div>
    <div class="sm-module-body" id="smModuleBody">
      <div class="sm-table-wrap">
        <table class="sm-table" id="smTable">
          <thead>
            <tr>
              <th style="width:130px">存储名</th>
              <th style="width:90px">类型</th>
              <th>内容</th>
              <th>位置 / 标识</th>
              <th style="width:80px">状态</th>
              <th style="width:140px">操作</th>
            </tr>
          </thead>
          <tbody id="smTableBody">
            <tr><td colspan="6" class="muted u-center-pad" >加载中…</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>`;
}

// 绑定存储管理面板按钮（每次分类切换重渲染后重新绑定）
function bindStorageModule() {
  const add = $("smAdd"); if (add) add.addEventListener("click", () => tcOpenStorageForm());
  const ref = $("smRefresh"); if (ref) ref.addEventListener("click", tcLoadStorageCfg);
  const tog = $("smToggle");
  if (tog) tog.addEventListener("click", () => {
    const body = $("smModuleBody");
    if (!body) return;
    const collapsed = body.classList.toggle("collapsed");
    const smIco = tog.querySelector(".ico-chev");
    if (smIco) smIco.classList.toggle("rot-r", collapsed);
  });
  tcLoadStorageCfg();
}

