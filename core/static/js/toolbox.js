// 工具箱：工具清单与分类、工具执行弹窗（参数表单 + 实时日志
// + 分级风险确认 + 执行节点选择）、定时开关机管理、虚机/容器快照管理。
//
// 依赖方向：调用 js/ui.js（$、格式化、转义、Toast、appConfirm、createSearchSelect）与
// js/api.js（api/cachedApi）；读写 js/state.js 的全局状态；由 app.js 的初始化流程与
// index.html 的内联 onclick 调用。classic script 加载，函数仍在全局作用域。

// ==================== 工具箱 ====================

let toolboxData = null;   // /api/tools 结果缓存
let currentCat = null;    // 当前选中分类
let currentTool = null;   // 弹窗中的工具
let pendingDynamicPrefill = [];  // 需在动态下拉填充后回填的 [name, value]（供 fillDynamicOptions 读取来源预填值）

let pollTimer = null;     // 日志轮询定时器
let logOffset = 0;

const RISK_TAG = {
  info: '<span class="risk info">只读</span>',
  low: '<span class="risk low">普通</span>',
  high: '<span class="risk high">高危</span>',
};

// ---- 工具执行弹窗 ----

// 工具生效风险：定义了 action_risk 的工具按当前所选操作取风险
//（如内核配置管理的“查看”是只读 info，升级/清理才是 high），未定义时用工具自身风险
function effectiveRisk(tool) {
  if (tool && tool.action_risk) {
    const actSel = document.querySelector('#toolForm select[data-param="action"]');
    const act = actSel ? actSel.value : "";
    if (tool.action_risk[act]) return tool.action_risk[act];
  }
  return tool ? tool.risk : "low";
}

// 高危确认词归一化：忽略所有空白与大小写差异。
// 工具名常含空格与英文（如「一键安装 Hammer 同步服务器」），要求逐字（含空格/大小写）精确
// 对照纯属折磨且容易静默输错；忽略空白与大小写后仍必须完整输入整名，防误点效果不变。
function normConfirm(s) {
  return String(s || "").replace(/\s+/g, "").toLowerCase();
}

// 确认词输入框的即时反馈：匹配=绿框 + 「已解锁」，不一致=红框 + 说明，未输入=中性提示。
// 让用户边输边知道对不对，不必等提交后由服务端报错（少走一趟往返、少一次困惑）。
function syncConfirmState() {
  const inp = $("confirmInput"), row = $("confirmRow"), hint = $("confirmHint");
  if (!inp || !row || !hint) return;
  const name = currentTool ? currentTool.name : "";
  const risky = !!name && !row.classList.contains("hidden");
  const typed = inp.value.trim();
  const ok = risky && !!typed && normConfirm(typed) === normConfirm(name);
  inp.classList.toggle("input-ok", !!ok);
  inp.classList.toggle("input-error", risky && !!typed && !ok);
  hint.classList.toggle("ok", !!ok);
  hint.classList.toggle("bad", risky && !!typed && !ok);
  // 提示行按需出现：未输入时整行隐藏（标题 + 灰掉的按钮已说明该做什么），少占一行
  const show = risky && (ok || !!typed);
  hint.classList.toggle("hidden", !show);
  hint.textContent = !show ? ""
    : ok ? "✓ 已解锁，可执行"
    : "与上方不一致（空格与大小写可忽略）";
  syncRunBtnGate();
}

// 高危工具：确认词匹配前，弹窗内所有「会真正下发变更」的按钮一律点不动
// （执行 / 重启生效 / 使直通生效）——GitHub / Vercel / AWS 的通用范式，把「点不动」当第一道防线，
// 而不是点了才报错。注：强制结束（中断动作）不参与闸门——任务卡住时必须能中止，锁住反而更危险。
const GATED_ACTION_BTNS = ["btnRunTool", "btnRebootNode", "btnRebootGuest"];
// 额外解锁通道（仅「重启生效」）：该工具本次已成功执行过 → 即使确认词已被清空也解锁。
// 理由是确认词刻意「一次确认只放行一次」（执行结束即清空），而重启恰好是这次执行的收尾——
// 脚本会明确提示「请点弹窗内『重启生效』按钮」，若这时按钮是灰的，用户只能把工具名重输一遍。
// 注意只放宽到「已跑成功」，且点击后仍有独立的手输「重启节点」确认，重启整机的门槛不降。
const GATE_REPEAT_OK_BTNS = ["btnRebootNode"];
// 本次弹窗会话中「已成功执行过」的工具 id（openTool 重置、执行成功后写入）
let lastOkToolId = null;

function syncRunBtnGate() {
  const row = $("confirmRow");
  const risky = !!(currentTool && row && !row.classList.contains("hidden")
    && effectiveRisk(currentTool) === "high");
  const ok = risky && normConfirm($("confirmInput").value) === normConfirm(currentTool.name);
  // 该工具本次已成功跑过：确认词虽已清空，也认作已放行（与 ok 互斥，便于阅读）
  const ranOk = risky && !ok && !!currentTool && lastOkToolId === currentTool.id;
  const runBtn = $("btnRunTool");
  const runBusy = !!runBtn && runBtn.dataset.busy === "1";   // 有任务在跑：一并锁住其它动作按钮
  const lockedTip = runBusy ? "有任务正在执行，请等待结束"
    : `请先完整输入「${currentTool.name}」以解锁`;
  for (const id of GATED_ACTION_BTNS) {
    const b = $(id);
    // 正忙的按钮由它自己的流程掌管禁用态；当前操作不需要（隐藏）的按钮不参与
    if (!b || b.dataset.busy === "1" || b.classList.contains("hidden")) continue;
    const repeatOk = ranOk && GATE_REPEAT_OK_BTNS.includes(id);
    const bLocked = runBusy || (risky && !ok && !repeatOk);
    b.disabled = bLocked;
    b.classList.toggle("confirm-locked", bLocked);
    b.title = bLocked ? lockedTip : "";
  }
}

// 一键复制确认词（GitHub 式）：HTTP 环境（非 localhost）下 clipboard API 不可用，回退 execCommand
async function copyConfirmWord() {
  const text = ($("confirmWord") || {}).textContent || "";
  if (!text) return;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e2) { ok = false; }
  }
  toast(ok ? "名称已复制，粘贴到输入框即可" : "复制失败，请手动输入", !ok);
}

// IP 获取方式（type=ipmode）复合控件：渲染「DHCP / 自定义静态 IP」模式下拉，
// 以及选「静态」才会出现的 ip/gw/dns 输入行（.ipmode-field 默认隐藏，由联动函数控制显隐与禁用）。
// fields 里的从属参数（如 gw/dns）由本控件接管渲染，主渲染循环会跳过它们。
function buildIpmodeHtml(p) {
  const esc = escapeHtml;
  const optDhcp = p.dhcp_label || "DHCP 自动获取（默认，推荐）";
  const optStatic = p.static_label || "自定义静态 IP";
  // 字段说明统一做成标签后的 ⓘ 悬浮提示（与其它控件一致）
  const desc = p.tooltip || p.hint || "";
  const labelHtml = desc
    ? `<label>${esc(p.label)}<span class="tip" data-tip="${esc(desc)}">ⓘ</span></label>`
    : `<label>${esc(p.label)}</label>`;
  const mkField = (name, lbl, placeholder, pattern) =>
    `<div class="form-row ipmode-field hidden"><label>${esc(lbl)}</label>` +
    `<input type="text" data-param="${esc(name)}" placeholder="${esc(placeholder || "")}"` +
 (pattern ? ` pattern="${esc(pattern)}"` : "") + `></div>`;
  const main = mkField(p.name, "IP地址", p.placeholder, p.pattern);
  const childs = (p.fields || []).map((n) => {
    const d = (currentTool.params || []).find((x) => x.name === n) || {};
    return mkField(n, d.label || n, d.placeholder, d.pattern);
  }).join("");
  return `<div class="form-row">${labelHtml}
    <select data-ipmode="${esc(p.name)}">
      <option value="dhcp" selected>${esc(optDhcp)}</option>
      <option value="static">${esc(optStatic)}</option>
    </select></div>${main}${childs}`;
}

// type=autotext 复合控件：把「留空 = 默认/自动」的隐式约定显式化。
// 渲染「自动值（默认选中）/ 常用值快捷项 / 自定义」下拉（data-autoselect），
// 选「自动值」时自定义输入行隐藏并禁用（提交空串，后端仍走原自动/默认分支，脚本零改动）；
// 选常用值快捷项（p.presets，如系统盘 100 GB）时输入行同样隐藏，但把该固定值写进输入框提交；
// 切到「自定义」才出现输入框，此时必须填值（runTool 里做空值拦截）。
// p.secret=True 时自定义输入框用密码框（如登录密码走模板默认密码），禁浏览器自动填充。
function buildAutoTextHtml(p) {
  const esc = escapeHtml;
  const autoLabel = p.auto_label || "使用自动值（默认）";
  const customLabel = p.custom_label || "自定义";
  // 字段说明（tooltip / hint）统一做成标签后的 ⓘ 悬浮提示：不再占用控件上方版面；
  // 说明写进 data-tip，由 CSS 渲染气泡（比原生 title 更快、配色统一、支持多行）
  const desc = p.tooltip || p.hint || "";
  const labelHtml = desc
    ? `<label>${esc(p.label)}<span class="tip" data-tip="${esc(desc)}">ⓘ</span></label>`
    : `<label>${esc(p.label)}</label>`;
  // 模板快速建机：手动填 VMID 后联动预填「系统-编号」名称（自动态不触发，名称由后端按最终分配编号生成）
  const deployIdHook = (currentTool && currentTool.id === "vm_deploy" && p.name === "newid")
    ? ' onchange="suggestDeployName()"' : "";
  const inputType = p.secret ? "password" : "text";
  const autoFill = p.secret ? ' autocomplete="new-password"' : "";
  // 常用值快捷项（p.presets）：如系统盘 100 GB，选它即用该固定值，无需再手填数字
  const presetOpts = (p.presets || []).map((o) =>
    `<option value="${esc(String(o.value))}">${esc(o.label || String(o.value))}</option>`).join("");
  return `<div class="form-row">${labelHtml}
    <select data-autoselect="${esc(p.name)}">
      <option value="auto" selected>${esc(autoLabel)}</option>${presetOpts}
      <option value="custom">${esc(customLabel)}</option>
    </select></div>
    <div class="form-row autotext-field hidden" data-autotext-field="${esc(p.name)}"><label></label>
      <input type="${inputType}" data-param="${esc(p.name)}" placeholder="${esc(p.placeholder || "")}"${autoFill}${deployIdHook} disabled></div>`;
}

// 工具说明（弹窗顶部 #toolDesc）：默认折到 2 行，溢出时露出「展开全部」，可再次收起。
// 说明里的前置/不可逆提示仍常显前 2 行，只有长文才需要展开，兼顾版面与可读性。
function renderToolDesc(text) {
  const el = $("toolDesc"), btn = $("toolDescToggle");
  if (!el || !btn) return;
  el.textContent = text || "";
  el.classList.add("clamped");
  btn.querySelector("span").textContent = "展开全部";
  btn.querySelector("svg").classList.remove("open");
  btn.classList.add("hidden");
  // 折起后仍溢出 → 说明被截断；同步读 scrollHeight 会触发一次布局，此处测得准
  if (el.scrollHeight - el.clientHeight > 1) btn.classList.remove("hidden");
  else el.classList.remove("clamped");
}

function toggleToolDesc() {
  const el = $("toolDesc"), btn = $("toolDescToggle");
  if (!el || !btn) return;
  const folded = el.classList.toggle("clamped");
  btn.querySelector("span").textContent = folded ? "展开全部" : "收起";
  btn.querySelector("svg").classList.toggle("open", !folded);
}

