// 硬件直通的收尾动作：IOMMU 直通后的节点重启提示，
// 以及「使直通生效」（关机 → 开机，让 PCIe 直通真正落地）。
//
// 依赖方向：调用 js/ui.js（$、转义、Toast、appConfirm）与 js/api.js（api）；由工具箱的工具执行
// 流程（js/toolbox.js）与 index.html 的内联 onclick（rebootNode / rebootGuestForPassthrough）调用。
// classic script 加载，函数仍在全局作用域。

// ---- IOMMU 直通后的节点重启 ----
let rebootTimer = null;        // 重启倒计时定时器
let rebootWatchTimer = null;   // 重启后探活轮询定时器

function stopRebootCountdown() {
  clearInterval(rebootTimer);
  rebootTimer = null;
}

// 倒计时归零后轮询服务是否恢复：必须先探到一次不可达（确认真的重启了），
// 之后的首次成功响应即刷新页面，避免在 shutdown 真正执行前误判为已恢复
function watchNodeRecovery() {
  clearInterval(rebootWatchTimer);
  let seenDown = false;
  let tries = 0;
  rebootWatchTimer = setInterval(async () => {
    // 上限保护：若重启始终没发生（脚本侧 nohup 失败、节点自己又起来了等），别让轮询无限跑下去
    if (++tries > 200) {   // 200 × 3s ≈ 10 分钟，足够慢速机器完成一次重启
      clearInterval(rebootWatchTimer);
      rebootWatchTimer = null;
      toast("未检测到节点重启完成，请手动刷新页面确认状态", true);
      return;
    }
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 3000);
    try {
      const resp = await fetch("/api/nodes", { signal: ctrl.signal, cache: "no-store" });
      if (!resp.ok) { seenDown = true; return; }
      if (!seenDown) return;   // 节点还没真重启，继续等
      clearInterval(rebootWatchTimer);
      rebootWatchTimer = null;
      location.reload();       // 节点已恢复，自动刷新页面
    } catch (e) {
      seenDown = true;         // 请求失败/超时 = 节点尚未起来，继续等
    } finally {
      clearTimeout(to);
    }
  }, 3000);
}

// 排程成功后 10 秒倒计时，归零提示重启中并开始探活等待自动刷新
function startRebootCountdown(node, doWatch = true) {
  stopRebootCountdown();
  let left = 10;
  const el = $("jobStatus");
  el.className = "job-status running";
  el.innerHTML = `<svg class="ico"><use href="#ico-refresh"></use></svg>${escapeHtml(node)} 将在 ${left} 秒后重启`;
  rebootTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
 stopRebootCountdown();
 if (doWatch) {
 // 仅本机（控制机）重启会令本页失联，才需要探活并自动刷新
 el.innerHTML = `<svg class="ico"><use href="#ico-refresh"></use></svg>${escapeHtml(node)} 正在重启，启动后将自动刷新页面`;
        watchNodeRecovery();
      } else {
        // 远程节点重启不影响本机服务，探活无效（本机 API 不会断），直接提示手动刷新
        el.innerHTML = `<svg class="ico"><use href="#ico-refresh"></use></svg>${escapeHtml(node)} 已重启，稍后请手动刷新页面`;
      }
      return;
    }
    el.innerHTML = `<svg class="ico"><use href="#ico-refresh"></use></svg>${escapeHtml(node)} 将在 ${left} 秒后重启`;
  }, 1000);
}

