// 概览页：节点卡片 / 虚机容器列表 / 分页与筛选 / 批量操作 / 数据加载。
//
// 依赖方向：调用 js/ui.js（$、格式化、转义、Toast）与 js/api.js（api/cachedApi），读写
// js/state.js 的全局状态；由 app.js 的初始化流程与 index.html 的内联 onclick 调用。
// classic script 加载，函数仍在全局作用域（内联 onclick 依赖这一点）。

// ---- 渲染 ----

// 存储类型的友好名，未收录的直接显示原值
const STORAGE_TYPES = {
  dir: "目录", lvm: "LVM", lvmthin: "LVM-Thin", zfspool: "ZFS",
  nfs: "NFS", cifs: "CIFS", cephfs: "CephFS", rbd: "Ceph RBD",
  btrfs: "Btrfs", pbs: "PBS", iscsi: "iSCSI",
};

// 磁盘介质分类：NVMe 总线归 M.2，ROTA 区分机械盘 HDD / 固态盘 SSD，未知归空
function diskCategory(d) {
  if (d.tran === "nvme") return "M.2";
  const rot = [true, "1", 1], ssd = [false, "0", 0];
  if (rot.includes(d.rota)) return "HDD";
  if (ssd.includes(d.rota)) return "SSD";
  return "";
}

// 单块物理盘的用量条行：盘名 + 介质标签(HDD/SSD/M.2) + 温度 + 状态与已用量
function diskMeterRow(d, n) {
  const ratio = d.size ? d.used / d.size : 0;
  const pct = (ratio * 100).toFixed(1);
  const unused = d.state === "空闲";
  // 介质标签：行内直接标 HDD/SSD/M.2，未知介质不出标签
  const media = diskCategory(d);
  const mediaTag = media ? `<i class="disk-type">${media}</i>` : "";
  // 磁盘温度：smartctl 读得到才显示（虚拟盘通常无数据）。
  // 超过该介质的安全线才上色：HDD 50/60℃、SSD 60/75℃、NVMe 55/70℃、介质未知按 60/80℃
  const temp = n.temps && n.temps.disks ? n.temps.disks[d.name] : null;
  const tempKind = media === "HDD" ? "hdd" : (media === "M.2" ? "nvme" : (media === "SSD" ? "ssd" : "default"));
  const tempCls = temp != null ? tempClass(temp, tempKind) : "";
  const tempTag = temp != null
    ? `<i class="disk-type${tempCls ? " " + tempCls : ""}" title="该介质的安全线：${TEMP_THRESHOLDS[tempKind].join(" / ")}℃"><svg class="ico ico-sm"><use href="#ico-temp"></use></svg>${temp}°C</i>`
    : "";
  // 在用盘行：只留「已用 / 总量 · 使用率」——不再重复标「使用中」（数据本身已经说明在用）；
  // 只有需要特别注意的才留状态字：被 VM 直通(RDM/PCIe)占用的盘标「直通占用」（橙，与网卡直通统一），
  // 完全空闲的盘标「空闲」
  const pt = d.state === "直通占用";
  const usage = `${fmtBytes(d.used)} / ${fmtBytes(d.size)} · <b class="pct ${barClass(ratio)}">${pct}%</b>`;
  const right = unused
    ? '<b class="disk-idle">空闲</b>'
    : (pt ? `<span><b class="pt">直通占用</b> · ${usage}</span>` : `<span>${usage}</span>`);
  return `
  <div class="meter disk-meter">
    <div class="label"><span>/dev/${escapeHtml(d.name)}${mediaTag}${tempTag}</span>${right}</div>
    <div class="bar"><i class="${unused ? "" : barClass(ratio)}" style="width:${unused ? 0 : pct}%"></i></div>
  </div>`;
}

// 节点卡片的硬盘区：以物理盘为准（含空闲盘）；拉不到物理盘时降级显示 PVE 存储
function renderDisks(n) {
  // 掉盘告警：后端对比前后两次枚举得出，置顶红色警示，优先级最高
  const lostRows = (n.disks_lost || [])
    .map((d) => `<div class="disk-lost"><svg class="ico ico-sm"><use href="#ico-warn"></use></svg>掉盘告警：/dev/${escapeHtml(d)} 已消失，请检查磁盘链路/供电</div>`)
    .join("");
  const phys = n.phys_disks || [];
  if (phys.length) {
    const expanded = expandedDisks.has(n.node);
    // 逐行展示，介质类型以行内标签(HDD/SSD/M.2)区分；默认露前 3 盘，更多折叠
    const shown = expanded ? phys : phys.slice(0, 3);
    const html = shown.map((d) => diskMeterRow(d, n)).join("");
    const more = phys.length - shown.length;
    const toggle = more > 0
      ? `<div class="disk-toggle" onclick="toggleDisks(event,${jsStr(n.node)})"><svg class="ico ico-sm ico-chev${expanded ? " open" : ""}"><use href="#ico-chevron"></use></svg>${expanded ? "收起磁盘" : `还有 ${more} 块磁盘`}</div>`
      : "";
    return lostRows + html + toggle;
  }
  // ---- 降级：PVE 存储用量，默认露出前 2 块，更多折叠 ----
  const disks = n.disks || [];
  if (!disks.length) return lostRows;
  const expanded = expandedDisks.has(n.node);
  const shown = expanded ? disks : disks.slice(0, 2);
  const meters = shown.map((d) => {
    const ratio = d.maxdisk ? d.disk / d.maxdisk : 0;
    const pct = fmtPct(ratio);
    const type = d.type ? `<i class="disk-type">${escapeHtml(STORAGE_TYPES[d.type] || d.type)}</i>` : "";
    return `
      <div class="meter disk-meter">
        <div class="label"><span>${escapeHtml(d.storage)}${type}</span><span>${fmtBytes(d.disk)} / ${fmtBytes(d.maxdisk)} · <b class="pct ${barClass(ratio)}">${pct}%</b></span></div>
        <div class="bar"><i class="${barClass(ratio)}" style="width:${pct}%"></i></div>
      </div>`;
  }).join("");
  const more = disks.length - 2;
  const toggle = more > 0
    ? `<div class="disk-toggle" onclick="toggleDisks(event,${jsStr(n.node)})"><svg class="ico ico-sm ico-chev${expanded ? " open" : ""}"><use href="#ico-chevron"></use></svg>${expanded ? "收起磁盘" : `还有 ${more} 块磁盘`}</div>`
    : "";
  return lostRows + meters + toggle;
}

// 展开/收起磁盘列表；阻断冒泡，别误触卡片的节点筛选
function toggleDisks(ev, node) {
  ev.stopPropagation();
  expandedDisks.has(node) ? expandedDisks.delete(node) : expandedDisks.add(node);
  renderNodes(allNodes);
}

// CPU 型号去噪：去掉 (R)/(TM)/CPU/@频率尾巴，标签里更紧凑
function shortCpuModel(model) {
  return model.replace(/\((R|TM)\)/gi, "").replace(/\s*CPU\s*/i, " ")
    .replace(/\s*@.*$/, "").replace(/\s+with.*$/i, "").replace(/\s+/g, " ").trim();
}

// 显卡描述去噪：去掉类别前缀（VGA compatible controller [0300]:）、公司后缀、vendor:device 编号、修订号
function shortGpuDesc(desc) {
  return (desc || "")
    .replace(/^[0-9a-fA-F:.]+\s+/, "")                 // 去 PCI 地址（已在 addr 字段，desc 里通常没有）
    .replace(/^(VGA|3D|Display|Display controller|VGA compatible controller).*?:/, "")  // 去类别前缀
    .replace(/\s+\[[0-9a-fA-F]{4}(:[0-9a-fA-F]{4})?\]/g, "")   // 去 vendor:device 编号
    .replace(/\s+\(rev\s+[0-9a-fA-F]+\)/g, "")         // 去修订号
    .replace(/\bCorporation\b|\bInc\.?|\bCo\.,?\s*Ltd\.?/g, "") // 去公司后缀
    .replace(/\s+/g, " ").trim();
}

// 节点系统信息标签行：PVE 版本/内核/主板/芯片组/显卡（CPU 信息就近放在 CPU 用量条处）
function renderNodeTags(n) {
  const tags = [];
  if (n.pveversion) tags.push("PVE " + n.pveversion);
  if (n.kversion) tags.push("内核 " + n.kversion);
  // 主板/芯片组：探测失败/虚机时字段为空，自然不展示；芯片组去掉 LPC/eSPI 尾巴更紧凑
  const board = [n.board_vendor, n.board_product].filter(Boolean).join(" ");
  if (board) tags.push("主板 " + board);
  if (n.chipset) tags.push("芯片组 " + n.chipset.replace(/\s+LPC.*$/, ""));
  if (!tags.length) return "";
  return `<div class="node-tags">${tags.map((t) => `<span class="node-tag">${escapeHtml(t)}</span>`).join("")}</div>`;
}

// dmidecode 主频形如 "2600 MHz"，整 GHz 时换算更易读（2600 → 2.6 GHz）
function fmtCpuSpeed(s) {
  const m = /^(\d+)\s*MHz$/i.exec(s || "");
  if (!m) return s || "";
  return `${parseFloat((Number(m[1]) / 1000).toFixed(2))} GHz`;
}

// CPU 插槽行：首行插槽占用，随后逐槽列出编号/型号/主频/核数；返回行数组，折叠由 renderCpuDetails 统一处理
function cpuSocketRows(n) {
  const socks = Array.isArray(n.cpu_sockets) ? n.cpu_sockets : [];
  // 无插槽信息（虚机/dmidecode 不可用）时退回型号+主频单行，避免 CPU 块空白
  if (!socks.length) {
    if (!n.cpu_model) return [];
    const parts = [shortCpuModel(n.cpu_model)];
    if (n.cpu_mhz && parseFloat(n.cpu_mhz) > 0) parts.push((parseFloat(n.cpu_mhz) / 1000).toFixed(2) + " GHz");
    return [`<div class="cpu-sub">${escapeHtml(parts.join(" · "))}</div>`];
  }
  const used = n.cpu_socket_used != null ? n.cpu_socket_used : socks.filter((s) => s.populated).length;
  const total = n.cpu_socket_total || socks.length;
  const rows = [`<div class="cpu-sub"><svg class="ico ico-sm"><use href="#ico-slot"></use></svg>CPU 插槽　已用 ${used}/共 ${total}</div>`];
  // 单插槽不显示槽位号（Socket Designation 常是无意义的编号如 U3E1）；多插槽用 CPU1/CPU2 区分
  const showLoc = total > 1;
  rows.push(...socks.map((s, idx) => {
    const loc = showLoc ? `<span class="mem-loc">CPU${idx + 1}</span>　` : "";
    if (!s.populated) return `<div class="cpu-sub cpu-socket">${loc}<span class="muted">空</span></div>`;
    const seg = [s.model ? shortCpuModel(s.model) : "", s.speed ? fmtCpuSpeed(s.speed) : "", s.cores ? `${s.cores} 核` : "", s.threads ? `${s.threads} 线程` : ""].filter(Boolean).join(" · ");
    return `<div class="cpu-sub cpu-socket">${loc}${escapeHtml(seg)}</div>`;
  }));
  return rows;
}