async function openTool(toolId, prefill = {}, preNode = "") {
  // 概览等页面的快捷入口可能在工具箱尚未加载时被点击（toolboxData 只在首次进入工具箱时拉取），
  // 此时先补一次加载再打开弹窗；加载失败（toolboxData 仍为空）则放弃，loadToolbox 内已 toast 提示
  if (!toolboxData) {
    await loadToolbox();
    if (!toolboxData) return;
  }
  currentTool = toolboxData.tools.find((t) => t.id === toolId);
  if (!currentTool) return;
  // 换工具 / 重开弹窗：上次的成功记录不跨工具复用，否则新工具一进来「重启生效」就是亮的
  lastOkToolId = null;
  // 在表单容器上挂工具标识 class（tool-<id>），便于按工具精确收敛样式（如某工具专属的控件高度）
  $("toolForm").className = "tool-form tool-" + currentTool.id;
  // 每次打开工具都先收起「强制结束」：它是上一次执行态的遗留（关弹窗只 stopPolling，
  // 跑完隐藏按钮的回调不会执行），不重置会让其它工具平白多出一个强杀按钮
  $("btnKillJob").classList.add("hidden");

  $("btnRunTool").classList.remove("hidden");

  stopPolling();
  stopRebootCountdown();   // 切换工具时清掉上一轮的重启倒计时

  $("toolModalTitle").innerHTML = `${escapeHtml(currentTool.name)} ${RISK_TAG[currentTool.risk] || ""}`;
  $("toolDesc").textContent = currentTool.desc;

  // 执行节点选择：本机项直接用真实节点名标注（就是跑本服务这台，后端 /api/nodes 已标 local），
  // 不再另造「本机（默认）」——它必然与列表里的同一台机器重复，而且"本机到底是哪台"以前全靠猜。
  // 本机项的值仍留空串：「空=不走 SSH、本地执行」是后端既有语义，保持不动可让各接口行为完全不变。
  // 同时标注在线/离线，离线节点置灰不可选（避免任务提交到不可用节点）。
  //
  // 默认选中「本机」：本机项排在下拉首位，select 默认即取第一项，故无需额外赋值；
  // 其余节点保持后端返回顺序。pvesh 返回顺序不保证本机在前，所以必须显式置顶。
  const localNode = allNodes.find((n) => n.local);
  const orderedNodes = localNode
    ? [localNode, ...allNodes.filter((n) => n !== localNode)]
    : allNodes;
  const nodeOpts = orderedNodes.map((n) => {
    const online = n.status === "online";
    const isLocal = !!n.local;
    // 本机顺带标出它是不是共享存储（pvewebtools）的导出端，省得再去别处核对
    const tags = [isLocal ? "本机" : "",
                  (isLocal && n.storage_role === "server") ? "存储服务端" : ""].filter(Boolean);
    const label = `${n.node}${tags.length ? `（${tags.join(" · ")}）` : ""}${online ? "" : "（离线）"}`;
    return `<option value="${isLocal ? "" : escapeHtml(n.node)}"${online ? "" : " disabled"}>${escapeHtml(label)}</option>`;
  }).join("");
  // 后端没标出本机时（旧版本 / 异常）退回原写法，确保「本机」这一项不会消失
  const nodeInner = allNodes.some((n) => n.local) ? nodeOpts
    : (allNodes.length === 1 ? nodeOpts : `<option value="">本机（默认）</option>${nodeOpts}`);
  const nodeRow = `<div class="form-row"><label>执行节点</label>
    <select id="nodeSelect">${nodeInner}</select></div>`;

  // 按参数定义生成表单控件
  // type=ipmode 会接管 fields 列出的从属参数（如 gw/dns），主循环跳过避免重复渲染
  const ipmodeOwned = new Set();
  (currentTool.params || []).forEach((pp) => {
    if (pp.type === "ipmode") (pp.fields || []).forEach((f) => ipmodeOwned.add(f));
  });
  let formHtml = nodeRow + (currentTool.params || []).map((p) => {
    if (ipmodeOwned.has(p.name)) return "";
    // IP 获取方式下拉 + 静态输入组（DHCP 默认；选「静态」时展开 ip/gw/dns）
    if (p.type === "ipmode") return buildIpmodeHtml(p);
    // 「自动值 / 自定义」下拉复合控件（默认自动值提交空串走默认；选自定义才显示输入框）
    if (p.type === "autotext") return buildAutoTextHtml(p);
    // 隐藏参数：不在表单展示，用 hidden input 承载（快照 action 等由列表按钮触发）
    // 支持 p.type === "hidden"（固定值参数）与 p.hidden（布尔标记），值优先取 p.value，其次 options[0]
    if (p.hidden || p.type === "hidden") {
      const hv = p.value || (p.options && p.options[0] ? p.options[0].value : "");
      return `<input type="hidden" data-param="${escapeHtml(p.name)}" value="${escapeHtml(hv)}">`;
    }
    // 字段说明（tooltip / hint）统一做成标签后的 ⓘ 悬浮提示：不再于控件上方占一行灰字，
    // 需要时鼠标悬停查看（如「留空自动分配空闲编号」「仅…时生效」这类信息）；
    // 文本框的示例文字仍留在框内 placeholder（不占版面，输入后自动隐藏）
    const hintText = p.hint || "";
    const textPlaceholder = p.placeholder || hintText || "";
    const fieldNote = "";   // 保留占位：各分支仍拼接它，但不再渲染控件上方的提示
    const desc = p.tooltip || p.hint || "";
    const labelHtml = desc
      ? `<label>${escapeHtml(p.label)}<span class="tip" data-tip="${escapeHtml(desc)}">ⓘ</span></label>`
      : `<label>${escapeHtml(p.label)}</label>`;
    // 选框组（如批量删除的虚机多选，参数带 multiple）：渲染成可勾选列表，提交时合并为逗号分隔值；
    // 普通单开关（其它工具的布尔复选框，不带 multiple）不在此处理，落到下方 1197 行的单勾选框逻辑
    if (p.type === "checkbox" && p.multiple) {
      return `${fieldNote}<div class="form-row">${labelHtml}
      <div class="checkbox-group" data-param="${escapeHtml(p.name)}" data-options-from="${escapeHtml(p.options_from)}"${p.no_template ? ' data-no-template="1"' : ""}${p.no_passthrough ? ' data-no-passthrough="1"' : ""}>
        <div class="checkbox-loading">加载中…</div></div>
      <div class="checkbox-actions">
        <span class="checkbox-count" style="font-size:12.5px;color:var(--muted);line-height:1.5">可多选</span>
        <button type="button" class="btn btn-sm" onclick="checkboxToggleAll(${jsStr(p.name)}, true)">全选</button>
        <button type="button" class="btn btn-sm" onclick="checkboxToggleAll(${jsStr(p.name)}, false)">清空</button>
      </div></div>`;
    }
    if (p.type === "select") {
      // 动态下拉：选项来自接口（虚机/磁盘列表），先占位后异步填充；no_template 参数透传给填充逻辑
      if (p.options_from) {
        // vBIOS ROM 下拉：附内联上传/删除入口（上传后自动刷新下拉并选中），满足"全部浏览器操作"
        if (p.options_from === "vbios_roms") {
          return `<div class="form-row">${labelHtml}
            <select data-param="${escapeHtml(p.name)}" data-options-from="vbios_roms" data-empty-hint="需要 ROM 请到顶部「传输中心」上传">
              <option value="">加载中…</option></select>
            <div style="margin-top:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <button type="button" class="btn btn-sm danger" onclick="deleteSelectedVbios()">删除选中</button>
              <button type="button" class="btn btn-sm" onclick="syncVbiosToCluster()">同步到集群</button>
            </div></div>`;
        }
        // 磁盘导入：磁盘镜像下拉 + 内联上传入口（上传后自动刷新下拉并选中），同行等高对齐，满足"全部浏览器操作"
        if (p.options_from === "disk_upload") {
          return `<div class="form-row">${labelHtml}
            <select data-param="${escapeHtml(p.name)}" data-options-from="disk_upload" data-empty-hint="需要镜像请到顶部「传输中心」上传" class="u-grow32">
              <option value="">加载中…</option></select></div>`;
        }
        // 云镜像装机：云镜像下拉 + 内联上传/下载入口（与 disk_upload 同范式，下载官方镜像走后台线程）
        // 「下载官方」直接按选中的发行版拉取官网镜像；「其他来源」展开自定义 URL 输入框
        // 用一个纵向容器把「选择/上传已有镜像」与「下载官方镜像」两组操作分组，
        // 避免直接作为 .form-row 的两个 flex:1 子项时被换行挤成一团
        if (p.options_from === "cloud_images") {
          // 云镜像建机的镜像下拉：选镜像后同时刷新「默认配置」组合值（如「默认配置（1 核 / kvm64）」）
          const ciHook = (currentTool && currentTool.id === "cloud_deploy" && p.name === "image")
            ? ' onchange="onCloudImageChange(this);loadDeployDefaults(this)"'
            : ' onchange="onCloudImageChange(this)"';
          return `<div class="form-row">${labelHtml}
            <select data-param="${escapeHtml(p.name)}" data-options-from="cloud_images" class="u-grow32"${ciHook}>
              <option value="">加载中…</option></select></div>`;
        }
        // 模板快速建机：模板来源下拉 + 内联「上传模板」入口（打开统一上传弹窗，预选 backup 类型）
        if (p.options_from === "deploy_sources" && currentTool && currentTool.id === "vm_deploy" && p.name === "template") {
          return `<div class="form-row">${labelHtml}
            <select data-param="${escapeHtml(p.name)}" data-options-from="deploy_sources" onchange="loadDeployDefaults(this)" class="u-grow32">
              <option value="">加载中…</option></select></div>`;
        }
        // 建机类工具：选来源（模板快速建机=模板 / 云镜像建机=云镜像）后刷新「默认配置」的具体值
        const deployHook = (currentTool && (
            (currentTool.id === "vm_deploy" && p.name === "template") ||
            (currentTool.id === "cloud_deploy" && p.name === "image")
          ))
          ? ' onchange="loadDeployDefaults(this)"'
          : "";
        // 多选下拉（如批量删除）：开启 multiple 并加辅助按钮与提示，提交时合并为逗号分隔值
        const multiAttr = p.multiple ? ' multiple size="4"' : "";
        const selectStyle = p.multiple ? ' style="height:auto"' : "";
        const multiBtns = p.multiple
          ? `<div style="display:flex;align-items:center;gap:8px;margin:6px 0 2px 0;justify-content:flex-end">
               <span style="font-size:12.5px;color:var(--muted);line-height:1.5">按住 Ctrl 多选</span>
               <button type="button" class="btn btn-sm" onclick="multiSelectToggle(${jsStr(p.name)}, true)">全选</button>
               <button type="button" class="btn btn-sm" onclick="multiSelectToggle(${jsStr(p.name)}, false)">清空</button>
             </div>`
          : "";
        return `${fieldNote}<div class="form-row">${labelHtml}
          <select data-param="${escapeHtml(p.name)}" data-options-from="${escapeHtml(p.options_from)}"${p.no_template ? ' data-no-template="1"' : ""}${p.no_passthrough ? ' data-no-passthrough="1"' : ""}${p.vm_only ? ' data-vm-only="1"' : ""}${p.empty_hint ? ` data-empty-hint="${escapeHtml(p.empty_hint)}"` : ""}${deployHook}${multiAttr}${selectStyle}>
            ${p.multiple ? "" : '<option value="">加载中…</option>'}</select></div>
          ${multiBtns}`;
      }
      // 隐藏选项（如快照的"删除"由列表按钮触发）：不渲染到下拉，但值可通过 JS 设置
      const opts = p.options.filter((o) => !o.hidden).map((o) =>
        `<option value="${escapeHtml(o.value)}"${p.default === o.value ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("");
      return `${fieldNote}<div class="form-row">${labelHtml}
        <select data-param="${escapeHtml(p.name)}">${opts}</select></div>`;
    }
    // 时间选择器：原生控件直接产出 HH:MM，免去手工填写的格式问题
    if (p.type === "time") {
      return `<div class="form-row">${labelHtml}
        <input type="time" data-param="${escapeHtml(p.name)}" placeholder="${escapeHtml(textPlaceholder)}"></div>`;
    }
    // 密码输入：隐藏回显，参数提交逻辑与普通文本一致
    if (p.type === "password") {
      return `<div class="form-row">${labelHtml}
        <input type="password" data-param="${escapeHtml(p.name)}" placeholder="${escapeHtml(textPlaceholder)}" autocomplete="new-password"></div>`;
    }
    // 选框（非必选布尔）：勾选提交 value，未勾选提交空
    if (p.type === "checkbox") {
      return `${fieldNote}<div class="form-row">${labelHtml}
        <label class="checkbox-wrap"><input type="checkbox" data-param="${escapeHtml(p.name)}" value="${escapeHtml(p.value || "1")}"${p.checked ? " checked" : ""}><span>${escapeHtml(p.placeholder || "启用")}</span></label></div>`;
    }
    // 多行文本框（如 SSH 公钥）：提交时取 value（含换行），其余字段处理与普通文本一致
    if (p.type === "textarea") {
      const rows = p.rows || 3;
      return `<div class="form-row">${labelHtml}
        <textarea data-param="${escapeHtml(p.name)}" rows="${rows}" placeholder="${escapeHtml(textPlaceholder)}" style="font-family:monospace;resize:vertical"></textarea></div>`;
    }
    // 单位后缀（如数据盘的 GB）：常驻显示在输入框右侧，免去用户猜测输入单位
    const unitHtml = p.unit ? `<span class="field-unit">${escapeHtml(p.unit)}</span>` : "";
    // 数字 / 端口：原生 number 控件（提交仍为字符串，由后端校验整数与范围）
    if (p.type === "number" || p.type === "port") {
      const min = p.type === "port" ? 1 : (p.min != null ? p.min : "");
      const max = p.type === "port" ? 65535 : (p.max != null ? p.max : "");
      const step = p.type === "port" ? 1 : (p.step != null ? p.step : 1);
      return `<div class="form-row">${labelHtml}
        <input type="number" data-param="${escapeHtml(p.name)}" min="${min}" max="${max}" step="${step}" placeholder="${escapeHtml(textPlaceholder)}">${unitHtml}</div>`;
    }
    const deployNewIdHook = (currentTool && currentTool.id === "vm_deploy" && p.name === "newid")
      ? ' onchange="suggestDeployName()"' : "";
    return `<div class="form-row">${labelHtml}
      <input type="text" data-param="${escapeHtml(p.name)}" placeholder="${escapeHtml(textPlaceholder)}"${deployNewIdHook}>${unitHtml}</div>`;
  }).join("");
  $("toolForm").innerHTML = formHtml;

  // 磁盘导出：在表单底部常驻「历史导出」面板，浏览/下载/删除已导出的文件（持久视图）
  if (currentTool.id === "disk_export") {
    const panel = document.createElement("div");
    panel.id = "exportHistoryPanel";
    panel.style.cssText = "margin-top:14px;padding-top:12px;border-top:1px dashed var(--border,#ddd)";
    panel.innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-weight:600">历史导出</span>
        <button type="button" class="btn btn-sm" id="exportHistoryRefresh"><svg class="ico"><use href="#ico-refresh"></use></svg>刷新</button>
      </div>
      <div id="exportHistoryList" class="iso-progress-text">（选择「导出到存储」后显示该存储已导出的磁盘镜像）</div>`;
    $("toolForm").appendChild(panel);
    const storageSel = $("toolForm").querySelector('select[data-param="storage"]');
    if (storageSel) storageSel.addEventListener("change", loadExportHistory);
    const refreshBtn = document.getElementById("exportHistoryRefresh");
    if (refreshBtn) refreshBtn.onclick = loadExportHistory;
  }

  // 快捷入口预填：执行节点选中虚机所在节点，参数按 prefill 填值
  if (preNode && [...$("nodeSelect").options].some((o) => o.value === preNode)) {
    $("nodeSelect").value = preNode;
  }
  // 动态下拉（options_from）的选项是异步填充的，prefill 值需等选项就绪后再回填；
  // 静态字段（text/select 固定选项）此刻直接赋值即可
  pendingDynamicPrefill = [];
  for (const [name, val] of Object.entries(prefill)) {
    const el = $("toolForm").querySelector(`[data-param="${name}"]`);
    if (!el) continue;
    if (el.dataset.optionsFrom) pendingDynamicPrefill.push([name, val]);
    else if (el.type === "checkbox") el.checked = (val === el.value);
    else el.value = val;
  }

  // IP 获取方式联动（vm_deploy/cloud_deploy 等含 ipmode 参数的工具）：
  // 默认 DHCP 时静态组隐藏并禁用（禁用字段提交为空串，后端走 DHCP）；
  // 选「自定义静态 IP」时展开并启用 ip/gw/dns 三行
  const ipmodeSel = document.querySelector("#toolForm select[data-ipmode]");
  if (ipmodeSel) {
    const staticRows = [...document.querySelectorAll("#toolForm .ipmode-field")];
    const syncIpmode = () => {
      const isStatic = ipmodeSel.value === "static";
      staticRows.forEach((row) => {
        const inp = row.querySelector("[data-param]");
        row.classList.toggle("hidden", !isStatic);
        if (inp) inp.disabled = isStatic;  // DHCP 时禁用，提交空串，后端走 DHCP
      });
    };
    // 兼容预填：模式默认 DHCP，但 IP 已被预填非空时自动切到「静态」，避免预填值被忽略
    const preIp = document.querySelector(`#toolForm [data-param="${ipmodeSel.dataset.ipmode}"]`);
    if (preIp && preIp.value.trim()) ipmodeSel.value = "static";
    syncIpmode();
    ipmodeSel.addEventListener("change", syncIpmode);
  }

  // autotext 复合控件联动：下拉默认「自动值」时隐藏并禁用自定义输入行
  // （禁用字段提交空串，后端按原默认/自动分支处理，脚本无需改动）；
  // 字段已被预填非空值（如快捷入口带 VMID）时自动切到「自定义」，避免预填值被忽略
  const autotextSels = [...document.querySelectorAll("#toolForm select[data-autoselect]")];
  if (autotextSels.length) {
    const syncAutoText = (sel) => {
      const name = sel.dataset.autoselect;
      const inp = $("toolForm").querySelector(`[data-param="${name}"]`);
      const row = $("toolForm").querySelector(`[data-autotext-field="${name}"]`);
      const auto = sel.value === "auto";
      // 快捷值项（p.presets，如系统盘 100 GB）：不需要手填，值直接由所选固定值给出
      const preset = !auto && sel.value !== "custom";
      // 被 show_if 隐藏时（如按「操作」分显的部署参数）整组保持隐藏且禁用
      const blocked = sel.dataset.showifBlocked === "1";
      if (row) row.classList.toggle("hidden", auto || preset || blocked);
      if (inp) {
        if (auto) inp.value = "";                    // 回自动态时清掉残留输入，避免下次误提交
        else if (preset) inp.value = sel.value;      // 快捷值写进输入框（隐藏但保持启用才能提交）
        inp.disabled = auto || blocked;
      }
    };
    autotextSels.forEach((sel) => {
      const inp = $("toolForm").querySelector(`[data-param="${sel.dataset.autoselect}"]`);
      if (inp && inp.value.trim()) {
        // 预填值与某个快捷项一致 → 落到快捷项；否则切到「自定义」显示输入框
        const pv = inp.value.trim();
        sel.value = [...sel.options].some((o) => o.value === pv) ? pv : "custom";
      }
      syncAutoText(sel);
      sel.addEventListener("change", () => syncAutoText(sel));
    });
  }

  // 条件显隐联动（show_if：仅当指定参数等于某值时才显示该参数行；否则隐藏并禁用/清空，提交为空）
  // show_if.value 支持标量或数组：数组表示「命中其中任一值即显示」（如 reboot_now 在准备/安装两步都可用）
  const showIfParams = (currentTool.params || []).filter((p) => p.show_if && p.show_if.param);
  if (showIfParams.length) {
    const syncShowIf = () => {
      showIfParams.forEach((p) => {
        const ctrl = document.querySelector(`#toolForm [data-param="${p.name}"]`);
        const dep = document.querySelector(`#toolForm [data-param="${p.show_if.param}"]`);
        const want = p.show_if.value;
        const wantList = Array.isArray(want) ? want : [want];
        const shown = !!dep && wantList.includes(dep.value);
        // autotext 复合控件占两行：可见行是带 data-autoselect 的「自动值」行，
        // data-param 输入在默认隐藏的「自定义」行里 —— 只对 ctrl 所在行显隐会漏掉可见行，
        // 故先给其自动值下拉打标记、走既有联动重算，再按 show_if 决定整组显隐
        const autoSel = document.querySelector(`#toolForm select[data-autoselect="${p.name}"]`);
        if (autoSel) {
          autoSel.dataset.showifBlocked = shown ? "" : "1";
          autoSel.dispatchEvent(new Event("change"));
          const autoRow = autoSel.closest(".form-row");
          if (autoRow) autoRow.classList.toggle("hidden", !shown);
          return;
        }
        const row = ctrl ? ctrl.closest(".form-row") : null;
        if (!row) return;
        row.classList.toggle("hidden", !shown);
        if (ctrl) {
          ctrl.disabled = !shown;
          if (!shown) {
            if (ctrl.type === "checkbox") ctrl.checked = false;
            else if (ctrl.type !== "select-one" && ctrl.type !== "select-multiple") ctrl.value = "";
          }
        }
      });
    };
    showIfParams.forEach((p) => {
      const dep = document.querySelector(`#toolForm [data-param="${p.show_if.param}"]`);
      if (dep) dep.addEventListener("change", syncShowIf);
    });
    syncShowIf();
  }

  // 登录用户名（ciuser）随所选模板/镜像自动关联默认账号：
  // 名称含 Windows（win/windows）→ administrator，否则 Linux → root。
  // 仅更新「自动值」选项的提示文本，不改写用户输入；提交空串时后端脚本仍按 SRCNAME 判定，行为与后端保持一致。
  const syncCiUserDefault = () => {
    const ciSel = $("toolForm").querySelector('select[data-autoselect="ciuser"]');
    if (!ciSel) return;
    const srcSel = $("toolForm").querySelector('[data-param="template"]')
               || $("toolForm").querySelector('[data-param="image"]');
    const srcVal = srcSel ? srcSel.value : "";
    const srcLabel = srcSel && srcSel.selectedOptions && srcSel.selectedOptions[0]
                     ? srcSel.selectedOptions[0].textContent : "";
    const isWin = /win|windows/i.test(srcVal + " " + srcLabel);
    const opt = ciSel.querySelector('option[value="auto"]');
    if (opt) opt.textContent = `默认账号 ${isWin ? "administrator" : "root"}`;
  };
  ["template", "image"].forEach((nm) => {
    const srcSel = $("toolForm").querySelector(`[data-param="${nm}"]`);
    if (srcSel) srcSel.addEventListener("change", syncCiUserDefault);
  });
  syncCiUserDefault();

  // 批量创建联动：勾选「批量创建」才启用「创建数量」，未勾选则禁用并清空（提交空串，后端按单台处理）；
  // IP 配置在批量下不再锁定：选「自定义静态 IP」时后端会以末段为基址逐台递增分配（网关/DNS 共用）
  const batchChk = $("toolForm").querySelector('[data-param="batch"]');
  if (batchChk) {
    const cntEl = $("toolForm").querySelector('[data-param="batch_count"]');
    const ipmodeSel = document.querySelector("#toolForm select[data-ipmode]");
    const syncIpmodeLocal = () => {
      if (!ipmodeSel) return;
      const isStatic = ipmodeSel.value === "static";
      document.querySelectorAll("#toolForm .ipmode-field").forEach((row) => {
        const inp = row.querySelector("[data-param]");
        row.classList.toggle("hidden", !isStatic);
        if (inp) inp.disabled = isStatic;
      });
    };
    const syncBatch = () => {
      const on = batchChk.checked;
      if (cntEl) {
        cntEl.disabled = !on;
        if (!on) cntEl.value = "";
      }
      if (ipmodeSel) {
        ipmodeSel.disabled = false;   // 批量支持静态 IP 递增分配，不再强制锁回 DHCP
        syncIpmodeLocal();
      }
    };
    syncBatch();
    batchChk.addEventListener("change", syncBatch);
  }

  // cloud_deploy 联动：数据盘指定大小后才出现「文件系统 / 挂载点」；时区选「自定义…」才出现自定义输入框
  if (currentTool.id === "cloud_deploy") {
    const rowOf = (el) => (el ? el.closest(".form-row") : null);
    const diskAuto = document.querySelector('#toolForm select[data-autoselect="data_disk"]');
    const diskInput = document.querySelector('#toolForm [data-param="data_disk"]');
    const fsEl = document.querySelector('#toolForm [data-param="data_fs"]');
    const mountEl = document.querySelector('#toolForm [data-param="data_mount"]');
    const tzSel = document.querySelector('#toolForm [data-param="timezone"]');
    const tzCustomEl = document.querySelector('#toolForm [data-param="tz_custom"]');
    const setRow = (el, show) => {
      const r = rowOf(el);
      if (!r) return;
      r.classList.toggle("hidden", !show);
      if (!el) return;
      el.disabled = !show;
      if (!show && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) el.value = "";
    };
    const syncDisk = () => {
      const custom = diskAuto && diskAuto.value === "custom";
      const hasSize = diskInput && diskInput.value.trim() !== "";
      const on = !!(custom && hasSize);
      setRow(fsEl, on);
      setRow(mountEl, on);
    };
    if (diskAuto) { syncDisk(); diskAuto.addEventListener("change", syncDisk); }
    if (diskInput) diskInput.addEventListener("input", syncDisk);
    const syncTz = () => setRow(tzCustomEl, !!(tzSel && tzSel.value === "__custom__"));
    if (tzSel) { syncTz(); tzSel.addEventListener("change", syncTz); }
  }

  // 迁移：qm/pct 只能操作本节点实例 —— 选实例后把「执行节点」切到它当前所在的节点（即源节点），
  // 并按所选实例的真实情况提示哪些限制会拦住迁移。脚本里对这些限制还有一份硬校验，
  // 前端这层只是让用户在提交前就看到，不必等任务跑起来才报错。
  if (currentTool.id === "vm_migrate") {
    const panel = document.createElement("div");
    panel.id = "migrateHint";
    panel.className = "iso-progress-text hidden";
    $("toolForm").appendChild(panel);
    const vmidSel = $("toolForm").querySelector('select[data-param="vmid"]');
    // 预填的 vmid 由弹窗末尾的全局回填逻辑异步写进下拉，此刻下拉里还是第一项：
    // 首次同步直接用 prefill 的值取实例，之后（用户改选）才以 select 当前值为准
    const wantVmid = prefill && prefill.vmid ? String(prefill.vmid) : "";
    let migrateInited = false;
    const syncMigrate = () => {
      const vmid = (!migrateInited && wantVmid) ? wantVmid : (vmidSel ? vmidSel.value : "");
      migrateInited = true;
      const nodeSel = $("nodeSelect");
      const vm = vmid ? allGuests.find((x) => String(x.vmid) === vmid) : null;
      if (vm && nodeSel && [...nodeSel.options].some((o) => o.value === vm.node) && nodeSel.value !== vm.node) {
        nodeSel.value = vm.node;
        nodeSel.dispatchEvent(new Event("change"));
      }
      if (!vm) { panel.classList.add("hidden"); panel.innerHTML = ""; return; }
      const line = (m) => `<div class="muted">· ${escapeHtml(m)}</div>`;
      const bits = [];
      if (vm.has_passthrough === 1) {
        const what = vm.passthrough_kind === "vgpu" ? "vGPU（mdev）"
          : (vm.passthrough_kind === "mixed" ? "硬件直通与 vGPU" : "硬件直通");
        bits.push(line(`该实例含${what}：只能选「离线迁移」，在线迁移无法同步内存状态`));
      }
      bits.push(line(`迁移在源节点 ${vm.node} 上执行；迁移后 MAC 地址与配置保持不变`));
      panel.innerHTML = bits.join("");
      panel.classList.remove("hidden");
    };
    if (vmidSel) vmidSel.addEventListener("change", syncMigrate);
    fillDynamicOptions("guests_all").then(syncMigrate);
    // 目标节点下拉的「本机不可选」判定随执行节点变化（源节点就是执行节点），而 nodes 是全局数据源、
    // 不在上面的 node_scoped/belongs_to_node/guest_node 联动列表里，故此处单独联动重填一次
    $("nodeSelect").addEventListener("change", () => fillDynamicOptions("nodes"));
  }

  // 定时开关机弹窗内嵌当前任务列表，随执行节点切换同步刷新
  const isSched = currentTool.id === "vm_schedule";
  $("scheduleSection").classList.toggle("hidden", !isSched);
  if (isSched) {
    $("schedFilterNote").textContent = "（跟随执行节点）";
    $("nodeSelect").addEventListener("change", loadScheduleList);
    loadScheduleList();
    // 定时任务写在执行节点的 cron 里：选虚机后自动把执行节点切到虚机所在节点，
    // 避免任务写到别的节点导致不生效；弹窗打开时按预填/默认选中值先同步一次。
    // 勾选框组多选：仅当所选虚机都在同一节点时才切换；跨节点时保持用户所选（执行时逐台按归属校验拦截）
    const vmidEl = $("toolForm").querySelector('[data-param="vmid"]');
    const syncNodeByGuest = () => {
      const gs = formVmids().map((id) => allGuests.find((x) => String(x.vmid) === id)).filter(Boolean);
      if (!gs.length) return;
      const nodes = [...new Set(gs.map((g) => g.node))];
      if (nodes.length !== 1) return;
      const nodeSel = $("nodeSelect");
      if (nodeSel && [...nodeSel.options].some((o) => o.value === nodes[0]) && nodeSel.value !== nodes[0]) {
        nodeSel.value = nodes[0];
        nodeSel.dispatchEvent(new Event("change"));   // 触发主板探测与任务列表刷新
      }
    };
    if (vmidEl) vmidEl.addEventListener("change", syncNodeByGuest);
    fillDynamicOptions("guests_all").then(syncNodeByGuest);
    // 定时开关机操作固定为「添加/覆盖」：action 为隐藏固定值 add，删除/暂停/启用由任务列表按钮触发
    // 时间冲突内联校验：开机/关机星期相同时，关机时间须晚于开机时间
    const sTime = $("toolForm").querySelector('[data-param="start_time"]');
    const sDays = $("toolForm").querySelector('[data-param="start_days"]');
    const oTime = $("toolForm").querySelector('[data-param="stop_time"]');
    const oDays = $("toolForm").querySelector('[data-param="stop_days"]');
    const validateSchedTime = () => {
      const st = sTime && sTime.value, ot = oTime && oTime.value;
      if (!st || !ot) return true;
      const sd = (sDays && sDays.value) || "*";
      const od = (oDays && oDays.value) || "*";
      // 只有星期相同（同一批日子）时，时间顺序才有意义
      if (sd === od && ot <= st) return false;
 return true;
 };
 const schedHint = document.createElement("div");
 schedHint.className = "sched-conflict hidden";
 schedHint.textContent = "⚠ 关机时间需晚于开机时间（同一天的开关机时间冲突）";
 const formEl = $("toolForm");
 if (sTime) {
 formEl.appendChild(schedHint);
 [sTime, sDays, oTime, oDays].forEach((el) => {
        if (el) el.addEventListener("change", () => {
          schedHint.classList.toggle("hidden", validateSchedTime());
        });
      });
    }
  }

  // 高危操作显示确认词输入框；工具定义了 action_risk 时按所选操作取风险
  // （如内核配置管理的“查看”只读免确认词），切换操作时同步显隐
  const syncConfirmRow = () => {
    $("confirmRow").classList.toggle("hidden", effectiveRisk(currentTool) !== "high");
    syncConfirmState();   // 随操作切换同步刷新输入框的匹配反馈（切到只读操作时清空提示）
  };
  // 先填确认词并清空上次输入，再显隐/复位反馈，避免残留上一工具的状态
  $("confirmWord").textContent = currentTool.name;
  $("confirmInput").value = "";
  $("confirmInput").classList.remove("input-ok");
  syncConfirmRow();
  const riskActSel = $("toolForm").querySelector('select[data-param="action"]');
  if (riskActSel && currentTool.action_risk) riskActSel.addEventListener("change", syncConfirmRow);

  // 依赖提示：建议的前置工具从未成功执行过时显示黄色提示条（不阻塞）
  const done = toolboxData.recommend_status || {};
  const pending = (currentTool.recommend || []).filter((r) => !done[r.tool]);
  const depEl = $("depHint");
  if (pending.length) {
    depEl.innerHTML = pending.map((r) => {
      const pre = toolboxData.tools.find((t) => t.id === r.tool);
      const link = pre
        ? `<a class="dep-link" data-tool="${escapeHtml(r.tool)}">「${escapeHtml(pre.name)}」</a>`
        : "";
      return `⚠ 建议先执行 ${link}：${escapeHtml(r.reason)}`;
    }).join("<br>");
    depEl.classList.remove("hidden");
  } else {
    depEl.innerHTML = "";
    depEl.classList.add("hidden");
  }

  $("jobStatus").textContent = "";
  $("jobStatus").className = "job-status";
  clearLogPanel();
  setLogVisible(false);
  const actSelForReboot = $("toolForm").querySelector('select[data-param="action"]');
  // 宿主机「重启生效」按钮：「开启硬件直通」弹窗常驻（写内核启动参数 + initramfs，必须重启宿主机）
  const syncHostRebootBtn = () => {
    $("btnRebootNode").classList.toggle("hidden", toolId !== "iommu_manage");
    syncRunBtnGate();   // 显隐变化后重新过闸门：刚露出的按钮不能绕过高危锁定
  };
  syncHostRebootBtn();
  // 涉及 VM 直通项增删（hostpci / usb / RDM）的工具都提供「使直通生效」按钮，一键完成关机→开机；
  // 只读检查类与「开启硬件直通」（要重启的是宿主机，另有按钮）不显示。
  // PCIe/USB 直通工具特殊：USB 支持热插拔（运行中即时生效），只有 PCIe 操作才需要开关机
  const guestRebootTools = ["disk_manage", "nic_manage", "gpu_manage", "passthrough_clear"];
  const syncGuestRebootBtn = () => {
    const need = guestRebootTools.includes(toolId)
      || (toolId === "pcie_manage" && (!actSelForReboot || /^pcie/.test(actSelForReboot.value)));
    $("btnRebootGuest").classList.toggle("hidden", !need);
    syncRunBtnGate();   // 显隐变化后重新过闸门：刚露出的按钮不能绕过高危锁定
  };
  syncGuestRebootBtn();
  // 切换「操作」后同步两个生效按钮：PCIe 直通 ↔ 块映射
  if (actSelForReboot && toolId === "pcie_manage") {
    actSelForReboot.addEventListener("change", () => { syncGuestRebootBtn(); syncHostRebootBtn(); });
  }

  setRunBtnLoading(false);
  syncRunBtnGate();   // 高危工具打开即锁定「执行」，直到确认词输入正确才解锁
  $("toolModal").classList.remove("hidden");
  renderToolDesc(currentTool.desc);   // 弹窗可见后再测量：说明超过 2 行才露出「展开全部」
  // 异步填充动态下拉（虚机/设备/模板），完成后回填动态下拉的 prefill 值；
  // 建机类工具的「默认配置」值已在各来源下拉填充完成时（fillDynamicOptions 内）即时刷新，无需在此等待
  fillDynamicOptions().then(() => {
    for (const [name, val] of pendingDynamicPrefill) {
      const el = $("toolForm").querySelector(`[data-param="${name}"]`);
      if (el && el.classList.contains("checkbox-group")) {
        // 勾选框组：按值勾选对应项，并派发 change 让依赖所选虚机的联动（快照列表/节点同步等）跟上
        const inp = [...el.querySelectorAll('input[type="checkbox"]')]
          .find((c) => c.value === String(val));
        if (inp && !inp.disabled) {
          inp.checked = true;
          el.dispatchEvent(new Event("change", { bubbles: true }));
        }
      } else if (el && el.options && [...el.options].some((o) => o.value === val)) el.value = val;
    }
    // 磁盘导出：动态下拉（含存储）填充完成后，刷新一次历史导出目录
    if (currentTool && currentTool.id === "disk_export") loadExportHistory();
  });

  // 执行节点切换后需要联动重拉的下拉：由服务端注入的「节点相关性」标记推导
  // （node_scoped=值来自执行节点 / belongs_to_node=虚机按执行节点过滤 / guest_node=跟随所选虚机）。
  // 不再维护数据源白名单 —— 新增工具只要数据源登记在 toolbox 的分类表里就自动生效。
  const nodeSel = $("nodeSelect");
  if (nodeSel) {
    const refreshSources = [...new Set(((currentTool && currentTool.params) || [])
      .filter((p) => p.node_scoped || p.belongs_to_node || p.guest_node)
      .map((p) => p.options_from)
      .filter(Boolean))];
    nodeSel.addEventListener("change", () => {
      for (const s of refreshSources) {
        if (document.querySelector(`#toolForm [data-options-from="${s}"]`)) {
          fillDynamicOptions(s);
        }
      }
    });
  }

  // 直通方式切换时：设备下拉数据源随之切换（块映射↔磁盘列表，PCIe↔存储设备列表）；
  // 总线类型仅在「块设备映射 + 添加」时有效，其余组合（PCIe 直通、移除操作）置灰禁用避免误选
  const modeSel = $("toolForm").querySelector('select[data-param="mode"]');
  if (modeSel) {
    const busSel = $("toolForm").querySelector('select[data-param="bus"]');
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    const syncBusState = () => {
      if (!busSel) return;
      busSel.disabled = !(modeSel.value === "block" && (!actSel || actSel.value === "add"));
    };
    syncBusState();   // 初始态跟随默认（块映射 + 添加 = 可选）
    modeSel.addEventListener("change", () => {
      syncBusState();
      const devSel = $("toolForm").querySelector('select[data-options-from="device"]');
      if (devSel) {
        devSel.closest(".form-row").querySelector("label").textContent =
          modeSel.value === "pcie" ? "PCIe 设备" : "磁盘（by-id）";
      }
      fillDynamicOptions("device");
    });
    // 移除/添加切换、切换目标 VM 时，设备下拉也要刷新：
    // 移除模式下只列「该 VM 已直通的设备」，避免用户从全部设备里选错
    const vmidSel = $("toolForm").querySelector('select[data-param="vmid"]');
    if (actSel) actSel.addEventListener("change", () => { syncBusState(); fillDynamicOptions("device"); });
    if (vmidSel) vmidSel.addEventListener("change", () => fillDynamicOptions("device"));
  }

  // CPU 调频：满载锁频仅对「性能优先」模式生效，其余模式置灰禁用避免误勾选
  if (toolId === "cpu_freq") {
    const govSel = $("toolForm").querySelector('select[data-param="governor"]');
    const lockChk = $("toolForm").querySelector('input[data-param="lock_freq"]');
    if (govSel && lockChk) {
      const syncLockState = () => {
        const usable = govSel.value === "performance";
        lockChk.disabled = !usable;
        const row = lockChk.closest(".form-row");
        if (row) row.classList.toggle("disabled", !usable);
      };
      syncLockState();   // 初始态跟随默认模式（性能优先=可选）
      govSel.addEventListener("change", syncLockState);
    }
  }

  // 磁盘新增与移除：操作切换时显隐对应参数；切换 VM 时刷新磁盘下拉
  if (toolId === "vm_disk") {
    const vmDiskForm = $("toolForm");
    const actSel2 = vmDiskForm.querySelector('select[data-param="action"]');
    const vmidSel2 = vmDiskForm.querySelector('select[data-param="vmid"]');
    const syncVmDisk = () => {
      const isAdd = !actSel2 || actSel2.value === "add";
      for (const n of ["bus", "storage", "size"]) {
        const el = vmDiskForm.querySelector(`[data-param="${n}"]`);
        if (el) el.closest(".form-row").classList.toggle("hidden", !isAdd);
      }
      for (const n of ["disk", "keep_disk"]) {
        const el = vmDiskForm.querySelector(`[data-param="${n}"]`);
        if (el) el.closest(".form-row").classList.toggle("hidden", isAdd);
      }
    };
    syncVmDisk();
    if (actSel2) actSel2.addEventListener("change", () => {
      syncVmDisk();
      if (actSel2.value !== "add") fillDynamicOptions("vm_disks");
    });
    if (vmidSel2) vmidSel2.addEventListener("change", () => fillDynamicOptions("vm_disks"));
  }

  // 快照管理：内嵌快照列表（删除按钮），操作固定为「创建快照」不可选，切换虚机后刷新列表
  const isSnapshot = currentTool.id === "vm_snapshot";
  $("snapshotSection").classList.toggle("hidden", !isSnapshot);
  if (isSnapshot) {
    // vmid 可能是勾选框组（div）：[data-param] 通配两种控件，change 事件都会冒泡到容器
    const vmidEl = $("toolForm").querySelector('[data-param="vmid"]');
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    // 操作固定为「创建快照」：禁用下拉，删除操作由快照列表按钮触发
    if (actSel) {
      actSel.disabled = true;
      actSel.value = "take";
    }
    currentSnapshotAction = "take";
    // 勾选/切换虚机后刷新快照列表
    if (vmidEl) vmidEl.addEventListener("change", () => loadSnapshotList());
    loadSnapshotList();
  }

  // 勾选框组多选虚机：勾选变化后联动刷新依赖所选虚机的动态下拉（如磁盘导出的磁盘列表）
  const vmidBox = $("toolForm").querySelector('.checkbox-group[data-param="vmid"]');
  if (vmidBox) {
    vmidBox.addEventListener("change", () => {
      for (const s of ["vm_disks"]) {
        if (document.querySelector(`#toolForm [data-options-from="${s}"]`)) fillDynamicOptions(s);
      }
    });
  }

  // GRUB 配置管理操作联动：启动参数仅增/删时用，备注仅备份时用，备份文件名仅恢复时用，其余置灰避免误填
  if (toolId === "grub_mgr") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const paramIn = $("toolForm").querySelector('input[data-param="param"]');
      const noteIn = $("toolForm").querySelector('input[data-param="note"]');
      const bakIn = $("toolForm").querySelector('select[data-param="backup_name"]');
      const syncGrubAction = () => {
        const act = actSel.value;
        if (paramIn) paramIn.disabled = act !== "add" && act !== "remove";
        if (noteIn) noteIn.disabled = act !== "backup";
        if (bakIn) bakIn.disabled = act !== "restore";
      };
      syncGrubAction();   // 初始态跟随默认操作（查看当前配置）
      actSel.addEventListener("change", syncGrubAction);
    }
    // 切换执行节点后重拉该节点的 GRUB 备份列表（恢复操作的目标节点）
    const nodeSel = $("nodeSelect");
    if (nodeSel) nodeSel.addEventListener("change", () => fillDynamicOptions("grub_backups"));
  }

  // 接口配置管理操作联动：备注仅备份时用，备份文件仅恢复时用，其余置灰避免误填
  if (toolId === "iface_manage") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const noteIn = $("toolForm").querySelector('input[data-param="note"]');
      const bakIn = $("toolForm").querySelector('select[data-param="backup_name"]');
      const syncIfaceAction = () => {
        const act = actSel.value;
        if (noteIn) noteIn.disabled = act !== "backup";
        if (bakIn) bakIn.disabled = act !== "restore";
      };
      syncIfaceAction();   // 初始态跟随默认操作（查看当前配置）
      actSel.addEventListener("change", syncIfaceAction);
    }
    // 切换执行节点后重拉该节点的网络配置备份列表
    const nodeSel = $("nodeSelect");
    if (nodeSel) nodeSel.addEventListener("change", () => fillDynamicOptions("net_backups"));
  }

  // 连通性测试操作联动：目标地址仅 Ping 时用，IPv6 就绪度检查置灰
  if (toolId === "net_ping") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    const targetIn = $("toolForm").querySelector('input[data-param="target"]');
    if (actSel && targetIn) {
      const syncPingAction = () => { targetIn.disabled = actSel.value !== "ping"; };
      syncPingAction();   // 初始态跟随默认操作（Ping）
      actSel.addEventListener("change", syncPingAction);
    }
  }

  // 调整虚机内核参数：「查看」是只读操作，快捷项/自定义/重启勾选一并置灰（禁用字段提交空值）；
  // 另按所选虚机的电源状态让两个勾选互斥——本来运行中则「关机时先自动开机」用不到，
  // 本来已关机则「改完重启虚机」不生效（脚本会强制关回原状），置灰避免误解。
  // 状态取值：列表（页面每 10 秒轮询，可能滞后）先立即置灰，再实时查一次校正，消除"点了才变"的延迟感
  // 释放 vfio 占用：「列出候选设备」是只读模式 → device 勾选与 driver_override 选项一并置灰；
  // 勾选列表的禁用态随选项下发，故切换操作时重新拉一次列表
  if (toolId === "vfio_release") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    const ovChk = $("toolForm").querySelector('input[data-param="clear_override"]');
    const syncVfioReleaseState = () => {
      const viewing = actSel ? actSel.value === "list" : false;
      if (ovChk) ovChk.disabled = viewing;
      fillDynamicOptions("vfio_devices");
    };
    syncVfioReleaseState();
    if (actSel) actSel.addEventListener("change", syncVfioReleaseState);
  }

  if (toolId === "guest_kargs") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    const vmidEl = $("toolForm").querySelector('[data-param="vmid"]');
    const presetSel = $("toolForm").querySelector('select[data-param="preset"]');
    const kargsIn = $("toolForm").querySelector('input[data-param="kargs"]');
    const rebootChk = $("toolForm").querySelector('input[data-param="reboot"]');
    const autoChk = $("toolForm").querySelector('input[data-param="autostart"]');
    let liveVMID = "";        // 已按实时状态校正过的虚机
    let liveStatus = null;    // 其实时状态（null = 未知）
    const syncGuestKargsState = () => {
      const viewing = actSel ? actSel.value === "list" : false;
      // 多选时以第一台作为电源态参考（单选行为不变）
      const cur = formVmids()[0] || "";
      const g = cur ? allGuests.find((x) => String(x.vmid) === cur) : null;
      // 有该虚机的实时状态就用它，否则退回列表状态；都没有（未选/下拉未就绪）则不置灰
      const st = (g && liveVMID === cur && liveStatus) ? liveStatus : (g ? g.status : null);
      if (presetSel) presetSel.disabled = viewing;
      if (kargsIn) kargsIn.disabled = viewing;
      if (rebootChk) rebootChk.disabled = viewing || (st ? st !== "running" : false);
      if (autoChk) autoChk.disabled = st ? st === "running" : false;
    };
    const refreshLiveStatus = () => {
      const cur = formVmids()[0] || "";
      const g = cur ? allGuests.find((x) => String(x.vmid) === cur) : null;
      if (!g) { liveVMID = ""; liveStatus = null; syncGuestKargsState(); return; }
      liveVMID = cur;                 // 先用列表里的状态（避免"先是都可点、随后才灰"的闪动）
      liveStatus = g.status || null;
      syncGuestKargsState();
      // 再实时查一次校正（弹窗打开期间状态可能刚变过）；期间切了虚机就丢弃过期结果
      api(`/guests/${g.node}/${g.type}/${g.vmid}/status`).then((st) => {
        if (!st || !st.status || (formVmids()[0] || "") !== cur) return;
        liveVMID = cur;
        liveStatus = st.status;
        syncGuestKargsState();
      }).catch(() => {});
    };
    refreshLiveStatus();   // 初始态跟随默认操作（查看）与所选虚机
    if (actSel) actSel.addEventListener("change", syncGuestKargsState);
    if (vmidEl) {
      vmidEl.addEventListener("change", refreshLiveStatus);
      // 虚机列表是异步填充的：选项就绪后再同步一次（不再拉接口，避免打乱已选值与预填值）
      const optWatch = new MutationObserver(() => {
        refreshLiveStatus();
        const ready = vmidEl.tagName === "SELECT"
          ? vmidEl.options.length > 1
          : vmidEl.querySelectorAll('input[type="checkbox"]').length > 1;
        if (ready) optWatch.disconnect();
      });
      optWatch.observe(vmidEl, { childList: true });
    }
  }

  // 网桥与 VLAN 管理操作联动：按操作启用对应字段，无关字段置灰避免误填
  if (toolId === "bridge_manage") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const f = {};
      for (const p of ["bridge", "port", "cidr", "tag", "vlan", "vcidr"]) {
        f[p] = $("toolForm").querySelector(`[data-param="${p}"]`);
      }
      const syncBridgeAction = () => {
        const act = actSel.value;
        const on = {   // 每个操作需要的字段
          add_bridge: ["port", "cidr"],
          del_bridge: ["bridge"],
          add_vlan: ["bridge", "tag", "vcidr"],
          del_vlan: ["vlan"],
        }[act] || [];
        for (const p of Object.keys(f)) {
          if (f[p]) f[p].disabled = !on.includes(p);
        }
      };
      syncBridgeAction();   // 初始态跟随默认操作（创建网桥）
      actSel.addEventListener("change", syncBridgeAction);
    }
  }

  // 内核配置管理操作联动：安装版本仅安装时用，默认启动版本仅设默认时用，其余置灰避免误填
  if (toolId === "kernel_mgr") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const verIn = $("toolForm").querySelector('select[data-param="version"]');
      const bootIn = $("toolForm").querySelector('select[data-param="boot_ver"]');
      const syncKernelAction = () => {
        const act = actSel.value;
        if (verIn) verIn.disabled = act !== "install";
        if (bootIn) bootIn.disabled = act !== "default";
      };
      syncKernelAction();   // 初始态跟随默认操作（显示当前内核，两框都置灰）
      actSel.addEventListener("change", syncKernelAction);
    }
    // 切换执行节点后重拉该节点的两个内核数据源（已装=设默认启动，可装=安装新内核）
    const nodeSel = $("nodeSelect");
    if (nodeSel) nodeSel.addEventListener("change", () => {
      fillDynamicOptions("kernels");
      fillDynamicOptions("kernels_available");
    });
  }

  // 磁盘分区与格式化操作联动：仅格式化操作需要分区数与文件系统
  if (toolId === "disk_prep") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const fsSel = $("toolForm").querySelector('select[data-param="fstype"]');
      const partsSel = $("toolForm").querySelector('select[data-param="parts"]');
      const modeSel = $("toolForm").querySelector('select[data-param="wipe_mode"]');
      const syncPrepAction = () => {
        const wiping = actSel.value === "wipe";
        if (fsSel) fsSel.disabled = wiping;
        if (partsSel) partsSel.disabled = wiping;
        if (modeSel) modeSel.disabled = !wiping;   // 擦除方式仅「清除旧盘」时可选
      };
      syncPrepAction();   // 初始态跟随默认操作
      actSel.addEventListener("change", syncPrepAction);
    }
  }

  // 时间同步操作联动：NTP 服务器仅同步时用，时区仅修改时区时用，其余置灰避免误填
  if (toolId === "time_sync") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const ntpSel = $("toolForm").querySelector('select[data-param="ntp_server"]');
      const tzSel = $("toolForm").querySelector('select[data-param="tz"]');
      const syncTimeAction = () => {
        const act = actSel.value;
        if (ntpSel) ntpSel.disabled = act !== "sync";
        if (tzSel) tzSel.disabled = act !== "tz";
      };
      syncTimeAction();   // 初始态跟随默认操作（查看状态，两框都置灰）
      actSel.addEventListener("change", syncTimeAction);
    }
  }


  // 虚机备份与还原操作联动
  // 备份→VM+备份存储+模式+压缩；还原→备份文件+还原存储
  if (toolId === "vm_backup") {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const backupFileSel = $("toolForm").querySelector('select[data-options-from="backups"]');
      const storeSel = $("toolForm").querySelector('select[data-options-from="backup_storages"]');
      const modeSel = $("toolForm").querySelector('select[data-param="mode"]');
      const compressSel = $("toolForm").querySelector('select[data-param="compress"]');
      const restoreStoreSel = $("toolForm").querySelector('select[data-options-from="disk_storages"]');
      const backupNameIn = $("toolForm").querySelector('input[data-param="backup_name"]');
      const syncBackupAction = () => {
        const isBackup = actSel.value === "backup";
        // vmid 为勾选框组多选：备份=源虚机、还原=目标虚机，两种操作都需要可选，不再随操作置灰
        if (storeSel) storeSel.disabled = !isBackup;
        if (modeSel) modeSel.disabled = !isBackup;
        if (compressSel) compressSel.disabled = !isBackup;
        if (backupNameIn) backupNameIn.disabled = !isBackup;
        // 还原：备份文件 + 还原存储
        if (backupFileSel) backupFileSel.disabled = isBackup;
        if (restoreStoreSel) restoreStoreSel.disabled = isBackup;
        // 切到还原时重新拉取备份文件列表（可能刚上传了新的）
        if (!isBackup) fillDynamicOptions("backups");
      };
      syncBackupAction();
      actSel.addEventListener("change", syncBackupAction);
    }
  }

  // 显卡类型切换时：x-vga/vBIOS ROM 仅独显有意义，核显模式置灰；设备下拉按类型重新过滤；
  // vBIOS ROM 还仅添加时可填，移除时置灰
  const gtypeSel = $("toolForm").querySelector('select[data-param="gtype"]');
  if (gtypeSel) {
    const xvgaSel = $("toolForm").querySelector('select[data-param="xvga"]');
    const romIn = $("toolForm").querySelector('select[data-param="romfile"]');
    const actSel2 = $("toolForm").querySelector('select[data-param="action"]');
    const igUseSel = $("toolForm").querySelector('select[data-param="igpu_use"]');
    const syncXvgaState = () => { if (xvgaSel) xvgaSel.disabled = gtypeSel.value === "igpu"; };
    // 「核显用途」仅核显 + 添加时有效（置灰不提交值，参数已声明非必填）
    const syncIgUseState = () => {
      if (!igUseSel) return;
      const adding = actSel2 ? actSel2.value === "add" : true;
      igUseSel.disabled = gtypeSel.value !== "igpu" || !adding;
    };
    const syncRomState = () => {
      if (!romIn) return;
      const adding = actSel2 ? actSel2.value === "add" : true;
      romIn.disabled = gtypeSel.value === "igpu" || !adding;
    };
    // 「为虚机适配内核参数」仅添加时有意义（移除时不涉及虚机引导）
    const gFixChk = $("toolForm").querySelector('input[data-param="guest_fix"]');
    const gFixPre = $("toolForm").querySelector('select[data-param="guest_preset"]');
    const gFixIn = $("toolForm").querySelector('input[data-param="guest_kargs"]');
    const syncGuestFixState = () => {
      const adding = actSel2 ? actSel2.value === "add" : true;
      // 未勾选「为虚机适配内核参数」时，快捷项与自定义项都无需填写
      const on = adding && (!gFixChk || gFixChk.checked);
      if (gFixChk) gFixChk.disabled = !adding;
      if (gFixPre) gFixPre.disabled = !on;
      if (gFixIn) gFixIn.disabled = !on;
    };
    if (gFixChk) gFixChk.addEventListener("change", syncGuestFixState);
    syncXvgaState();   // 初始态跟随默认类型（独显=可选）
    syncIgUseState();
    syncRomState();
    syncGuestFixState();
    gtypeSel.addEventListener("change", () => {
      syncXvgaState();
      syncIgUseState();
      syncRomState();
      // 显卡下拉的选项源是 gpu（按 kind 过滤独显/核显）；此处曾误写为 device（磁盘直通的磁盘列表），
      // 导致切换到「核显」后列表不刷新，看起来像"没有核显"
      fillDynamicOptions("gpu");
    });
    if (actSel2) actSel2.addEventListener("change", () => { syncIgUseState(); syncRomState(); syncGuestFixState(); });
  }

  // USB 3.0 选项仅「添加 USB 直通」时可用，其余操作置灰
  const usb3Sel = $("toolForm").querySelector('select[data-param="usb3"]');
  if (usb3Sel) {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const syncUsb3 = () => { usb3Sel.disabled = actSel.value !== "usb_add"; };
      syncUsb3();   // 初始态跟随默认操作
      actSel.addEventListener("change", syncUsb3);
    }
  }

  // 内存气球：只有选「自定义最小内存」时才需要填 MB（两个建机工具共用同一组参数）
  const balloonSel = $("toolForm").querySelector('select[data-param="balloon"]');
  const balloonIn = $("toolForm").querySelector('input[data-param="balloon_min"]');
  if (balloonSel && balloonIn) {
    const syncBalloon = () => { balloonIn.disabled = balloonSel.value !== "custom"; };
    syncBalloon();   // 初始态跟随默认（不启用）
    balloonSel.addEventListener("change", syncBalloon);
  }

  // 屏蔽独显/核显驱动仅「开启硬件直通」时生效，其余操作置灰，避免用户误以为设置有效
  const blDgpuSel = $("toolForm").querySelector('select[data-param="blacklist_dgpu"]');
  const blIgpuSel = $("toolForm").querySelector('select[data-param="blacklist_igpu"]');
  if (blDgpuSel || blIgpuSel) {
    const actSel = $("toolForm").querySelector('select[data-param="action"]');
    if (actSel) {
      const syncBlacklist = () => {
        const off = actSel.value !== "enable";
        if (blDgpuSel) blDgpuSel.disabled = off;
        if (blIgpuSel) blIgpuSel.disabled = off;
      };
      syncBlacklist();   // 初始态跟随默认操作
      actSel.addEventListener("change", syncBlacklist);
    }
  }

  // 「一键安装Docker」：版本下拉为全量稳定版，不随虚机变化，无需联动刷新

  // PCIe/USB 直通管理操作联动：PCIe 设备下拉仅 pcie_* 操作可选，USB 设备下拉仅 usb_* 操作可选
  const ptActSel = $("toolForm").querySelector('select[data-param="action"]');
  const pciDevSel = $("toolForm").querySelector('select[data-options-from="pci_generic"]');
  const usbDevSel = $("toolForm").querySelector('select[data-options-from="usb"]');
  if (ptActSel && pciDevSel && usbDevSel) {
    const syncPtFields = () => {
      const isPcie = ptActSel.value.startsWith("pcie_");
      pciDevSel.disabled = !isPcie;
      usbDevSel.disabled = isPcie;
    };
    syncPtFields();   // 初始态跟随默认操作（PCIe 添加）
    ptActSel.addEventListener("change", syncPtFields);
  }

  // 带有 pattern 的 text 输入框：即时校验并显示提示
  document.querySelectorAll("#toolForm input[data-param][pattern]").forEach((el) => {
    const paramDef = (currentTool.params || []).find((p) => p.name === el.dataset.param);
    if (!paramDef || !paramDef.pattern) return;
    const re = new RegExp("^(?:" + paramDef.pattern + ")$");
    const hint = document.createElement("div");
    hint.className = "field-hint hidden";
    el.parentNode.appendChild(hint);
    const check = () => {
      const v = el.value.trim();
      if (!v && !paramDef.required) {
        // 非必填且为空：不校验（留空使用默认值）
        hint.classList.add("hidden");
        el.classList.remove("input-error");
        return;
      }
      if (!re.test(v)) {
        el.classList.add("input-error");
        hint.textContent = paramDef.errmsg || paramDef.hint || `格式不正确`;
        hint.classList.remove("hidden");
      } else {
        el.classList.remove("input-error");
        hint.classList.add("hidden");
      }
    };
    el.addEventListener("input", check);
    el.addEventListener("blur", check);
    check();   // 初始态校验
  });
}