async function rebootNode() {
  // 重启目标跟随弹窗里的执行节点选择（留空=本机）
  const node = ($("nodeSelect") && $("nodeSelect").value) || localNodeName();
  // 要求原样输入确认词（应用内确认框），而非一键确认：重启整机影响大，防误触/自动化误点
  const ok = await appConfirm(
    `即将重启节点 ${node}，该节点上所有运行中的虚拟机/容器将被中断！（约 10 秒后重启）`,
    { title: "重启节点", okText: "确认重启", danger: true, requireText: "重启节点" });
  if (!ok) return;
  const btn = $("btnRebootNode");
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.classList.add("hidden");
  // 仅本机（控制机）重启会令本页失联，才探活自动刷新；远程节点探活无效，直接提示手动刷新
  startRebootCountdown(node, node === localNodeName());
  try {
    await api(`/nodes/${node}/reboot`, "POST", { confirm: "重启节点" });
    toast(`${node} 将于约 10 秒后重启，重启完成后刷新页面即可`, false);
  } catch (e) {
    // 下发失败：停掉倒计时与探活，并恢复按钮
    stopRebootCountdown();
    clearInterval(rebootWatchTimer);
    rebootWatchTimer = null;
    $("jobStatus").textContent = "";
    $("jobStatus").className = "job-status";
    toast("重启指令下发失败: " + e.message, true);
    btn.dataset.busy = "";
    btn.classList.remove("hidden");
    syncRunBtnGate();
  }
}

// ---- 直通工具：使直通生效（关机 → 开机，让 PCIe 直通真正生效）----
async function rebootGuestForPassthrough() {
  if (!currentTool) return;
  const vmidEl = $("toolForm").querySelector('[data-param="vmid"]');
  const vmid = vmidEl ? vmidEl.value.trim() : "";
  if (!vmid) return toast("请先选择虚机/容器（VMID）", true);
  // 直通只针对 VM（qm）；执行节点默认跟随弹窗所选节点，未选则回退到本机
  const node = ($("nodeSelect") && $("nodeSelect").value) || localNodeName();
  if (!(await appConfirm(`将对该 VM 执行「使直通生效」（依次：关机 → 开机）。\n\n目标：${node} 节点 VM ${vmid}\n\n确定继续吗？`,
                         { title: "使直通生效", okText: "关机并开机", danger: true }))) return;
  const btn = $("btnRebootGuest");
  btn.dataset.busy = "1";   // 标记忙，闸门不再掌管其禁用态
  btn.disabled = true;
  $("jobStatus").textContent = `正在处理 VM ${vmid} ...`;
  $("jobStatus").className = "job-status running";
  try {
    // 第一步：关机（若已在停止状态则跳过）
    let st = await api(`/guests/${node}/qemu/${vmid}/status`);
    if (st && st.status === "running") {
      $("jobStatus").textContent = `正在关机 VM ${vmid} ...`;
      await api(`/guests/${node}/qemu/${vmid}/shutdown`, "POST");
      // 等待关机完成（最多 120 秒）
      for (let i = 0; i < 60; i++) {
 await new Promise((r) => setTimeout(r, 2000));
        try {
          st = await api(`/guests/${node}/qemu/${vmid}/status`);
          if (st && st.status === "stopped") break;
        } catch (e) { /* 查询失败继续等 */ }
      }
      if (!st || st.status !== "stopped") {
        throw new Error("关机超时，VM 未在预期时间内停止，请手动确认状态后重试");
      }
    }
    // 第二步：开机
    $("jobStatus").textContent = `正在开机 VM ${vmid} ...`;
    await api(`/guests/${node}/qemu/${vmid}/start`, "POST");
    $("jobStatus").textContent = `✓ VM ${vmid} 直通已生效`;
    $("jobStatus").className = "job-status";
    toast(`VM ${vmid} 直通已生效`, false);
    setTimeout(refresh, 1500);
  } catch (e) {
    $("jobStatus").textContent = "";
    $("jobStatus").className = "job-status";
    toast("使直通生效失败: " + e.message, true);
  } finally {
    btn.dataset.busy = "";
    syncRunBtnGate();   // 结束后按确认词状态重新决定是否可点（高危工具不因结束而绕过闸门）
  }
}