// 温度分档：返回 t-warn / t-crit / 空串（正常温度不上色，整屏才看得出哪里不对）
// kind 取 TEMP_THRESHOLDS 的键：cpu / hdd / ssd / nvme / default
function tempClass(t, kind = "default") {
  const th = TEMP_THRESHOLDS[kind] || TEMP_THRESHOLDS.default;
  if (t >= th[1]) return "t-crit";
  if (t >= th[0]) return "t-warn";
  return "";
}

// CPU 块监控行：统一「图标 + 标签 + 数值」格式，返回行数组供统一折叠
// 行序：插槽（cpuSocketRows 提供）→ 频率 → 温度 → 负载（按关注度递减）
// 频率读每核 sysfs scaling_cur_freq（intel_pstate 下为实时频率），温度读 sensors 整包温度
function cpuDetailLines(n) {
  const lines = [];
  if (n.cpu_freq && n.cpu_freq.cores > 0 && n.cpu_freq.avg) {
    const ghz = (m) => (m / 1000).toFixed(2) + " GHz";
    lines.push(`<div class="cpu-sub"><svg class="ico ico-sm"><use href="#ico-bolt"></use></svg>CPU 频率　平均 <b>${ghz(n.cpu_freq.avg)}</b></div>`);
  }
  if (n.temps && n.temps.cpu != null) {
    lines.push(`<div class="cpu-sub"><svg class="ico ico-sm"><use href="#ico-temp"></use></svg>CPU 温度　<b class="${tempClass(n.temps.cpu, "cpu")}">${n.temps.cpu}°C</b></div>`);
  }
  if (Array.isArray(n.loadavg) && n.loadavg.length === 3) {
    lines.push(`<div class="cpu-sub"><svg class="ico ico-sm"><use href="#ico-load"></use></svg>系统负载　<b>${n.loadavg[0]} / ${n.loadavg[1]} / ${n.loadavg[2]}</b>　<span class="muted">(1/5/15分钟)</span></div>`);
  }
  return lines;
}

// CPU 明细汇总：插槽行 + 负载行统一成行列表，默认露前 3 行、超出折叠，
// 与硬盘/网卡的折叠口径一致
function renderCpuDetails(n) {
  const rows = [...cpuSocketRows(n), ...cpuDetailLines(n)];
  if (!rows.length) return "";
  const expanded = expandedCpu.has(n.node);
  const shown = expanded ? rows : rows.slice(0, 3);
  let html = shown.join("");
  const more = rows.length - shown.length;
  if (more > 0) {
    html += `<div class="mem-toggle" onclick="toggleCpu(event,${jsStr(n.node)})"><svg class="ico ico-sm ico-chev${expanded ? " open" : ""}"><use href="#ico-chevron"></use></svg>${expanded ? "收起 CPU 信息" : `还有 ${more} 条 CPU 信息`}</div>`;
  }
  return html;
}

// 展开/收起 CPU 明细；阻断冒泡，别误触卡片的节点筛选
function toggleCpu(ev, node) {
  ev.stopPropagation();
  expandedCpu.has(node) ? expandedCpu.delete(node) : expandedCpu.add(node);
  renderNodes(allNodes);
}

// dmidecode 的容量形如 "16384 MB"，整 GB 时换算成 GB 更易读
function fmtMemSize(s) {
  const m = /^(\d+)\s*MB$/i.exec(s || "");
  if (m && Number(m[1]) >= 1024 && Number(m[1]) % 1024 === 0) return `${Number(m[1]) / 1024} GB`;
  return s || "";
}

// 内存块明细：首行类型/插槽占用，随后逐条列出每根内存的插槽位/容量/品牌/型号/频率
// expanded: 是否展开全部内存条；默认只露前 2 根，更多折叠
function memInfoLine(n, expanded) {
  const head = [];
  // 槽位句式四类模块统一：插槽名 + 占用 X/共 Y，内存类型跟在后面；图标与 CPU 插槽行保持同一视觉
  if (n.mem_modules && n.mem_slots && n.mem_slots >= n.mem_modules) {
    head.push(`内存插槽　已用 ${n.mem_modules}/共 ${n.mem_slots}`);
  }
  if (n.mem_type) head.push(n.mem_type);
  const headIcon = head.length ? '<svg class="ico ico-sm"><use href="#ico-slot"></use></svg>' : "";
  let html = head.length ? `<div class="cpu-sub">${headIcon}${escapeHtml(head.join(" · "))}</div>` : "";
  const sticks = Array.isArray(n.mem_sticks) ? n.mem_sticks : [];
  if (sticks.length) {
    const shown = expanded ? sticks : sticks.slice(0, 2);
    html += shown.map((s) => {
      // 逐条：插槽位 + 容量 · 品牌 · 频率；缺字段自然跳过（料号无用不展示）
      // 容量是这一行唯一的关键值，用主文字色突出；插槽位只加粗对位，与其它明细行保持同一配色规则
      const cap = fmtMemSize(s.size);
      const seg = [s.maker, s.speed].filter(Boolean).join(" · ");
      const locTxt = [s.locator, s.bank].filter(Boolean).join(" · ");
      const loc = locTxt ? `<span class="mem-loc">${escapeHtml(locTxt)}</span>\u3000` : "";
      return `<div class="cpu-sub mem-stick">${loc}`
        + [cap ? `<b>${escapeHtml(cap)}</b>` : "", escapeHtml(seg)].filter(Boolean).join(" · ")
        + `</div>`;
    }).join("");
    const more = sticks.length - 2;
    if (more > 0) {
      html += `<div class="mem-toggle" onclick="toggleMem(event,${jsStr(n.node)})"><svg class="ico ico-sm ico-chev${expanded ? " open" : ""}"><use href="#ico-chevron"></use></svg>${expanded ? "收起内存明细" : `还有 ${more} 根内存条`}</div>`;
    }
    return html;
  }
  // 降级：无逐条明细（旧后端/虚机）时退回汇总行
  const parts = [];
  if (n.mem_type) parts.push(n.mem_type + (n.mem_speed ? " " + n.mem_speed : ""));
  else if (n.mem_speed) parts.push(n.mem_speed);
  if (n.mem_modules && n.mem_slots && n.mem_slots >= n.mem_modules) {
    parts.push(`内存插槽 已用 ${n.mem_modules}/共 ${n.mem_slots}`);
  }
  return parts.length ? html + `<div class="cpu-sub">${escapeHtml(parts.join(" · "))}</div>` : html;
}

// 展开/收起内存条明细；阻断冒泡，别误触卡片的节点筛选
function toggleMem(ev, node) {
  ev.stopPropagation();
  expandedMem.has(node) ? expandedMem.delete(node) : expandedMem.add(node);
  renderNodes(allNodes);
}

// 网卡块：物理网卡/bond 的链路与速率，底部保留整机吞吐；无吞吐数据时降级提示
function fmtNicSpeed(mbps) {
  if (!mbps || mbps <= 0) return "—";
 return mbps >= 1000 ? `${(mbps / 1000).toFixed(mbps % 1000 ? 1 : 0)} Gb/s` : `${mbps} Mb/s`;
}

// 设备「没人用、但宿主也用不了」时的统一说法：术语（vfio 占用）放进 hover 说明，
// 徽章只讲状态 + 可做什么，普通用户一眼能懂
const VFIO_BADGE = "未使用（可释放）";
const VFIO_HINT = "该设备没有被任何虚机直通，但驱动被 vfio 占用着，宿主机也用不了；"
  + "可到「直通 → 回收直通设备」把它要回来";
// 没有驱动的情况（多为上次释放时原驱动没装上）：也不是「宿主使用」，单列一档
const NODRV_HINT = "该设备没有被任何虚机直通，宿主机也没有驱动在用；"
  + "可到「直通 → 回收直通设备」尝试装回原驱动";

function renderNics(n) {
  const nics = n.nics || [];
  if (!nics.length) return '<div class="cpu-sub">未探测到物理网卡</div>';
  const expanded = expandedNics.has(n.node);
  const row = (i) => {
    const up = i.state === "up";
    const pt = i.state === "passthrough";
    const vfio = i.state === "vfio";
    // 驱动已被 vfio-pci 接管的网卡（无 /sys/class/net 接口）：显示设备型号 + 中文类型标签。
    // 只有 VM 配置里真有 hostpci 才叫「直通占用」（橙）；其余为「未使用（可释放）」（灰）——
    // 典型场景：从 VM 配置里删了 hostpci，但 PVE 没把驱动还给原驱动，设备仍被 vfio 占着
    if (pt || vfio) {
      const name = i.desc || i.name;
      const badge = pt
        ? '<b class="pt">直通占用</b>'
        : `<b class="vfio" title="${escapeHtml(VFIO_HINT)}">${VFIO_BADGE}</b>`;
      return `<div class="net-row"><span><span class="dot stopped"></span><span class="net-name">${escapeHtml(name)}</span><i class="disk-type">${i.wireless ? "无线" : "有线"}</i></span>
        ${badge}</div>`;
    }
    // 行内标签：无线/有线/聚合（统一中文，语义清晰）
    const kind = i.wireless ? "无线" : (i.name.startsWith("bond") ? "聚合" : (i.name.startsWith("vmbr") ? "桥接" : "有线"));
    const typeTag = `<i class="disk-type">${kind}</i>`;
    // 桥/聚合标注其物理成员口：内核桥速率=成员最大口，含 VM tap 时会被顶高，标出成员避免误读
    const memTag = (i.members && i.members.length)
      ? `<span class="muted">（${escapeHtml(i.members.join("，"))}）</span>` : "";
    // 管理口：绿色标签，其后紧跟具体 IP（与标签同组，比放在速度后更顺眼）
    const mgmtTag = i.mgmt ? `<i class="disk-type mgmt">管理口</i>` : "";
    const ipText = (i.mgmt && i.ips && i.ips.length)
      ? `<span class="nic-ip">${(i.ips || []).map(escapeHtml).join("，")}</span>` : "";
    return `<div class="net-row"><span><span class="dot ${up ? "running" : "stopped"}"></span> ${escapeHtml(i.name)}${typeTag}${memTag}${mgmtTag}${ipText}</span>
      <b>${up ? "Up" : "Down"} · ${fmtNicSpeed(i.speed)}</b></div>`;
  };
  // 逐行展示，不再按有线/无线分组；默认露前 3 个，更多折叠
  const shown = expanded ? nics : nics.slice(0, 3);
  let html = shown.map(row).join("");
  const more = nics.length - shown.length;
  if (more > 0) {
    html += `<div class="disk-toggle" onclick="toggleNics(event,${jsStr(n.node)})"><svg class="ico ico-sm ico-chev${expanded ? " open" : ""}"><use href="#ico-chevron"></use></svg>${expanded ? "收起网卡" : `还有 ${more} 个网卡`}</div>`;
  }
  const tp = (n.netin != null || n.netout != null)
    ? `<div class="cpu-sub">↓ ${fmtRate(n.netin)} · ↑ ${fmtRate(n.netout)}（整机吞吐）</div>` : "";
  return html + tp;
}