// 动态下拉填充：guests=虚机列表（磁盘直通只对虚机生效，容器不支持 qm 挂载）；
// device=按「直通方式」选择数据源：块映射用可直通磁盘，PCIe 用存储类 PCIe 设备
// 批量选项：把工具弹窗的多个只读下拉请求合并成 1 个 GET /api/options（首屏/开弹窗请求数从 N 降为 1）。
// OptionBatch 收集本批需要的上游 path、一次性取回；整批失败则回退逐条 api() 调用，容错不变。
class OptionBatch {
  constructor() {
    this.paths = [];                 // 去重后的上游 path 列表（保序）
    this._seen = new Set();
    this.map = Object.create(null);  // path -> data | Error
    this.fallback = false;
  }
  need(path) {
    if (!this._seen.has(path)) { this._seen.add(path); this.paths.push(path); }
  }
  // dry=true：仅登记 path，返回占位（分支用空数组安全执行，不真正请求）
  // dry=false：返回已取回数据；子请求曾失败则抛出，交由分支 catch 降级为空选项
  get(path, dry) {
    this.need(path);
    if (dry) return [];
    if (this.fallback) return api(path);   // 整批失败：逐条真实请求兜底，行为等同旧版
    if (!(path in this.map)) return api(path);   // 安全网：预扫描漏登记的 path 逐条兜底，避免取空
    const v = this.map[path];
    if (v instanceof Error) throw v;
    return v;
  }
  async fetch() {
    if (!this.paths.length) return;
    const q = this.paths.map((p) => "path=" + encodeURIComponent(p)).join("&");
    try {
      const res = await cachedApi("/options?" + q);   // 复用前端短缓存（api() 会再补 /api 前缀）
      this.paths.forEach((p, i) => {
        const r = res[i] || {};
        this.map[p] = r.ok ? r.data : new Error(r.error || "选项拉取失败");
      });
    } catch (e) {
      this.fallback = true;   // 整批异常：渲染阶段逐条 api() 兜底，保证原有容错
    }
  }
}