// 按关键词给日志行着色（所有工具统一）：
// 前置 [TAG] 渲染为「带图标的彩色徽章 + 中性正文」；无 TAG 的行按关键词整行着色兜底；
// 所有行内 PCI 地址（如 0000:00:02.0）统一 muted 等宽。语义色见 style.css 的 --log-*。
const LOG_TAG_LEVEL = {
  "通过": "ok", "成功": "ok", "完成": "ok",
  "失败": "err", "错误": "err", "重要": "err", "必须": "err",
  "警告": "warn", "WARN": "warn",
  "信息": "info", "提示": "info", "备份": "info", "开始": "info", "节点": "info",
};
const LOG_TAG_ICON = {
  "通过": "✓", "成功": "✓", "完成": "✓",
  "失败": "✕", "错误": "✕", "重要": "✕", "必须": "✕",
  "警告": "⚠", "WARN": "⚠",
  "信息": "ℹ", "提示": "ℹ", "备份": "⛁", "开始": "▶", "节点": "◆",
};

// 给已转义文本里的 PCI 地址上 muted 等宽色（如 0000:00:02.0）
function highlightPci(esc) {
  return esc.replace(/\b([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])\b/gi,
    '<span class="log-pci">$1</span>');
}

// 由工具写入的调整虚机内核参数：脚本用 «» 标记，这里上橙色加粗，
// 好与「原本就在的参数」区分（如 quiet splash 灰、nomodeset 橙）
function highlightKarg(esc) {
  return esc.replace(/«([^»]+)»/g, '<span class="log-karg">$1</span>');
}

// 行内高亮的统一入口（内核参数、PCI 地址两类标记互不重叠）
function highlightInline(esc) {
  return highlightPci(highlightKarg(esc));
}

// 结果卡片行：脚本用「▸」标记“完成后输出”的信息（如 vGPU 授权服务器部署结果），
// 逐行渲染成 .log-card 信息条（样式见 style.css），四种形态：
//   ▸▸ 小节标题 | ▸ $ 命令（附「复制」按钮） | ▸ 键: 值（键值对齐） | ▸ 其它说明
function renderLogCard(trimmed) {
  let body = trimmed.replace(/^▸\s?/, "");
  if (body.startsWith("▸")) {   // ▸▸ 小节标题
    return `<span class="log-card log-card--t">${escapeHtml(body.replace(/^▸\s?/, ""))}</span>`;
  }
  const cmd = body.match(/^\$\s*(.+)$/);
  if (cmd) {   // 命令 / 待复制内容（路径也可用此形态）
    // 显示保留「$」前缀（看着像终端里的命令），复制出去的仍是纯命令（不含 $ 与空格）
    const txt = escapeHtml(cmd[1]);
    return `<span class="log-card log-card--cmd"><span class="log-cmd">$ ${txt}</span>` +
           `<button type="button" class="log-copy" data-copy="${txt}" title="复制命令">复制</button></span>`;
  }
  // 键值行：键限 2~20 个「非空白非冒号」字符，避免把 C:\ 这类盘符误认成键
  const kv = body.match(/^([^\s:：]{2,20})[：:][ \t]*(.+)$/);
  if (kv) {
    return `<span class="log-card"><span class="log-card-key">${escapeHtml(kv[1])}</span>` +
           `<span class="log-card-val">${highlightInline(escapeHtml(kv[2]))}</span></span>`;
  }
  return `<span class="log-card">${highlightInline(escapeHtml(body))}</span>`;
}

// 复制文本：优先 clipboard API（HTTPS / localhost 可用），HTTP 访问下面板拿不到该 API，回退 execCommand
async function copyTextToClipboard(text) {
  if (!text) return false;
  try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* 回退 */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}

// 日志卡片「复制」按钮：工具弹窗（logPanel，逐行追加）与执行日志弹窗（jobFullLog，整段重绘）
// 两处都渲染卡片，故统一用 document 级委托绑定一次（节点被重绘/裁剪也不失效）
document.addEventListener("click", (ev) => {
  const btn = ev.target && ev.target.closest ? ev.target.closest(".log-copy") : null;
  if (!btn) return;
  copyTextToClipboard(btn.dataset.copy || "")
    .then((ok) => toast(ok ? "已复制到剪贴板" : "复制失败，请手动选择后复制", !ok));
});