// 展开/收起网卡列表；阻断冒泡，别误触卡片的节点筛选
function toggleNics(ev, node) {
  ev.stopPropagation();
  expandedNics.has(node) ? expandedNics.delete(node) : expandedNics.add(node);
  renderNodes(allNodes);
}

// 显卡卡片块：逐块列出精简型号。
// 「vGPU 占用」= hostpci 带 mdev=（物理卡仍由宿主 nvidia 驱动接管，只是切了一份给虚机）；
// 「直通占用」= hostpci 不带 mdev 的物理直通；两者都以 VM 配置为准（橙）；
// 驱动挂在 vfio-pci 上但没人用 →「未使用（可释放）」（灰）
function renderGpus(n) {
  const gpus = n.gpus || [];
  if (!gpus.length) return '<div class="cpu-sub">未探测到显卡</div>';
  return gpus.map((g) => {
    const model = shortGpuDesc(g.desc);
    const pt = g.passthrough === true;
    const vgpu = g.vgpu === true;
    let badge;
    if (vgpu) badge = '<b class="pt">vGPU 占用</b>';
    else if (pt) badge = '<b class="pt">直通占用</b>';
    else if (g.vfio) badge = `<b class="vfio" title="${escapeHtml(VFIO_HINT)}">${VFIO_BADGE}</b>`;
    else if (g.driver) badge = '<b>宿主使用</b>';
    else badge = `<b class="vfio" title="${escapeHtml(NODRV_HINT)}">未使用（无驱动）</b>`;
    return `<div class="net-row"><span><span class="net-name">${escapeHtml(model || g.addr)}</span></span>${badge}</div>`;
  }).join("");
}

// ---- 节点卡片：局部更新（自动刷新时只打补丁，不整段重绘，避免闪烁/滚动跳动） ----
function cpuMeterHtml(n) {
  const cpuPct = fmtPct(n.cpu);
  const coreLabel = n.cpu_cores ? `${n.cpu_cores} 核` : `${n.maxcpu} 核`;
  const cpuSpec = n.cpu_threads ? `${coreLabel} / ${n.cpu_threads} 线程` : coreLabel;
  return `<div class="meter">
    <div class="label"><span>${cpuSpec}</span><span><b class="pct ${barClass(n.cpu || 0)}">${cpuPct}%</b></span></div>
    <div class="bar"><i class="${barClass(n.cpu || 0)}" style="width:${cpuPct}%"></i></div>
    ${renderCpuDetails(n)}
  </div>`;
}
function memMeterHtml(n) {
  const memRatio = n.maxmem ? n.mem / n.maxmem : 0;
  const memPct = fmtPct(memRatio);
  return `<div class="meter">
    <div class="label"><span>${fmtBytes(n.mem)} / ${fmtBytes(n.maxmem)}</span><span><b class="pct ${barClass(memRatio)}">${memPct}%</b></span></div>
    <div class="bar"><i class="${barClass(memRatio)}" style="width:${memPct}%"></i></div>
    ${memInfoLine(n, expandedMem.has(n.node))}
  </div>`;
}
function ovBlockHtml(area, n) {
  const T = { cpu: ["CPU", "ico-cpu"], mem: ["内存", "ico-mem"], disk: ["硬盘", "ico-disk"], nic: ["网卡", "ico-nic"], gpu: ["显卡", "ico-gpu"] };
  const t = T[area];
  let body = "";
  if (area === "cpu") body = cpuMeterHtml(n);
  else if (area === "mem") body = memMeterHtml(n);
  else if (area === "disk") body = renderDisks(n);
  else if (area === "nic") body = renderNics(n);
  else if (area === "gpu") body = renderGpus(n);
  return `<div class="ov-block" data-area="${area}"><div class="ov-title"><svg class="ico"><use href="#${t[1]}"></use></svg>${t[0]}</div>${body}</div>`;
}
function nodeCardHtml(n, sel, hw) {
  const head = `
      <div class="card-title">
        <span><span class="dot ${n.status}"></span>${escapeHtml(n.node)}</span>
        <span class="muted">已运行 ${fmtUptime(n.uptime)}</span>
      </div>
      ${renderNodeTags(n)}`;
  const hwToggle = `<div class="hw-toggle" onclick="toggleHw(event,${jsStr(n.node)})"><svg class="ico ico-sm ico-chev${hw ? " open" : ""}"><use href="#ico-chevron"></use></svg>${hw ? "收起硬件明细" : "展开硬件明细"}</div>`;
  const attrs = `data-node="${escapeHtml(n.node)}" data-sel="${sel ? "1" : ""}" data-hw="${hw ? "1" : ""}"`;
  if (!hw) {
    return `<div class="card node-card collapsed${sel ? " selected" : ""}" ${attrs} onclick="selectNode(${jsStr(n.node)})" title="点击筛选该节点的虚机/容器">
      ${head}
      ${hwSummary(n)}
      ${hwToggle}
    </div>`;
  }
  return `<div class="card node-card${sel ? " selected" : ""}" ${attrs} onclick="selectNode(${jsStr(n.node)})" title="点击筛选该节点的虚机/容器">
      ${head}
      <div class="node-grid">
        ${ovBlockHtml("cpu", n)}${ovBlockHtml("mem", n)}${ovBlockHtml("disk", n)}${ovBlockHtml("nic", n)}${ovBlockHtml("gpu", n)}
      </div>
      ${hwToggle}
    </div>`;
}
// 原地更新已存在卡片的易变指标（状态点/运行时长/各资源占用），避免整段 innerHTML 重绘
function patchNodeCard(card, n) {
  const head = card.querySelector(".card-title");
  if (head) {
    const dot = head.querySelector(".dot");
    if (dot) dot.className = "dot " + n.status;
    const muted = head.querySelector(".muted");
    if (muted) muted.textContent = "已运行 " + fmtUptime(n.uptime);
  }
  const tags = card.querySelector(".node-tags");
  if (tags) tags.outerHTML = renderNodeTags(n);
  if (card.dataset.hw === "1") {
    ["cpu", "mem", "disk", "nic", "gpu"].forEach((a) => {
      const blk = card.querySelector(`[data-area="${a}"]`);
      if (blk) blk.outerHTML = ovBlockHtml(a, n);
    });
  } else {
    const line = card.querySelector(".hw-line");
    if (line) line.outerHTML = hwSummary(n);
  }
  card.classList.toggle("selected", n.node === selectedNode);
}
function renderNodes(nodes) {
  // 选中某节点时只渲染该节点一张卡（节点多时可省掉整屏滚动）；未选中则并排全部节点
  const list = selectedNode ? nodes.filter((n) => n.node === selectedNode) : nodes;
  const wrap = $("nodeCards");
  const st = wrap.scrollTop;   // 保住滚动位置，自动刷新不打回顶部
  // 首屏骨架屏占位用的是 .skeleton-card（非 .node-card），下面的增量清理只处理 .node-card，
  // 不会移除它；这里先清掉，避免灰条一直残留在真实节点卡片上方
  wrap.querySelectorAll(".skeleton-card").forEach((el) => el.remove());
  const existing = new Map([...wrap.querySelectorAll(".node-card")].map((el) => [el.dataset.node, el]));
  const wanted = new Set(list.map((n) => n.node));
  for (const [name, el] of existing) if (!wanted.has(name)) el.remove();
  list.forEach((n) => {
    const hw = hwExpanded(n.node);
    const sel = n.node === selectedNode;
    const old = existing.get(n.node);
    // 结构态（选中/展开）未变 → 仅打补丁（局部更新）；否则重建该卡片（新增节点/切展开态）
    if (old && old.dataset.sel === (sel ? "1" : "") && old.dataset.hw === (hw ? "1" : "")) {
      patchNodeCard(old, n);
    } else {
      const tmp = document.createElement("div");
      tmp.innerHTML = nodeCardHtml(n, sel, hw);
      const card = tmp.firstElementChild;
      if (old) old.replaceWith(card); else wrap.appendChild(card);
    }
  });
  wrap.scrollTop = st;
}