// 执行节点的「有效节点名」：本机项的 value 是空串，需解析成本机真实节点名后才能用于归属过滤 ——
// 否则"留空即不过滤"，集群里其它节点的虚机也会列出来，选中必然失败（qm/pct 只操作本节点）。
// 取不到本机节点名时返回空串：宁可多列几项，也不要因为解析失败给出一个空列表。
function effectiveExecNode() {
  const el = $("nodeSelect");
  if (!el) return "";
  if (el.value) return el.value;
  const local = localNodeName();
  return local === "本机" ? "" : local;
}

// 当前工具里某参数的声明（服务端在 /api/tools 里已按数据源分类表注入 node_scoped /
// belongs_to_node / guest_node / node_agnostic 标记），前端一律据此处理节点联动。
function paramDecl(name) {
  return ((currentTool && currentTool.params) || []).find((p) => p.name === name);
}

// 当前表单所选虚机列表：勾选框组取勾选项；select 取 value（多选下拉为逗号分隔，同样拆开）
function formVmids() {
  const box = $("toolForm").querySelector('.checkbox-group[data-param="vmid"]');
  if (box) {
    return [...box.querySelectorAll('input[type="checkbox"]:checked')]
      .map((c) => c.value.trim()).filter(Boolean);
  }
  const sel = $("toolForm").querySelector('select[data-param="vmid"]');
  return sel && sel.value ? sel.value.split(",").map((s) => s.trim()).filter(Boolean) : [];
}