function colorLog(line) {
  const esc = escapeHtml(line);
  // 阶段大标题：===== xxx ===== / ### xxx
  if (/^(={3,}|#{3,})/.test(line.trim())) return `<span class="log-title">${esc}</span>`;
  // 结果卡片行（见 renderLogCard）：以「▸」开头
  if (line.trim().startsWith("▸")) return renderLogCard(line.trim());
  // 步骤进度：[1/6] xxx
  const step = line.match(/^\[(\d+\/\d+)\]\s?(.*)$/);
  if (step) return `<span class="log-step">[${step[1]}]</span> ${highlightInline(escapeHtml(step[2]))}`;
  // 前置标签：[TAG] 正文 → 彩色徽章 + 中性正文
  const tag = line.match(/^\[([^\]]+)\]\s?(.*)$/);
  if (tag) {
    const lvl = LOG_TAG_LEVEL[tag[1]];
    if (lvl) {
      const icon = LOG_TAG_ICON[tag[1]] ? LOG_TAG_ICON[tag[1]] + " " : "";
      const badge = `<span class="log-badge log-${lvl}">${icon}${escapeHtml(tag[1])}</span>`;
      return `${badge} ${highlightInline(escapeHtml(tag[2]))}`;
    }
    // [*] 常规日志：用中性白（继承日志面板默认文字色），与彩色告警/错误/步骤区分
    if (tag[1] === "*") return highlightInline(esc);
    // 其它未登记标签（如 [guest]）：整条步骤色，但其中的内核参数 / PCI 地址仍要高亮
    return `<span class="log-step">${highlightInline(esc)}</span>`;
  }
  // 无前置标签：按关键词整行着色兜底
  if (/错误|失败(?![:\s]*0)|error|✗|无法|not found|异常|报错|重要|必须/i.test(line)) {
    return `<span class="log-err">${highlightInline(esc)}</span>`;
  }
  if (/警告|warn|注意/i.test(line)) return `<span class="log-warn">${highlightInline(esc)}</span>`;
  if (/成功|完成|✓|done/i.test(line)) return `<span class="log-ok">${highlightInline(esc)}</span>`;
  if (/提示|备份|backup|信息/i.test(line)) return `<span class="log-info">${highlightInline(esc)}</span>`;
  return highlightInline(esc);
}

async function runTool() {
  if (!currentTool) return;
  // 收集表单参数
  const params = {};
  const checkboxAcc = {};  // 选框组：同名多选框累计勾选项，最后合并为逗号分隔值
  // 只收集真正的表单控件（input/select/textarea）；选框组容器 div 也带 data-param（供 fillDynamicOptions 用），
  // 但其 .value 为 undefined，若一并收集会在 el.value.trim() 处抛错，故这里显式排除
  document.querySelectorAll("#toolForm input[data-param], #toolForm select[data-param], #toolForm textarea[data-param]").forEach((el) => {
    // 被联动置灰的禁用字段不收残留值，避免误传给服务端；
    // 特殊：action 被禁用的工具按各自固定值兜底（快照=创建快照，定时开关机=添加）
    if (el.dataset.param === "action" && el.disabled) {
      if (currentTool && currentTool.id === "vm_snapshot") params["action"] = currentSnapshotAction;
      else if (currentTool && currentTool.id === "vm_schedule") params["action"] = "add";
      return;
    }
    // 选框：勾选的累计到同名组；禁用选框保持空值（仅当该组尚未累计到勾选项）
    if (el.type === "checkbox") {
      const cn = el.dataset.param;
      if (el.disabled) {
        if (!(cn in checkboxAcc)) params[cn] = "";
        return;
      }
      if (el.checked) (checkboxAcc[cn] ||= []).push(el.value);
      return;
    } else if (el.multiple) {
      // 多选下拉（批量删除等）：收集所有选中项，逗号分隔提交
      const vals = [...el.selectedOptions].map((o) => o.value.trim()).filter(Boolean);
      params[el.dataset.param] = el.disabled ? "" : vals.join(",");
    } else {
      params[el.dataset.param] = el.disabled ? "" : el.value.trim();
    }
  });
  // 选框组：把累计的勾选项合并为逗号分隔值（未勾选则保持空串）
  for (const [cn, vals] of Object.entries(checkboxAcc)) params[cn] = vals.join(",");
  // 一键部署 k8s：架构形态对勾选虚机数量有硬性要求（后端脚本同样拦截，这里先本地即时反馈，免得白跑一趟）
  if (currentTool && currentTool.id === "vm_install_kubeadm") {
    const ka = params["arch"] || "allinone";
    const kc = (params["vmid"] || "").split(",").map((s) => s.trim()).filter(Boolean).length;
    if (ka === "ha" && kc !== 3) {
      toast(`高可用集群需恰好勾选 3 台虚机（当前已选 ${kc} 台）`, true);
      return;
    }
    if (ka === "allinone" && kc !== 1) {
      toast(`单机 All-in-One 仅支持勾选 1 台虚机（当前已选 ${kc} 台）`, true);
      return;
    }
    if (ka === "expand" && kc < 1) {
      toast(`节点扩容需勾选至少 1 台虚机`, true);
      return;
    }
  }
  // IP 获取方式联动校验：选了「自定义静态 IP」却没填 IP 地址时拦截，避免静默降级为 DHCP
  const ipmSel = document.querySelector("#toolForm select[data-ipmode]");
  if (ipmSel && ipmSel.value === "static") {
    const ipMain = document.querySelector(`#toolForm [data-param="${ipmSel.dataset.ipmode}"]`);
    const ipVal = (ipMain && ipMain.value.trim()) || "";
    if (!ipVal) {
      toast(`已选择「自定义静态 IP」，请填写「IP地址」（CIDR 格式，如 192.168.1.10/24）`, true);
      if (ipMain) { ipMain.focus(); ipMain.classList.add("input-error"); }
      return;
    }
  }
  // autotext 复合控件：选了「自定义」却没填值 → 拦截，避免静默回落默认行为
  for (const sel of document.querySelectorAll("#toolForm select[data-autoselect]")) {
    if (sel.value !== "custom") continue;
    const p = (currentTool.params || []).find((x) => x.name === sel.dataset.autoselect);
    const inp = $("toolForm").querySelector(`[data-param="${sel.dataset.autoselect}"]`);
    if (p && inp && !inp.value.trim()) {
      toast(`已选「${p.custom_label || "自定义"}」，请填写「${p.label}」`, true);
      inp.focus();
      inp.classList.add("input-error");
      return;
    }
  }
  // 提交前最终校验：检查所有带 pattern 的参数
  for (const p of (currentTool.params || [])) {
    const val = (params[p.name] || "").trim();
    if (!val && !p.required) continue;   // 非必填留空跳过
    if (p.pattern) {
      const re = new RegExp("^(?:" + p.pattern + ")$");
      if (!re.test(val)) {
        toast(`${p.label}：${p.errmsg || p.hint || "格式不正确"}`, true);
        const el = $("toolForm").querySelector(`[data-param="${p.name}"]`);
        if (el) { el.focus(); el.classList.add("input-error"); }
        return;
      }
    }
  }
  const effRisk = effectiveRisk(currentTool);
  // 高危确认词：提交前本地先比一次（与服务端同口径，忽略空白与大小写），
  // 不一致就直接聚焦输入框提示，避免白跑一趟接口才拿到报错
  if (effRisk === "high" && normConfirm($("confirmInput").value) !== normConfirm(currentTool.name)) {
    const ci = $("confirmInput");
    if (ci) { ci.focus(); syncConfirmState(); ci.classList.add("input-error"); }
    toast(`请完整输入「${currentTool.name}」以确认（空格与大小写可忽略）`, true);
    return;
  }
  const payload = { params };
  const nodeSel = $("nodeSelect");
  if (nodeSel && nodeSel.value) payload.node = nodeSel.value;
  if (effRisk === "high") payload.confirm = $("confirmInput").value.trim();
  // 风险分级不变：只读直接执行；高危已由「完整输入工具名」这一强确认把关（GitHub / Vercel / AWS
  // 同款范式），不再叠加第二次询问；「普通」统一走应用内确认框——不再弹浏览器原生 confirm，
  // 免得同一个位置一会儿是系统弹窗、一会儿是应用内确认词，风格割裂。
  if (effRisk !== "info" && effRisk !== "high"
      && !(await appConfirm(`确定执行「${currentTool.name}」吗？`, { title: "执行确认" }))) return;

  $("btnRunTool").disabled = true;
  setRunBtnLoading(true);
  syncRunBtnGate();   // 立即把「运行中」施加到「重启生效 / 使直通生效」：执行期间它们不该能点
  $("btnKillJob").classList.add("hidden");
  $("jobStatus").textContent = "执行中...";
  $("jobStatus").className = "job-status running";
  clearLogPanel();
  setLogVisible(true);
  logOffset = 0;

  try {
    const { job_id } = await api(`/tools/${currentTool.id}/run`, "POST", payload);
    currentRunJobId = job_id;
    $("btnKillJob").classList.remove("hidden"); // 执行期间随时可强杀卡住的任务
    pollJob(job_id);
  } catch (e) {
    $("jobStatus").textContent = e.message;
    $("jobStatus").className = "job-status failed";
    setRunBtnLoading(false);
    syncRunBtnGate();   // 提交失败后按确认词状态重新决定按钮是否可点
  }
}

let currentRunJobId = null; // 工具弹窗当前执行中的任务，供强杀按钮定位

// 工具弹窗内的强杀按钮：杀掉当前执行中的任务（含其子进程），轮询会自动收到失败态
async function killCurrentJob() {
  if (!currentRunJobId) return;
  if (!(await appConfirm("确定强制结束当前任务吗？脚本及其拉起的子进程（如 apt）会被立即杀死，已执行的部分不会回滚。",
                         { title: "强制结束任务", okText: "强制结束", danger: true }))) return;
  try {
    await api(`/jobs/${currentRunJobId}/kill`, "POST", {});
    toast("强杀信号已发送");
  } catch (e) {
    toast("强杀失败: " + e.message, true);
  }
}

// 执行按钮 loading 态：文案前加转圈图标，恢复时还原为 ▶；
// 同时用 dataset.busy 标记执行中，供 syncRunBtnGate 判断（运行期间不得被输入事件解锁）
function setRunBtnLoading(on) {
  const btn = $("btnRunTool");
  btn.dataset.busy = on ? "1" : "";
  btn.innerHTML = on
    ? '<span class="spinner"></span>执行中'
    : "▶ 执行";
}

// 执行成功后自动刷新当前工具表单内的所有动态下拉：扫描表单实际用到的
// data-options-from 源，依赖虚机缓存者先穿透刷新 allGuests，再逐个 fillDynamicOptions，
// 免去为每个工具手工写刷新分支
async function autoRefreshFormSources() {
  const sources = new Set();
  document.querySelectorAll("#toolForm select[data-options-from]")
    .forEach((el) => sources.add(el.dataset.optionsFrom));
  if (sources.has("guests") || sources.has("guests_all")) {
    try { allGuests = await api("/guests?fresh=1"); } catch (_) {}
  }
  for (const s of sources) {
    try { await fillDynamicOptions(s); } catch (_) {}
  }
}

// 日志增量渲染：逐行建 DOM 节点追加（替代 innerHTML += 整段重解析，消除长日志 O(n²) 卡顿），
// 并用定长环缓冲裁掉最旧行，把 DOM 规模与内存恒定在 LOG_MAX_LINES 内（几千行任务也不会越滚越卡）。
const LOG_MAX_LINES = 3000;

// 日志原文缓冲：DOM 会按 LOG_MAX_LINES 环缓冲裁剪，而「复制日志」要的是完整原文，故另存一份纯文本。
// 只存字符串引用，开销远小于 DOM；与后端内存中的任务日志同量级。
let logRawLines = [];

// 日志面板与标题条（含复制按钮）同进同退：开关只认这一个入口，避免面板与标题条状态错位
function setLogVisible(on) {
  const wrap = $("logWrap");
  if (wrap) wrap.classList.toggle("hidden", !on);
}

// 清空日志（新任务开始 / 切换工具时调用）：面板 DOM、原文缓冲、行数一并复位
function clearLogPanel() {
  const panel = $("logPanel");
  if (panel) panel.textContent = "";
  logRawLines = [];
  const cnt = $("logCount");
  if (cnt) cnt.textContent = "";
}

// 一键复制本次执行的全部日志原文（纯文本，不含渲染用的 HTML 标签）
async function copyCurrentLog() {
  if (!logRawLines.length) { toast("暂无可复制的日志", true); return; }
  const ok = await copyTextToClipboard(logRawLines.join("\n"));
  toast(ok ? `已复制 ${logRawLines.length} 行日志` : "复制失败，请手动选择日志后复制", !ok);
}

function appendLogLines(lines) {
  const panel = $("logPanel");
  if (!panel || !lines || !lines.length) return;
  const frag = document.createDocumentFragment();
  for (const raw of lines) {
    logRawLines.push(raw);
    const div = document.createElement("div");
    div.className = "log-line";
    div.innerHTML = colorLog(raw);
    frag.appendChild(div);
  }
  panel.appendChild(frag);
  // 环缓冲裁剪：超出上限时丢弃最旧行（保留最近 LOG_MAX_LINES 行）
  let over = panel.childElementCount - LOG_MAX_LINES;
  while (over-- > 0 && panel.firstElementChild) panel.removeChild(panel.firstElementChild);
  panel.scrollTop = panel.scrollHeight;
  const cnt = $("logCount");
  if (cnt) cnt.textContent = `（${logRawLines.length} 行）`;
}

// 任务日志实时流：SSE 替代 800ms 轮询；断流自动回退轮询，保证日志不丢
let jobStream = null;        // 当前任务的 EventSource 连接

async function pollJob(jobId) {
  // 先拉一次已存在的历史日志，再把 SSE 起点对齐到当前偏移，避免重复输出
  try {
    const snap = await api(`/jobs/${jobId}?offset=0`);
    if (snap && snap.lines.length) {
      appendLogLines(snap.lines);
      $("logPanel").scrollTop = $("logPanel").scrollHeight;
      logOffset = snap.offset;
    }
    if (snap && snap.status !== "running") { finishJob(snap); return; }
  } catch (e) { /* 首拉失败也继续尝试 SSE */ }
  openJobStream(jobId);
}

function openJobStream(jobId) {
  if (jobStream) { try { jobStream.close(); } catch (_) {} }
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  // from=logOffset：SSE 只回放连接之后的新增行，与首拉不重复
  if (typeof EventSource === "undefined") { pollJobFallback(jobId); return; }
  jobStream = new EventSource(`/api/jobs/${jobId}/stream?from=${logOffset}`);
  jobStream.onmessage = (e) => {
    appendLogLines([e.data]);
    $("logPanel").scrollTop = $("logPanel").scrollHeight;
    logOffset += 1;   // 同步偏移：SSE 断流回退轮询时不重复已展示的行
  };
  jobStream.addEventListener("done", (e) => {
    if (jobStream) { try { jobStream.close(); } catch (_) {} jobStream = null; }
    let snap = {};
    try { snap = JSON.parse(e.data); } catch (_) {}
    finishJob({ status: snap.status, returncode: snap.returncode,
                tool_id: currentTool ? currentTool.id : null, ended_at: Date.now() / 1000 });
  });
  jobStream.onerror = () => {
    // SSE 中断（如服务重启）：关闭连接并回退到轮询，保证日志不丢
    if (jobStream) { try { jobStream.close(); } catch (_) {} jobStream = null; }
    pollJobFallback(jobId);
  };
}

async function pollJobFallback(jobId) {
  try {
    const snap = await api(`/jobs/${jobId}?offset=${logOffset}`);
    if (snap && snap.lines.length) {
      appendLogLines(snap.lines);
      $("logPanel").scrollTop = $("logPanel").scrollHeight;
      logOffset = snap.offset;
    }
    if (snap && snap.status !== "running") { finishJob(snap); return; }
  } catch (e) {}
  if (currentRunJobId === jobId)
    pollTimer = setTimeout(() => pollJobFallback(jobId), 800);
}

function finishJob(snap) {
  const ok = snap.status === "success";
  // 执行成功后刷新前置工具完成状态，下次打开依赖它的工具不再提示
  if (ok && toolboxData && snap.tool_id) {
    toolboxData.recommend_status = toolboxData.recommend_status || {};
    toolboxData.recommend_status[snap.tool_id] = snap.ended_at || Date.now() / 1000;
  }
  // 定时开关机执行成功后同步刷新内嵌任务列表
  if (ok && currentTool && currentTool.id === "vm_schedule") loadScheduleList();
  // 备份/还原执行成功后刷新备份文件下拉 + 模板来源下拉
  if (ok && currentTool && currentTool.id === "vm_backup") {
    fillDynamicOptions("backups");
    fillDynamicOptions("deploy_sources");
  }
  // 快照管理执行成功后刷新快照列表
  if (ok && currentTool && currentTool.id === "vm_snapshot") loadSnapshotList();
  // 自动刷新：执行成功后刷新当前工具表单内的所有动态下拉（虚机/备份/模板源等）
  if (ok && currentTool) autoRefreshFormSources();
  // 磁盘导出成功后：刷新常驻「历史导出」面板（导出后新文件立即可见/可管理），并滚动到面板
  if (ok && currentTool && currentTool.id === "disk_export") {
    loadExportHistory();
    const ph = document.getElementById("exportHistoryPanel");
    if (ph) ph.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  const name = currentTool ? currentTool.name : "任务";
  $("jobStatus").textContent = ok ? "✓ 执行成功" : `✗ 执行失败 (code ${snap.returncode ?? "?"})`;
  $("jobStatus").className = "job-status " + (ok ? "success" : "failed");
  setRunBtnLoading(false);
  // 高危：一次确认只授权一次执行（AWS 删桶同款），任务结束后清空确认词，让「执行」重新锁定
  if (currentTool && effectiveRisk(currentTool) === "high") {
    const ci = $("confirmInput");
    if (ci) { ci.value = ""; syncConfirmState(); }
  }
  // 记录「本次弹窗会话里该工具已成功跑过」：确认词刚被上面清空，但脚本常以「请点弹窗内『重启生效』
  // 按钮」收尾，该按钮据此仍保持可点（见 syncRunBtnGate 的 GATE_REPEAT_OK_BTNS）。
  // 失败则清掉，不把这次授权留在一次没跑成的执行上。
  lastOkToolId = (ok && currentTool) ? currentTool.id : null;
  syncRunBtnGate();   // 任务结束后按确认词状态重新决定按钮是否可点
  $("btnKillJob").classList.add("hidden");
  currentRunJobId = null;
  // 有工具带「完成后自动重启」勾选（如 vGPU 解锁的阶段一）时，脚本会延迟重启宿主机，本页随之中断：
  // 先探到不可达、等节点回来再自动刷新。但勾了 ≠ 一定会重启——脚本按实际状态判定，只有确实需要时才
  // 重启，并以「已勾选自动重启」这行告知；不核对这行就会在「本次无需重启」时白等（探不到 down）。
  // 另外只在目标节点就是本机时探活：跨节点执行本页不会失联，同样永远等不到「down」。
  if (ok) {
    const rnEl = $("toolForm").querySelector('input[data-param="reboot_now"]');
    const willReboot = !!(rnEl && rnEl.checked && $("logPanel").textContent.includes("已勾选自动重启"));
    if (willReboot) {
      const rnNode = ($("nodeSelect") && $("nodeSelect").value) || localNodeName();
      const isLocal = rnNode === localNodeName();
      $("jobStatus").textContent = `✓ 执行成功（${rnNode} 约 8 秒后自动重启）`;
      $("jobStatus").className = "job-status running";
      toast(isLocal ? `已勾选自动重启：${rnNode} 约 8 秒后重启，本页短暂失联后自动刷新`
                    : `已勾选自动重启：${rnNode} 约 8 秒后重启，稍后刷新即可`, false);
      if (isLocal) watchNodeRecovery();
      return;
    }
  }
  toast(ok ? `「${name}」执行成功` : `「${name}」执行失败`, !ok);
}