// 节点切换标签：全部 + 各节点。数字是「虚机/容器 运行中/总数」（不是节点/宿主机数量），
// 显式带「虚机」前缀避免与「节点」标题混淆；点选后只显示该节点卡片并过滤虚机列表
function renderNodeTabs() {
  const wrap = $("nodeTabs");
  if (!wrap) return;
  // 只有一个节点时不渲染标签栏：既没有切换的意义，「全部」与它的数字还完全相同、纯冗余
  if (allNodes.length <= 1) { wrap.innerHTML = ""; return; }
 // 每节点虚机数：口径与虚机列表一致（不含模板）
 const countOf = (node) => {
    let run = 0, total = 0;
    allGuests.forEach((g) => {
      if (g.template) return;
      if (node && g.node !== node) return;
      total += 1;
      if (g.status === "running") run += 1;
    });
    return { run, total };
  };
  // 节点数多（>6）时改用下拉选择：一排标签会折成多行把虚机列表往下压；
  // 下拉只留「节点」+ 当前选中项，选项内含 运行中/总数 与离线标注，onchange 直接过滤
  if (allNodes.length > 6) {
    const opt = (label, value, off, c) =>
      '<option value="' + escapeHtml(value) + '"' + ((selectedNode || "") === value ? " selected" : "") + '>'
      + escapeHtml(label) + off + '  虚机 ' + c.run + '/' + c.total + '</option>';
    const opts = [opt("全部", "", "", countOf(null))];
    allNodes.forEach((n) => {
      const off = n.status === "online" ? "" : "（离线）";
      opts.push(opt(n.node, n.node, off, countOf(n.node)));
    });
    wrap.innerHTML = '<label class="node-select-wrap">节点<select class="node-select" onchange="selectNode(this.value)">' + opts.join("") + '</select></label>';
    return;
  }
  const tab = (label, value, online) => {
    const c = countOf(value || null);
    const active = (selectedNode || "") === value ? " active" : "";
    const dot = online === false ? '<span class="dot stopped"></span>' : "";
    const tip = `点击筛选${value ? `节点 ${label}` : "全部节点"}的虚机/容器；数字为该范围虚机/容器的 运行中/总数（不含模板）`;
    return `<button type="button" class="node-tab${active}" title="${escapeHtml(tip)}" onclick="selectNode(${jsStr(value)})">${dot}${escapeHtml(label)}<span class="nt-count">虚机 ${c.run}/${c.total}</span></button>`;
  };
  wrap.innerHTML = tab("全部", "", null)
    + allNodes.map((n) => tab(n.node, n.node, n.status === "online")).join("");
}

// 节点卡片硬件明细是否展开：手动切换过以切换值为准；未切换则按默认策略——
// 单节点展开（信息一次看全），多节点收起（避免卡片铺满好几屏把虚机列表挤到屏幕外）
function hwExpanded(node) {
  return hwPref.has(node) ? hwPref.get(node) : allNodes.length <= 1;
}

// 展开/收起节点卡片的硬件明细；阻断冒泡，别误触卡片的节点筛选
function toggleHw(ev, node) {
 ev.stopPropagation();
 hwPref.set(node, !hwExpanded(node));
 renderNodes(allNodes);
}

// 节点卡片收起态的紧凑摘要：CPU/内存用量 + 磁盘/网卡/显卡数量；掉盘告警始终置顶
function hwSummary(n) {
 const lost = n.disks_lost || [];
 const warn = lost.length
 ? `<div class="disk-lost"><svg class="ico ico-sm"><use href="#ico-warn"></use></svg>掉盘告警：${lost.map((d) => "/dev/" + escapeHtml(d)).join("、")} 已消失，请检查磁盘链路/供电</div>`
    : "";
  const cpuRatio = n.cpu || 0;
  const memRatio = n.maxmem ? n.mem / n.maxmem : 0;
  const diskCount = (n.phys_disks || []).length || (n.disks || []).length;
  const item = (label, value, cls) =>
    `<span class="hw-item">${label} <b class="${cls || ""}">${value}</b></span>`;
  return warn + `<div class="hw-line">
      ${item("CPU", fmtPct(cpuRatio) + "%", "pct " + barClass(cpuRatio))}
      ${item("内存", fmtPct(memRatio) + "%", "pct " + barClass(memRatio))}
      ${item("磁盘", diskCount + " 块")}
      ${item("网卡", (n.nics || []).length + " 个")}
      ${item("显卡", (n.gpus || []).length + " 块")}
    </div>`;
}

// 集群仲裁状态（/cluster/quorum）：can_degrade 由服务端强校验，前端只据它显隐按钮。
// 仅「在线 == 不可达」（偶数集群恰好掉一半）时放行，奇数集群恒不满足，按钮自然不出现。
let quorumInfo = null;

// 服务端的手动刷新已改为「立即回旧缓存 + 后台重采慢硬件」，故补两次延迟回读把新数据取回来。
// 两次覆盖 dmidecode/smartctl/sensors 跨节点 SSH 的耗时差异；回读都命中缓存，开销极小。
function reReadAfterManualRefresh(reload) {
  [3000, 9000].forEach((ms) => setTimeout(() => {
    if (!isModalOpen()) reload();
  }, ms));
}

// 节点管理视图：复用 /nodes 渲染只读节点概览卡片（节点级管理操作后续接入）
// silent=true 用于重采后的回读：保留现有卡片，不闪「加载中」
async function loadNodeMgmt(manual, silent = false) {
  const wrap = $("nodeMgmtCards");
  if (!wrap) return;
  if (!silent) {
    wrap.innerHTML = '<div class="empty-state"><div class="empty-icon"><svg class="ico"><use href="#ico-hourglass"></use></svg></div><div>加载中…</div></div>';
  }
  if ($("nodeMgmtEmpty")) $("nodeMgmtEmpty").classList.add("hidden");
  try {
    // 仲裁状态与节点列表并行拉取；仲裁接口失败（老版本/异常）不阻塞节点列表展示
    const [nodes, quorum] = await Promise.all([
      api(manual ? "/nodes?fresh=1" : "/nodes"),
      api("/cluster/quorum").catch(() => null),
    ]);
    quorumInfo = quorum;
    renderNodeMgmt(nodes);
    if (manual) reReadAfterManualRefresh(() => loadNodeMgmt(false, true));
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state"><div class="empty-icon"><svg class="ico"><use href="#ico-warn"></use></svg></div><div>节点数据加载失败：${escapeHtml(String(e))}</div></div>`;
  }
}

// 集群仲裁提示条：无仲裁时给出「临时取得仲裁/恢复正常仲裁」入口；离线节点行已单独给按钮时此处兜底。
function renderQuorumBar(hasOfflineRow) {
  const el = $("quorumBar");
  if (!el) return;
  const q = quorumInfo;
  if (!q || !q.in_cluster) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const online = q.online || [], offline = q.offline || [];
  const bits = [];
  if (!q.quorate) {
    bits.push('<span class="qb-state bad">集群无仲裁</span>');
    bits.push(`<span>在线 ${online.length} 台${offline.length ? "、不可达 " + offline.length + " 台（" + escapeHtml(offline.join("、")) + "）" : ""}</span>`);
    bits.push('<span class="muted">PVE 已把 /etc/pve 置为只读：建机、改配置等写操作都会报 no quorum</span>');
  } else {
    bits.push('<span class="qb-state ok">集群仲裁正常</span>');
    bits.push(`<span>在线 ${online.length}/${q.total_nodes || online.length} 台 · 预期票数 ${q.expected ?? "—"}</span>`);
  }
  if (q.needs_restore) {
    bits.push(`<span class="qb-warn">预期票数已被下调（${q.expected} &lt; ${q.total_nodes}）</span>`);
  }
  const btns = [];
  if (q.can_degrade && !hasOfflineRow) {
    btns.push('<button class="btn btn-sm danger" onclick="degradeQuorum()">临时取得仲裁</button>');
  }
  if (q.needs_restore) {
    btns.push('<button class="btn btn-sm" onclick="restoreQuorum()">恢复正常仲裁</button>');
  }
  el.classList.remove("hidden");
  el.classList.toggle("alert", !q.quorate);
  el.innerHTML = `<div class="qb-main">${bits.join('<span class="qb-sep">·</span>')}</div>
    ${btns.length ? `<div class="qb-act">${btns.join("")}</div>` : ""}
    ${q.reason ? `<div class="qb-reason muted">${escapeHtml(q.reason)}</div>` : ""}`;
}

function renderNodeMgmt(nodes) {
  const wrap = $("nodeMgmtCards");
  if (!wrap) return;
  const empty = $("nodeMgmtEmpty");
  if (!nodes || !nodes.length) {
    wrap.innerHTML = "";
    if (empty) empty.classList.remove("hidden");
    renderQuorumBar(false);
    return;
  }
  if (empty) empty.classList.add("hidden");
  const offlineSet = new Set((quorumInfo && quorumInfo.offline) || []);
  const canDegrade = !!(quorumInfo && quorumInfo.can_degrade);
  let hasOfflineRow = false;
  wrap.innerHTML = nodes.map((n) => {
    const ip = n.ip || n.address || "—";
    const cpuPct = fmtPct(n.cpu);
    const memRatio = n.maxmem ? n.mem / n.maxmem : 0;
    const memPct = fmtPct(memRatio);
    const coreLabel = n.cpu_cores ? `${n.cpu_cores} 核` : `${n.maxcpu} 核`;
    const cpuSpec = n.cpu_threads ? `${coreLabel} / ${n.cpu_threads} 线程` : coreLabel;
    // 共享存储修复统一放工具栏（一键修所有节点），节点行不再逐台放按钮，详见 fixAllNodeStorage()
    // 占位条目：pvesh 没返回该节点（集群降级时 pmxcfs 成员视图不同步），仅由 corosync 成员表补出。
    // 此时数据全为 0、重启/修复必然失败，故只显示节点名与状态，并说明原因。
    const ph = !!n.placeholder;
    // 离线节点行给出「临时取得仲裁」：把该节点票数排除出仲裁计算（非隔离/剔除节点本身）；
    // 服务端强校验放行条件，且仅下调运行时票数，不改配置、不删节点、不影响虚机。
    const isOffline = offlineSet.has(n.node) || n.status === "offline";
    if (isOffline && canDegrade) hasOfflineRow = true;
    const quarantineBtn = (isOffline && canDegrade)
      ? `<button class="btn btn-sm danger" title="临时取得仲裁（等价 pvecm expected）：把该离线节点的票数排除出仲裁计算，使本机重新可写（只改运行时票数，不改集群配置、不删节点、不影响虚机）" onclick="degradeQuorum(${jsStr(n.node)})">临时取得仲裁</button>`
      : "";
    const pveCell = ph ? '<span class="muted">集群降级：未取到该节点信息</span>'
                       : `PVE ${escapeHtml(n.pveversion || "—")}`;
    const cpuCell = ph ? "" : `CPU ${cpuPct}%<small class="muted"> · ${cpuSpec}</small>`;
    const memCell = ph ? "" : `内存 ${memPct}%<small class="muted"> · ${fmtBytes(n.mem)}/${fmtBytes(n.maxmem)}</small>`;
    // 节点级动作统一收进「操作」下拉：离线 / 占位条目状态未知，SSH 类动作必然失败，不给下拉；
    // 离线行仍只保留「临时取得仲裁」。
    const opsMenu = (ph || isOffline) ? "" : `
      <details class="node-ops">
        <summary class="btn btn-sm">操作 ▾</summary>
        <div class="node-ops-menu">
          <button class="btn btn-sm" onclick="rebootNodeFromMgmt(${jsStr(n.node)})">重启节点</button>
          <button class="btn btn-sm" onclick="restartPveServices(${jsStr(n.node)})">重启 PVE 服务</button>
          <button class="btn btn-sm" onclick="diagNode(${jsStr(n.node)})">一键诊断</button>
          <button class="btn btn-sm danger" onclick="shutdownNodeFromMgmt(${jsStr(n.node)})">关机</button>
        </div>
      </details>`;
    return `
    <div class="card node-card node-row">
      <span class="nc-name"><span class="dot ${n.status}"></span>${escapeHtml(n.node)}</span>
      <span class="nc-ip muted">${escapeHtml(ip)}</span>
      <span class="nc-pve muted">${pveCell}</span>
      <span class="nc-cpu">${cpuCell}</span>
      <span class="nc-mem">${memCell}</span>
      <span class="node-actions">
        ${quarantineBtn}
        ${opsMenu}
      </span>
    </div>`;
  }).join("");
  renderQuorumBar(hasOfflineRow);
}