// 按执行节点过滤后列表为空时的占位项：明确区分"没有虚机"与"不在这个节点上"
function emptyNodeOption(node) {
  return {
    value: "",
    label: node ? `（执行节点 ${node} 上没有虚机/容器，请在「执行节点」里切换）` : "（没有可用的虚机/容器）",
    disabled: true,
  };
}

// 动态下拉数据源拉取失败时的占位项：让「加载失败」与「确实没有可选项」在界面上可区分
function optionLoadErrLabel(err) {
  const msg = cleanErr(String((err && err.message) || err || "").trim()) || "未知错误";
  return `（选项加载失败：${msg.length > 80 ? msg.slice(0, 80) + "…" : msg}）`;
}

async function fillDynamicOptions(onlyFrom = null) {
  const all = [...document.querySelectorAll("#toolForm select[data-options-from], #toolForm .checkbox-group[data-options-from]")];
  const selects = onlyFrom ? all.filter((el) => el.dataset.optionsFrom === onlyFrom) : all;
  const ctx = new OptionBatch();
  // 第一遍（dry）：仅登记本批所有下拉需要的上游 path，不渲染
  for (const el of selects) await _fillOptionsFor(el, (p) => ctx.get(p, true), true);
  await ctx.fetch();   // 合并成 1 个 GET /api/options
  // 第二遍（real）：从批量结果渲染，api() 直接读已取回数据，不再各自打后端
  for (const el of selects) await _fillOptionsFor(el, (p) => ctx.get(p, false), false);
}