// 临时取得仲裁（降级运行）：把运行时预期票数下调到在线节点数，让失仲裁的集群重新可写。
// 安全前提（服务端强校验）：仅当两侧票数相等（偶数集群恰好掉一半）时放行，避免双写。
async function degradeQuorum(node) {
  const q = quorumInfo || {};
  const offline = q.offline || [];
  const others = node ? offline.filter((x) => x !== node) : offline;
  const names = [node, ...others].filter(Boolean).join("、") || "不可达节点";
  const ok = await appConfirm(
    `临时把离线节点「${names}」排除出仲裁，票数降为在线节点数，本机恢复可写。\n` +
    `仅改运行时票数（不改配置、不删节点、不影响虚机），服务重启后失效；节点恢复后点「恢复正常仲裁」。\n` +
    `前提：节点真实宕机，非网络分区（否则可能两侧双写）。`,
    { title: "临时取得仲裁", okText: "确认执行", danger: true, requireText: "临时取得仲裁" });
  if (!ok) return;
  try {
    const data = await api("/cluster/quorum/degrade", "POST", { confirm: "临时取得仲裁" });
    toast(data.message || "已临时取得仲裁", !data.ok);
    loadNodeMgmt(true);
  } catch (e) {
    toast("临时取得仲裁失败: " + e.message, true);
  }
}

// 恢复正常仲裁：把所有节点都拉回来后，把预期票数还原为集群节点总数。
// 不还原的危害：集群长期停留在"半数即可写"的降级状态，真发生网络分区时会两侧同时可写。
async function restoreQuorum() {
  const q = quorumInfo || {};
  const ok = await appConfirm(
    `将把预期票数还原为集群节点总数（${q.total_nodes || "全部节点"}），恢复按全量计票的正常仲裁规则。\n\n` +
    `请确认所有集群节点均已在线——否则还原后本机会立刻重新失去仲裁（写操作又会报 no quorum）。`,
    { title: "恢复正常仲裁", okText: "确认执行", requireText: "恢复正常仲裁" });
  if (!ok) return;
  try {
    const data = await api("/cluster/quorum/restore", "POST", { confirm: "恢复正常仲裁" });
    toast(data.message || "已恢复正常仲裁", false);
    loadNodeMgmt(true);
  } catch (e) {
    toast("恢复正常仲裁失败: " + e.message, true);
  }
}

// 集群名与确认词的校验（「加入节点」第 1 步：创建集群用）；不通过时已弹 toast。
function validClusterInputs(name, confirmWord) {
  if (!name) { toast("请填写集群名", true); return false; }
  if (!/^[A-Za-z][A-Za-z0-9._-]{0,63}$/.test(name)) {
    toast("集群名不合法：须字母开头，只能包含字母、数字、. - _，最长 64 个字符", true);
    return false;
  }
  if (normConfirm(confirmWord) !== normConfirm("创建集群")) {
    toast("确认词不正确，请输入：创建集群", true);
    return false;
  }
  return true;
}

// 加入集群：中心节点 SSH 到新机器执行 pvecm add（节点管理页集中管理）。
// 本机还没有集群时，就地先做「第 1 步：创建集群」，建好自动续到第 2 步；已有集群的机器打开
// 就是原来的加机表单（条件式，不会多出一层「第 1 步已完成」的噪音）。
async function openJoinClusterModal() {
  $("modalTitle").textContent = "加入集群节点";
  $("modalBody").innerHTML = '<p class="muted">加载中…</p>';
  smShowModal();
  let info = null;
  try { info = await api("/cluster/info"); } catch (_) { info = null; }
  // 状态取不到时不臆断：直接给原加机表单，由服务端的加机前置检查回准确原因
  if (info && !info.in_cluster) renderJoinStepCreate();
  else renderJoinForm((info && info.name) || "");
}

// 「加入节点」第 1 步（仅本机尚未建集群时）：加入节点只能加入已存在的集群，故先建集群
function renderJoinStepCreate() {
  $("modalTitle").textContent = "加入集群节点（第 1/2 步：创建集群）";
  $("modalBody").innerHTML = `<div class="sm-form">
    <p class="muted u-para" >本机还没有集群，请先创建。<b>集群名创建后不可更改</b>。</p>
    <div class="tc-row"><label>集群名</label><input type="text" id="jcClusterName" value="webtools" placeholder="默认 webtools，可修改"></div>
    <div class="tc-row"><label>确认词</label><input type="text" id="jcClusterConfirm" placeholder="请原样输入：创建集群"></div>
    <div class="tc-row tc-actions u-justify-end" >
      <button type="button" class="btn" onclick="smHideModal()">取消</button>
      <button type="button" class="btn run" id="jcStep1Ok">创建集群并继续</button>
    </div>
  </div>`;
  smShowModal();
  $("jcStep1Ok").onclick = createClusterAndContinue;
}

// 第 1 步提交：就地建集群，成功后直接续到第 2 步（填写要加入的新节点）
async function createClusterAndContinue() {
  const name = $("jcClusterName").value.trim();
  if (!validClusterInputs(name, $("jcClusterConfirm").value)) return;
  const btn = $("jcStep1Ok");
  btn.disabled = true; btn.textContent = "创建中…";
  try {
    const data = await api("/cluster/create", "POST", { name, confirm: "创建集群" });
    // 建成但状态未就绪不拦着：集群已经建出来了，接着填新节点即可（若加入报未就绪，稍等重试）
    if (data.ready === false) toast(`集群 ${data.name} 已创建，但状态尚未就绪；加入若失败请稍等片刻重试`, true);
    else toast(`集群 ${data.name} 已创建，继续填写要加入的新节点`);
    renderJoinForm(data.name);
    loadNodeMgmt(true);
  } catch (e) {
    toast("创建集群失败: " + (e.message || e), true);
    btn.disabled = false; btn.textContent = "创建集群并继续";
  }
}

// 「加入节点」第 2 步：填写要加入的新节点（本机已在集群里）
function renderJoinForm(clusterName) {
  const head = clusterName
    ? `<p class="muted u-para" >将加入集群：<b>${escapeHtml(clusterName)}</b></p>`
    : "";
  $("modalTitle").textContent = "加入集群节点";
  $("modalBody").innerHTML = `<div class="sm-form">
    ${head}
    <div class="tc-row"><label>新节点 IP 地址</label><input type="text" id="jcNewHost" placeholder="如 192.168.1.20（必须 IP，不支持主机名）"></div>
    <div class="tc-row"><label>新节点 root 密码</label><input type="password" id="jcNewPw" placeholder="新机器 root 密码"></div>
    <div class="tc-row tc-actions u-justify-end" >
      <button type="button" class="btn" onclick="smHideModal()">取消</button>
      <button type="button" class="btn run" id="jcOk">加入集群</button>
    </div>
  </div>`;
  smShowModal();
  $("jcOk").onclick = joinCluster;
}

async function joinCluster() {
  const new_host = $("jcNewHost").value.trim();
  const new_password = $("jcNewPw").value;
  if (!new_host || !new_password)
    return toast("请填写：新节点 IP、新节点 root 密码", true);
  if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(new_host) && new_host.indexOf(":") < 0)
    return toast("新节点地址请填写 IP（不支持主机名）", true);
  const btn = $("jcOk");
  btn.disabled = true; btn.textContent = "加入中…";
  try {
    const data = await api("/cluster/join", "POST", { new_host, new_password });
    let tip = `已将 ${data.joined} 加入集群`;
    let isErr = false;
    if (data.storage_init && !data.storage_init.ok) { tip += `；共享存储接入未完成：${data.storage_init.msg}`; isErr = true; }
    toast(tip, isErr);
    smHideModal();
    loadNodeMgmt(true);
  } catch (e) {
    toast("加入失败: " + (e.message || e), true);
    btn.disabled = false; btn.textContent = "加入集群";
  }
}

// 一键修复所有节点的共享存储：后端按「导出端优先」逐台执行并返回逐台结果。
// 装 nfs-kernel-server / 逐台 SSH 可能持续数分钟，故单独放宽超时并禁用重试，
// 避免 POST 重发导致修复脚本重复执行。
async function fixAllNodeStorage() {
  const btn = $("btnFixStorageAll");
  // 与其它节点级异步动作互斥：它们共用同一个结果条，并行会互相覆盖输出
  if (!btn || btn.dataset.busy === "1" || nodeActionBusy) return;
  const ok = await appConfirm(
    "将一键修复集群内所有节点的共享存储：先修 NFS 导出端，再逐台重挂客户端并铺 vBIOS 软链；" +
    "离线 / 状态未知的节点自动跳过。\n\n" +
    "这会重挂各节点的共享存储，过程可能持续数分钟，期间请保持页面打开。",
    { title: "一键修复共享存储", okText: "开始修复" });
  if (!ok) return;
  const label = btn.textContent;
  btn.dataset.busy = "1";
  nodeActionBusy = true;
  btn.disabled = true;
  btn.textContent = "修复中…";
  try {
    const data = await api("/nodes/storage-fix-all", "POST", {}, { timeout: 1800000, retries: 0 });
    renderStorageFixResult(data);
    toast(data.msg || "共享存储修复完成", !data.ok);
    loadNodeMgmt(true);
  } catch (e) {
    toast("一键修复失败: " + e.message, true);
  } finally {
    nodeActionBusy = false;
    btn.disabled = false;
    btn.textContent = label;
    delete btn.dataset.busy;
  }
}

// ---- 节点行「操作」下拉与节点级动作 ----
// 点空白处 / 其它行时收起菜单；各动作开始前也会显式收起，弹确认框时不留一个展开的下拉
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.node-ops[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  });
});

function closeNodeOps() {
  document.querySelectorAll("details.node-ops[open]").forEach((d) => { d.open = false; });
}

// 同一时刻只允许一个节点级异步动作（重启服务 / 诊断），避免连点提交一堆重复 job
let nodeActionBusy = false;

// 在指定节点上跑工具箱工具：本机留空走本地执行，其余节点由后端解析为 SSH 目标
async function runNodeTool(node, toolId, params) {
  const payload = { params };
  if (node !== localNodeName()) payload.node = node;
  return api(`/tools/${toolId}/run`, "POST", payload);
}

// 轮询任务快照直到结束（诊断 / 服务重启是异步 job）；超时不阻断，提示去执行日志看完整输出
async function waitNodeJob(jobId, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const snap = await api(`/jobs/${encodeURIComponent(jobId)}?offset=0`);
    if (snap.status && snap.status !== "running") return snap;
    await new Promise((r) => setTimeout(r, 1200));
  }
  return { status: "timeout", lines: ["等待执行结果超时，请到「执行日志」查看完整输出"] };
}

function nodeJobLog(lines, limit = 30) {
  return `<pre class="node-action-log">${escapeHtml((lines || []).slice(-limit).join("\n") || "（无输出）")}</pre>`;
}

// 节点列表上方的结果条：一键修复 / 重启服务 / 一键诊断共用（复用仲裁提示条样式）
function showNodeActionResult(title, rows, ok) {
  const el = $("nodeActionResult");
  if (!el) return;
  el.classList.remove("hidden");
  el.classList.toggle("alert", !ok);
  el.innerHTML = `<div class="qb-main"><b>${escapeHtml(title)}</b></div>${rows.map((r) => `<div class="qb-reason">${r}</div>`).join("")}
    <div class="qb-act"><button class="btn btn-sm" onclick="hideNodeActionResult()">关闭</button></div>`;
}

function hideNodeActionResult() {
  const el = $("nodeActionResult");
  if (el) el.classList.add("hidden");
}

// 一键修复共享存储的逐台结果：失败标红，跳过的节点单独说明
function renderStorageFixResult(data) {
  const rows = (data.results || []).map((r) => {
    const role = r.role === "server" ? "导出端" : "客户端";
    const mark = r.ok ? "✓" : "✗";
    const cls = r.ok ? "u-ok-text" : "u-err-msg";
    return `<span class="${cls}">${mark} ${escapeHtml(r.node)}（${role}）：${escapeHtml(r.msg || (r.ok ? "修复完成" : "修复失败"))}</span>`;
  });
  if ((data.skipped || []).length) {
    rows.push(`已跳过离线 / 状态未知节点：${escapeHtml(data.skipped.join("、"))}`);
  }
  showNodeActionResult(data.msg || "共享存储修复完成", rows, data.ok);
}

// 重启 PVE 服务：复用工具箱「PVE 服务诊断」的 restart 动作，只重启核心服务、不重启整机
async function restartPveServices(node) {
  closeNodeOps();
  if (nodeActionBusy) return;
  const isLocal = node === localNodeName();
  const ok = await appConfirm(
    `将重启节点 ${node} 的 PVE 核心服务（pve-cluster / pvedaemon / pvestatd / pve-firewall / pveproxy）。\n\n` +
    (isLocal
      ? "⚠ 这是控制机：WebUI 与工具箱会短暂中断数秒，期间页面可能失联；不会重启整机，虚拟机不受影响。"
      : "不会重启整机，虚拟机不受影响；该节点 WebUI 可能短暂中断。"),
    { title: "重启 PVE 服务", okText: "确认重启" });
  if (!ok) return;
  nodeActionBusy = true;
  try {
    const { job_id } = await runNodeTool(node, "svc_diag", { action: "restart" });
    toast(`已提交 ${node} 的 PVE 服务重启任务，正在等待结果…`);
    showNodeActionResult(`PVE 服务重启 · ${node}`, ["任务执行中，完成后在此显示输出…"], true);
    const snap = await waitNodeJob(job_id);
    showNodeActionResult(`PVE 服务重启 · ${node}`, [nodeJobLog(snap.lines, 40)], snap.status === "success");
  } catch (e) {
    toast("重启 PVE 服务失败: " + e.message, true);
  } finally {
    nodeActionBusy = false;
  }
}

// 一键诊断：四项只读检查（服务状态 / 启动状态 / 错误日志 / 网络诊断），不改任何配置
const NODE_DIAG_TOOLS = [
  { id: "svc_diag", name: "PVE 服务状态", params: { action: "status" } },
  { id: "boot_check", name: "启动状态检查", params: {} },
  { id: "log_check", name: "错误日志速查", params: {} },
  { id: "net_diagnose", name: "网络诊断", params: {} },
];

async function diagNode(node) {
  closeNodeOps();
  if (nodeActionBusy) return;
  const ok = await appConfirm(
    `将对节点 ${node} 执行一组只读诊断（服务状态 / 启动状态 / 错误日志 / 网络诊断）。\n\n` +
    "不会修改任何配置；结果汇总在列表上方，完整输出也可到「执行日志」查看。",
    { title: "一键诊断", okText: "开始诊断" });
  if (!ok) return;
  nodeActionBusy = true;
  toast(`正在对 ${node} 执行诊断…`);
  const rows = [];
  let allOk = true;
  showNodeActionResult(`一键诊断 · ${node}（0/${NODE_DIAG_TOOLS.length}）`, ["诊断进行中…"], true);
  try {
    for (const t of NODE_DIAG_TOOLS) {
      try {
        const { job_id } = await runNodeTool(node, t.id, t.params);
        const snap = await waitNodeJob(job_id);
        const good = snap.status === "success";
        allOk = allOk && good;
        rows.push(`<b>${good ? "✓" : "✗"} ${escapeHtml(t.name)}</b>${nodeJobLog(snap.lines)}`);
      } catch (e) {
        allOk = false;
        rows.push(`<b>✗ ${escapeHtml(t.name)}</b><span class="u-err-msg">提交/执行失败：${escapeHtml(e.message)}</span>`);
      }
      // 每完成一项就刷新结果条，避免多项串行期间看起来像卡住
      showNodeActionResult(`一键诊断 · ${node}（${rows.length}/${NODE_DIAG_TOOLS.length}）`,
        [...rows, rows.length < NODE_DIAG_TOOLS.length ? "诊断进行中…" : ""].filter(Boolean), allOk);
    }
    showNodeActionResult(`一键诊断 · ${node}`, rows, allOk);
    toast(`一键诊断完成：${node}`, !allOk);
  } finally {
    nodeActionBusy = false;
  }
}

// 关机：项目没有远程开机（WoL）能力，控制机自身关机后工具箱与 WebUI 都不可用，必须现场开机
async function shutdownNodeFromMgmt(node) {
  closeNodeOps();
  const isLocal = node === localNodeName();
  const ok = await appConfirm(
    `即将关闭节点 ${node} 的电源，该节点上所有运行中的虚拟机/容器将被中断！（约 10 秒后关机）\n\n` +
    (isLocal
      ? "⚠ 这是控制机：关机后本工具箱与 WebUI 全部不可用，且没有远程开机（WoL）能力，必须到机器现场开机。"
      : "⚠ 项目没有远程开机（WoL）能力，关机后需到该机器现场开机。"),
    { title: "关机节点", okText: "确认关机", danger: true, requireText: "关机节点" });
  if (!ok) return;
  try {
    await api(`/nodes/${encodeURIComponent(node)}/shutdown`, "POST", { confirm: "关机节点" });
    toast(`${node} 将于约 10 秒后关机${isLocal ? "，页面将失联，请到现场开机" : "，需现场开机恢复"}`, false);
  } catch (e) {
    toast("关机指令下发失败: " + e.message, true);
  }
}

// 节点管理页：重启宿主机（直通/内核参数等改动需重启生效时使用）。
// 与工具箱弹窗内「重启生效」共用 /nodes/<node>/reboot，同样要求手输确认词防误触。
async function rebootNodeFromMgmt(node) {
  closeNodeOps();
  // 要求原样输入确认词（应用内确认框），而非一键确认：重启整机影响大，防误触/自动化误点
  const ok = await appConfirm(
    `即将重启节点 ${node}，该节点上所有运行中的虚拟机/容器将被中断！（约 10 秒后重启）`,
    { title: "重启节点", okText: "确认重启", danger: true, requireText: "重启节点" });
  if (!ok) return;
  const isLocal = node === localNodeName();
  try {
    await api(`/nodes/${encodeURIComponent(node)}/reboot`, "POST", { confirm: "重启节点" });
    if (isLocal) {
      // 控制机自身重启：本页会随之失联，等重启真正启动后再探活，恢复即自动刷新
      toast(`${node} 将于约 10 秒后重启，本页会短暂失联，恢复后自动刷新`, false);
      setTimeout(watchNodeRecovery, 12000);
    } else {
      toast(`${node} 将于约 10 秒后重启，稍后刷新即可查看状态`, false);
    }
  } catch (e) {
    toast("重启指令下发失败: " + e.message, true);
  }
}

// 点击节点卡片 / 节点标签：选中并过滤虚机列表，再点一次（或点「全部」）取消
function selectNode(node) {
  node = node || null;   // 标签「全部」传空串，统一归一为 null
  // 切节点前先记住当前节点的页码，切过去再恢复目标节点的页码（各记各的，互不干扰）
  guestPageByNode[selectedNode || "__all__"] = guestPage;
  selectedNode = selectedNode === node ? null : node;
  guestPage = guestPageByNode[selectedNode || "__all__"] || 1;
  renderNodes(allNodes);
  renderGuests();
}