async function _fillOptionsFor(el, api, dry) {
  let opts = [];
  // 数据源拉取失败的原因；用于把「加载失败」与「确实没有可选项」区分开（见下方渲染处）
  let loadErr = null;
  // 「是否把请求指向执行节点」由服务端注入的标记决定（node_scoped），不再靠各分支手写 ?node=：
  // 以前漏一处就会拿本机数据（例如 LXC 存储下拉），而漏在哪里完全靠人记。
  const decl = paramDecl(el.dataset.param);
  const nodeQuery = () => {
    if (!decl || !decl.node_scoped) return "";
    const nodeEl = $("nodeSelect");
    const node = nodeEl ? nodeEl.value : "";
    return node ? "?node=" + encodeURIComponent(node) : "";
  };
  try {
      if (el.dataset.optionsFrom === "guests") {
        const guests = allGuests.length ? allGuests : await api("/guests");
        const noTpl = el.dataset.noTemplate === "1";
        // 一律按「有效执行节点」过滤：qm/pct 只能操作本节点虚机，跨节点选中会在脚本里
        // qm status 查不到而静默失败（服务端 _verify_guest_ownership 也会拒绝提交）。
        // 原先只对直通类工具过滤（靠工具名白名单），其它工具漏了就会误导用户，故改为通用规则。
        const nodeFilter = effectiveExecNode();
        // 磁盘相关工具对模板无意义（模板上 qm set 受锁限制），no_template 时直接隐藏模板项
        const list = (noTpl ? guests.filter((g) => g.type === "qemu" && !g.template) : guests.filter((g) => g.type === "qemu"))
          .filter((g) => !nodeFilter || g.node === nodeFilter);
        opts = list.map((g) => ({
          value: String(g.vmid),
          label: `${g.vmid} · ${g.name || "未命名"}（${g.node}）`,
        }));
        if (!opts.length) opts = [emptyNodeOption(nodeFilter)];
      } else if (el.dataset.optionsFrom === "guests_all") {
        // 虚拟机 + 容器列表（快照等功能对两者都生效）；模板统一标注，
        // 参数声明 no_template 的工具（如定时开关机）模板项置灰不可选；
        // 参数声明 no_passthrough 的工具（如克隆）带 PCIe 直通/RDM 盘的虚机置灰不可选
        const guests = allGuests.length ? allGuests : await api("/guests");
        const noTpl = el.dataset.noTemplate === "1";
        const noPt = el.dataset.noPassthrough === "1";
        const noGpu = el.dataset.vmOnly === "1";   // 仅虚拟机可用（如 vGPU：mdev 挂不到容器）
        // 同 guests：容器/虚机都必须与执行节点同节点（pct/qm 是节点本地命令），一律过滤
        const nodeFilter = effectiveExecNode();
        const shown = nodeFilter ? guests.filter((g) => g.node === nodeFilter) : guests;
        opts = shown.map((g) => {
          const pt = g.has_passthrough === 1;
          // 直通类型细分：vGPU（hostpci 带 mdev=）与物理直通都禁止克隆，但原因不同，文案分开
          const kind = g.passthrough_kind || "";
          const isC = g.type !== "qemu";
          let label = `${g.vmid} · ${g.name || "未命名"}（${isC ? "LXC" : "VM"}${g.template ? "，模板" : ""}，${g.node}）`;
          if (g.template && noTpl) label += "（模板不可操作）";
          if (pt && noPt) {
            const what = kind === "vgpu" ? "vGPU"
              : (kind === "mixed" ? "直通与 vGPU" : "直通盘");
            label += `（含${what}，不支持克隆）`;
          }
          if (isC && noGpu) label += "（容器不支持 vGPU）";
          return { value: String(g.vmid), label, disabled: !!(g.template && noTpl) || !!(pt && noPt) || !!(isC && noGpu) };
        });
        if (!opts.length) opts = [emptyNodeOption(nodeFilter)];
      } else if (el.dataset.optionsFrom === "nodes") {
        // 集群节点列表（迁移选目标节点）：成员表全局一致，与「在哪台机器上执行」无关，
        // 因此不按执行节点过滤、也不带 ?node=（数据源在 toolbox.NODE_AGNOSTIC_SOURCES 登记为 global）
        const offline = new Set((quorumInfo && quorumInfo.offline) || []);
        // 「本机」指迁移脚本实际执行的那台（= 执行节点 = 源节点），不是跑本 Web 控制台的那台：
        // 脚本里用 hostname -s 与目标节点比对（见 vm_migrate 脚本），而控制台固定驻留在某一台机器上。
        // 用控制台节点判断会把「合法的目标节点」（控制台自己）错误置灰 —— 典型场景：控制台在 pve01，
        // 迁 pve02 上的虚机到 pve01 明明可行却被禁。故一律按「有效执行节点」判断。
        const self = effectiveExecNode() || localNodeName();
        const ns = (await api("/nodes")).filter((n) => !n.placeholder);
        opts = ns.map((n) => {
          const name = String(n.node);
          const isSelf = name === self;
          const isOff = offline.has(name) || n.status === "offline";
          const marks = [isSelf ? "本机（当前节点）" : "", isOff ? "离线" : ""].filter(Boolean);
          return {
            value: name,
            label: `${name}${marks.length ? "（" + marks.join("，") + "）" : ""}`,
            // 本机不是有效目标（脚本里也会拦截），离线节点迁不过去：置灰但保留，让用户看清原因
            disabled: isSelf || isOff,
          };
        });
        if (!opts.length) opts = [{ value: "", label: "（没有可用的目标节点）", disabled: true }];
      } else if (el.dataset.optionsFrom === "device") {
        const modeEl = document.querySelector('#toolForm select[data-param="mode"]');
        const actEl = document.querySelector('#toolForm select[data-param="action"]');
        const vmidEl = document.querySelector('#toolForm select[data-param="vmid"]');
        const isDel = actEl && actEl.value === "del";
        if (modeEl && modeEl.value === "pcie") {
          if (isDel && vmidEl && vmidEl.value) {
            // 移除模式：只列出该 VM 当前已直通的设备（避免用户从全部设备里选错）
            const g = allGuests.find((x) => String(x.vmid) === vmidEl.value);
            if (g) {
              const cfg = await api(`/guests/${g.node}/${g.type}/${g.vmid}/config`);
              // 设备描述要到 VM 所在节点取，否则多节点时描述对不上
              const pci = await api(`/pci${g.node ? "?node=" + encodeURIComponent(g.node) : ""}`);
              opts = [];
              for (const [k, v] of Object.entries(cfg || {})) {
                if (!/^hostpci\d+$/.test(k)) continue;
                // hostpci 值形如 0000:03:00,pcie=1 或 0000:01:00,x-vga=1，取第一个逗号前的地址
                const addr = String(v).split(",")[0].trim();
                if (!addr) continue;
                const info = pci.find((d) => d.addr === addr) || pci.find((d) => d.addr.startsWith(addr));
                const desc = info ? info.desc : "";
                opts.push({ value: addr, label: `${addr} · ${desc || "已直通设备"}` });
              }
              if (!opts.length) opts = [{ value: "", label: "（该 VM 无 PCIe 直通设备）" }];
            }
          } else {
            // 添加模式：设备列表跟随执行节点
            const devs = await api("/pci" + nodeQuery());
            opts = devs.map((d) => ({
              value: d.addr,
              label: `${d.addr} · ${d.desc}${d.host_used ? "　⚠ 承载系统盘，直通会掉盘，禁止" : d.compat === false ? "　⚠ MSI-X 不兼容，直通会失败，建议改用块映射（RDM）" : ""}`,
              disabled: !!d.host_used,
            }));
          }
        } else {
          const disks = await api("/disks/byid");
          opts = disks.map((d) => ({
            value: d.name,
            label: `${d.name}（${d.size || "?"}，${d.dev}${d.host_used ? "，系统盘/已挂载 ⚠" : ""}）`,
            disabled: !!d.host_used,
          }));
        }
      } else if (el.dataset.optionsFrom === "nic") {
        // PCIe 网卡列表：标注宿主接口名，宿主占用网口（有 IP/桥接成员/默认路由）置灰（直通后宿主断网）
        const nics = await api("/pci/nics" + nodeQuery());
        // 无线网卡（含 CNVi）：只标注不置灰（仍可选，直通给 Linux 虚机做 AP 是合法用法）；
        // 宿主占用口（有 IP / 桥成员 / 默认路由）置灰
        opts = nics.map((d) => ({
          value: d.addr,
          label: `${d.addr} · ${d.desc}${d.wireless ? "　⚠ 无线网卡，虚机通常无法当网口使用" : ""}${d.ifname
            ? (d.in_use ? `（${d.ifname}，宿主占用 ⚠ 禁止直通）` : `（${d.ifname}）`)
            : ""}${d.speed > 0 ? ` · ${fmtNicSpeed(d.speed)}` : ""}${d.group > 1 ? ` · 隔离组含 ${d.group} 个设备` : ""}`,
          disabled: !!d.in_use,
        }));
      } else if (el.dataset.optionsFrom === "gpu") {
        // PCIe 显示设备：按「显卡类型」过滤独显/核显，标注宿主已绑定驱动（直通前可能需屏蔽）
        const gpus = await api("/pci/gpus" + nodeQuery());
        const gtEl = document.querySelector('#toolForm select[data-param="gtype"]');
        const gtype = gtEl ? gtEl.value : "";
        opts = gpus
          .filter((d) => !gtype || d.kind === gtype)
          .map((d) => ({
            value: d.addr,
            // 核显带代次（igpu_gen，服务端按设备 ID 查 INTEL_IGPU_GENERATIONS）：
            // 老代次 legacy-igd 可用、新代次通常无效/不适用，直接标在选项里便于选路线
            label: `${d.addr} · ${d.desc}（${d.kind === "igpu" ? "核显" : "独显"}${d.driver ? `，宿主驱动: ${d.driver}` : ""}${d.group > 1 ? `，隔离组含 ${d.group} 个设备` : ""}）${d.igpu_gen ? `　${d.igpu_gen.label}${d.igpu_gen.legacy_igd === "rare" ? "（legacy-igd 通常无效）" : (d.igpu_gen.legacy_igd === "no" ? "（无 legacy-igd）" : "")}` : ""}${d.recommend ? "　⚠ 直通易报 Code 43，推荐配置见提示" : ""}`,
          }));
      } else if (el.dataset.optionsFrom === "pci_generic") {
        // 其余 PCIe 设备（采集卡/声卡/USB 控制器等）：标注宿主驱动与已占用 VM
        const devs = await api("/pci/generic" + nodeQuery());
        opts = devs.map((d) => ({
          value: d.addr,
          label: `${d.addr}${d.tag ? ` ${d.tag}` : ""} · ${d.desc}${d.used_by
            ? `（已直通给 VM ${d.used_by} ⚠）`
            : d.driver ? `（宿主驱动: ${d.driver}）` : ""}${d.group > 1 ? ` · 隔离组含 ${d.group} 个设备（整组直通会连带）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "vfio_devices") {
        // 「被 vfio 接管但没有 VM 使用」的设备（释放 vfio 占用工具）。
        // 只读的「列出候选设备」模式下整组置灰，避免看着能勾、实际不做事
        const actEl = document.querySelector('#toolForm select[data-param="action"]');
        const viewing = actEl ? actEl.value === "list" : false;
        const devs = await api("/pci/vfio" + nodeQuery());
        opts = (devs || []).map((d) => ({
          value: d.addr,
          disabled: viewing,
          label: `${d.addr} · ${d.name || d.desc}（${d.state === "unbound"
            ? `无驱动${d.orig_driver ? `，可装回 ${d.orig_driver}` : ""}`
            : (d.orig_driver
              ? `原驱动 ${d.orig_driver}${d.orig_blacklisted ? "，已被屏蔽" : ""}`
              : "原驱动未知")}）`,
        }));
        if (!opts.length) {
          opts = [{ value: "", label: "（没有被 vfio 接管且无人使用的设备）", disabled: true }];
        }
      } else if (el.dataset.optionsFrom === "usb") {
        // USB 设备列表：标注已被哪些 VM 直通占用（同设备重复直通会被脚本拦截）
        const devs = await api("/usb/devices" + nodeQuery());
        opts = devs.map((d) => ({
          value: d.id,
          label: `${d.id} · ${d.desc}${d.used_by.length ? `（已直通给 VM ${d.used_by.join(", ")} ⚠）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "kernels") {
        // 已装内核列表（随执行节点联动）：运行中内核标注，供设默认启动选择
        const ks = await api("/kernels" + nodeQuery());
        opts = ks.map((k) => ({
          value: k.version,
          label: `${k.version}${k.running ? " ← 运行中" : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "kernels_available") {
        // 仓库可装内核包（随执行节点联动）：供安装新内核选择；
        // 后端已按运行内核 signed/未签名形态过滤、最新版排最前，
        // 下拉默认选中第一项即最新内核，不再提供空值「自动装最新」项
        const ks = await api("/kernels/available" + nodeQuery());
        opts = ks.map((k) => ({
          value: k.name,
          label: `${k.name}${k.signed ? "（签名版，支持安全启动）" : ""}${k.meta ? "（元包）" : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "grub_backups") {
        // GRUB 配置备份列表（随执行节点联动）：供从备份恢复选择；
        // 后端按时间戳倒序最近日期在前，下拉默认选中第一项即最新备份
        const bs = await api("/grub/backups" + nodeQuery());
        opts = bs.map((b) => ({ value: b.name, label: b.name }));
      } else if (el.dataset.optionsFrom === "net_backups") {
        // 网络配置备份列表（随执行节点联动）：供接口配置管理从备份恢复选择，同 GRUB 规则最近在前
        const bs = await api("/network/backups" + nodeQuery());
        opts = bs.map((b) => ({ value: b.name, label: b.name }));
      } else if (el.dataset.optionsFrom === "vbios_roms") {
        // 已上传的 vBIOS ROM 列表（/usr/share/kvm/，已排除 QEMU 系统固件）：
        // 供显卡直通-romfile 下拉选择；未上传任何 ROM 时给提示占位项
        const roms = await api("/vbios/roms");
        opts = roms.length
          ? roms.map((r) => ({ value: r.name, label: r.name }))
          : [{ value: "", label: "尚未上传 vBIOS ROM（点下方按钮上传）", disabled: true }];
      } else if (el.dataset.optionsFrom === "docker_versions") {
        // Docker 引擎稳定版清单（docker-ce 仓库静态目录，与发行版无关；服务端缓存 6h）
        const dvs = await api("/docker-versions");
        if (!dvs.length) {
          opts = [{ value: "latest", label: "最新稳定版（版本清单获取失败，将装仓库最新）" }];
        } else {
          opts = [
            { value: "latest", label: `最新稳定版（${dvs[0].final}，推荐）` },
            ...dvs.map((d, i) => ({
              value: d.major,
              label: `${d.major}.x（最终稳定 ${d.final}${i === 0 ? " · 最新大版本" : ""}）`,
            })),
          ];
        }
      } else if (el.dataset.optionsFrom === "bridges") {
        // vmbr 网桥：标注 IP/桥接网口，被 VM 使用时警告（删除会被脚本拦截）
        const brs = await api("/network/bridges" + nodeQuery());
        opts = brs.map((b) => ({
          value: b.name,
          label: `${b.name}${b.address ? ` · ${b.address}` : ""}${b.ports ? `（桥接 ${b.ports}）` : ""}${b.used_by ? `（VM ${b.used_by} 在用 ⚠）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "vlans") {
        // VLAN 接口：标注 IP 与所属设备
        const vs = await api("/network/vlans" + nodeQuery());
        opts = vs.map((v) => ({
          value: v.name,
          label: `${v.name}${v.address ? ` · ${v.address}` : " · 无 IP"}${v.raw ? `（在 ${v.raw} 上）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "phys_nics") {
        // 物理网口（已排除 lo/虚拟接口/VLAN 子接口）
        const ns = await api("/network/interfaces" + nodeQuery());
        opts = ns.map((n) => ({ value: n.name, label: n.name }));
      } else if (el.dataset.optionsFrom === "disks") {
        // 物理磁盘列表（已排除虚拟设备），供磁盘分区与格式化选盘。
        // 危险程度分级：PVE 系统盘/存储盘置灰禁止操作；含其他系统的盘只警告不禁止
        //（other_os 是启发式判断、可能误判，如纯 NTFS 数据盘，故留给用户确认后仍可操作）
        const ds = await api("/disks" + nodeQuery());
        opts = ds.map((d) => {
          let warn = "";
          if (d.pve_system) warn = "　⚠ PVE 系统盘，禁止操作";
          else if (d.pve_data) warn = "　⚠ PVE 存储盘，正在使用";
          else if (d.other_os) warn = "　⚠ 含其他系统（Windows/Linux），格式化会清除";
          return {
            value: d.name,
            label: `${d.name} · ${d.model || "未知型号"}${d.size ? `（${d.size}）` : ""}${warn}`,
            disabled: !!(d.pve_system || d.pve_data),
          };
        });
      } else if (el.dataset.optionsFrom === "disk_storages") {
        // 支持 images 内容的 dir 型存储（虚机导入导出选存储），附剩余空间
        // 排除项目共享存储 pvewebtools：该存储只服务于传输中心，不作其它工具的目标存储
        const ss = (await api("/disks/storages" + nodeQuery())).filter((s) => !isSharedStorage(s));
        opts = ss.map((s) => ({
          value: s.storage,
          label: `${s.storage}${s.free != null ? `（剩余 ${fmtBytes(s.free)}）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "lxc_storages") {
        // 支持容器 rootfs 的存储（rootdir 或 images 内容），供创建 LXC 容器选系统盘/数据盘存储
        // 走按节点采集的接口：存储"对该节点是否可用"是节点级事实，容器要在执行节点上创建，
        // 原先读本机存储列表（/storages），切到其它节点后会选出该节点上不存在的存储。
        // 排除项目共享存储 pvewebtools：该存储只服务于传输中心
        const ss = (await api("/storages/rootdir" + nodeQuery())).filter((s) => !isSharedStorage(s));
        opts = ss.map((s) => ({
          value: s.storage,
          label: `${s.storage}${s.free != null ? `（剩余 ${fmtBytes(s.free)}）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "image_storages") {
        // 支持 images 内容的全部存储（含 lvmthin/zfs/ceph 等，虚机新增磁盘选存储）
        // 排除项目共享存储 pvewebtools：只服务于传输中心
        const ss = (await api("/storages/images" + nodeQuery())).filter((s) => !isSharedStorage(s));
        opts = (ss || []).map((s) => ({
          value: s.storage,
          label: `${s.storage}（${s.type || "存储"}）`,
        }));
      } else if (el.dataset.optionsFrom === "disk_upload") {
        // 已上传/下载到 disk-images（普通磁盘镜像）的列表，供磁盘导入工具下拉选择
        // value 编码为 <storage>::<name>，脚本侧据此解析真实源路径
        const files = await api("/disks/imported?kind=disk");
        // 已识别过系统的镜像（meta.json 有 label/distro）在下拉里带上系统名，便于选择
        const sysTag = (f) => (f.label && f.label !== f.name)
          ? ` · ${f.label}`
          : (f.distro ? ` · ${f.distro}${f.version ? " " + f.version : ""}` : "");
        opts = files.length
          ? files.map((f) => ({ value: `${f.storage}::${f.name}`, label: `${f.name}${sysTag(f)}${f.vsize != null ? `（${fmtBytes(f.vsize)}）` : (f.size != null ? `（${fmtBytes(f.size)}）` : "")}${f.storage && f.storage !== "__import__" ? ` · ${f.storage}` : ""}` }))
          : [{ value: "", label: "请选择磁盘镜像", disabled: true }];
      } else if (el.dataset.optionsFrom === "cloud_images") {
        // 已就绪的云镜像列表（与磁盘导入共用导入目录，供云镜像装机下拉选择），value 同样编码存储
        // 同时记录每个镜像的虚拟磁盘大小（vsize），供选中后同步更新「系统盘大小」默认项
        const q = new URLSearchParams({ kind: "cloud" });
        const files = await api("/disks/imported?" + q.toString());
        const vmap = {};
        const mmeta = {};
        opts = files.length
          ? files.map((f) => {
              const val = `${f.storage}::${f.name}`;
              vmap[val] = f.vsize;
              mmeta[val] = { distro: f.distro || "", version: f.version || "" };
              const disp = (f.label && f.label !== f.name) ? f.label : f.name;
              const sz = f.vsize != null
                ? `（系统盘 ${fmtBytes(f.vsize)}）`
                : (f.size != null ? `（文件 ${fmtBytes(f.size)}）` : "");
              const loc = f.storage && f.storage !== "__import__" ? ` · ${f.storage}` : "";
              return { value: val, label: `${disp}${sz}${loc}` };
            })
          : [{ value: "", label: "请先到「传输中心」获取云镜像", disabled: true }];
        window.__cloudImageVSize = vmap;
        window.__cloudImageMeta = mmeta;
      } else if (el.dataset.optionsFrom === "vm_disks") {
        // VM 磁盘列表（随所选 VM 联动）：导入后挂载 / 导出时选择。
        // vmid 为勾选框组多选（如磁盘导出）：未选提示先选；勾选多台时无法跨台罗列磁盘，
        // 仅保留空选项（导出=每台全部磁盘，脚本逐台处理）
        const vmids = formVmids();
        const vmid = vmids.length === 1 ? vmids[0] : "";
        if (!vmids.length) {
          el.innerHTML = `<option value="">（请先选择 VM）</option>`;
          return;
        }
        if (vmids.length > 1) {
          const tip = (currentTool && currentTool.id === "disk_export")
            ? "（全部磁盘·多台各自导出）" : "（多台批量：自动按台处理）";
          el.innerHTML = `<option value="">${tip}</option>`;
          return;
        }
        const g = allGuests.find((x) => String(x.vmid) === vmid);
        if (!g || g.type !== "qemu") {
          el.innerHTML = `<option value="">（仅 VM 支持，非 LXC）</option>`;
          return;
        }
        const disks = await api(`/disks/vm?node=${encodeURIComponent(g.node)}&type=qemu&vmid=${vmid}`);
        opts = disks.map((d) => ({
          value: d.key,
          label: `${d.key} · ${d.volid}${d.size ? `（${d.size}）` : ""}`,
        }));
        // 空选项文案按工具区分：导出=全部磁盘；其余（导入挂载等）=自动挂载下一个空闲槽位
        const emptyLabel = (currentTool && currentTool.id === "disk_export")
          ? "（全部磁盘）"
          : (currentTool && currentTool.id === "vm_disk")
          ? "（请选择要移除的磁盘）"
          : "（自动挂载到下一个空闲槽位）";
        opts.unshift({ value: "", label: emptyLabel });
      } else if (el.dataset.optionsFrom === "backup_storages") {
        // 支持 backup 内容的 dir 型存储（虚机备份选存储），附剩余空间
        // 排除项目共享存储 pvewebtools：只服务于传输中心
        const ss = (await api("/backup/storages" + nodeQuery())).filter((s) => !isSharedStorage(s));
        opts = ss.map((s) => ({
          value: s.storage,
          label: `${s.storage}${s.free != null ? `（剩余 ${fmtBytes(s.free)}）` : ""}`,
        }));
      } else if (el.dataset.optionsFrom === "backups") {
        // 备份文件列表（还原时下拉选）
        const bs = await api("/backup/files" + nodeQuery());
        opts = bs.map((b) => ({
          value: b.name,
          label: `${b.name}（${b.storage}，${fmtBytes(b.size)}）`,
        }));
      } else if (el.dataset.optionsFrom === "deploy_sources") {
        // 模板快速建机的模板来源：只列 pvewebtools 共享存储 dump/ 里的模板文件（.vma）
        // 后端把 storage="pvewebtools" 的备份文件标记 template:true，供此处精确筛选
        const bs = await api("/backup/files");
        opts = (bs || []).filter((b) => b.template).map((b) => ({
          value: `file:${b.path}`,
          label: `${b.name}（模板文件）`,
        }));
        if (!opts.length) opts = [{ value: "", label: "请先到「传输中心」上传虚机模板", disabled: true }];
      } else if (el.dataset.optionsFrom === "lxc_templates") {
        // 创建 LXC 容器：已下载的 CT 模板来源（value 形如 <storage>::<name>）
        // 只列共享存储 pvewebtools 的模板（集群各节点建机均可用），其它存储的模板不关注
        const ts = (await api("/lxc-template/downloaded")).filter((t) => t.storage === "pvewebtools");
        opts = (ts || []).map((t) => ({
          value: `${t.storage}::${t.name}`,
          label: `${t.name}${t.size != null ? `（${fmtBytes(t.size)}）` : ""}`,
        }));
        if (!opts.length) opts = [{ value: "", label: "请先到「传输中心」获取 LXC 模板", disabled: true }];
      }
    } catch (e) {
      loadErr = e;
      opts = [];
    }
    if (dry) return;   // 仅登记 path 的预扫描阶段：不渲染，避免无意义的 DOM 写入
    // 弹窗可能已关闭/切换，选项只填当前还在 DOM 里的控件
    if (!el.isConnected) return;
    // 请求失败 ≠ 没有可选项：以前两者都渲染成「（无可用选项）」，接口/节点故障
    // （如外节点采集失败）会被误读成"没有存储/没有虚机"，排查时毫无线索。
    if (!opts.length && loadErr) {
      opts = [{ value: "", label: optionLoadErrLabel(loadErr), disabled: true }];
    }
    // 记住当前选中值：执行成功后 autoRefreshFormSources() 会重填动态下拉，
    // 若直接把 innerHTML 换掉会把选择重置成第一项——直通这类操作会让人误以为对象变了
    // （典型：给 VM 102 直通，刷新后界面显示 100）
    const keep = el.options ? el.value : "";
    // 参数标记了 default_option（如模板快速建机的「目标存储」「网桥」）：
    // 在列表最前插入空值占位项，选中它即留空、走脚本的默认配置（存储 local / 网桥 vmbr0）
    const pdef = (currentTool && currentTool.params || []).find((x) => x.name === el.dataset.param);
    if (pdef && pdef.default_option && opts.length) {
      opts = [{ value: "", label: pdef.default_option }, ...opts];
    }
    // 选框组：用 <label><input type=checkbox></label> 列表渲染，提交时逗号拼接
    if (el.classList && el.classList.contains("checkbox-group")) {
      const cname = el.dataset.param;
      el.innerHTML = opts.length
        ? opts.map((o) => `<label class="checkbox-item${o.disabled ? " is-disabled" : ""}"><input type="checkbox" data-param="${escapeHtml(cname)}" value="${escapeHtml(o.value)}"${o.disabled ? " disabled" : ""}>${escapeHtml(o.label)}</label>`).join("")
        : `<div class="checkbox-empty">（无可用虚机/容器）</div>`;
    } else if (opts.length && opts.some((o) => o.group)) {
      // 带分组的下拉（目前只有「社区脚本」按官网分类）：渲染 <optgroup>。
      // 组的顺序 = 首次出现顺序（后端已按官网 sort_order 排好），组内保持后端顺序；
      // 没带 group 的项平铺在最后，避免个别缺分类的选项被吞掉。
      const opt = (o) => `<option value="${escapeHtml(o.value)}"${o.disabled ? " disabled" : ""}${o.disabled && o.reason ? ` title="${escapeHtml(o.reason)}"` : ""}>${escapeHtml(o.label)}</option>`;
      const order = [];
      const byGroup = new Map();
      const loose = [];
      opts.forEach((o) => {
        const g = o.group || "";
        if (!g) { loose.push(o); return; }
        if (!byGroup.has(g)) { byGroup.set(g, []); order.push(g); }
        byGroup.get(g).push(o);
      });
      el.innerHTML = order.map((g) => `<optgroup label="${escapeHtml(g)}">${byGroup.get(g).map(opt).join("")}</optgroup>`).join("")
        + loose.map(opt).join("");
    } else {
      el.innerHTML = opts.length
        ? opts.map((o) => `<option value="${escapeHtml(o.value)}"${o.disabled ? " disabled" : ""}${o.disabled && o.reason ? ` title="${escapeHtml(o.reason)}"` : ""}>${escapeHtml(o.label)}</option>`).join("")
        : `<option value="">（无可用选项）</option>`;
    }
    // 原选中项在新列表里仍存在且仍可选 → 恢复选择；否则保持默认（第一项）
    // （要求 !disabled：重填可能让原选项变为不可选，如迁移目标节点在执行节点切换后成了「本机」，
    //  此时再把它恢复成选中值会让表单带着一个无效目标提交，任务必然报错）
    if (keep && el.options && [...el.options].some((o) => o.value === keep && !o.disabled)) el.value = keep;
    // 若当前选中项被置灰（如刷新后默认第一项恰为不可选的「最新稳定版」），自动改选第一个可选项，
    // 避免表单带着无效值提交（典型：CentOS/RHEL 7 EOL 下「最新稳定版」被灰，应选 18.x）
    if (el.options && [...el.options].some((o) => o.value === el.value && o.disabled)) {
      const firstEnabled = [...el.options].find((o) => !o.disabled && o.value);
      if (firstEnabled) el.value = firstEnabled.value;
    }
    if (el.dataset.optionsFrom === "cloud_images") onCloudImageChange(el);
    // 建机类工具：来源下拉（云镜像 / 模板）填充完成后立即刷新「默认配置」的具体值，
    // 不必等待其余动态下拉（网桥/存储等）全部加载完，避免默认配置迟迟不显示。
    // 此时动态预填值可能尚未在 .then() 中回填，故先在此应用该来源项的 prefill 再触发。
    const isSrc = (el.dataset.optionsFrom === "cloud_images" && currentTool && currentTool.id === "cloud_deploy" && el.dataset.param === "image")
      || (el.dataset.optionsFrom === "deploy_sources" && currentTool && currentTool.id === "vm_deploy" && el.dataset.param === "template");
    if (isSrc) {
      const pf = pendingDynamicPrefill.find(([n]) => n === el.dataset.param);
      if (pf && [...el.options].some((o) => o.value === pf[1])) el.value = pf[1];
      if (el.value) loadDeployDefaults(el);
    }
    // 下拉为空时，「去传输中心上传」这类来源提示合并进该字段的 ⓘ 悬浮框（不再占用控件上方一行）；
    // 有可选项时还原为字段自身的提示。仅对声明了 data-empty-hint 的字段生效
    // （vBIOS ROM / 磁盘导入 / vGPU 宿主机物料）
    const emptyHint = el.dataset.emptyHint;
    if (emptyHint) {
      const row = el.closest(".form-row");
      const label = row ? row.querySelector("label") : null;
      let tip = label ? label.querySelector(".tip") : null;
      if (label && !tip) {
        // 字段自身没有提示：补一个 ⓘ 专门承载来源提示，有可选项时再把图标隐藏
        tip = document.createElement("span");
        tip.className = "tip";
        tip.textContent = "ⓘ";
        tip.dataset.baseTip = "";
        tip.dataset.tipOnly = "1";
        label.appendChild(tip);
      }
      if (tip) {
        if (tip.dataset.baseTip === undefined) tip.dataset.baseTip = tip.dataset.tip || "";
        const base = tip.dataset.baseTip;
        const empty = !opts.some((o) => o.value !== "");
        tip.dataset.tip = empty ? (base ? base + "\n" : "") + emptyHint : base;
        if (tip.dataset.tipOnly === "1") tip.hidden = !empty;
      }
    }
}

// 选中云镜像后，把「系统盘大小」默认项的文案动态显示为「系统镜像配置（默认 XG）」
function onCloudImageChange(sel) {
  const vs = (window.__cloudImageVSize && window.__cloudImageVSize[sel.value]) || null;
  const sysEl = document.querySelector('#toolForm select[data-param="sys_disk"]');
  if (sysEl) {
    const opt0 = sysEl.querySelector('option[value=""]');
    if (opt0) opt0.textContent = vs != null ? `系统镜像配置（默认 ${fmtBytes(vs)}）` : "系统镜像配置（默认）";
  }
  // 把镜像元数据（distro/version）回填到隐藏参数，部署脚本优先使用准确版本，否则回退文件名推断
  const meta = (window.__cloudImageMeta && window.__cloudImageMeta[sel.value]) || {};
  const dEl = document.querySelector('#toolForm [data-param="distro"]');
  const vEl = document.querySelector('#toolForm [data-param="version"]');
  if (dEl) dEl.value = meta.distro || "";
  if (vEl) vEl.value = meta.version || "";
}

// 由部署模板来源推导发行版标签（与后端 deploy_distro_label 保持一致）
function deriveDeployLabel(src) {
  let name = "";
  if (src.startsWith("file:")) {
    name = src.slice(5).split("/").pop()
      .replace(/^vzdump-(qemu|lxc)-/, "").replace(/\.(vma|tar)(\.[^.]+)?$/, "");
  } else if (src.startsWith("vmid:")) {
    const vid = src.slice(5);
    const g = (window.allGuests || []).find((x) => String(x.vmid) === vid);
    name = g && g.name ? g.name : "";
  }
  const n = name.toLowerCase();
  const KW = [
    ["centos", "centos"], ["rocky", "rocky"], ["alma", "alma"], ["fedora", "fedora"],
    ["debian", "debian"], ["ubuntu", "ubuntu"], ["opensuse", "opensuse"], ["suse", "opensuse"],
    ["alpine", "alpine"], ["arch", "arch"], ["gentoo", "gentoo"], ["oracle", "oracle"],
    ["freebsd", "freebsd"], ["openbsd", "openbsd"], ["netbsd", "netbsd"],
    ["windows", "win"], ["win", "win"],
    ["openwrt", "openwrt"], ["ikuai", "ikuai"], ["istoreos", "istoreos"], ["fnos", "fnos"],
    ["unraid", "unraid"], ["truenas", "truenas"], ["pfsense", "pfsense"], ["vyos", "vyos"],
    ["mikrotik", "routeros"], ["routeros", "routeros"],
  ];
  for (const [k, v] of KW) if (n.includes(k)) return v;
  // 别名表未命中：由名称清洗出合法前缀（与后端 deploy_label_fallback 同一套规则），
  // 别一律回落 vm —— 否则未收录的系统会显示成「vm-编号」，看着像没识别出系统
  return deployLabelFallback(n);
}

// 别名表未命中时的兜底标签（与后端 DEPLOY_LABEL_HELPERS 保持一致）：
// 取名称里第一个有信息量的词，跳过纯数字段（时间戳/位宽）、架构 / 引导方式 / 无意义词
function deployLabelFallback(name) {
  const NOISE = new Set(["x86", "i386", "i686", "amd64", "aarch64", "arm64", "arm",
    "uefi", "bios", "efi", "legacy", "vm", "img", "image", "template", "disk", "tmp", "test"]);
  const segs = String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").split("-");
  for (const seg of segs) {
    if (!/^[a-z0-9]{2,}$/.test(seg)) continue;   // 空段 / 单字符 / 含非字母数字
    if (!/[a-z]/.test(seg)) continue;            // 纯数字段（时间戳、位宽）
    if (NOISE.has(seg)) continue;
    return seg.slice(0, 40);                     // 主机名总长 63 上限，前缀留 40 字符余量
  }
  return "vm";
}

// 多选下拉（批量删除等）：一键全选/清空；仅选中可选且非空的 option
function multiSelectToggle(name, selectAll) {
  const el = $("toolForm").querySelector(`select[data-param="${name}"]`);
  if (!el || !el.multiple) return;
  [...el.options].forEach((o) => {
    if (!o.disabled && o.value) o.selected = !!selectAll;
  });
}

// 选框组已选计数：勾选变化后刷新「已选 N 台」提示（列表超高内滚时仍能看清勾了多少）
function updateCheckboxCount(grp) {
  if (!grp) return;
  const n = grp.querySelectorAll('input[type="checkbox"]:checked').length;
  const row = grp.closest(".form-row");
  const tip = row && row.querySelector(".checkbox-count");
  if (tip) tip.textContent = n ? `已选 ${n} 台` : "可多选";
}

// 手动勾选：change 冒泡到 document 统一刷新计数（全选/清空走 checkboxToggleAll 内的直接调用）
document.addEventListener("change", (e) => {
  const cb = e.target;
  if (cb && cb.type === "checkbox" && cb.closest(".checkbox-group")) {
    updateCheckboxCount(cb.closest(".checkbox-group"));
  }
});

// 选框组（如批量删除的虚机多选）：一键全选/清空；仅切换可选（未禁用）的选框
function checkboxToggleAll(name, selectAll) {
  const grp = $("toolForm").querySelector(`.checkbox-group[data-param="${name}"]`);
  if (!grp) return;
  grp.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    if (!cb.disabled) cb.checked = !!selectAll;
  });
  updateCheckboxCount(grp);
  // 程序化赋值不触发 change：手动派发一次，让依赖所选虚机的联动（快照列表/节点同步/磁盘下拉等）跟上
  grp.dispatchEvent(new Event("change", { bubbles: true }));
}

// 模板快速建机：用户填了编号则预填「系统-编号」；编号留空时由后端按 系统-VMID 生成（避免与真实分配编号错位）
async function suggestDeployName() {
  const form = $("toolForm");
  if (!form || !(currentTool && currentTool.id === "vm_deploy")) return;
  const tpl = form.querySelector('[data-param="template"]');
  const idEl = form.querySelector('[data-param="newid"]');
  const nameEl = form.querySelector('[data-param="newname"]');
  if (!tpl || !idEl || !nameEl) return;
  const vmid = idEl.value.trim();
  const oldid = idEl.dataset.prevVmid || "";   // 变更前的 VMID，用于判断是否「系统-旧编号」形态
  idEl.dataset.prevVmid = vmid;               // 记录，供下次变更比对
  if (!tpl.value) return;         // 未选模板：不预填（后端按桩名生成）
  if (!vmid) return;              // 留空自动分配：交给后端生成 系统-VMID

  const name = nameEl.value.trim();
  const m = name.match(/^(.+)-(\d+)$/);
  // 仅当名称当前是「系统-旧编号」形态（后缀等于变更前的 VMID）才跟随更新
  if (m && m[2] === oldid) {
    nameEl.value = `${m[1]}-${vmid}`;
    return;
  }
  if (name) return;               // 用户已手填自定义名称（如 centos-115）：不动
  nameEl.value = `${deriveDeployLabel(tpl.value)}-${vmid}`;  // 名称留空：自动预填
}

// 建机类工具（模板快速建机 vm_deploy / 云镜像建机 cloud_deploy）：选择来源后，
// 把各字段「默认配置」选项刷新为实际生效值（如「默认配置（vmbr0）」）。
// 关键：每次都要「先复位再套用」。后端会为模板缺省的项补 PVE 默认值，但拉取失败、
// 或该来源确实没有这一项（如模板没配网桥）时不会返回，不复位就会留着上一个来源的值，
// 看起来就像「配置没跟随模板」。
// 拉取失败时同样只留复位后的中性文案，不阻塞部署、也不残留上一个来源的值。
async function loadDeployDefaults(sel) {
  const tool = currentTool && currentTool.id;
  const setDefaultLabel = (param, label) => {
    // cores / memory 已改为 autotext（下拉带 data-autoselect，默认项 value=auto），
    // 与仍然用 select 的参数（cpu/bios/machine/bridge/storage）两种形态都要能改到默认项文案
    const t = $("toolForm").querySelector(`select[data-param="${param}"], select[data-autoselect="${param}"]`);
    if (!t) return;
    const opt = [...t.options].find((o) => o.value === "" || o.value === "auto");
    if (opt) opt.textContent = label;
  };
  // 各参数定义里自带的默认项文案（autotext 取 auto_label，静态下拉取 value=""
  // 那项的 label，动态下拉回落 default_option）。
  // 复位必须用它而不是统一的「默认配置」：cloud_deploy 的 bios/machine 后端根本不返回，
  // 统一抹平后就会从「默认（SeaBIOS）」「默认（q35）」退化成没有括号值的裸「默认配置」。
  const defaultLabelOf = (param) => {
    const p = (currentTool.params || []).find((x) => x.name === param);
    if (!p) return "默认配置";
    if (p.type === "autotext") return p.auto_label || "默认配置";
    const o = (p.options || []).find((x) => x.value === "");
    if (o && o.label) return o.label;
    return p.default_option || "默认配置";
  };
  const restore = (params) => params.forEach((p) => setDefaultLabel(p, defaultLabelOf(p)));
  // 先复归各参数自身的默认文案：选到来源后被替换成「默认配置（实际值）」，取不到就保持它
  restore(["cores", "memory", "cpu", "bios", "machine", "bridge", "storage"]);
  if (!sel.value) return;   // 清空来源选择：复位即可
  let cfg;
  try {
    // 模板快速建机：从模板 VM / .vma 读真实配置；云镜像建机：云镜像无模板配置，直接给脚本内置兜底值（按系统区分）
    if (tool === "cloud_deploy") {
      cfg = await api("/deploy/cloud-defaults?image=" + encodeURIComponent(sel.value));
    } else {
      cfg = await api(`/deploy/template-config?src=${encodeURIComponent(sel.value)}`);
    }
  } catch (e) {
    return;
  }
  if (!cfg || typeof cfg !== "object") return;
  const fmtMem = (mb) => {
    const n = parseInt(mb, 10);
    if (!n) return "";
    return n >= 1024 && n % 1024 === 0 ? `${n / 1024} GB` : `${n} MB`;
  };
  if (tool === "cloud_deploy") {
    // 云镜像建机默认配置为脚本写死的固定值（按系统区分：Linux 1核/2G/host，Windows 2核/4G/host）
    if (cfg.cores) setDefaultLabel("cores", `默认配置（${cfg.cores} 核）`);
    if (cfg.memory) setDefaultLabel("memory", `默认配置（${fmtMem(cfg.memory)}）`);
    if (cfg.cpu) setDefaultLabel("cpu", `默认配置（${cfg.cpu}）`);
    if (cfg.bridge) setDefaultLabel("bridge", `默认配置（${cfg.bridge}）`);
    if (cfg.storage) setDefaultLabel("storage", `默认配置（${cfg.storage}）`);
  } else {
    if (cfg.cores) setDefaultLabel("cores", `默认配置（${cfg.cores} 核）`);
    if (cfg.memory) setDefaultLabel("memory", `默认配置（${fmtMem(cfg.memory)}）`);
    if (cfg.cpu) setDefaultLabel("cpu", `默认配置（${cfg.cpu}）`);
    if (cfg.bios) setDefaultLabel("bios", `默认配置（${cfg.bios}）`);
    if (cfg.machine) setDefaultLabel("machine", `默认配置（${cfg.machine}）`);
    if (cfg.bridge) setDefaultLabel("bridge", `默认配置（${cfg.bridge}）`);
    if (cfg.boot_storage) setDefaultLabel("storage", `默认配置（${cfg.boot_storage}）`);
  }

  // 虚机名称：用户填了编号则预填「系统-编号」；编号留空时由后端按 系统-VMID 生成
  suggestDeployName();
}

function closeToolModal() {
  stopPolling();
  $("toolModal").classList.add("hidden");
}

// ---- 定时开关机管理：内嵌任务列表 ----

let currentSchedules = [];

async function loadScheduleList() {
  const node = $("nodeSelect") ? $("nodeSelect").value : "";
  $("scheduleList").innerHTML = '<div class="sched-empty">加载中…</div>';
  try {
    currentSchedules = await api(`/schedules${node ? "?node=" + encodeURIComponent(node) : ""}`);
  } catch (e) {
    currentSchedules = [];
  }
  renderScheduleList();
}

function renderScheduleList() {
  if (!currentSchedules.length) {
    $("scheduleList").innerHTML = '<div class="sched-empty">暂无定时任务（可在下方表单添加）</div>';
    return;
  }
  const DAYS_CN = { "*": "每天", "1-5": "工作日", "0,6": "周末" };
  const fmtT = (t, d) => t ? `<span class="nowrap">${t}（${DAYS_CN[d] || d}）</span>` : "未设";
  $("scheduleList").innerHTML = '<div class="sched-list">' + currentSchedules.map((s) => {
    const paused = !!s.paused;
    const statusTag = paused
      ? '<span class="sched-status paused">暂停</span>'
      : '<span class="sched-status active">启用</span>';
    return `
    <div class="sched-row${paused ? " paused" : ""}">
      <span class="type-tag ${s.gtype === "qm" ? "qemu" : "lxc"}">${s.gtype === "qm" ? "VM" : "LXC"}</span>
      <b>${escapeHtml(s.vmid)}</b>
      <span class="sched-times">开机 <b>${fmtT(s.start_time, s.start_days)}</b> · 关机 <b>${fmtT(s.stop_time, s.stop_days)}</b></span>
      ${statusTag}
      <span class="sched-actions">
        <button class="btn btn-sm" onclick="schedPauseToggle('${escapeHtml(s.vmid)}','${paused ? "resume" : "pause"}')">${paused ? "启用" : "禁用"}</button>
        <button class="btn btn-sm danger" onclick="schedDelAction('${escapeHtml(s.vmid)}')">删除</button>
      </span>
    </div>`;
  }).join("") + "</div>";
}

// 删除：直接提交 action=del（绕过表单，参考快照列表按钮）
// 通用：直接提交定时开关机操作（绕过表单），复用工具弹窗执行态展示
async function schedSubmit(params) {
  const payload = { params };
  const nodeSel = $("nodeSelect");
  if (nodeSel && nodeSel.value) payload.node = nodeSel.value;
  $("btnRunTool").disabled = true;
  setRunBtnLoading(true);
  syncRunBtnGate();   // 同一执行态：运行中一并锁住「重启生效 / 使直通生效」
  $("btnKillJob").classList.add("hidden");
  $("jobStatus").textContent = "执行中...";
  $("jobStatus").className = "job-status running";
  clearLogPanel();
  setLogVisible(true);
  logOffset = 0;
  try {
    const { job_id } = await api(`/tools/${currentTool.id}/run`, "POST", payload);
    currentRunJobId = job_id;
    $("btnKillJob").classList.remove("hidden");
    pollJob(job_id);
  } catch (e) {
    $("jobStatus").textContent = e.message;
    $("jobStatus").className = "job-status failed";
    $("btnRunTool").disabled = false;
    setRunBtnLoading(false);
    syncRunBtnGate();   // 提交失败：按确认词状态重算各动作按钮
  }
}

async function schedDelAction(vmid) {
  if (!(await appConfirm(`确定删除 VMID ${vmid} 的定时任务吗？`, { danger: true }))) return;
  await schedSubmit({ action: "del", vmid: vmid });
}

async function schedPauseToggle(vmid, action) {
  const label = action === "pause" ? "暂停" : "启用";
  if (!(await appConfirm(`确定${label} VMID ${vmid} 的定时任务吗？`))) return;
  await schedSubmit({ action: action, vmid: vmid });
}

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = null;
  if (jobStream) { try { jobStream.close(); } catch (_) {} jobStream = null; }
}

// ---- 虚机快照管理：快照列表与一键恢复/删除 ----

let currentSnapshots = [];

async function loadSnapshotList() {
  const el = $("snapshotList");
  el.innerHTML = '<div class="sched-empty">加载中…</div>';
  const vmids = formVmids();
  if (!vmids.length) {
    el.innerHTML = '<div class="sched-empty">请先勾选虚机（VMID）</div>';
    currentSnapshots = [];
    return;
  }
  // 逐台拉取快照（后端接口按单台取数）；多台时行首标注 VMID 便于区分
  const results = await Promise.all(vmids.map(async (vmid) => {
    try {
      return { vmid: String(vmid), snaps: await api(`/guests/${encodeURIComponent(vmid)}/snapshots`) || [] };
    } catch (e) {
      return { vmid: String(vmid), snaps: [] };
    }
  }));
  currentSnapshots = results.flatMap((r) => r.snaps.map((s) => ({ ...s, vmid: r.vmid })));
  renderSnapshotList(results.length > 1);
}

function renderSnapshotList(multi) {
  const el = $("snapshotList");
  if (!currentSnapshots.length) {
    el.innerHTML = '<div class="sched-empty">暂无快照（可在下方创建）</div>';
    return;
  }
  el.innerHTML = '<div class="sched-list">' + currentSnapshots.map((s) => `
    <div class="sched-row">
      ${multi ? `<span class="muted">VM ${escapeHtml(s.vmid)}</span>` : ""}
      <span class="iso-name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
      <span class="muted">${s.description ? escapeHtml(s.description) : ""}</span>
      <span style="flex:1"></span>
      <button class="btn btn-sm" onclick="snapshotAction('rollback',${jsStr(s.name)},${jsStr(s.vmid)})">回滚</button>
      <button class="btn btn-sm danger" onclick="snapshotAction('del',${jsStr(s.name)},${jsStr(s.vmid)})">删除</button>
    </div>`).join("") + "</div>";
}

// 快照列表按钮：直接触发回滚/删除操作（绕过表单直接提交 API）
let currentSnapshotAction = "take";

async function snapshotAction(action, snapname, vmid) {
  // 快照列表行自带 vmid（多台勾选时逐行触发）；未传时回退当前所选（单台）
  vmid = vmid || formVmids()[0] || "";
  if (!vmid) return toast("请先勾选虚机/容器（VMID）", true);
  const warn = action === "rollback"
    ? `确定将虚机 ${vmid} 回滚到快照「${snapname}」吗？当前状态将被丢弃，运行中的虚机会重启。`
    : `确定删除虚机 ${vmid} 的快照「${snapname}」吗？此操作不可恢复。`;
  if (!(await appConfirm(warn, { danger: true }))) return;
  // 直接构造 payload 提交，避免走 runTool 的确认词/表单收集流程
  const nodeSel = $("nodeSelect");
  const payload = {
    params: { action: action, vmid: vmid, snapname: snapname },
  };
  if (nodeSel && nodeSel.value) payload.node = nodeSel.value;
  // 复用工具弹窗的执行态展示
  $("btnRunTool").disabled = true;
  setRunBtnLoading(true);
  syncRunBtnGate();   // 同一执行态：运行中一并锁住「重启生效 / 使直通生效」
  $("btnKillJob").classList.add("hidden");
  $("jobStatus").textContent = "执行中...";
  $("jobStatus").className = "job-status running";
  clearLogPanel();
  setLogVisible(true);
  logOffset = 0;
  try {
    const { job_id } = await api(`/tools/${currentTool.id}/run`, "POST", payload);
    currentRunJobId = job_id;
    $("btnKillJob").classList.remove("hidden");
    pollJob(job_id);
  } catch (e) {
    $("jobStatus").textContent = e.message;
    $("jobStatus").className = "job-status failed";
    $("btnRunTool").disabled = false;
    setRunBtnLoading(false);
    syncRunBtnGate();   // 提交失败：按确认词状态重算各动作按钮
  }
}