function guestActions(g) {
  const key = `${g.node}|${g.type}|${g.vmid}`;
  // 模板只读，无法开机/关机：操作列给禁用按钮并说明原因
  if (g.template) {
    return `<button class="btn btn-sm" disabled title="模板无法开机/关机">启动</button>`;
  }
  let btns;
  if (g.status === "running") {
    btns = `
      <button class="btn btn-sm danger" onclick="doAction(${jsStr(key)},'shutdown')">关机</button>
      <button class="btn btn-sm danger" onclick="doAction(${jsStr(key)},'stop')">强制停止</button>
      <button class="btn btn-sm" onclick="doAction(${jsStr(key)},'reboot')">重启</button>`;
  } else {
    btns = `<button class="btn btn-sm start" onclick="doAction(${jsStr(key)},'start')">启动</button>`;
  }
  return btns + migrateBtn(g);
}

// 迁移按钮：打开工具箱「迁移虚机/容器」弹窗，并预填源实例与执行节点（源节点）。
// 集群只有本机一台时无处可迁，直接置灰并说明原因，省得用户点进去才发现没有目标节点。
function migrateBtn(g) {
  if (allNodes.length < 2) {
 return `<button class="btn btn-sm" disabled title="当前集群只有本机一个节点，无处可迁">迁移</button>`;
  }
  const call = `openTool('vm_migrate',{vmid:${jsStr(String(g.vmid))}},${jsStr(g.node)})`;
  return `<button class="btn btn-sm" title="迁移到集群内其它节点" onclick="${call}">迁移</button>`;
}

// 开机自启单元格：模板只读显示「—」，否则显示滑块开关，点击切换（开=绿、关=灰）
function onbootCell(g) {
  if (g.template) return '<span class="muted">—</span>';
  const on = g.onboot === 1;
  return `<span class="onboot-switch ${on ? "on" : ""}" role="switch" aria-checked="${on}" title="${on ? "已开启开机自启（点击关闭）" : "未开启开机自启（点击开启）"}" onclick="toggleOnboot(${jsStr(g.node)},${jsStr(g.type)},${g.vmid},${on ? 0 : 1})"><span class="knob"></span></span>`;
}

// 切换开机自启：调后端接口，成功后刷新列表
async function toggleOnboot(node, type, vmid, enabled) {
  try {
    await api(`/guests/${node}/${type}/${vmid}/onboot`, "POST", { enabled: !!enabled });
    toast(`已${enabled ? "开启" : "关闭"} ${vmid} 的开机自启`, false);
    refresh();
  } catch (e) {
    toast("设置开机自启失败: " + e.message, true);
  }
}

// 备注单元格：模板只读显示「—」；其余显示当前备注（可点击内联编辑），无备注则提示「点击添加」
function descriptionCell(g) {
  if (g.template) return '<span class="muted">—</span>';
  const desc = (g.description || "").trim();
  const disp = desc ? escapeHtml(desc) : '<span class="muted desc-add">点击添加</span>';
  return `<span class="desc-edit" onclick="editDescription(${jsStr(g.node)},${jsStr(g.type)},${g.vmid}, this)" title="点击编辑备注">${disp}</span>`;
}

// 按节点/类型/VMID 在全局虚机列表里定位对象，用于取当前备注与保存后回写
function findGuest(node, type, vmid) {
  return allGuests.find((x) => x.node === node && x.type === type && String(x.vmid) === String(vmid));
}

// 内联编辑备注：点击进入 textarea，回车（或失焦）保存，Esc 取消
async function editDescription(node, type, vmid, el) {
  if (el.querySelector("textarea")) return;   // 已在编辑，避免重复进入
  const g = findGuest(node, type, vmid) || {};
  const cur = (g.description || "").trim();
  const ta = document.createElement("textarea");
  ta.className = "desc-input";
  ta.value = cur;
  el.innerHTML = "";
  el.appendChild(ta);
  ta.focus();
  let done = false;
  const commit = async () => {
    if (done) return;
    done = true;
    const val = ta.value;
    // 先回显避免等待网络时空白，再异步保存
    el.innerHTML = val.trim()
      ? escapeHtml(val)
      : '<span class="muted desc-add">点击添加</span>';
    if (val.trim() === cur) return;   // 无变化不请求
    try {
      await api(`/guests/${node}/${type}/${vmid}/description`, "POST", { description: val });
      if (g) g.description = val;     // 回写本地数据，避免下次编辑取到旧值
      toast("备注已保存", false);
    } catch (e) {
      toast("保存备注失败: " + e.message, true);
    }
  };
  ta.addEventListener("blur", commit);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ta.blur(); }
    else if (e.key === "Escape") { ta.value = cur; ta.blur(); }
  });
}

function filteredGuests() {
  const kw = $("searchBox").value.trim().toLowerCase();
  const type = $("typeFilter").value;
  const status = $("statusFilter").value;

  return allGuests
    .filter((g) => !g.template)   // 概览不显示模板 VM（模板归「模板快速建机」管理）
    .filter((g) => !selectedNode || g.node === selectedNode)
    .filter((g) => !type || g.type === type)
    .filter((g) => !status || g.status === status)
    .filter((g) => !kw || String(g.vmid).includes(kw) || (g.name || "").toLowerCase().includes(kw) || (g.ip || []).some((ip) => ip.includes(kw)))
    .sort((a, b) => a.vmid - b.vmid);
}

// 当前页虚机：按页码切片；筛选变化导致越页时自动夹回最后一页
function currentPageRows() {
  const rows = filteredGuests();
  const totalPages = Math.max(1, Math.ceil(rows.length / guestPageSize));
  guestPage = Math.min(Math.max(guestPage, 1), totalPages);
  return rows.slice((guestPage - 1) * guestPageSize, guestPage * guestPageSize);
}

function renderGuests() {
  renderNodeTabs();   // 节点标签的数量/选中态与虚机列表同步刷新
  const rows = filteredGuests();
  const pageRows = currentPageRows();

  // 节点筛选标签：提示当前只看某节点，点 ✕ 清除
  const tag = $("nodeFilterTag");
  tag.classList.toggle("hidden", !selectedNode);
  if (selectedNode) {
    tag.innerHTML = `节点: ${escapeHtml(selectedNode)} <b onclick="selectNode(${jsStr(selectedNode)})"><svg class="ico ico-sm"><use href="#ico-close"></use></svg></b>`;
  }

  $("emptyTip").classList.toggle("hidden", rows.length > 0);

  const guestHtml = pageRows.map((g) => {
    const key = `${g.node}|${g.type}|${g.vmid}`;
    // CPU / 内存与节点卡共用同一套使用率阈值：运行中且 ≥70% 才上色，正常值保持正文色
    const cpuPct = g.status === "running" ? `<b class="pct ${barClass(g.cpu || 0)}">${fmtPct(g.cpu)}%</b>` : "-";
    const memRatio = (g.status === "running" && g.maxmem) ? g.mem / g.maxmem : 0;
    const mem = g.status === "running"
      ? `<b class="pct ${barClass(memRatio)}">${fmtBytes(g.mem)} / ${fmtBytes(g.maxmem)}</b>`
      : "-";
    const ip = g.status === "running" ? (g.ip || []).join("，") || "-" : "-";
    return `
    <tr${g.template ? ' class="template-row"' : ""}>
      <td class="cb" data-label="选择"><input type="checkbox" ${selectedGuests.has(key) ? "checked" : ""} onchange="toggleGuestSelect(${jsStr(key)}, this.checked)"></td>
      <td data-label="状态">${statusBadge(g.status)}</td>
      <td data-label="VMID">${g.vmid}</td>
      <td class="name" data-label="名称">
        <span onclick="showConfig(${jsStr(g.node)},${jsStr(g.type)},${g.vmid},${jsStr(g.name || "")})">${escapeHtml(g.name || "-")}</span>
        ${g.template ? "" : `<span class="rename-icon" onclick="renameGuest(${jsStr(g.node)},${jsStr(g.type)},${g.vmid},${jsStr(g.name || "")})" title="重命名"><svg class="ico ico-sm"><use href="#ico-pencil"></use></svg></span>`}
      </td>
      <td data-label="类型"><span class="type-tag ${g.type}">${g.type === "qemu" ? "VM" : "LXC"}</span>${g.template ? '<span class="tpl-tag">模板</span>' : ""}</td>
      <td data-label="节点">${escapeHtml(g.node)}</td>
      <td data-label="CPU">${cpuPct}</td>
      <td data-label="内存">${mem}</td>
      <td data-label="IP 地址">${escapeHtml(ip)}</td>
      <td data-label="运行时长">${fmtUptime(g.uptime)}</td>
      <td data-label="操作">${guestActions(g)}</td>
      <td data-label="开机自启">${onbootCell(g)}</td>
      <td data-label="备注">${descriptionCell(g)}</td>
    </tr>`;
  }).join("");
  // 局部更新：仅当渲染结果真的变了才重写 innerHTML，避免自动刷新时整段重绘导致闪烁
  if (guestHtml !== lastGuestHtml) {
    $("guestBody").innerHTML = guestHtml;
    lastGuestHtml = guestHtml;
  }

  renderPager(rows.length);
  // 全选只作用于当前页，避免翻页后误操作看不到的虚机
  syncSelectAll(pageRows);
  updateBatchBar();
}

// 翻页条：总条数 + 页码 + 上/下一页 + 每页条数（10/20/30）；无数据时清空
function renderPager(total) {
  const pager = $("pagerBar");
  if (!pager) return;
  if (!total) { pager.innerHTML = ""; return; }
  const totalPages = Math.max(1, Math.ceil(total / guestPageSize));
  const btn = (label, page, disabled) =>
    `<button class="pager-btn" ${disabled ? "disabled" : ""} onclick="gotoPage(${page})">${label}</button>`;
  pager.innerHTML = `
    <span class="pager-info">共 ${total} 条 · 第 ${guestPage}/${totalPages} 页</span>
    <span class="pager-ctrl">
      ${btn("上一页", guestPage - 1, guestPage <= 1)}
 ${btn("下一页", guestPage + 1, guestPage >= totalPages)}
      <select class="pager-size" onchange="changePageSize(this.value)">
        ${[10, 20, 30].map((v) => `<option value="${v}" ${v === guestPageSize ? "selected" : ""}>${v} 条/页</option>`).join("")}
      </select>
    </span>`;
}

function gotoPage(p) {
  guestPage = p;
  renderGuests();
}

// 切换每页条数后回第一页，避免越页
function changePageSize(v) {
  guestPageSize = parseInt(v, 10) || 10;
  guestPage = 1;
  renderGuests();
}

// ---- 批量选择与批量操作 ----

const selectedGuests = new Set();   // key = node|type|vmid，跨自动刷新保持

function guestKey(g) { return `${g.node}|${g.type}|${g.vmid}`; }

function toggleGuestSelect(key, checked) {
  if (checked) selectedGuests.add(key); else selectedGuests.delete(key);
  syncSelectAll(currentPageRows());   // 全选框口径与「全选」一致：只统计当前页，避免跨页勾选时状态错乱
  updateBatchBar();
}

function toggleSelectAll(cb) {
  // 全选口径与翻页一致：只勾当前页，不勾看不见的行
  const rows = currentPageRows();
  rows.forEach((g) => { if (cb.checked) selectedGuests.add(guestKey(g)); else selectedGuests.delete(guestKey(g)); });
  renderGuests();
}

function clearSelection() {
  selectedGuests.clear();
  renderGuests();
}

function syncSelectAll(rows) {
  const cb = $("selectAllGuests");
  cb.checked = rows.length > 0 && rows.every((g) => selectedGuests.has(guestKey(g)));
}

function updateBatchBar() {
  const n = selectedGuests.size;
  $("batchBar").classList.toggle("hidden", n === 0);
  $("batchCount").textContent = `已选 ${n} 个虚机/容器`;
}

// 批量执行：仅对状态匹配的虚机下发指令（启动只对已停止，关机/强停/重启只对运行中）
async function batchAction(action) {
  const targets = allGuests.filter((g) => selectedGuests.has(guestKey(g)));
  if (!targets.length) return toast("请先勾选虚机/容器", true);
  const eligible = targets.filter((g) => action === "start" ? g.status === "stopped" : g.status === "running");
  if (!eligible.length) return toast(`选中的虚机/容器均与「${ACTION_NAMES[action]}」状态不匹配`, true);
  const skipped = targets.length - eligible.length;
  const desc = `${ACTION_NAMES[action]} ${eligible.length} 个虚机${skipped ? `（另有 ${skipped} 个状态不匹配将跳过）` : ""}`;
  if (!(await appConfirm(`确定要批量${desc}吗？`))) return;

  let ok = 0;
  const fails = [];
  for (const g of eligible) {
    try {
      await api(`/guests/${g.node}/${g.type}/${g.vmid}/${action}`, "POST");
      ok++;
    } catch (e) {
      fails.push(`${g.vmid}`);
    }
  }
  toast(fails.length ? `批量${ACTION_NAMES[action]}：成功 ${ok}，失败 ${fails.length}（${fails.join(", ")}）` : `批量${ACTION_NAMES[action]}指令已下发（${ok} 个）`, !!fails.length);
  setTimeout(refresh, 1200);
}

// ---- 数据加载 ----

// 首次加载前的骨架屏占位，避免白屏等待
function renderSkeleton() {
  $("nodeCards").innerHTML = Array(2).fill(
    `<div class="card skeleton-card">
       <div class="sk-line sk-w40"></div>
       <div class="sk-bar"></div><div class="sk-bar"></div>
     </div>`).join("");
  $("guestBody").innerHTML = Array(4).fill(
    `<tr class="skeleton-row">${'<td><div class="sk-line"></div></td>'.repeat(10)}</tr>`).join("");
  // 骨架行是直接写进表体的，缓存字符串（以及它代表的「表体内容」）随之失效，必须置 null。
  // 否则虚机为空时 renderGuests 里 guestHtml 也是 ""，会与初始值 "" 判成「没变化」而跳过重写，
  // 骨架行便永久残留在表体里（表现为灰条与「没有匹配的虚机/容器」同时出现）。
  lastGuestHtml = null;
}

async function refresh(manual = false) {
  // 手动刷新期间按钮图标持续旋转，完成后恢复
  const btn = $("btnRefresh");
  if (manual) btn.classList.add("loading");
  try {
    // 手动刷新带 fresh=1：/guests 同步重采；/nodes 由服务端排后台重采（慢硬件不阻塞请求）
    const [nodes, guests] = await Promise.all([api(manual ? "/nodes?fresh=1" : "/nodes"), api(manual ? "/guests?fresh=1" : "/guests")]);
    allNodes = nodes;
    renderNodes(nodes);
    allGuests = guests;
    renderGuests();
    $("refreshTime").textContent = "更新于 " + new Date().toLocaleTimeString() + "（每 10 秒自动刷新）";
    // 手动刷新：服务端只把重采排到后台，稍后再回读一次拿新数据（无需用户再点）
    if (manual) reReadAfterManualRefresh(() => refresh());
  } catch (e) {
    toast("加载失败: " + e.message, true);
  } finally {
    if (manual) btn.classList.remove("loading");
  }
}

// 重置自动刷新计时；手动刷新后从头计 10 秒，避免刚点完又紧跟一次自动刷新
function restartAutoRefresh() {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    if (isModalOpen()) return;  // 弹窗打开时跳过，避免重渲染干扰操作
    refresh();
  }, 10000);
}

// ---- 操作 ----

const ACTION_NAMES = { start: "启动", stop: "强制停止", shutdown: "关机", reboot: "重启" };

async function doAction(key, action) {
  const [node, type, vmid] = key.split("|");
  // 破坏性操作需要二次确认（走应用内确认框，与工具箱执行确认同一视觉）
  if (["stop", "shutdown", "reboot"].includes(action)) {
    if (!(await appConfirm(`确定要${ACTION_NAMES[action]} ${vmid} 吗？`, { danger: true }))) return;
  }
  try {
    await api(`/guests/${node}/${type}/${vmid}/${action}`, "POST");
    // 启动操作：后台验证是否真正进入 running，QEMU 启动失败（磁盘权限等）能及时反馈
    if (action === "start") {
      toast(`正在启动 ${vmid} ...`);
      verifyStart(node, type, vmid);
    } else {
      toast(`${ACTION_NAMES[action]}指令已发送 (${vmid})`);
    }
    setTimeout(refresh, 1200);
  } catch (e) {
    toast(`操作失败: ${e.message}`, true);
  }
}

// 启动后轮询状态，检测 QEMU 启动失败（磁盘权限/镜像损坏等），失败则红字提示
async function verifyStart(node, type, vmid) {
  for (let i = 0; i < 10; i++) {
 await new Promise((r) => setTimeout(r, 1000));
    try {
      const st = await api(`/guests/${node}/${type}/${vmid}/status`);
      const status = st && st.status;
      if (status === "running") {
        toast(`${vmid} 已成功启动`);
        refresh();
        return;
      }
    } catch (e) {
      // 查询失败可能是虚机短暂不可达，继续重试
    }
  }
  toast(`${vmid} 启动失败（10 秒内未进入运行状态，请查看执行日志排查，常见原因：磁盘权限/镜像损坏）`, true);
  refresh();
}

// 由 PVE 配置 + 当前名推导发行版标签，拼成标准主机名 <label>-<vmid>
function deriveHostLabel(type, oldName, cfg) {
  const name = (oldName || "").toLowerCase();
  const ostype = (cfg && cfg.ostype ? String(cfg.ostype) : "").toLowerCase();
  // 从 VM 名称解析发行版（QEMU 模板名常含发行版字样）
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
  for (const [k, v] of KW) if (name.includes(k)) return v;
  if (type === "lxc") {
    // LXC 的 ostype 本身就是发行版名（centos/ubuntu/debian...）
    if (ostype && /^[a-z0-9]+$/.test(ostype)) return ostype;
    return "host";
  }
  // QEMU：ostype 较粗（l26/win10/w2k*），未命中回退 linux
  if (ostype.startsWith("win") || ostype.startsWith("w2k")) return "win";
  if (ostype === "solaris") return "solaris";
  return "linux";
}

function suggestHostname(type, vmid, oldName, cfg) {
  return `${deriveHostLabel(type, oldName, cfg)}-${vmid}`;
}

// 主机名规则：全小写、仅 [a-z0-9-]、长度<=63、首尾非 -、无下划线
function validateHostname(name) {
 if (!name) return "名称不能为空";
 if (name.length > 63) return "名称长度不能超过 63 个字符";
  if (!/^[a-z0-9-]+$/.test(name)) return "只能包含小写字母、数字和连字符，且不能使用下划线";
  if (name.startsWith("-") || name.endsWith("-")) return "不能以连字符开头或结尾";
  return null;
}

async function renameGuest(node, type, vmid, oldName) {
  // 自动预填标准主机名：系统标签 + VMID
  let suggestion = oldName || "";
  try {
    const cfg = await api(`/guests/${node}/${type}/${vmid}/config`);
    suggestion = suggestHostname(type, vmid, oldName, cfg);
  } catch (e) {
    suggestion = oldName || "";   // 取不到配置就用原名兜底
  }
  const name = prompt(`重命名 ${vmid}（当前：${oldName || "-"}）\n已按标准主机名预填（系统-${vmid}），可修改`, suggestion);
  if (name === null) return;                       // 取消
  const trimmed = name.trim().toLowerCase();
  const err = validateHostname(trimmed);
  if (err) return toast(err, true);
  if (trimmed === (oldName || "").toLowerCase()) return;
  try {
    await api(`/guests/${node}/${type}/${vmid}/rename`, "POST", { name: trimmed });
    toast(`已重命名为「${trimmed}」`, false);
    refresh(true);   // 强制穿透服务端短缓存，让列表立即显示新名
  } catch (e) {
    toast("重命名失败: " + e.message, true);
  }
}

async function showConfig(node, type, vmid, name) {
  $("modalTitle").textContent = `${vmid} - ${name} 配置`;
  $("modalBody").innerHTML = '<p class="muted">加载中...</p>';
  $("modal").classList.remove("hidden");
  try {
    const cfg = await api(`/guests/${node}/${type}/${vmid}/config`);
    const rows = Object.keys(cfg).sort().map((k) =>
      `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(cfg[k])}</td></tr>`).join("");
    $("modalBody").innerHTML = `<table>${rows}</table>`;
  } catch (e) {
    $("modalBody").innerHTML = `<p class="muted">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}
