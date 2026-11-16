// 传输中心：上传文件 / 获取云镜像 / 获取 LXC 模板 / 存储内容 /
// 缓存加速 五个子模块，以及它们共用的可搜索下拉（SearchSelect）与存储管理表单、分片上传参数。
//
// 依赖方向：调用 js/ui.js（$、格式化、转义、Toast、appConfirm）与 js/api.js（api/cachedApi）；
// 读写 js/state.js 的全局状态；被 app.js 的初始化流程与 index.html 的内联 onclick
// （selectTcPane / tcOnTypeChange / tcClearFinished …）调用。classic script 加载，函数仍在全局作用域。

// 分片上传参数：8MB 太碎（4GB 要 512 次请求，且旧实现串行——每次都得等上一片写完，跨网时每片
// 一个往返更拖）。改为 32MB/片 + 最多 4 片并发：服务端是按 index 各写一个 partN 文件、收齐才合并
// （合并处有锁），天然支持乱序与并发；请求数降到 1/4，高带宽/高延迟下明显更快。
const UPLOAD_CHUNK = 32 * 1024 * 1024;  // 32MB/片
const UPLOAD_PARALLEL = 4;              // 同时在传的分片数

// 打开统一上传弹窗
// ===== 传输中心（全局上传/下载模块，前端骨架；后端通道待接入） =====
const tcTransfers = [];   // { id, name, dir:'up'|'down'|'lxc', state, pct, error, cancel? }
                          // cancel：上传任务的本地中止回调（下载类走后端取消接口，不用它）
let tcSeq = 0;

// 渲染活动传输列表 + 顶部计数徽标
// 上传/下载两个面板的「活动传输」各自只显示本方向的传输（通过 .tc-activity-wrap 上的 data-tc-dir 区分），
// 避免全局列表被两个面板共用导致「下载时上传面板也出现、上传时下载面板也出现」的串台
// 传输是否仍在进行：上传的「合并中」（分片已全部送达、服务端在后台拼文件）也算进行中
const tcActive = (t) => t.state === "running" || t.state === "merging";

function tcRenderActivity() {
  document.querySelectorAll(".tc-activity-wrap").forEach((wrap) => {
    const dir = wrap.dataset.tcDir || "";
    const list = dir ? tcTransfers.filter((t) => t.dir === dir) : tcTransfers;
    wrap.classList.toggle("tc-activity-empty", !list.length);
    const cnt = wrap.querySelector(".tc-active-count"); if (cnt) cnt.textContent = list.length;
    // 「清除已结束」按钮：本方向有已结束（完成/失败/已取消）条目时才出现，并带上条数
    const clr = wrap.querySelector("[data-tc-clear]");
    if (clr) {
      const n = list.filter((t) => !tcActive(t)).length;
      clr.classList.toggle("hidden", !n);
      clr.textContent = n ? `清除已结束（${n}）` : "清除已结束";
    }
    const act = wrap.querySelector(".tc-activity");
    act.innerHTML = !list.length
      ? '<div class="tc-empty">暂无进行中的传输</div>'
      : list.map((t) => `
   <div class="tc-item">
     <span class="tc-name">${t.dir === "up" ? "↑" : "↓"} ${escapeHtml(t.name)}</span>
     ${t.state === "error" && t.error
        ? `<span class="tc-err" title="${escapeHtml(t.error)}">${escapeHtml(t.error)}</span>` : ""}
     <div class="iso-progress ${tcActive(t) ? "" : "hidden"}"><i style="width:${t.pct}%"></i></div>
      <span class="tc-state ${t.state}">${{ running: t.pct + "%", merging: "合并中…", done: "完成", cancel: "已取消" }[t.state] || "失败"}</span>
      ${t.state === "running" && (t.dir === "down" || t.dir === "lxc" || t.dir === "up")
        ? `<button type="button" class="tc-cancel" title="${t.dir === "up"
 ? "取消上传（已传分片保留，重选同一文件可续传）" : "强制取消下载"}" onclick="tcCancelTransfer(${t.id})">✕</button>`
        : ""}
    </div>`).join("");
  });
}

// 取消活动列表中的传输：
//   上传 → 本地中止在飞的 XHR（服务端已收分片保留，重选同一文件可续传，无需调后端清理）；
//   下载 → 云镜像与 LXC 模板各走自己的取消接口（后端清理 .part 半成品）
function tcCancelTransfer(id) {
  const t = tcTransfers.find((x) => x.id === id);
  if (!t || t.state !== "running") return;
  if (t.dir === "up") {
    if (t.cancel) { try { t.cancel(); } catch (e) {} }
    t.state = "cancel";
    tcRenderActivity();
    return;
  }
  const apiPath = t.dir === "lxc" ? "/lxc-template/cancel" : "/cloud-image/cancel";
  api(apiPath, "POST", { name: t.name }).catch(() => {});
  t.state = "cancel";
  tcRenderActivity();
}

function tcAddTransfer(name, dir) {
  const t = { id: ++tcSeq, name, dir, state: "running", pct: 0 };
  tcTransfers.unshift(t);
  tcRenderActivity();
  return t;
}

// 清除活动列表里已结束的条目（完成 / 失败 / 已取消）：只清当前面板方向的，进行中的不动
function tcClearFinished(btn) {
  const wrap = btn ? btn.closest("[data-tc-dir]") : null;
  const dir = wrap ? (wrap.dataset.tcDir || "") : "";
  const kept = tcTransfers.filter((t) => !(t.dir === dir && !tcActive(t)));
  if (kept.length === tcTransfers.length) return;
  tcTransfers.splice(0, tcTransfers.length, ...kept);
  tcRenderActivity();
}

// 折叠/展开：折叠所属传输面板
function tcToggleCenter(btn) {
  const c = btn ? btn.closest(".transfer-center") : $("uploadCenter");
  if (!c) return;
  const collapsed = c.classList.toggle("tc-collapsed");
  // 图标跟随面板状态旋转（只改 transform，不触发重排）
  const ico = btn && btn.querySelector(".ico-chev");
  if (ico) ico.classList.toggle("rot-r", collapsed);
}

// 项目共享存储（pvewebtools）：仅服务于传输中心；其它工具的「选存储」下拉不暴露该存储
const SHARED_STORAGE = "pvewebtools";
const isSharedStorage = (s) => s.storage === SHARED_STORAGE;

// 存储内容：加载本机所有已启用存储到选择器，不过滤任何存储。
// 含 PVE 默认的 local / local-lvm / local-zfs 等：该面板不再只围绕 pvewebtools 工作，
// PVE 自带存储里的内容同样可以在这里浏览 / 下载 / 删除。
// 默认选中项目共享存储 pvewebtools；用户已选的存储若仍存在则保持不变（避免重建列表时跳回默认）。
async function tcLoadContentStorages() {
  const sel = $("tcContentStorage");
  if (!sel) return;
  const keep = sel.value;   // 重建选项前记录当前选择
  try {
    // /storages 返回本机所有已配置存储（含 pvewebtools，此时 content 为空），pvewebtools 会自然出现，无需兜底注入。
    // 30s 前端缓存：存储定义是分钟级事实，切标签/切节点不必重复拉取（后端读 storage.cfg，本就很快）
    const ss = await cachedApi("/storages", 30000);
    sel.innerHTML = ss
      .map((s) => `<option value="${escapeHtml(s.storage)}">${escapeHtml(s.storage)}${s.content ? `（${escapeHtml(s.content)}）` : ""}</option>`).join("");
    const has = (n) => ss.some((s) => s.storage === n);
    if (keep && has(keep)) sel.value = keep;
    else if (has(SHARED_STORAGE)) sel.value = SHARED_STORAGE;
    // 原选中存储已不存在（被删）：下拉落到别的存储上，内容区停在旧存储会「选 A 显示 B」，同步重列
    if (keep && sel.value !== keep) tcLoadStorageContent();
  } catch (e) {
    sel.innerHTML = "";
    toast(`读取存储列表失败：${e.message || e}`, true);
  }
}

// 存储内容：可按类型进行下载 / 删除的内容集合
const SC_DL_TYPES = new Set(["vztmpl", "backup", "snippets", "import", "vbios"]);
// vbios 不在此列：它的删除要走 DELETE /api/vbios（连带清理节点侧副本），见 tcDeleteVbios
const SC_DEL_TYPES = new Set(["vztmpl", "backup", "snippets", "import"]);
// 不在本面板渲染的分组：images/rootdir 为只读保护（误删会丢 VM 盘）；iso 本工具不上传也不处理，一并隐藏
const SC_HIDDEN_TYPES = new Set(["images", "rootdir", "iso"]);
// 支持在存储之间移动的内容类型（目标目录按 PVE 约定保持不变；images/rootdir 涉及 VM 命名与引用，不支持）
const SC_MOVE_TYPES = new Set(["iso", "vztmpl", "backup", "snippets", "import"]);

// 存储内容：加载并渲染指定存储的已有内容，按 content 类型分组
async function tcLoadStorageContent() {
  const sel = $("tcContentStorage");
  const box = $("tcContentList");
  if (!sel || !box) return;
  const storage = sel.value;
  if (!storage) { box.innerHTML = '<div class="muted">请选择一个存储</div>'; return; }
  box.innerHTML = '<div class="muted">加载中…</div>';
  try {
    const [items, imports] = await Promise.all([
      api(`/storage/${encodeURIComponent(storage)}/content`),
      // 导入目录（cloud-images / disk-images / import）按所选存储过滤，与上方内容列表同一口径
      api(`/disks/imported?storage=${encodeURIComponent(storage)}`).catch(() => []),
    ]);
    const contentHtml = renderStorageContent(items);
    const importHtml = renderImportDisks(imports || []);
    let html = (contentHtml || "") + importHtml;
    if (!html) html = '<div class="muted">该存储暂无内容</div>';
    box.innerHTML = html;
    // 为每行操作按钮绑定事件：PVE 内容（data-volid）/ 导入目录（data-name + data-storage）
    box.querySelectorAll("[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const act = btn.dataset.act;
        if (act === "dl") return tcDownloadContent(btn.getAttribute("data-volid"));
        if (act === "mv") return tcMoveContent(btn.getAttribute("data-volid"));
        if (act === "del") return tcDeleteContent(btn.getAttribute("data-volid"));
        if (act === "del-vbios") return tcDeleteVbios(btn.getAttribute("data-name"));
        if (act === "dl-imp") return tcDownloadImport(btn.getAttribute("data-name"), btn.getAttribute("data-storage"), btn.getAttribute("data-kind"));
        if (act === "del-imp") return tcDeleteImport(btn.getAttribute("data-name"), btn.getAttribute("data-storage"), btn.getAttribute("data-kind"));
        if (act === "mv-imp") return tcMoveImport(btn.getAttribute("data-name"), btn.getAttribute("data-storage"), btn.getAttribute("data-subdir"));
      });
    });
    if (!box.dataset.scBound) {
      box.addEventListener("click", (ev) => {
        const title = ev.target.closest(".sc-group-title");
        if (!title) return;
        if (ev.target.closest("label, input")) return; // 不拦截「全选本组」/只读
        const grp = title.closest(".sc-group");
        const collapsed = grp.classList.toggle("collapsed");
        const caretIco = title.querySelector(".sc-caret .ico-chev");
        if (caretIco) caretIco.classList.toggle("rot-r", collapsed);
      });
      box.dataset.scBound = "1";
    }
    // 批量选择：行复选框 + 分组全选 + 刷新选择状态
    box.querySelectorAll(".sc-chk").forEach((cb) => cb.addEventListener("change", updateScSelection));
    box.querySelectorAll(".sc-grp-chk").forEach((cb) => cb.addEventListener("change", () => {
      const grp = cb.closest(".sc-group");
      grp.querySelectorAll(".sc-chk").forEach((rc) => { rc.checked = cb.checked; });
      updateScSelection();
    }));
    updateScSelection();
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message || e)}</div>`;
  }
}

function renderStorageContent(items) {
  if (!Array.isArray(items) || !items.length) return "";
  // vbios 为项目自管的合成分组（PVE 无此内容类型，仅 pvewebtools 有）
  const order = ["backup", "vbios", "iso", "vztmpl", "snippets", "images", "rootdir", "import"];
  const labels = { backup: "虚机模板", vbios: "vBIOS ROM", iso: "ISO 镜像", vztmpl: "容器模板", snippets: "配置片段", images: "磁盘镜像", rootdir: "容器", import: "导入" };
  const groups = {};
  let hasReadOnly = false;
  for (const it of items) {
    const c = it.content || "other";
    // 不在本面板渲染的分组：images/rootdir 只读保护（误删会丢 VM 盘）；iso 本工具不上传也不处理，一并隐藏
    if (SC_HIDDEN_TYPES.has(c)) {
      if (c === "images" || c === "rootdir") hasReadOnly = true;
      continue;
    }
    (groups[c] = groups[c] || []).push(it);
  }
  const keys = Object.keys(groups).sort((a, b) => {
    const ia = order.indexOf(a), ib = order.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
 });
 if (!keys.length) {
 if (hasReadOnly) return '<div class="muted">该存储仅含只读内容（磁盘镜像 / 容器），此面板不提供操作</div>';
    return "";
  }
  return keys.map((c) => {
    // sel 决定该组是否可勾选/批量删除：vbios 走专用删除接口，但仍允许勾选（批量时另路处理）
    const sel = SC_DEL_TYPES.has(c) || c === "vbios";
    const ro = SC_HIDDEN_TYPES.has(c);
    const rows = groups[c].map((it) => {
      const path = String(it.volid || "").split(":").slice(1).join(":");
      const name = path.split("/").pop();
      const size = it.size != null ? fmtBytes(it.size) : "—";
      const time = it.ctime ? new Date(it.ctime * 1000).toLocaleString() : "—";
      const vm = it.vmid != null ? ` · VM ${it.vmid}` : "";
      const acts = [];
      if (SC_DL_TYPES.has(c)) acts.push(`<button type="button" class="btn btn-sm" data-act="dl" data-volid="${escapeHtml(it.volid)}">下载</button>`);
      if (SC_MOVE_TYPES.has(c)) acts.push(`<button type="button" class="btn btn-sm" data-act="mv" data-volid="${escapeHtml(it.volid)}">移动</button>`);
      if (c === "vbios") acts.push(`<button type="button" class="btn btn-sm danger" data-act="del-vbios" data-name="${escapeHtml(name)}">删除</button>`);
      else if (SC_DEL_TYPES.has(c)) acts.push(`<button type="button" class="btn btn-sm danger" data-act="del" data-volid="${escapeHtml(it.volid)}">删除</button>`);
      const actHtml = acts.length ? `<span class="sc-actions">${acts.join("")}</span>` : "";
      // 勾选项：vbios 另用 kind 标记，批量删除时改走 /api/vbios
      const chk = sel
        ? (c === "vbios"
          ? `<input type="checkbox" class="sc-chk" data-kind="vbios" data-name="${escapeHtml(name)}">`
          : `<input type="checkbox" class="sc-chk" data-kind="content" data-volid="${escapeHtml(it.volid)}">`)
        : "";
      return `<div class="sc-row${ro ? " sc-row--ro" : ""}">${chk}<span class="sc-name">${escapeHtml(name)}</span>` +
             `<span class="sc-meta">${escapeHtml(size)}${vm} · ${escapeHtml(time)}</span>${actHtml}</div>`;
    }).join("");
    let grpRight = "";
    if (sel) grpRight = `<label class="sc-grp-sel"><input type="checkbox" class="sc-grp-chk">全选本组</label>`;
    else if (ro) grpRight = `<span class="sc-grp-ro">只读</span>`;
    return `<div class="sc-group"><div class="sc-group-title"><span class="sc-caret"><svg class="ico ico-sm ico-chev"><use href="#ico-chevron"></use></svg></span>${escapeHtml(labels[c] || c)}（${groups[c].length}）${grpRight}</div><div class="sc-group-body">${rows}</div></div>`;
  }).join("");
}

// 下载存储内已有文件：以浏览器下载方式触发（GET /api/storage/download）
function tcDownloadContent(volid) {
  const storage = $("tcContentStorage").value;
  if (!storage) return toast("请先选择存储", true);
  const qs = new URLSearchParams({ storage, volid }).toString();
  window.location.href = `/api/storage/download?${qs}`;
}

// 删除存储内已有文件（iso/vztmpl/backup/snippets）
async function tcDeleteContent(volid) {
  const storage = $("tcContentStorage").value;
  if (!storage) return toast("请先选择存储", true);
  if (!(await appConfirm(`确定删除 ${volid} ？此操作不可恢复`, { danger: true }))) return;
  try {
    await api("/storage", "DELETE", { storage, volid });
    toast("已删除");
    tcLoadStorageContent();
  } catch (e) {
    toast("删除失败：" + (e.message || e), true);
  }
}

// 存储之间移动文件：目标存储 + 目标目录（合法候选由后端 move-targets 给出，避免落进 PVE 不扫描的目录）
// → POST /api/storage/move-content；同盘为瞬时改名，跨盘为复制后删除
async function tcMoveContent(volid) {
  const storage = $("tcContentStorage").value;
  if (!storage) return toast("请先选择存储", true);
  const ctype = String(volid).split(":").slice(1).join(":").split("/")[0];
  if (!SC_MOVE_TYPES.has(ctype)) return toast(`该类型（${ctype}）不支持移动`, true);
  const name = String(volid).split("/").pop();
  let targets = [];
  try {
    targets = await api(`/storage/move-targets?ctype=${encodeURIComponent(ctype)}&storage=${encodeURIComponent(storage)}`);
  } catch (e) {
    return toast("获取目标存储失败：" + (e.message || e), true);
  }
  if (!targets || !targets.length) return toast(`没有其他可用于 ${ctype} 的目标存储`, true);
  const storeOpts = targets.map((t) => `<option value="${escapeHtml(t.storage)}">${escapeHtml(t.storage)}</option>`).join("");
  $("modalTitle").textContent = `移动文件：${name}`;
  $("modalBody").innerHTML = `<div class="sm-form">
    <p class="muted u-para" >源：<code>${escapeHtml(volid)}</code>。同盘瞬时改名，跨盘复制后删除（大文件较慢，勿关页）</p>
    <div class="tc-row"><label>目标存储</label><select id="mvContentTarget" class="form-control">${storeOpts}</select></div>
    <div class="tc-row"><label>目标目录</label><select id="mvContentDir" class="form-control"></select></div>
    <p class="muted u-para" id="mvContentNote" ></p>
    <p class="muted u-para" >已被 VM/CT 引用时（挂载的 ISO、选定的备份等），移动后引用失效，请先解除引用</p>
    <div class="tc-row tc-actions u-justify-end" >
      <button type="button" class="btn" onclick="smHideModal()">取消</button>
      <button type="button" class="btn run" id="mvContentOk">移动</button>
    </div>
  </div>`;
  const dirsBySid = {};
  targets.forEach((t) => { dirsBySid[t.storage] = t.dirs || []; });
  const curDirs = () => dirsBySid[$("mvContentTarget").value] || [];
  const showNote = () => {
    const d = curDirs().find((x) => x.subdir === $("mvContentDir").value);
    $("mvContentNote").textContent = d && d.note ? d.note : "";
  };
  const fillDirs = () => {
    const ds = curDirs();
    $("mvContentDir").innerHTML = ds
      .map((d) => `<option value="${escapeHtml(d.subdir)}"${d.default ? " selected" : ""}>${escapeHtml(d.label || d.subdir)}</option>`)
      .join("");
    showNote();
  };
  $("mvContentTarget").addEventListener("change", fillDirs);
  $("mvContentDir").addEventListener("change", showNote);
  fillDirs();
  smShowModal();
  $("mvContentOk").onclick = async () => {
    const dst = $("mvContentTarget").value;
    const subdir = $("mvContentDir").value;
    const btn = $("mvContentOk");
    btn.disabled = true;
    try {
      const r = await api("/storage/move-content", "POST", { storage, volid, dst, subdir });
      smHideModal();
      toast(r && r.to ? `已移动到 ${r.to}` : `已移动到 ${dst}`);
      tcLoadStorageContent();
    } catch (e) {
      toast("移动失败：" + (e.message || e), true);
      btn.disabled = false;
    }
  };
}

// 存储内容：渲染「导入目录」分组（上传/下载的磁盘镜像，PVE content API 不收录，故单独列出并提供下载/删除）
// 按 kind 拆成两组：cloud-images=含 cloud-init 的云镜像（供云镜像建机）；disk-images=普通磁盘镜像（供磁盘导入）
function renderImportDisks(items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) return "";
  const groups = { cloud: [], disk: [] };
  for (const it of list) (it.kind === "disk" ? groups.disk : groups.cloud).push(it);
  // 目录名随存储而变：cloud-images 通用；普通磁盘在 pvewebtools 为 disk-images，在其它存储为 import
  const labels = { cloud: "云镜像（cloud-images，含 cloud-init，供云镜像建机）", disk: "磁盘镜像（disk-images / import，普通磁盘，供虚机磁盘导入）" };
  return Object.keys(groups).filter((k) => groups[k].length).map((k) => {
    const rows = groups[k].map((it) => {
      const size = it.size != null ? fmtBytes(it.size) : "—";
      const st = it.storage === "__import__" ? "默认导入目录" : it.storage;
      // 「移动」对任意存储可用：pvewebtools 走 dump/cloud-images/disk-images/vbios；其它存储走 cloud-images/import
      const sid = it.storage === "__import__" ? SHARED_STORAGE : it.storage;
      const srcSubdir = k === "cloud" ? "cloud-images" : (it.storage === "__import__" ? "disk-images" : "import");
      const moveBtn = `<button type="button" class="btn btn-sm" data-act="mv-imp" data-name="${escapeHtml(it.name)}" data-storage="${escapeHtml(sid)}" data-subdir="${srcSubdir}">移动</button>`;
      const acts =
        `<span class="sc-actions">` +
        `<button type="button" class="btn btn-sm" data-act="dl-imp" data-name="${escapeHtml(it.name)}" data-storage="${escapeHtml(it.storage)}" data-kind="${k}">下载</button>` +
        moveBtn +
        `<button type="button" class="btn btn-sm danger" data-act="del-imp" data-name="${escapeHtml(it.name)}" data-storage="${escapeHtml(it.storage)}" data-kind="${k}">删除</button>` +
        `</span>`;
      const chk = `<input type="checkbox" class="sc-chk" data-kind="${k}" data-name="${escapeHtml(it.name)}" data-storage="${escapeHtml(it.storage)}">`;
      return `<div class="sc-row">${chk}<span class="sc-name">${escapeHtml(it.name)}</span>` +
             `<span class="sc-meta">${escapeHtml(size)} · ${escapeHtml(st)}</span>${acts}</div>`;
    }).join("");
    const grpSel = `<label class="sc-grp-sel"><input type="checkbox" class="sc-grp-chk">全选本组</label>`;
    // 与 renderStorageContent 同构：标题带折叠箭头、行包在 sc-group-body 里，
    // 这样导入目录分组（云镜像/磁盘镜像）也能折叠（点击事件按 .sc-group-title 通用委托）
    return `<div class="sc-group"><div class="sc-group-title"><span class="sc-caret"><svg class="ico ico-sm ico-chev"><use href="#ico-chevron"></use></svg></span>${escapeHtml(labels[k])}（${groups[k].length}）${grpSel}</div><div class="sc-group-body">${rows}</div></div>`;
  }).join("");
}

// 下载导入目录中的磁盘镜像（GET /api/disks/download?kind=import，浏览器触发下载）
function tcDownloadImport(name, storage, kind) {
  const qs = new URLSearchParams({ kind: kind || "disk", storage: storage || "__import__", name }).toString();
  window.location.href = `/api/disks/download?${qs}`;
}

// 删除导入目录中的磁盘镜像（POST /api/disks/delete?kind=import，参数走 query）
async function tcDeleteImport(name, storage, kind) {
  if (!(await appConfirm(`确定删除导入目录中的 ${name} ？此操作不可恢复`, { danger: true }))) return;
  try {
    await api(`/disks/delete?kind=${encodeURIComponent(kind || "disk")}&storage=${encodeURIComponent(storage || "__import__")}&name=${encodeURIComponent(name)}`, "POST");
    toast("已删除");
    tcLoadStorageContent();
  } catch (e) {
    toast("删除失败：" + (e.message || e), true);
  }
}

// 在所选存储的各子目录间移动文件
// （pvewebtools：dump ↔ cloud-images ↔ disk-images ↔ vbios；其它存储：cloud-images ↔ import）
function tcMoveImport(name, storage, subdir) {
  const sid = !storage || storage === "__import__" ? SHARED_STORAGE : storage;
  const all = sid === SHARED_STORAGE ? ["dump", "cloud-images", "disk-images", "vbios"] : ["cloud-images", "import"];
  const opts = all.filter((s) => s !== subdir).map((s) => `<option value="${s}">${s}</option>`).join("");
  $("modalTitle").textContent = `移动文件：${name}`;
  $("modalBody").innerHTML = `<div class="sm-form">
    <p class="muted u-para" >存储：<code>${escapeHtml(sid)}</code>，当前位置：<code>${escapeHtml(subdir)}</code>。选择目标子目录后，文件（含元数据侧车）将移动到该目录。</p>
    <div class="tc-row"><label>目标子目录</label><select id="mvDst" class="form-control">${opts}</select></div>
    <div class="tc-row tc-actions u-justify-end" >
      <button type="button" class="btn" onclick="smHideModal()">取消</button>
      <button type="button" class="btn run" id="mvOk">移动</button>
    </div>
  </div>`;
  smShowModal();
  $("mvOk").onclick = async () => {
    const dst = $("mvDst").value;
    const btn = $("mvOk");
    btn.disabled = true;
    try {
      await api("/storage/move", "POST", { storage: sid, name, src: subdir, dst });
      smHideModal();
      toast(`已移动到 ${dst}`);
      tcLoadStorageContent();
    } catch (e) {
      toast("移动失败：" + (e.message || e), true);
      btn.disabled = false;
    }
  };
}

// 存储内容：删除 vBIOS ROM（合成分组）
// 走 DELETE /api/vbios 而非通用 /api/storage：vBIOS 主副本在共享存储 vbios/，
// 各节点 /usr/share/kvm/ 有软链/副本，需由该接口一并清理，避免留下悬空软链
async function tcDeleteVbios(name) {
  if (!name) return;
  if (!(await appConfirm(`确定删除 vBIOS ROM ${name} ？删除后需重新上传`, { danger: true }))) return;
  try {
    await api("/vbios", "DELETE", { name });
    toast(`已删除 ${name}`);
    tcLoadStorageContent();
  } catch (e) {
    toast("删除失败：" + (e.message || e), true);
  }
}

// 存储内容：刷新批量选择状态（计数 / 全选框三态 / 显示或隐藏操作条）
function updateScSelection() {
  const box = $("tcContentList");
  const bar = $("scBatchBar");
  const cnt = $("scSelCount");
  const selAll = $("scSelectAll");
  if (!box || !bar) return;
  const all = Array.from(box.querySelectorAll(".sc-chk"));
  const checked = all.filter((c) => c.checked);
  if (cnt) cnt.textContent = String(checked.length);
  bar.classList.toggle("hidden", checked.length === 0);
  if (selAll) {
    selAll.checked = all.length > 0 && checked.length === all.length;
    selAll.indeterminate = checked.length > 0 && checked.length < all.length;
 }
 // 各分组全选框三态
 box.querySelectorAll(".sc-group").forEach((grp) => {
    const gAll = Array.from(grp.querySelectorAll(".sc-chk"));
    const gChk = grp.querySelector(".sc-grp-chk");
    if (!gChk || !gAll.length) return;
    const gChecked = gAll.filter((c) => c.checked).length;
    gChk.checked = gChecked === gAll.length;
    gChk.indeterminate = gChecked > 0 && gChecked < gAll.length;
 });
}

// 存储内容：批量删除选中项（content 走 DELETE /api/storage，导入目录走 POST /api/disks/delete?kind=import）
async function tcBatchDelete() {
 const box = $("tcContentList");
 const storage = $("tcContentStorage").value;
 if (!box || !storage) return toast("请先选择存储", true);
 const sel = Array.from(box.querySelectorAll(".sc-chk:checked"));
 if (!sel.length) return toast("请先勾选要删除的文件", true);
 if (!(await appConfirm(`确定批量删除选中的 ${sel.length} 个文件？此操作不可恢复`, { danger: true }))) return;
 const tasks = sel.map((cb) => {
    if (cb.dataset.kind === "vbios") {
      const n = cb.dataset.name;
      return api("/vbios", "DELETE", { name: n })
        .then(() => ({ ok: true, label: n }))
        .catch((e) => ({ ok: false, label: n, err: e.message || e }));
    }
    if (cb.dataset.kind === "cloud" || cb.dataset.kind === "disk") {
      const s = cb.dataset.storage || "__import__";
      const n = cb.dataset.name;
      return api(`/disks/delete?kind=${encodeURIComponent(cb.dataset.kind)}&storage=${encodeURIComponent(s)}&name=${encodeURIComponent(n)}`, "POST")
        .then(() => ({ ok: true, label: n }))
        .catch((e) => ({ ok: false, label: n, err: e.message || e }));
    }
    const volid = cb.dataset.volid;
    return api("/storage", "DELETE", { storage, volid })
      .then(() => ({ ok: true, label: volid }))
      .catch((e) => ({ ok: false, label: volid, err: e.message || e }));
  });
  try {
    const res = await Promise.allSettled(tasks);
    const ok = res.filter((r) => r.value && r.value.ok).length;
    const fail = res.filter((r) => !r.value || !r.value.ok);
    if (fail.length) {
      const detail = fail.slice(0, 3).map((r) => `· ${r.value.label}：${r.value.err}`).join("；");
      toast(`已删除 ${ok} 个，失败 ${fail.length} 个（${detail}）`, true);
    } else {
      toast(`已批量删除 ${ok} 个文件`);
    }
  } catch (e) {
    toast("批量删除失败：" + (e.message || e), true);
  }
  tcLoadStorageContent();
}

// 单片 POST：这里用 XHR 而不是 fetch，是因为只有 XHR 有上传进度事件（xhr.upload.onprogress），
// fetch 没有任何上传进度可用，拿不到「这一片已发出多少字节」，进度只能等整片返回才跳一档。
// onSent(loaded) 上报片内已发送字节；返回服务端 JSON（非 2xx 抛出 body.error 或 HTTP 状态）。
function postChunk(url, chunk, onSent, ctl) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    if (ctl) ctl.xhrs.add(xhr);
    const release = () => { if (ctl) ctl.xhrs.delete(xhr); };
    xhr.open("POST", url);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");  // CSRF 防护
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onSent) onSent(e.loaded); };
    xhr.onload = () => {
      release();
      let body = {};
      try { body = JSON.parse(xhr.responseText) || {}; } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
 else reject(new Error(body.error || `HTTP ${xhr.status}`));
 };
 xhr.onerror = () => { release(); reject(new Error("网络中断或连接被重置")); };
    xhr.onabort = () => { release(); reject(new Error("上传已取消")); };
    // 取消已在路上时连这一片都不必发（abort 一个 UNSENT 的 xhr 未必触发事件，直接拒绝更明确）
    if (ctl && ctl.aborted) { release(); reject(new Error("上传已取消")); return; }
    xhr.send(chunk);
  });
}

// 分片上传辅助：按 UPLOAD_CHUNK 切片 POST 到指定端点，fields 为附加查询参数（如 storage），onProgress(pct) 回调进度
// 进度按「已传字节 / 总字节」算（片内也走），不再是一整片跳一档的阶梯
async function chunkedUpload(endpoint, file, fields, onProgress, received, ctl) {
  const count = Math.max(1, Math.ceil(file.size / UPLOAD_CHUNK));
  const total = Math.max(1, file.size);
  let lastPct = -1;
  // 只在整数百分比变化时回调：onprogress 每几十毫秒就触发一次，而 onProgress 会触发活动列表整表重建
  // （tcRenderActivity 里 innerHTML 重排），不去重的话上传全程都在重排，明显卡顿
  const report = (bytes) => {
    if (!onProgress) return;
    const pct = Math.max(0, Math.min(100, Math.round((bytes / total) * 100)));
    if (pct === lastPct) return;
    lastPct = pct;
    onProgress(pct);
  };
  // 每片已发送字节各记各的，进度取总和：并发乱序完成时只增不减，不会出现进度回退
  const sentPer = new Array(count).fill(0);
  const reportAll = () => report(sentPer.reduce((a, b) => a + b, 0));
  let next = 0;
  let failed = null;
  let merging = false;   // 服务端已收齐分片、转入后台合并（据此决定前端是否要等它）
  // N 个 worker 依次领片；断点续传命中的片直接记为已传；任一片失败即停止领新片并把错误抛出去
  const worker = async () => {
    while (!failed) {
      if (ctl && ctl.aborted) return;   // 已取消：不再领新片（在飞的请求已由 cancel() 中止）
      const i = next++;
      if (i >= count) return;
      const size = Math.min(UPLOAD_CHUNK, total - i * UPLOAD_CHUNK);
      if (received && received.has(i)) {   // 服务端已存在该分片（刷新后重选同一文件可续传）
        sentPer[i] = size;
        reportAll();
        continue;
      }
      const chunk = file.slice(i * UPLOAD_CHUNK, (i + 1) * UPLOAD_CHUNK);
      // size：整个文件的总字节数 —— 服务端据此在第一片就校验单文件上限（不必等传完再拒）
      const qs = new URLSearchParams({ name: file.name, index: i, count, size: total, ...fields }).toString();
      try {
        const r = await postChunk(`${endpoint}?${qs}`, chunk, (loaded) => { sentPer[i] = loaded; reportAll(); }, ctl);
        if (r && r.merging) merging = true;   // 收齐的那一片的响应会带上该标记
      } catch (e) {
        if (ctl && ctl.aborted) return;   // 取消造成的中止不算失败，由外层统一判定
        failed = e;
        throw e;
      }
      // 响应回来即视为该片传完：onprogress 末次未必正好落在片尾，补足该片字节
      sentPer[i] = size;
      reportAll();
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, count) }, worker));
  } catch (e) {
    if (ctl && ctl.aborted) return { ok: false, merging: false };   // 取消造成的中止：不当失败上报
    throw e;
  }
  if (ctl && ctl.aborted) return { ok: false, merging: false };     // 已取消：进度停在已传部分，服务端分片保留可续传
  report(total);
  // ok=分片全部送达；merging=服务端正在后台合并（调用方据此决定是否轮询）
  return { ok: true, merging };
}

// 等后台合并完成：轮询 /api/upload/status（服务端在其中返回 merging / done / error）。
// 返回 "done" | "error" | "canceled" | "timeout"；网络抖动不中断轮询。
async function waitUploadMerged(ctl, cfg, file, fields) {
  const qs = new URLSearchParams({
    kind: cfg.kind || "backup",
    name: file.name,
    storage: fields.storage || "",
  }).toString();
  for (let i = 0; i < 1800; i++) { // 最多等 30 分钟（每秒探测一次，NFS 慢盘上合并大文件够用）
 if (ctl && ctl.aborted) return "canceled";
 try {
 const st = await api(`/upload/status?${qs}`);
 if (st && st.error) return "error";
 if (st && st.done) return "done";
 } catch (e) { /* 网络抖动：继续轮询 */ }
 await new Promise((r) => setTimeout(r, 1000));
  }
  return "timeout";
}

// 秒传指纹：大小 + 首/尾各 1MB 的 FNV-1a 双种子哈希（非加密，只用于判断「是不是同一个文件」）。
// 故意不用 crypto.subtle —— 面板多为 http://ip:port 访问，那种上下文下 crypto.subtle 不可用
async function quickFileHash(file) {
  const CH = 1024 * 1024;
  const parts = [file.slice(0, Math.min(CH, file.size))];
  if (file.size > CH) parts.push(file.slice(file.size - CH));
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (const p of parts) {
    const buf = new Uint8Array(await p.arrayBuffer());
    for (let i = 0; i < buf.length; i++) {
 h1 = Math.imul(h1 ^ buf[i], 16777619) >>> 0;
      h2 = Math.imul((h2 + buf[i] + 1) >>> 0, 2654435761) >>> 0;
    }
  }
  return `${file.size}-${h1.toString(16)}-${h2.toString(16)}`;
}

// 上传（或秒传）完成后的统一收尾：刷新该类型相关的下拉，以及正看着的「存储内容」
function tcAfterUploadDone(cfg, fields) {
  cfg.refresh.forEach((f) => fillDynamicOptions(f));
  // LXC 模板落到所选存储；其余类型（虚机模板/云镜像/磁盘镜像/vBIOS/vGPU 物料）都落在 pvewebtools，
  // 正看该存储时刷新即可看到刚上传的文件（秒传命中同样要刷新，否则列表里看不到）
  const cs = $("tcContentStorage");
  if (cfg.storagePick) {
    if (cs && cs.value === fields.storage) tcLoadStorageContent();
  } else if (cs && cs.value === SHARED_STORAGE) {
    tcLoadStorageContent();
  }
}

// 登记秒传指纹（服务端只在目标文件确实存在时才记录）；失败无感，只是下次不能秒传
async function tcRegisterHash(cfg, fields, file, quickHash) {
  if (!quickHash) return;
  try {
    await api("/upload/register", "POST", {
      kind: cfg.kind || "backup",
      name: file.name,
      storage: fields.storage || "",
      hash: quickHash,
      size: file.size,
    });
  } catch (e) { /* 登记失败不影响本次上传 */ }
}

// 获取云镜像：预设源（value 即下载地址）。
// 预设改为由后端提供（GET /api/cloud-image/presets，读取可编辑 config/cloud-image-presets.json，缺省回退内置默认）。
// 此处仅保留「网络异常时」的回退默认值，保证 UI 不崩。
// 与后端内置默认（web/server.py 的 DEFAULT_CLOUD_IMAGE_PRESETS）及 config/cloud-image-presets.json 三处保持一致：
// 全部为国内镜像站直链（官方源在国内下载慢或不通），Fedora 构建号与 Alpine 小版本均已核对直链。
let cloudImagePresets = [];
let cloudMirrors = [];   // 可选下载源：[{id,label,host}]，均为域名替换式镜像站（后端首项中科大 USTC 为默认）
const CLOUD_IMAGE_PRESETS_FALLBACK = [
  { label: "Ubuntu 24.04 (Noble)", url: "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-amd64.img" },
  { label: "Ubuntu 22.04 (Jammy)", url: "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/jammy/current/jammy-server-cloudimg-amd64.img" },
  { label: "Ubuntu 20.04 (Focal)", url: "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/focal/current/focal-server-cloudimg-amd64.img" },
  { label: "Ubuntu 18.04 (Bionic, EOL)", url: "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/bionic/current/bionic-server-cloudimg-amd64.img" },
  { label: "Debian 13 (Trixie)", url: "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/trixie/latest/debian-13-generic-amd64.qcow2" },
  { label: "Debian 12 (Bookworm)", url: "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/bookworm/latest/debian-12-generic-amd64.qcow2" },
  { label: "Debian 11 (Bullseye)", url: "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/bullseye/latest/debian-11-generic-amd64.qcow2" },
  { label: "Fedora 44 (Cloud Base)", url: "https://mirrors.ustc.edu.cn/fedora/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2" },
  { label: "Fedora 43 (Cloud Base)", url: "https://mirrors.ustc.edu.cn/fedora/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2" },
  { label: "Rocky Linux 10", url: "https://mirrors.ustc.edu.cn/rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2" },
  { label: "Rocky Linux 9", url: "https://mirrors.ustc.edu.cn/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2" },
  { label: "Rocky Linux 8", url: "https://mirrors.ustc.edu.cn/rocky/8/images/x86_64/Rocky-8-GenericCloud-Base.latest.x86_64.qcow2" },
  { label: "AlmaLinux 10", url: "https://mirrors.aliyun.com/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2" },
  { label: "AlmaLinux 9", url: "https://mirrors.aliyun.com/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2" },
  { label: "AlmaLinux 8", url: "https://mirrors.aliyun.com/almalinux/8/cloud/x86_64/images/AlmaLinux-8-GenericCloud-latest.x86_64.qcow2" },
  { label: "CentOS 7.6 (GenericCloud 1811, EOL)", url: "https://mirrors.nju.edu.cn/centos-cloud/centos/7/images/CentOS-7-x86_64-GenericCloud-1811.qcow2" },
  { label: "CentOS 8.4 (GenericCloud, EOL)", url: "https://mirrors.nju.edu.cn/centos-cloud/centos/8/x86_64/images/CentOS-8-GenericCloud-8.4.2105-20210603.0.x86_64.qcow2" },
  { label: "CentOS Stream 9", url: "https://mirrors.ustc.edu.cn/centos-cloud/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2" },
  { label: "CentOS Stream 10", url: "https://mirrors.ustc.edu.cn/centos-cloud/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2" },
  { label: "Alpine 3.22 (Cloud, BIOS)", url: "https://mirrors.ustc.edu.cn/alpine/v3.22/releases/cloud/generic_alpine-3.22.2-x86_64-bios-tiny-r0.qcow2" },
];

// 各上传类型 → 端点 / 是否需要存储 / 传完刷新的下拉 / 文件类型限制 / 说明
// storage: 落盘需指定存储；storageFixed: 固定落项目共享存储 pvewebtools；storagePick: 由「目标存储」下拉选择（LXC 模板）
const TC_UPLOAD_TYPES = {
  // maxSize：单文件上限，须与后端常量一致（MAX_BACKUP_SIZE / MAX_VZTMPL_SIZE / MAX_DISK_SIZE /
  // MAX_VBIOS_SIZE）：选文件时即拦，不必等几 GB 传完才被服务端拒绝
  backup: { storage: true,  storageFixed: "pvewebtools", endpoint: "/api/backup/upload", refresh: ["backups", "deploy_sources"], accept: ".vma,.vma.zst,.vma.gz,.vma.lzo", note: "虚机模板", maxSize: 256 * 1024 * 1024 * 1024 },
  lxc:    { storage: true,  storagePick: true, kind: "vztmpl", endpoint: "/api/lxc-template/upload", refresh: ["lxc_templates"], accept: ".tar.gz,.tar.xz,.tar.zst", note: "LXC 容器模板", maxSize: 64 * 1024 * 1024 * 1024 },
  cloud:  { storage: false, endpoint: "/api/disks/upload", kind: "cloud", refresh: ["cloud_images"], accept: ".qcow2,.raw,.img,.vmdk,.vhdx,.vdi,.vpc,.xz,.gz,.zst", note: "云镜像（含 cloud-init）", maxSize: 256 * 1024 * 1024 * 1024 },
  disk:   { storage: false, endpoint: "/api/disks/upload", kind: "disk",  refresh: ["disk_upload"],  accept: ".qcow2,.raw,.img,.vmdk,.vhdx,.vdi,.vpc,.xz,.gz,.zst", note: "磁盘镜像（普通磁盘，如 openEuler）", maxSize: 256 * 1024 * 1024 * 1024 },
  vbios:  { storage: false, endpoint: "/api/vbios/upload",    refresh: ["vbios_roms"],                accept: ".rom,.bin",                              note: "vBIOS ROM", maxSize: 16 * 1024 * 1024 },
};

// 上传目标存储（仅 LXC 模板类型需要）：复用「获取 LXC 模板」的存储列表，只列支持 vztmpl 内容的共享存储
async function tcLoadUploadStorages() {
  const sel = $("tcUploadStorage");
  if (!sel) return;
  const keep = sel.value;
  let ss = [];
  try { ss = await api("/lxc-template/storages"); } catch (e) { ss = []; }
  if (!Array.isArray(ss)) ss = [];
  // 集群场景：排除单节点 local 系存储，避免模板落到非共享位置
  ss = ss.filter((s) => !isLocalNodeStorage(s));
  sel.innerHTML = ss.length
    ? ss.map((s) => `<option value="${escapeHtml(s.storage)}">${escapeHtml(s.storage)}${s.free != null ? `（可用 ${fmtBytes(s.free)}）` : ""}</option>`).join("")
    : '<option value="">（无支持容器模板的共享存储）</option>';
  const has = (n) => ss.some((s) => s.storage === n);
  if (keep && has(keep)) sel.value = keep;
  else if (has("pvewebtools")) sel.value = "pvewebtools";
}

// 切换上传类型：更新文件类型限制、目标存储行可见性（云镜像/磁盘/vBIOS 上传到默认目录，无需选存储）
function tcOnTypeChange() {
  const type = $("tcUploadType").value;
  const cfg = TC_UPLOAD_TYPES[type];
  const fi = $("tcFileInput"); if (fi && cfg) fi.accept = cfg.accept;
  const row = $("tcUploadStorageRow");
  if (row) row.style.display = (cfg && cfg.storagePick) ? "" : "none";
  if (cfg && cfg.storagePick) tcLoadUploadStorages();
}

// 按文件扩展名推断候选上传类型：从各类型的 accept 声明推导（单一来源，避免两处维护）。
// 长扩展名优先（.vma.zst 应胜过 .zst），同长度按「类型」下拉里的先后顺序（云镜像排在磁盘镜像之前）
function tcGuessTypes(name) {
  const low = (name || "").toLowerCase();
  const sel = $("tcUploadType");
  const order = sel ? [...sel.options].map((o) => o.value) : Object.keys(TC_UPLOAD_TYPES);
  const hits = [];
  order.forEach((k) => {
    const cfg = TC_UPLOAD_TYPES[k];
    if (!cfg) return;
    const exts = String(cfg.accept || "").split(",")
      .map((s) => s.trim().toLowerCase()).filter((s) => s.startsWith("."));
    let best = 0;
    exts.forEach((e) => { if (low.endsWith(e)) best = Math.max(best, e.length); });
    if (best) hits.push({ type: k, len: best });
  });
  hits.sort((a, b) => b.len - a.len);
  return hits;   // [{type, len}]，按匹配长度降序
}

// 选完文件后自动对齐「类型」：只在当前所选类型不接受该扩展名时才改（不覆盖用户的有效选择）；
// 扩展名同属多个类型时（如 .qcow2 既可是云镜像也可是普通磁盘镜像）取下拉顺序里的第一个并提示确认
function tcAutoAlignType() {
  const fi = $("tcFileInput"), sel = $("tcUploadType");
  if (!fi || !sel || !fi.files[0]) return;
  const hits = tcGuessTypes(fi.files[0].name);
  if (!hits.length) return;
  const cands = hits.map((h) => h.type);
  if (cands.includes(sel.value)) return;   // 当前类型能收这个文件 → 尊重用户选择
  sel.value = cands[0];
  tcOnTypeChange();   // 同步文件类型限制与「目标存储」行
  const label = sel.options[sel.selectedIndex].textContent;
  // 仅当「最优匹配」并列（如 .qcow2 同时属于云镜像/磁盘镜像）才提示需人工确认
  const tie = hits.filter((h) => h.len === hits[0].len).length > 1;
  toast(tie
    ? `已按扩展名匹配「${label}」；该扩展名也可能属于其它类型，请确认`
    : `已按文件扩展名自动选择「${label}」`);
}

// 上传（全局：按类型 dispatch 到对应端点，完成后刷新相关下拉）
async function tcUpload() {
  const file = $("tcFileInput").files[0];
  const type = $("tcUploadType").value;
  const cfg = TC_UPLOAD_TYPES[type];
  if (!file) return toast("请先选择文件", true);
  // 客户端扩展名预校验：类型选错时后端会在读完请求头后立即返回 400 并截断连接，
  // 浏览器把这种「响应发一半就断」的连接呈现成连接级错误，真正的 400 原因被吞掉。
  // 这里按所选类型的 accept 提前拦截，并指出应切换到哪一类型。
  {
    const lc = file.name.toLowerCase();
    // CT 模板（.tar.gz/.tar.xz/.tar.zst）易与云镜像/磁盘的单层压缩包（.gz/.xz/.zst）混淆，
    // 先按模板名判定，避免选错类型时被当作压缩镜像误传到镜像目录
    if (/\.tar\.(gz|xz|zst)$/i.test(lc) && type !== "lxc") {
      return toast(`文件类型不匹配：${file.name} 是 LXC 容器模板，请把「类型」切换到「${TC_UPLOAD_TYPES.lxc.note}」后再上传。`, true);
    }
    const acc = cfg.accept.toLowerCase().split(",").map((s) => s.trim()).filter(Boolean);
    if (!acc.some((e) => lc.endsWith(e))) {
      const hit = Object.entries(TC_UPLOAD_TYPES).find(([, c]) =>
        c.accept.toLowerCase().split(",").map((s) => s.trim()).some((e) => lc.endsWith(e))
      );
      const sug = hit ? `请把「类型」切换到「${hit[1].note}」后再上传。` : "该文件格式暂不支持上传。";
      return toast(`文件类型不匹配：你选了「${cfg.note}」，但 ${file.name} 不属于此类型。${sug}`, true);
    }
  }
  // 单文件大小上限：与后端常量一致，选文件即拦（否则要把几 GB 传完，才在服务端合并时被拒）
  if (cfg.maxSize && file.size > cfg.maxSize) {
    return toast(`文件过大：${cfg.note} 单文件上限 ${fmtBytes(cfg.maxSize)}，当前 ${fmtBytes(file.size)}`, true);
  }
  const fields = {};
  // 存储落点：虚机模板固定 pvewebtools（→dump/）；LXC 模板由「目标存储」下拉选择（→<存储>/template/cache）；
  // 云镜像→cloud-images/、磁盘→disk-images/、vBIOS→vbios/ 均落固定项目目录，无需选存储
  if (cfg.storage) {
    fields.storage = cfg.storagePick ? (($("tcUploadStorage") || {}).value || "") : cfg.storageFixed;
    if (cfg.storagePick && !fields.storage) return toast("请选择目标存储（需支持容器模板 vztmpl 内容）", true);
  }
  if (cfg.kind) fields.kind = cfg.kind;
  const txt = $("tcUploadText");
  // 秒传：先算快速指纹（大小 + 首尾 1MB）问服务端 —— 本节点上传过同一文件且目标仍在，就跳过整个上传
  const quickHash = await quickFileHash(file).catch(() => "");
  if (quickHash) {
    try {
      const v = await api(`/upload/verify?kind=${encodeURIComponent(cfg.kind || "backup")}`
        + `&name=${encodeURIComponent(file.name)}`
        + `&storage=${encodeURIComponent(fields.storage || "")}`
        + `&hash=${encodeURIComponent(quickHash)}&size=${file.size}`);
      if (v && v.hit) {
        const t0 = tcAddTransfer(`${file.name}（${cfg.note}）`, "up");
        t0.state = "done";
        t0.pct = 100;
        tcRenderActivity();
        if (txt) txt.textContent = "秒传完成 ✓";
        toast(`${file.name} 秒传完成（目标已存在相同文件，未重复传输）`);
        tcAfterUploadDone(cfg, fields);
        return;   // 清场交给 finally
      }
    } catch (e) { /* 探测失败：照常上传 */ }
  }
  // 上传断点续传：先问服务端已收到哪些分片（刷新后重新选择同一文件即可跳过已传部分继续；
  // 浏览器不保留文件句柄，无法自动续传，但重选同一文件可由服务端已收分片判定续传点）
  let received = new Set();
  try {
    const uk = cfg.kind || "backup";
    const st = await api(`/upload/status?kind=${encodeURIComponent(uk)}&name=${encodeURIComponent(file.name)}&storage=${encodeURIComponent(fields.storage || "")}`);
    if (st && st.received && st.received.length) received = new Set(st.received);
  } catch (e) {}
  const t = tcAddTransfer(`${file.name}（${cfg.note}）`, "up");
  // 取消上传：记录在飞的 XHR，点活动列表的 ✕ 即中止（服务端已收分片保留，重选同一文件可续传）
  const ctl = { aborted: false, xhrs: new Set() };
  t.cancel = () => { ctl.aborted = true; ctl.xhrs.forEach((x) => { try { x.abort(); } catch (e) {} }); };
  // 记录正在上传的文件，刷新页面后若再次选择同一文件可据此提示「上传被中断，重选可续传」
  try { sessionStorage.setItem("pve-uploading", JSON.stringify({ name: file.name, note: cfg.note })); } catch (e) {}
  if (txt) txt.textContent = "上传中 0%";
  try {
    const res = await chunkedUpload(cfg.endpoint, file, fields, (pct) => {
      t.pct = pct;
      if (txt) txt.textContent = "上传中 " + pct + "%";
      tcRenderActivity();
    }, received, ctl);
    if (!res.ok) {
      t.state = "cancel";
      if (txt) txt.textContent = "已取消";
      toast(`${file.name} 上传已取消（已传分片保留，重选同一文件可续传）`);
      return;   // 清场交给 finally
    }
    if (res.merging) {
      // 分片已全部送达、服务端在后台合并：进度停在 100% 并轮询到合并结束，
      // 再把下游下拉/存储内容刷新出来（否则用户会看不到刚上传的文件）
      t.state = "merging";
      if (txt) txt.textContent = "合并中…";
      tcRenderActivity();
      const mr = await waitUploadMerged(ctl, cfg, file, fields);
      if (mr === "canceled") {
        t.state = "cancel";
        if (txt) txt.textContent = "已取消";
        toast(`${file.name} 已停止等待（分片已收齐、服务端合并已开始，文件仍会生成）`);
        return;
      }
      if (mr !== "done") {
        t.state = "error";
        if (txt) txt.textContent = "上传失败";
        toast(`${file.name} ${mr === "error" ? "服务端合并失败" : "合并仍在后台进行，稍后可到列表确认"}`, true);
        return;
      }
    }
    t.state = "done";
    if (txt) txt.textContent = "上传完成 ✓";
    toast(`${file.name} 上传完成`);
    tcAfterUploadDone(cfg, fields);
    tcRegisterHash(cfg, fields, file, quickHash);   // 登记指纹，下次同一文件即可秒传（失败无感）
  } catch (e) {
    t.state = "error";
    if (txt) txt.textContent = "上传失败";
    toast("上传失败: " + e.message, true);
  } finally {
    try { sessionStorage.removeItem("pve-uploading"); } catch (e) {}
    tcRenderActivity();
    $("tcFileName").textContent = "未选择";
    $("tcFileInput").value = "";
  }
}

// 云镜像后台下载（仅云镜像）：POST /cloud-image/download 后台拉取，前端轮询进度，复用常驻横幅
// cb: { onProgress(pct), onDone(), onError(msg), onCancel() } —— 供传输中心活动列表同步
// storage: 可选，指定后下载到该存储的 cloud-images 子目录，否则默认导入目录
async function startCloudImageDownload(rawUrl, cb = {}, storage = "", meta = {}, mirror = "default") {
  const url = (rawUrl || "").trim();
  if (!/^https?:\/\//i.test(url)) { toast("请选择或填写有效的镜像 URL", true); return; }
  let name = "";
  try {
    const body = { url, storage, mirror: mirror || "default" };
    if (meta && meta.label) body.label = meta.label;
    if (meta && meta.distro) body.distro = meta.distro;
    if (meta && meta.version) body.version = meta.version;
    if (meta && meta.source) body.source = meta.source;
    const r = await api("/cloud-image/download", "POST", body);
    name = r.name;
    await pollCloudImageDownload(name, cb);
  } catch (e) {
    setCloudDlBannerText(name || "云镜像", "✕ 下载失败: " + e.message, true, false);
    hideCloudDlBanner(name || "云镜像", 5000);
    toast("下载失败: " + e.message, true);
    if (cb.onError) cb.onError(e.message);
  }
}

// 轮询云镜像后台下载进度（下载发起后调用；刷新页面后可用 name 重连进度）
// cb: { onProgress(pct), onDone(), onError(msg), onCancel() }
async function pollCloudImageDownload(name, cb = {}) {
  const banner = showCloudDlBanner(name, () => {
    api("/cloud-image/cancel", "POST", { name }).catch(() => {});
  });
  banner.cancelRequested = false;
  setCloudDlBannerText(name, `云镜像下载中 ${name}…`, false, true);
  let lastPct = -1;
  for (;;) {
    if (banner.cancelRequested) {
      setCloudDlBannerText(name, `已取消：${name}`, false, false);
      hideCloudDlBanner(name, 2000);
      if (cb.onCancel) cb.onCancel();
      return;
    }
    await new Promise((res) => setTimeout(res, 1000));
    const st = await api(`/cloud-image/status?name=${encodeURIComponent(name)}`);
    if (st.status === "cancelled") {
      setCloudDlBannerText(name, `已取消：${name}`, false, false);
      hideCloudDlBanner(name, 2000);
      if (cb.onCancel) cb.onCancel();
      return;
    }
    if (st.status === "done") {
      setCloudDlBannerText(name, `✓ 云镜像 ${name} 下载完成`, false, false);
      hideCloudDlBanner(name, 3000);
      break;
    }
    if (st.status === "error") {
      setCloudDlBannerText(name, `✕ 下载失败: ${st.error || ""}`, true, false);
      hideCloudDlBanner(name, 5000);
      if (cb.onError) cb.onError(st.error || "下载失败");
      return;
    }
    if (st.status === "notfound") {
      setCloudDlBannerText(name, "✕ 下载任务丢失，请重试", true, false);
      hideCloudDlBanner(name, 5000);
      if (cb.onError) cb.onError("任务丢失");
      return;
    }
    const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
    if (pct !== lastPct) {
      setCloudDlBannerText(name, `云镜像下载中 ${name} — ${pct}%`, false, true);
      if (cb.onProgress) cb.onProgress(pct);
      lastPct = pct;
    }
  }
  await fillDynamicOptions("cloud_images");
  if (cb.onDone) cb.onDone();
}

// ===== 可搜索下拉（SearchSelect）=====
// 为什么不用原生 <select>：云镜像预设几十条、LXC 模板几百条（pveam 全量更多），
// 原生下拉只能靠滚动 + 首字母定位，找「debian 12」这类要翻很久；
// 这里换成「输入框即过滤器 + 浮层候选列表」，输入即实时缩小范围。
//
// 用法：HTML 放 <div class="ss" id="xxx" data-ss-ph="占位提示" data-ss-empty="无匹配提示"></div>
//      再 const p = createSearchSelect("xxx"); p.setOptions([{value, label, group, hint}], 选中值)。
// 兼容原生 <select> 的老用法：组件在 root 元素上定义了 .value（可读可写）并派发 change 事件，
// 所以 `$("xxx").value` 和 `root.addEventListener("change", fn)` 都无需改动。
// 语义对齐原生：程序化 setOptions / 赋值 .value 不派发 change（原生 select.value = x 同样不派发），只有用户点选才派发。
let tcCloudPicker = null;   // 「获取云镜像」的镜像选择器
let tcLxcPicker = null;     // 「获取 LXC 模板」的模板选择器

function createSearchSelect(rootOrId, opts = {}) {
  const root = typeof rootOrId === "string" ? $(rootOrId) : rootOrId;
  if (!root) return null;
  const placeholder = root.dataset.ssPh || opts.placeholder || "输入关键字搜索…";
  const emptyText = root.dataset.ssEmpty || opts.emptyText || "（无匹配项）";
  root.classList.add("ss");
  root.innerHTML =
    `<div class="ss-box">` +
      `<svg class="ico ss-ico"><use href="#ico-search"></use></svg>` +
      `<input type="text" class="ss-input" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="false" placeholder="${escapeHtml(placeholder)}">` +
      `<svg class="ico ss-chev"><use href="#ico-chevron"></use></svg>` +
    `</div>` +
    `<div class="ss-menu hidden" role="listbox"></div>`;
  const input = root.querySelector(".ss-input");
  const menu = root.querySelector(".ss-menu");

  let items = [];    // 全量选项 [{value,label,group,hint}]
  let cur = null;    // 当前选中项
  let shown = [];    // 当前过滤后的可见项（顺序与浮层里的 .ss-item 一一对应）
  let active = -1;   // 浮层高亮项下标
  let query = "";    // 搜索词（与「输入框里显示的已选名称」区分开）

  const norm = (s) => String(s == null ? "" : s).toLowerCase();
  const hay = (it) => norm(it.label) + " " + norm(it.value) + " " + norm(it.group);
  // 空格分词、需全部命中：输入 "debian 12" 能同时命中 Debian 12（而不是命中所有 debian）
  const keywords = () => query.trim().split(/\s+/).filter(Boolean).map(norm);

  // 命中片段高亮：先定位再逐段 escape（先 escape 会导致下标错位）
  function highlight(text, keys) {
    const t = String(text == null ? "" : text);
    if (!keys.length) return escapeHtml(t);
    const lower = t.toLowerCase();
    const spans = [];
    keys.forEach((k) => {
      let i = lower.indexOf(k);
      while (i >= 0) { spans.push([i, i + k.length]); i = lower.indexOf(k, i + k.length); }
    });
    if (!spans.length) return escapeHtml(t);
    spans.sort((a, b) => a[0] - b[0]);
    const merged = [];
    spans.forEach(([a, b]) => {
      const last = merged[merged.length - 1];
      if (last && a <= last[1]) last[1] = Math.max(last[1], b);
 else merged.push([a, b]);
 });
 let out = "", pos = 0;
 merged.forEach(([a, b]) => {
      out += escapeHtml(t.slice(pos, a)) + `<b class="ss-hl">${escapeHtml(t.slice(a, b))}</b>`;
      pos = b;
    });
    return out + escapeHtml(t.slice(pos));
  }

  function render() {
    const keys = keywords();
    shown = keys.length ? items.filter((it) => keys.every((k) => hay(it).includes(k))) : items.slice();
    if (!shown.length) {
      menu.innerHTML = `<div class="ss-empty">${escapeHtml(emptyText)}</div>`;
      active = -1;
      return;
    }
    // 按 options 声明顺序分组（group 相同的连续排列），组标题只在首次出现时输出
    const order = [], buckets = {};
    shown.forEach((it) => {
      const g = it.group || "";
      if (!buckets[g]) { buckets[g] = []; order.push(g); }
      buckets[g].push(it);
    });
    // 用递增下标代替 shown.indexOf(it)：模板几百条时逐条 indexOf 是 O(n²)，每敲一个字都要跑一遍
    let html = "", idx = -1;
    order.forEach((g) => {
      if (g) html += `<div class="ss-group">${escapeHtml(g)}</div>`;
      buckets[g].forEach((it) => {
        idx += 1;
        html += `<div class="ss-item${it === cur ? " selected" : ""}${idx === active ? " active" : ""}" role="option" aria-selected="${it === cur}" data-i="${idx}" title="${escapeHtml(it.label)}">` +
          `<span class="ss-check">✓</span>` +
          `<span class="ss-label">${highlight(it.label, keys)}</span>` +
          (it.hint ? `<span class="ss-hint">${escapeHtml(it.hint)}</span>` : "") +
        `</div>`;
      });
    });
    menu.innerHTML = html;
  }

  // 移动高亮：只在节点上切 class + 按需滚动，不重建 DOM（重建会让滚动位置和 hover 态反复丢失）
  function setActive(i) {
    const nodes = menu.querySelectorAll(".ss-item");
    if (!nodes.length) return;
    active = Math.max(0, Math.min(i, nodes.length - 1));
    nodes.forEach((n, idx) => n.classList.toggle("active", idx === active));
    const hit = nodes[active];
    if (hit && hit.scrollIntoView) hit.scrollIntoView({ block: "nearest" });
  }

  function open() {
    if (root.classList.contains("open")) return;
    root.classList.add("open");
    menu.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    active = -1;
    render();
    // 有选中项时把高亮落在它上面，打开即可看清当前位置
    if (cur) {
      const i = shown.indexOf(cur);
      if (i >= 0) setActive(i);
    }
  }

  function close() {
    root.classList.remove("open");
    menu.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    query = "";
    active = -1;
    input.value = cur ? cur.label : "";   // 放弃搜索时恢复显示已选名称
  }

  const notify = () => root.dispatchEvent(new Event("change", { bubbles: true }));

  // 值 → 选中项（不派发 change）：等价原生 select.value 赋值
  function selectByValue(v) {
    cur = items.find((x) => x.value === v) || null;
    input.value = cur ? cur.label : "";
    query = "";
  }

  // 点选具体项：与原生一致，值没变就不派发 change
  function pick(i) {
    const it = shown[i];
    if (!it) return;
    const changed = !cur || cur.value !== it.value;
    cur = it;
    close();
    if (changed) notify();
  }

  function setOptions(list, selectedValue) {
    items = (Array.isArray(list) ? list : []).map((it) => ({
      value: String(it && it.value != null ? it.value : ""),
      label: String(it && it.label != null ? it.label : (it && it.value != null ? it.value : "")),
      group: (it && it.group) || "",
      hint: (it && it.hint) || "",
    }));
    input.placeholder = placeholder;
    // 目标值不在新列表里（如换源后原模板已消失）就退到首项，保证「总有可用值」
    const want = selectedValue != null ? String(selectedValue) : (cur ? cur.value : "");
    selectByValue(items.some((x) => x.value === want) ? want : (items[0] ? items[0].value : ""));
    if (!menu.classList.contains("hidden")) render();
    return cur ? cur.value : "";
  }

  // 展开入口必须挂在 pointerdown 上而不是只靠 focus：
  // 点空白处收起浮层时输入框可能仍保持焦点，再点回来不会再触发 focus，只挂 focus 会「点不开」。
  // pointerdown 先于 mousedown/focus，这里展开后 focus 处理器会因「已展开」跳过，不会重复渲染。
  root.querySelector(".ss-box").addEventListener("pointerdown", () => { if (!root.classList.contains("open")) open(); });
  input.addEventListener("focus", () => { if (!root.classList.contains("open")) { input.select(); open(); } });
  input.addEventListener("input", () => {
    query = input.value;
    if (root.classList.contains("open")) render(); else open();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (menu.classList.contains("hidden")) { open(); return; }
      // 到顶/到底循环，与原生下拉一致；active=-1 时分别从首/尾进入
      if (e.key === "ArrowDown") setActive(active + 1 >= shown.length ? 0 : active + 1);
      else setActive(active - 1 < 0 ? shown.length - 1 : active - 1);
 } else if (e.key === "Enter") {
 e.preventDefault();
 if (active >= 0) pick(active);
      else if (shown.length === 1) pick(0);   // 只剩一条时回车直接选中，省一次 ↓
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
      input.blur();
    } else if (e.key === "Tab") {
      close();
    }
  });
  // 浮层点选：mousedown 阻止默认，避免输入框先失焦（失焦会收起浮层，click 就丢了）
  menu.addEventListener("mousedown", (e) => e.preventDefault());
  menu.addEventListener("click", (e) => {
    const node = e.target.closest(".ss-item");
    if (node) pick(Number(node.dataset.i));
  });
  // 点击组件外部收起；root 内部点击不收起（否则浮层点不中）
  document.addEventListener("pointerdown", (e) => {
    if (root.classList.contains("open") && !root.contains(e.target)) close();
  });
  input.addEventListener("blur", () => { if (root.classList.contains("open")) close(); });

  // 兼容原生 <select> 的读取习惯：外部直接 $("xxx").value 也能拿到当前值
  Object.defineProperty(root, "value", {
    configurable: true,
    get: () => (cur ? cur.value : ""),
    set: (v) => selectByValue(String(v)),
  });

  return {
    root, input, menu, setOptions,
    get value() { return cur ? cur.value : ""; },
    set value(v) { selectByValue(String(v)); },
    setPlaceholder: (t) => { input.placeholder = t; },
    focus: () => input.focus(),
  };
}

// 填充「镜像」下拉：从后端拉取可编辑预设源（config/cloud-image-presets.json），网络异常回退内置默认值
async function tcLoadCloudPresets() {
  if (!tcCloudPicker) return;
  let list = [], err = "";
  try {
    list = await api("/cloud-image/presets");
  } catch (e) {
    list = CLOUD_IMAGE_PRESETS_FALLBACK;
    err = "预设源读取失败，已回退内置列表";
  }
  if (!Array.isArray(list)) list = [];
  cloudImagePresets = list;
  const items = list.map((p) => ({ value: p.url, label: p.label || p.url }));
  tcCloudPicker.setOptions(items, items.length ? items[0].value : "");
  tcSyncCloudUrl();
  // 状态行与「获取 LXC 模板」对齐：列出可选镜像数量，便于确认预设源是否加载成功
  const txt = $("tcDownloadText");
  if (txt) {
    txt.textContent = items.length
      ? `已列出 ${items.length} 个镜像${err ? `（${err}）` : ""}`
      : (err ? `列表为空：${err}` : "暂无可用镜像预设");
  }
}

// 拉取可选下载源（域名替换式镜像站；后端首项即默认项 = 中科大 USTC）
async function tcLoadCloudMirrors() {
  const sel = $("tcCloudMirror");
  if (!sel) return;
  let list = [];
  try { list = await api("/cloud-image/mirrors"); } catch (e) { list = []; }
  cloudMirrors = Array.isArray(list) ? list : [];
  sel.innerHTML = cloudMirrors.length
    ? cloudMirrors.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join("")
    : '<option value="ustc">中科大 USTC</option>';
  // 默认中科大 USTC（后端已置首，这里再显式选中，避免后端调整顺序后默认值漂移）
  if (cloudMirrors.some((m) => m.id === "ustc")) sel.value = "ustc";
  // 预设列表与下载源加载顺序不固定，这里就绪后统一刷新一次地址预览
  tcSyncCloudUrl();
}

// 拉取云镜像下载目标存储（images/import 内容，落 <存储根>/cloud-images/）
async function tcLoadCloudStorages() {
  const sel = $("tcCloudStorage");
  if (!sel) return;
  sel.innerHTML = '<option value="">加载中…</option>';
  sel.disabled = true;
  let ss = [];
  try {
    // 60s 前端缓存（与后端 5 分钟缓存叠加，切标签基本零等待）。
    // 超时放宽到 30s 且不重试：默认 15s×3 次的组合会把「后端偶发慢」放大成近一分钟的加载态。
    ss = await cachedApi("/cloud-image/storages", 60000, { timeout: 30000, retries: 0 });
  } catch (e) {
    ss = [];
    // 失败不再甩「（无可用存储）」——value="" 本就等价 pvewebtools 共享存储，按默认项渲染即可下载
    console.warn("读取云镜像目标存储失败：", e && e.message ? e.message : e);
  } finally {
    sel.disabled = false;
  }
  if (!Array.isArray(ss)) ss = [];
  // 集群场景：排除单节点 local 系存储，避免镜像落到非共享位置；首个「默认共享存储」即 pvewebtools
  ss = ss.filter((s) => !isLocalNodeStorage(s));
  sel.innerHTML = ss.length
    ? ss.map((s) =>
      `<option value="${escapeHtml(s.storage)}">${escapeHtml(s.label || s.storage || "默认")}${s.free != null ? `（可用 ${fmtBytes(s.free)}）` : ""}</option>`
    ).join("")
    : '<option value="">pvewebtools（存储信息暂不可用，仍按共享存储落盘）</option>';
  // 默认选中首个（「默认共享存储」→ pvewebtools/cloud-images/）
  sel.value = ss.length ? ss[0].storage : "";
}

// 版本/下载源 → 地址联动：先取预设原始地址，再按所选镜像站改写域名（仅前端预览，下载时后端按 mirror 权威改写）
function tcSyncCloudUrl() {
  const ver = $("tcDownloadVersion");
  const url = $("tcDownloadUrl");
  const mir = $("tcCloudMirror");
  if (!ver || !url) return;
  url.value = tcApplyCloudMirrorPreview(ver.value, mir ? mir.value : "default");
}

function tcApplyCloudMirrorPreview(url, mirrorId) {
  const m = (cloudMirrors || []).find((x) => x.id === mirrorId);
  if (!m || !m.host) return url || "";
  return (url || "").replace(/^https?:\/\/[^/]+/i, "https://" + m.host);
}

// 版本 → 地址单向联动：选中版本即带出其下载地址（输入框只读，不允许自定义/其他地址）
function tcWireCloudPresets() {
  const ver = $("tcDownloadVersion");
  if (!ver) return;
  ver.addEventListener("change", tcSyncCloudUrl);
}

// 从预设 label 解析 distro / version（如 "Ubuntu 24.04 (Noble)" → {distro:"ubuntu", version:"24.04"}）
// 下载官方镜像时随请求带上，落盘为侧车元数据，部署阶段直接使用准确版本（无需再从文件名猜）
function parsePresetMeta(label) {
  const L = (label || "").toLowerCase();
  let distro = "";
  if (L.includes("ubuntu")) distro = "ubuntu";
  else if (L.includes("almalinux") || L.includes("alma")) distro = "alma";
  else if (L.includes("rocky")) distro = "rocky";
  else if (L.includes("fedora")) distro = "fedora";
  else if (L.includes("debian")) distro = "debian";
  else if (L.includes("centos")) distro = "centos";
  else if (L.includes("opensuse") || L.includes("suse")) distro = "opensuse";
  else if (L.includes("alpine")) distro = "alpine";
  const vm = label && label.match(/(\d+(?:\.\d+)*)/);
  const version = vm ? vm[1] : "";
  return { label: label || "", distro, version, source: "preset" };
}

// 下载（全局：仅云镜像）：把进度同步到传输中心活动列表
async function tcDownload() {
  const ver = $("tcDownloadVersion");
  const url = (($("tcDownloadUrl") || {}).value || "").trim();
  if (!/^https?:\/\//i.test(url)) return toast("请选择或填写有效的镜像 URL", true);
  const storage = ($("tcCloudStorage") || {}).value || "";
  const mirror = ($("tcCloudMirror") || {}).value || "default";
  const t = tcAddTransfer(url, "down");
  const txt = $("tcDownloadText"); if (txt) txt.textContent = "请求中…";
  // 命中预设则带出准确 distro/version，落盘为元数据，部署阶段直接使用（无需从文件名猜）
  // 镜像地址可能已被下载源改写过，原始/改写后的预设 URL 都要能命中
  const preset = cloudImagePresets.find((p) => p.url === url)
    || cloudImagePresets.find((p) => tcApplyCloudMirrorPreview(p.url, mirror) === url);
  const meta = preset ? parsePresetMeta(preset.label) : null;
  startCloudImageDownload(url, {
    onProgress: (pct) => { t.pct = pct; if (txt) txt.textContent = "下载中 " + pct + "%"; tcRenderActivity(); },
    onDone: () => { t.state = "done"; if (txt) txt.textContent = "下载完成 ✓"; tcRenderActivity(); },
    onError: (msg) => { t.state = "error"; t.error = msg || ""; if (txt) txt.textContent = "下载失败"; tcRenderActivity(); },
    onCancel: () => { t.state = "cancel"; if (txt) txt.textContent = "已取消"; tcRenderActivity(); },
  }, storage, meta, mirror);
}

// 失败原因文案：后端 status 既可能是 HTTP 码，也可能是异常串（超时 / 证书 / DNS 解析失败等），一律透出便于定位
function presetFailText(r) {
  const name = (r && (r.label || r.url)) || "未知源";
  const s = r && r.status;
  if (s === undefined || s === null || s === "") return name;
  return `${name}（${typeof s === "number" ? "HTTP " + s : s}）`;
}

// 校验预设源可用性：调用后端 HEAD/GET 探活，结果汇总到状态条并提示不可用的项（含原因）
async function tcCheckPresets() {
  const btn = $("tcCheckPresetsBtn"); const out = $("tcPresetCheck");
  if (btn) btn.disabled = true;
  if (out) out.textContent = "校验中…";
  try {
    const res = await api("/cloud-image/presets/check", "POST");
    const arr = Array.isArray(res) ? res : [];
    const bad = arr.filter((r) => !r.ok);
    const okN = arr.length - bad.length;
    const badTxt = bad.map(presetFailText).join("、");
    if (out) out.textContent = `✓ ${okN}/${arr.length} 可用` + (bad.length ? `，不可用：${badTxt}` : "");
    if (bad.length) toast(`以下源不可用：${badTxt}`, true);
    else toast("所有预设源均可访问");
  } catch (e) {
    if (out) out.textContent = "校验失败";
    toast("校验失败: " + e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 管理预设源（网页维护下载源，无需手改 JSON）：打开弹窗并渲染当前列表
async function openPresetModal() {
  const mask = $("presetModal");
  if (!mask) return;
  let list = [];
  try { list = await api("/cloud-image/presets"); } catch (e) { list = cloudImagePresets || []; }
  if (!Array.isArray(list)) list = [];
  renderPresetRows(list);
  const msg = $("presetMsg"); if (msg) msg.textContent = "";
  mask.classList.remove("hidden");
}

function presetRowHtml(label, url, idx) {
  return `<div class="preset-row" style="display:flex;gap:6px;margin-bottom:6px;align-items:center">
    <span class="preset-idx" style="flex:0 0 28px;text-align:right;color:var(--muted)">${idx}.</span>
    <input class="preset-label" style="flex:1" placeholder="名称" value="${escapeHtml(label)}">
    <input class="preset-url" style="flex:2" placeholder="https://..." value="${escapeHtml(url)}">
    <button type="button" class="btn-icon preset-del" title="删除"><svg class="ico"><use href="#ico-close"></use></svg></button>
  </div>`;
}

function renderPresetRows(list) {
  const box = $("presetList");
  const totalEl = $("presetTotal");
  if (!box) return;
  box.innerHTML = list.map((p, i) => presetRowHtml(p.label || "", p.url || "", i + 1)).join("");
  if (totalEl) totalEl.textContent = list.length;
}

function reindexPresetRows() {
  const rows = document.querySelectorAll("#presetList .preset-row");
  rows.forEach((r, i) => {
    const idxEl = r.querySelector(".preset-idx");
    if (idxEl) idxEl.textContent = (i + 1) + ".";
  });
  const totalEl = $("presetTotal");
  if (totalEl) totalEl.textContent = rows.length;
}

// 保存预设源：收集弹窗内各行 → 调后端写接口 → 成功即刷新下拉
async function tcSavePresets() {
  const box = $("presetList");
  const msg = $("presetMsg");
  if (!box) return;
  const presets = [];
  box.querySelectorAll(".preset-row").forEach((r) => {
    const label = (r.querySelector(".preset-label").value || "").trim();
    const url = (r.querySelector(".preset-url").value || "").trim();
    if (label || url) presets.push({ label, url });
  });
  try {
    await api("/cloud-image/presets", "POST", { presets });
    if (msg) msg.textContent = "已保存 ✓";
    $("presetModal").classList.add("hidden");
    await tcLoadCloudPresets();
    toast("预设源已保存");
  } catch (e) {
    if (msg) msg.textContent = "保存失败: " + (e.message || e);
  }
}

// 重置预设源为内置默认：调 reset 接口（覆盖自定义、旧文件备份）→ 刷新下拉并重新渲染弹窗列表
async function tcResetPresets() {
  const msg = $("presetMsg");
  if (msg) msg.textContent = "重置中…";
  try {
    await api("/cloud-image/presets/reset", "POST");
    if (msg) msg.textContent = "已恢复默认 ✓";
    await tcLoadCloudPresets();
    renderPresetRows(cloudImagePresets || []);
    toast("预设源已重置为默认");
  } catch (e) {
    if (msg) msg.textContent = "重置失败: " + (e.message || e);
  }
}

// 刷新页面后，用后端「进行中任务列表」恢复下载进度（仅下载可恢复；上传为前端直推，刷新即中断）
// 后端下载线程在服务器上持续运行，刷新不会中断它，这里只是把前端列表与进度重新接上
async function tcResumeCloudDownloads() {
  let list = [];
  try { list = await api("/cloud-image/list"); } catch (e) { return; }
  if (!Array.isArray(list) || !list.length) return;
  for (const task of list) {
    const name = task.name;
    if (!name) continue;
    if (tcTransfers.some((t) => t.dir === "down" && t.name === name)) continue;  // 防重复
    const t = tcAddTransfer(name, "down");
    if (task.status === "downloading") {
      t.state = "running";
      t.pct = task.total ? Math.round((task.done / task.total) * 100) : 0;
      tcRenderActivity();
      pollCloudImageDownload(name, {
        onProgress: (pct) => { t.pct = pct; tcRenderActivity(); },
        onDone: () => { t.state = "done"; t.pct = 100; tcRenderActivity(); },
        onError: (msg) => { t.state = "error"; t.error = msg || ""; tcRenderActivity(); },
        onCancel: () => { t.state = "error"; tcRenderActivity(); },
      });
    } else if (task.status === "done") {
      t.state = "done"; t.pct = 100; tcRenderActivity();
    } else {
      t.state = "error"; tcRenderActivity();
    }
  }
}

// ===== 获取 LXC 模板（CT 模板）=====
// 与「获取云镜像」的差异：模板列表来自本机 pveam（随上游实时变化，不是固定清单），
// 下载源默认「跟随本机」= APLInfo.pm 里配置的 CT 源（即「更换软件源和 LXC 源」切好的那个），
// 落盘目录是目标存储的 template/cache/，写完 PVE 立即可见（pveam list / 建 CT 下拉）。
let lxcTemplates = [];

// 分类小标题：pveam 的 section → 中文标签，分类筛选下拉与模板浮层分组标题共用同一套文案；
// 顺序决定分组/选项先后（系统镜像优先，应用类垫后），未列出者回退原始 section 名
const LXC_SECTION_LABEL = {
  system: "系统镜像",
  turnkeylinux: "TurnKey 应用",
  mail: "邮件服务器",
};
const LXC_SECTION_ORDER = ["system", "turnkeylinux", "mail"];
// 当前分类筛选值：空串 = 全部分类（section 为后端 pveam 原样提供的英文名）
let lxcSectionFilter = "";
// section → 展示用小标题（无中文映射时用原始 section 名，再兜底「其他」）
const lxcSectionName = (sec) => LXC_SECTION_LABEL[sec || ""] || sec || "其他";

// 单节点存储（local/local-lvm/local-zfs 等）：只在本节点可见，集群里下 CT 模板到它会「和 local 混放」
// 且其它节点建 CT 看不到，故从 LXC 目标下拉中排除；仅保留集群共享存储（pvewebtools）。
const isLocalNodeStorage = (s) => {
  const n = (s.storage || "");
  return n === "local" || n.startsWith("local-");
};
// 目标存储：仅列出支持 vztmpl 内容的共享存储；默认优先 pvewebtools（集群共享，模板一次下载全节点可用）
async function tcLoadLxcStorages() {
  const sel = $("tcLxcStorage");
  if (!sel) return;
  const keep = sel.value;
  let ss = [];
  try { ss = await api("/lxc-template/storages"); } catch (e) { ss = []; }
  if (!Array.isArray(ss)) ss = [];
  // 集群场景：排除单节点 local 系存储，避免模板落到非共享位置
  ss = ss.filter((s) => !isLocalNodeStorage(s));
  sel.innerHTML = ss.length
    ? ss.map((s) => `<option value="${escapeHtml(s.storage)}">${escapeHtml(s.storage)}${s.free != null ? `（可用 ${fmtBytes(s.free)}）` : ""}</option>`).join("")
    : '<option value="">（无支持容器模板的共享存储）</option>';
  const has = (n) => ss.some((s) => s.storage === n);
  if (keep && has(keep)) sel.value = keep;
  else if (has("pvewebtools")) sel.value = "pvewebtools";
}

// 集群 CT 源一致性提示：检测到其它节点源与本机不一致时，提醒一键批量换源
async function tcLoadClusterSources() {
  const box = $("tcClusterWarn");
  if (!box) return;
  let info;
  try {
    info = await api("/lxc-template/cluster-sources");
  } catch (e) {
    return;  // 检测失败（如非集群、API 异常）时不阻断面板
  }
  if (!info || !info.multi_node || info.consistent) {
    box.classList.add("hidden"); box.innerHTML = "";
    return;
  }
  const us = info.local_source || "未知";
  const diffs = (info.nodes || [])
    .filter((n) => n.ok && n.source && n.source !== us)
    .map((n) => `${escapeHtml(n.node)} → ${escapeHtml(n.source)}`);
  const fails = (info.nodes || [])
    .filter((n) => !n.ok)
    .map((n) => `${escapeHtml(n.node)}（${escapeHtml(n.error || "不可达")}）`);
  const sameCnt = (info.nodes || []).filter((n) => n.ok && n.source === us).length;
  box.innerHTML = `
    <div class="ccw-head">⚠ 集群 CT 模板源不一致：本机 <b>${escapeHtml(info.local_node)}</b>=${escapeHtml(us)}，
      另有 ${diffs.length} 个节点源不同（建 CT 时各节点可能拉到不同版本）。</div>
    <div class="ccw-detail">
      ${diffs.length ? "不同源节点：" + diffs.join("；") + "。" : ""}
      ${fails.length ? "不可达节点：" + fails.join("；") + "。" : ""}
      一致节点 ${sameCnt} 个。
    </div>
    <div class="ccw-actions">
      <label>批量换成
        <select id="tcBatchMirror">
          <option value="ustc">中科大 USTC</option>
          <option value="tuna">清华 TUNA</option>
          <option value="zju">浙江大学 ZJU</option>
          <option value="sdu">山东大学 SDU</option>
          <option value="official">官方源</option>
        </select>
      </label>
      <label>安全源
        <select id="tcBatchSecurity">
          <option value="mirror">镜像站源</option>
          <option value="official">官方源</option>
        </select>
      </label>
      <button type="button" class="btn run" id="tcBatchSwapBtn">一键批量换源</button>
      <button type="button" class="btn" id="tcBatchDismiss">忽略</button>
    </div>
    <div id="tcBatchResult" class="ccw-result"></div>`;
  box.classList.remove("hidden");
  const bm = $("tcBatchMirror"), bs = $("tcBatchSecurity");
  const bb = $("tcBatchSwapBtn"), bd = $("tcBatchDismiss");
  if (bb) bb.addEventListener("click", () => tcBatchSwap(bm ? bm.value : "ustc", bs ? bs.value : "mirror"));
  if (bd) bd.addEventListener("click", () => { box.classList.add("hidden"); });
}

// 一键批量换源：对集群各节点提交 change_sources 任务并轮询进度
async function tcBatchSwap(mirror, security) {
  const res = $("tcBatchResult");
  const btn = $("tcBatchSwapBtn");
  if (btn) btn.disabled = true;
  if (res) res.textContent = "提交中…";
  let data;
  try {
    data = await api("/lxc-template/batch-source-swap", "POST", { mirror, security });
  } catch (e) {
    if (res) res.textContent = "提交失败：" + e.message;
    if (btn) btn.disabled = false;
    return;
  }
  const jobs = (data && data.jobs) || [];
  const failed = jobs.filter((j) => !j.ok);
  const running = jobs.filter((j) => j.ok);
  if (res) res.innerHTML = `已对 ${running.length}/${jobs.length} 个节点提交换源任务，执行中…`;
  const pollOne = async (j) => {
    try {
      const snap = await api(`/jobs/${j.job_id}?offset=0`);
      j._status = snap.status;
    } catch (e) { j._status = "error"; }
  };
  let done = 0;
  while (running.length) {
    await Promise.all(running.map(pollOne));
    done = running.filter((j) => j._status && j._status !== "running").length;
    if (res) res.innerHTML = `换源进度：${done}/${running.length} 完成` +
      (failed.length ? `（${failed.length} 个节点提交失败）` : "") + "<br>" +
      running.map((j) => `${escapeHtml(j.node)}: ${escapeHtml(j._status || "running")}`).join("；");
    if (done >= running.length) break;
    await new Promise((r) => setTimeout(r, 2000));
  }
  try { await api("/lxc-template/cluster-sources?refresh=1"); } catch (e) {}
  if (res) res.innerHTML = `✓ 批量换源完成：${done}/${running.length} 成功` +
    (failed.length ? `；失败 ${failed.length} 个：${failed.map((j) => escapeHtml(j.node)).join("、")}` : "") +
    "。建议稍后点「刷新模板列表」加载新源模板。";
  if (btn) btn.disabled = false;
  tcLoadClusterSources();  // 重新检测是否仍不一致
}

// 下载源下拉：由后端给出（首项为「跟随本机」，其余为提供了 CT 源的镜像站）
function tcFillLxcMirrors(mirrors) {
  const sel = $("tcLxcMirror");
  if (!sel) return;
  const keep = sel.value || "follow";
  sel.innerHTML = (mirrors || []).map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join("");
  const hit = (mirrors || []).some((m) => m.id === keep);
  sel.value = hit ? keep : ((mirrors && mirrors[0]) ? mirrors[0].id : "follow");
}

// 模板项 → 可搜索下拉选项：按 section 排好序（系统镜像 → TurnKey → 其余，组内保持后端原序），
// 架构与本机不符的用 hint 在行尾标注（下载了也跑不起来）
function tcLxcOptions(items) {
  const rank = (s) => {
    const i = LXC_SECTION_ORDER.indexOf(s);
    return i < 0 ? LXC_SECTION_ORDER.length : i;
 };
 return items
 .map((it, i) => ({ it, i }))
    .sort((a, b) => (rank(a.it.section) - rank(b.it.section)) || (a.i - b.i))
    .map(({ it }) => ({
      value: it.name,
      label: it.name,
      group: lxcSectionName(it.section),
      hint: it.arch ? `（${it.arch}）` : "",
    }));
}

// 分类筛选下拉：按当前列表里实际出现的 section 生成选项（「全部分类（N）」+ 各类中文小标题及数量）；
// 保留已选分类（切换下载源后该类可能不存在 → 自动回落「全部分类」）
function tcFillLxcSections(items) {
  const sel = $("tcLxcSection");
  if (!sel) return;
  if (!items.length) {
    sel.innerHTML = '<option value="">（暂无分类）</option>';
    sel.value = "";
    lxcSectionFilter = "";
    return;
  }
  const order = [];
  const counts = {};
  items.forEach((it) => {
    const s = it.section || "";
    if (!(s in counts)) { counts[s] = 0; order.push(s); }
    counts[s] += 1;
  });
  const rank = (s) => {
    const i = LXC_SECTION_ORDER.indexOf(s);
    return i < 0 ? LXC_SECTION_ORDER.length : i;
 };
 // 先按声明的分类顺序排，其余保持后端原序（用原始下标作次键，避免组内乱序）
 const sorted = order
 .map((s, i) => ({ s, i }))
    .sort((a, b) => (rank(a.s) - rank(b.s)) || (a.i - b.i))
    .map((x) => x.s);
  sel.innerHTML = [`<option value="">全部分类（${items.length}）</option>`]
    .concat(sorted.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(lxcSectionName(s))}（${counts[s]}）</option>`))
    .join("");
  sel.value = sorted.includes(lxcSectionFilter) ? lxcSectionFilter : "";
  lxcSectionFilter = sel.value;
}

// 按当前分类筛选重建模板选择器；默认选中：全部分类时优先 system 组首项，否则取该类首项
function tcRenderLxcPicker() {
  if (!tcLxcPicker) return;
  const list = lxcSectionFilter
    ? lxcTemplates.filter((x) => (x.section || "") === lxcSectionFilter)
    : lxcTemplates;
  if (!list.length) {
    tcLxcPicker.setOptions([], "");
    tcLxcPicker.setPlaceholder("（该分类暂无模板）");
    const u = $("tcLxcUrl"); if (u) u.value = "";
    return;
  }
  const def = (!lxcSectionFilter ? list.find((x) => (x.section || "") === "system") : null) || list[0];
  tcLxcPicker.setOptions(tcLxcOptions(list), def ? def.name : "");
  tcSyncLxcUrl();
}

// 选中模板 → 带出下载地址（地址由后端按「模板名 + 所选源」拼出，前端只读展示）
function tcSyncLxcUrl() {
  const sel = $("tcLxcVersion");
  const url = $("tcLxcUrl");
  if (!sel || !url) return;
  const it = lxcTemplates.find((x) => x.name === sel.value);
  url.value = it ? (it.url || "") : "";
}

// 拉取模板列表：refresh=true 时后端先跑 pveam update 刷新索引
async function tcLoadLxcTemplates(refresh = false) {
  const txt = $("tcLxcText");
  if (!tcLxcPicker) return;
  const mirror = ($("tcLxcMirror") || {}).value || "follow";
  if (refresh && txt) txt.textContent = "刷新索引中…";
  let data = {};
  try {
    data = await api(`/lxc-template/available?refresh=${refresh ? 1 : 0}&mirror=${encodeURIComponent(mirror)}`);
  } catch (e) {
    if (txt) txt.textContent = "列表加载失败";
    return toast("模板列表加载失败: " + e.message, true);
  }
  lxcTemplates = Array.isArray(data.items) ? data.items : [];
  tcFillLxcMirrors(Array.isArray(data.mirrors) ? data.mirrors : []);
  tcFillLxcSections(lxcTemplates);
  if (!lxcTemplates.length) {
    tcLxcPicker.setOptions([], "");
    tcLxcPicker.setPlaceholder("（暂无可用模板）");
    const u = $("tcLxcUrl"); if (u) u.value = "";
    if (txt) txt.textContent = data.error ? `列表为空：${data.error}` : "暂无可用模板";
    if (data.error) toast(data.error, true);
    return;
  }
  // 按当前分类筛选重建选择器（默认从系统镜像开始，避免默认停在 TurnKey/其他应用）
  tcRenderLxcPicker();
  // 后端会剔除上游已下架、各镜像站均无文件的条目（hidden_stale），这里如实告知，避免用户以为列表少了
  const stale = Number(data.hidden_stale || 0) > 0
    ? `，已隐藏 ${data.hidden_stale} 个上游已下架的模板` : "";
  if (txt) txt.textContent = data.error
    ? `已列出 ${lxcTemplates.length} 个模板${stale}（${data.error}）`
    : `已列出 ${lxcTemplates.length} 个模板${stale}`;
}

// 下载（CT 模板）：进度同步到本面板的「活动传输」
async function tcDownloadLxc() {
  const name = ($("tcLxcVersion") || {}).value || "";
  const storage = ($("tcLxcStorage") || {}).value || "";
  const mirror = ($("tcLxcMirror") || {}).value || "follow";
  if (!name) return toast("请选择要下载的模板", true);
  if (!storage) return toast("请选择目标存储（需支持容器模板 vztmpl 内容）", true);
  const t = tcAddTransfer(name, "lxc");
  const txt = $("tcLxcText"); if (txt) txt.textContent = "请求中…";
  try {
    // section 决定后端拼 URL 时 images/ 下的一级目录（system/mail/...），缺了会 404
    const payload = { name, storage, mirror };
    const it = lxcTemplates.find((x) => x.name === name);
    if (it && it.section) payload.section = it.section;
    const r = await api("/lxc-template/download", "POST", payload);
    await pollLxcDownload((r && r.name) || name, {
      onProgress: (pct) => { t.pct = pct; if (txt) txt.textContent = "下载中 " + pct + "%"; tcRenderActivity(); },
      onDone: () => {
        t.state = "done"; t.pct = 100;
        if (txt) txt.textContent = "下载完成 ✓";
        tcRenderActivity();
        // 若「存储内容」当前看的就是该存储，顺带刷新出刚下载的模板
        const cs = $("tcContentStorage");
        if (cs && cs.value === storage) tcLoadStorageContent();
      },
      onError: (msg) => {
        t.state = "error";
        t.error = msg || "";   // 记下失败原因并显示在活动列表，避免只看到「失败」
        if (txt) txt.textContent = "下载失败";
        tcRenderActivity();
      },
      onCancel: () => { t.state = "cancel"; if (txt) txt.textContent = "已取消"; tcRenderActivity(); },
    });
  } catch (e) {
    t.state = "error";
    t.error = e.message || "";
    if (txt) txt.textContent = "下载失败";
    tcRenderActivity();
    toast("下载失败: " + e.message, true);
  }
}

// 轮询 CT 模板后台下载进度（复用云镜像的常驻提示条，仅状态接口不同）
async function pollLxcDownload(name, cb = {}) {
  const banner = showCloudDlBanner(name, () => {
    api("/lxc-template/cancel", "POST", { name }).catch(() => {});
  });
  banner.cancelRequested = false;
  setCloudDlBannerText(name, `LXC 模板下载中 ${name}…`, false, true);
  let lastPct = -1;
  for (;;) {
    if (banner.cancelRequested) {
      setCloudDlBannerText(name, `已取消：${name}`, false, false);
      hideCloudDlBanner(name, 2000);
      if (cb.onCancel) cb.onCancel();
      return;
    }
    await new Promise((res) => setTimeout(res, 1000));
    const st = await api(`/lxc-template/status?name=${encodeURIComponent(name)}`);
    if (st.status === "cancelled") {
      setCloudDlBannerText(name, `已取消：${name}`, false, false);
      hideCloudDlBanner(name, 2000);
      if (cb.onCancel) cb.onCancel();
      return;
    }
    if (st.status === "done") {
      setCloudDlBannerText(name, `✓ LXC 模板 ${name} 下载完成`, false, false);
      hideCloudDlBanner(name, 3000);
      break;
    }
    if (st.status === "error") {
      setCloudDlBannerText(name, `✕ 下载失败: ${st.error || ""}`, true, false);
      hideCloudDlBanner(name, 5000);
      if (cb.onError) cb.onError(st.error || "下载失败");
      return;
    }
    if (st.status === "notfound") {
      setCloudDlBannerText(name, "✕ 下载任务丢失，请重试", true, false);
      hideCloudDlBanner(name, 5000);
      if (cb.onError) cb.onError("任务丢失");
      return;
    }
    const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
    if (pct !== lastPct) {
      setCloudDlBannerText(name, `LXC 模板下载中 ${name} — ${pct}%`, false, true);
      if (cb.onProgress) cb.onProgress(pct);
      lastPct = pct;
    }
  }
  if (cb.onDone) cb.onDone();
}

// 刷新页面后接回仍在后台跑的模板下载进度
async function tcResumeLxcDownloads() {
  let list = [];
  try { list = await api("/lxc-template/list"); } catch (e) { return; }
  if (!Array.isArray(list) || !list.length) return;
  for (const task of list) {
    const name = task.name;
    if (!name) continue;
    if (tcTransfers.some((t) => t.dir === "lxc" && t.name === name)) continue;  // 防重复
    const t = tcAddTransfer(name, "lxc");
    if (task.status === "downloading") {
      t.state = "running";
      t.pct = task.total ? Math.round((task.done / task.total) * 100) : 0;
      tcRenderActivity();
      pollLxcDownload(name, {
        onProgress: (pct) => { t.pct = pct; tcRenderActivity(); },
        onDone: () => { t.state = "done"; t.pct = 100; tcRenderActivity(); },
        onError: (msg) => { t.state = "error"; t.error = msg || ""; tcRenderActivity(); },
        onCancel: () => { t.state = "error"; tcRenderActivity(); },
      });
    } else if (task.status === "done") {
      t.state = "done"; t.pct = 100; tcRenderActivity();
    } else {
      t.state = "error"; tcRenderActivity();
    }
  }
}

// ===== 存储管理（数据中心 → 存储：列表 / 新增 / 编辑 / 移除） =====

// PVE 支持的内容类型（与存储内容面板的官方译名一致）
const SM_CONTENT_TYPES = [
  { v: "iso", l: "ISO 镜像" },
  { v: "vztmpl", l: "容器模板" },
  { v: "backup", l: "备份" },
  { v: "snippets", l: "配置片段" },
  { v: "images", l: "磁盘镜像" },
  { v: "rootdir", l: "容器" },
  { v: "import", l: "导入" },
];

// 编辑已存在存储时，以下位置/身份字段不可变更（pvesh set 不支持，前端亦不回传，避免 500）
const SM_IMMUTABLE_FIELDS = new Set([
  "path", "server", "export", "vgname", "thinpool", "pool",
  "portal", "target", "share", "datastore", "monhost", "fs-name",
]);

// 存储类型表（与 PVE「数据中心 → 存储 → 新增」菜单顺序一致；其它高级参数可走 options 透传）
const SM_TYPES = [
  { v: "dir", l: "目录 (dir)", fields: [
    { n: "path", l: "路径", t: "text", req: true, ph: "/mnt/storage" },
    { n: "content", l: "内容", t: "content", def: "iso,vztmpl,backup,images" },
    { n: "shared", l: "共享", t: "bool", def: "0" },
  ]},
  { v: "lvm", l: "LVM", fields: [
    { n: "vgname", l: "卷组", t: "text", req: true, ph: "vg0" },
    { n: "content", l: "内容", t: "content", def: "images,rootdir" },
    { n: "shared", l: "共享", t: "bool", def: "0" },
  ]},
  { v: "lvmthin", l: "LVM-Thin", fields: [
    { n: "vgname", l: "卷组", t: "text", req: true },
    { n: "thinpool", l: "Thin Pool", t: "text", req: true },
    { n: "content", l: "内容", t: "content", def: "images,rootdir" },
  ]},
  { v: "btrfs", l: "BTRFS", fields: [
    { n: "path", l: "路径", t: "text", req: true },
    { n: "content", l: "内容", t: "content", def: "iso,vztmpl,backup,images" },
  ]},
  { v: "nfs", l: "NFS", fields: [
    { n: "server", l: "NFS 服务器", t: "text", req: true, ph: "192.168.1.10" },
    { n: "export", l: "导出路径", t: "text", req: true, ph: "/srv/nfs/share" },
    { n: "content", l: "内容", t: "content", def: "iso,vztmpl,backup,images,snippets" },
    { n: "options", l: "挂载选项 (vers=3,nolock,...)", t: "text" },
  ]},
  { v: "cifs", l: "SMB/CIFS", fields: [
    { n: "server", l: "服务器", t: "text", req: true },
    { n: "share", l: "共享名", t: "text", req: true },
    { n: "username", l: "用户名", t: "text" },
    { n: "password", l: "密码", t: "password" },
    { n: "content", l: "内容", t: "content", def: "iso,vztmpl,backup,images,snippets" },
  ]},
  { v: "iscsi", l: "iSCSI", fields: [
    { n: "portal", l: "Portal", t: "text", req: true, ph: "192.168.1.10" },
    { n: "target", l: "Target IQN", t: "text", req: true },
    { n: "content", l: "内容", t: "content", def: "images" },
  ]},
  { v: "zfs-over-iscsi", l: "ZFS over iSCSI", fields: [
    { n: "portal", l: "Portal", t: "text", req: true },
    { n: "target", l: "Target", t: "text", req: true },
    { n: "pool", l: "ZFS Pool", t: "text", req: true },
    { n: "content", l: "内容", t: "content", def: "images" },
  ]},
  { v: "cephfs", l: "CephFS", fields: [
    { n: "content", l: "内容", t: "content", def: "snippets,iso,vztmpl,backup,images,rootdir" },
    { n: "fs-name", l: "CephFS 名", t: "text" },
    { n: "monhost", l: "Monitors (逗号分隔)", t: "text" },
  ]},
  { v: "rbd", l: "RBD", fields: [
    { n: "pool", l: "Pool", t: "text", req: true },
    { n: "content", l: "内容", t: "content", def: "images,rootdir" },
    { n: "monhost", l: "Monitors", t: "text" },
  ]},
  { v: "zfspool", l: "ZFS", fields: [
    { n: "pool", l: "Pool 名", t: "text", req: true, ph: "tank" },
    { n: "content", l: "内容", t: "content", def: "images,rootdir" },
    { n: "sparse", l: "Sparse (1/0)", t: "text", def: "1" },
  ]},
  { v: "pbs", l: "Proxmox Backup Server", fields: [
    { n: "server", l: "PBS 服务器", t: "text", req: true },
    { n: "datastore", l: "Datastore", t: "text", req: true },
    { n: "username", l: "用户名 (user@realm)", t: "text", req: true },
    { n: "password", l: "密码", t: "password", req: true },
    { n: "content", l: "内容", t: "content", def: "backup" },
  ]},
  { v: "esxi", l: "ESXi", fields: [
    { n: "server", l: "ESXi 服务器", t: "text", req: true },
    { n: "username", l: "用户名", t: "text", req: true },
    { n: "password", l: "密码", t: "password", req: true },
    { n: "content", l: "内容", t: "content", def: "images" },
  ]},
];

const SM_TYPE_LABEL = Object.fromEntries(SM_TYPES.map((t) => [t.v, t.l]));
const smTypeOf = (v) => SM_TYPES.find((t) => t.v === v) || null;
const smShowModal = () => $("modal") && $("modal").classList.remove("hidden");
const smHideModal = () => $("modal") && $("modal").classList.add("hidden");

// ===== 传输中心 → 缓存加速（节点本机盘的建机/还原数据源缓存：模板/云镜像/备份/LXC 模板）=====
// 缓存由建机 / 还原脚本在节点本机盘上维护（后端 /api/template-cache 读取与删除），
// 这里提供按节点查看占用、删单条、清空，以及把某个模板「预热」到指定节点。
// 预热走异步任务（与工具箱同一条任务通道），因此可看进度、可强杀。

let _tmplWarmTimer = null;
let _tmplWarmOffset = 0;
let _tmplCacheDumpLocal = false;   // 当前所选节点是否为共享存储导出端（导出端无需缓存）

// 加载节点列表 + 所选节点的缓存明细。
// 节点下拉只列「需要缓存」的节点：共享存储服务端的模板目录就在本机盘上，读模板本就是
// 本地 IO，缓存没有意义——列出来只会让人误选，所以直接不列，并在说明里交代隐藏了谁。
async function tmplCacheLoad() {
  const sel = $("tmplCacheNode");
  if (!sel) return;
  const box = $("tmplCacheList");
  const sum = $("tmplCacheSummary");
  const keep = sel.value;
  if (sum) sum.textContent = "读取中…";
  let meta = null;
  try {
    meta = await api("/template-cache/nodes");
  } catch (e) {
    if (sum) sum.textContent = "";
    if (box) box.innerHTML = `<div class="muted">读取失败：${escapeHtml(e.message || e)}</div>`;
    return;
  }
  const all = (meta && Array.isArray(meta.nodes)) ? meta.nodes : [];
  const usable = all.filter((n) => !n.dump_local && !n.error);
  const want = usable.map((n) => `<option value="${escapeHtml(n.node)}">${escapeHtml(n.node)}</option>`).join("");
  if (sel.dataset.sig !== want) {
    sel.innerHTML = want;
    sel.dataset.sig = want;
  }
  // 默认选中「本机」：首次进入或原选择已失效时落到本机（跑本服务这台），
  // 本机不在可缓存列表里（如它是共享存储服务端）才退回第一个可用节点。
  const localName = (meta && meta.local) || localNodeName();
  if (keep && usable.some((n) => n.node === keep)) sel.value = keep;
  else if (usable.some((n) => n.node === localName)) sel.value = localName;
  else if (usable.length) sel.value = usable[0].node;
  if (!usable.length) {
    // 区分「确实都不需要缓存」和「一个都没问到」：后者多半是节点离线，不能误导成前者
    const allErr = all.length > 0 && all.every((n) => n.error);
    if (sum) {
      if (allErr) {
        sum.textContent = "读取各节点缓存信息失败（节点可能离线）。";
      } else {
        // 结论只按「实际读到的节点」说，不笼统讲「本集群各节点都直读」：
        // 一是跑本服务的这台往往就是数据源所在机器，用户关心的是「本机无需缓存」；
        // 二是读不到的节点（SSH 失败 / 离线）不能被这句话顺带吞掉，否则会误以为它们也不用缓存。
        const locals = all.filter((n) => !n.error && n.dump_local).map((n) => n.node);
        const errs = all.filter((n) => n.error).map((n) => n.node);
        let txt = (locals.length === 1 && locals[0] === localName)
          ? "本机无需缓存，集群内其他节点缓存模板后，有助于加速建机。"
          : (locals.length
              ? `这些节点的模板目录都在各自本机盘上，建机 / 还原直接本地读取，无需缓存：${locals.join("、")}。`
              : "没有需要缓存的节点。");
        if (errs.length) txt += `（未能读取：${errs.join("、")}，可能离线）`;
        sum.textContent = txt;
      }
    }
    if (box) box.innerHTML = "";
    return;
  }
  try {
    tmplCacheRender(await api(`/template-cache?node=${encodeURIComponent(sel.value)}`));
  } catch (e) {
    if (sum) sum.textContent = "";
    if (box) box.innerHTML = `<div class="muted">读取失败：${escapeHtml(e.message || e)}</div>`;
  }
}

// 缓存时间用短格式（MM-DD HH:mm）：toLocaleString 的长日期在行内太占地方
function tmplCacheShortTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function tmplCacheRender(d) {
  const sum = $("tmplCacheSummary");
  const box = $("tmplCacheList");
  if (!box) return;
  if (!d || !d.root_ok) {
    _tmplCacheDumpLocal = false;
    if (sum) sum.textContent = "该节点未启用缓存加速（未找到可用的本机存储落点）";
    box.innerHTML = "";
    return;
  }
  const limit = (d.limit_mb || 0) * 1024 * 1024;
  // dump_local：该节点上模板目录就在本机盘，也就是这台机器是共享存储的导出端。
  // 导出端读模板本来就是本地 IO，缓存没有意义（后端已直接跳过、不落缓存），
  // 这里必须明说，否则「暂无缓存」会被当成漏配或功能失效。
  _tmplCacheDumpLocal = !!d.dump_local;
  // 用量在前、路径在后：先给最关心的信息，路径作为次要信息收在行尾
  if (sum) {
    const limitTxt = (d.limit_mb && d.limit_mb > 0) ? fmtBytes(limit) : "不限（仅按保留天数）";
    const data = `已用 ${fmtBytes(d.total)} / ${limitTxt}` +
      (d.free != null ? ` · 盘剩余 ${fmtBytes(d.free)}` : "") + ` · ${d.root}`;
    const policy = `策略：缓存不超过本机盘 ${((d.max_ratio || 0) * 100).toFixed(0)}%` +
      (d.max_age_days ? ` · 超过 ${d.max_age_days} 天未用自动清理` : "");
    const role = _tmplCacheDumpLocal
      ? "该节点即共享存储服务端：数据源就在本机盘上，建机 / 还原直接本地读取，无需缓存"
      : "该节点经共享存储网络读取数据源：缓存可显著加速本节点的建机与还原";
    sum.innerHTML = `<div>${escapeHtml(data)}</div>` +
      (policy ? `<div class="u-sub">${escapeHtml(policy)}</div>` : "") +
      `<div class="tc-role${_tmplCacheDumpLocal ? " warn" : ""}">${escapeHtml(role)}</div>`;
  }
  const items = Array.isArray(d.items) ? d.items : [];
  if (!items.length) {
    box.innerHTML = _tmplCacheDumpLocal
      ? '<div class="muted">该节点无需缓存，此处保持为空。</div>'
      : '<div class="muted">该节点暂无缓存（首次建机、还原或预热时会自动拉取）</div>';
    return;
  }
  // 只有异常条目才在行内标状态：正常条目标「已缓存」纯属噪音
  const label = { orphan: "残留（缺元数据）", partial: "拉取中或残留" };
  const rows = items.map((it) => {
    // 拉取中的中间文件不给「删除」按钮，避免删掉别的任务正在写的文件
    const acts = it.state === "partial" ? "" :
      `<button type="button" class="btn btn-sm danger" data-cache-del="${escapeHtml(it.name)}">删除</button>`;
    const meta = [fmtBytes(it.size), label[it.state] || "", tmplCacheShortTime(it.last_used)]
      .filter(Boolean).join(" · ");
    // 类型标签：缓存本身不记用途，由后端按来源路径推断（虚机模板 / 云镜像 / 备份 / LXC 模板），
    // 认不出来就不显示，不用「未知」占位 —— 那只会白占一行宽度
    const kind = it.kind ? `<span class="tc-cache-kind">${escapeHtml(it.kind)}</span>` : "";
    // 来源路径不单独占一行（文件名与它同名，铺开会把操作按钮挤到第二行），收进悬浮提示
    const tip = it.src ? ` title="来源：${escapeHtml(it.src)}"` : "";
    return `<div class="sc-row tc-cache-row"${tip}>${kind}<span class="sc-name">${escapeHtml(it.name)}</span>` +
      `<span class="sc-meta">${escapeHtml(meta)}</span>` +
      `<span class="sc-actions">${acts}</span></div>`;
  }).join("");
  // 「清空」「清理过期」跟它作用的列表放在同一行的右端：放到工具条最右会被中间的大片空白推离上下文，
  // 看起来像页面级操作，其实它只清这一份列表
  box.innerHTML = `<div class="tc-list-head"><span>缓存条目（${items.length}）</span>` +
    `<span style="display:inline-flex;gap:8px">` +
    `<button type="button" class="btn btn-sm" data-cache-expire="1">清理过期</button>` +
    `<button type="button" class="btn danger btn-sm" data-cache-clear="1">清空缓存</button></span></div>` +
    `<div class="tc-list-body">${rows}</div>`;
  box.querySelectorAll("[data-cache-del]").forEach((b) =>
    b.addEventListener("click", () => tmplCacheDelete(b.dataset.cacheDel)));
  box.querySelectorAll("[data-cache-clear]").forEach((b) =>
    b.addEventListener("click", tmplCacheClear));
  box.querySelectorAll("[data-cache-expire]").forEach((b) =>
    b.addEventListener("click", tmplCacheExpire));
}

async function tmplCacheDelete(name) {
  const node = $("tmplCacheNode") ? $("tmplCacheNode").value : "";
  if (!(await appConfirm(`确定删除缓存「${name}」？\n删除后该节点下次建机 / 还原需重新从共享存储拉取（首次较慢）。`, { danger: true }))) return;
  try {
    await api("/template-cache", "DELETE", { node, name });
    toast("已删除");
    tmplCacheLoad();
  } catch (e) {
    toast("删除失败：" + (e.message || e), true);
  }
}

async function tmplCacheClear() {
  const node = $("tmplCacheNode") ? $("tmplCacheNode").value : "";
  const where = node ? `节点「${node}」` : "本机";
  if (!(await appConfirm(`确定清空 ${where} 上的全部缓存？\n请勿在拉取 / 建机任务执行中操作；清空后该节点下次建机需重新拉取。`, { danger: true }))) return;
  try {
    const r = await api("/template-cache", "DELETE", { node, all: true });
    toast(`已清理 ${r.count} 个文件，释放 ${fmtBytes(r.freed || 0)}`);
    tmplCacheLoad();
  } catch (e) {
    toast("清空失败：" + (e.message || e), true);
  }
}

async function tmplCacheExpire() {
  const node = $("tmplCacheNode") ? $("tmplCacheNode").value : "";
  const where = node ? `节点「${node}」` : "本机";
  if (!(await appConfirm(`确定清理 ${where} 上「超过保留天数」的过期缓存？\n只删除很久未用（超期）的条目，不影响近期在用缓存。`))) return;
  try {
    const r = await api("/template-cache/expire", "POST", { node });
    const note = r.note ? `（${r.note}）` : "";
    toast(`已清理 ${r.count} 个过期条目，释放 ${fmtBytes(r.freed || 0)}${note}`);
    tmplCacheLoad();
  } catch (e) {
    toast("清理失败：" + (e.message || e), true);
  }
}

// 预热候选：与「模板快速建机」同源；只有 file: 来源的模板文件才存在缓存可预热
async function tmplWarmupLoadTemplates() {
  const sel = $("tmplWarmupTemplate");
  if (!sel) return;
  const keep = sel.value;
  try {
    const bs = await api("/backup/files");
    const opts = (bs || []).filter((b) => b.template).map((b) => {
      const name = String(b.path || "").split("/").pop();
      const size = b.size != null ? `（${fmtBytes(b.size)}）` : "";
      return `<option value="file:${escapeHtml(b.path)}">${escapeHtml(name)}${escapeHtml(size)}</option>`;
    });
    sel.innerHTML = '<option value="">选择虚机模板…</option>' + opts.join("");
    if (keep) sel.value = keep;
  } catch (_) {
    sel.innerHTML = '<option value="">虚机模板列表加载失败</option>';
  }
}

// 一键复制预热日志：这块面板是纯文本累加（textContent），直接取原文即可，无需另存缓冲
async function tmplWarmupCopyLog() {
  const box = $("tmplWarmupLog");
  const text = box ? box.textContent.trim() : "";
  if (!text) { toast("暂无可复制的日志", true); return; }
  const ok = await copyTextToClipboard(text);
  toast(ok ? "已复制预热日志" : "复制失败，请手动选择日志后复制", !ok);
}

async function tmplWarmupRun() {
  const tsel = $("tmplWarmupTemplate");
  const nsel = $("tmplCacheNode");
  const wrap = $("tmplWarmupLogWrap");
  const box = $("tmplWarmupLog");
  if (!tsel || !tsel.value) return toast("请先选择要预热的虚机模板", true);
  const node = nsel ? nsel.value : "";
  const where = node ? `节点「${node}」` : "本机";
  // 兜底：正常情况下这类节点已从下拉里过滤掉；万一后端判定变化导致它出现，也别白跑一趟
  if (_tmplCacheDumpLocal) return toast(`${where}是共享存储服务端，模板本机直读，无需预热`);
  if (!(await appConfirm(`确定把该模板预热到 ${where}？\n会在目标节点本机盘上拉取一份缓存（10G 级模板可能十几分钟），期间可在「执行日志」查看进度。`,
                         { title: "预热模板缓存", okText: "开始预热" }))) return;
  if (wrap) wrap.classList.remove("hidden");
  const logToggle = $("tmplWarmupLogToggle");
  if (logToggle) logToggle.textContent = "收起";
  if (box) { box.classList.remove("hidden"); box.textContent = ""; }
  tmplWarmupProgress(0, "准备中…");
  try {
    const r = await api("/template-cache/warmup", "POST", { node, template: tsel.value });
    _tmplWarmOffset = 0;
    if (box) box.textContent = `[任务已提交] ${r.job_id}\n`;
    tmplWarmupPoll(r.job_id);
  } catch (e) {
    tmplWarmupProgress(0, "提交失败");
    toast("提交失败：" + (e.message || e), true);
  }
}

// 进度条：后台任务的进度只体现在会滚动的日志里，这里把它同步成一条可见的进度。
// 数据源就是日志里的「拉取中 xx%」行——不额外增加接口，也保证与日志口径一致。
function tmplWarmupProgress(pct, text) {
  const p = $("tmplWarmupProg");
  if (!p) return;
  p.classList.remove("hidden");
  const fill = p.querySelector(".iso-progress > i");
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  const txt = $("tmplWarmupPct");
  if (txt && text != null) txt.textContent = text;
}

// 预热进度：轮询增量日志，任务结束后刷新缓存列表（体积已变）
async function tmplWarmupPoll(jobId) {
  const box = $("tmplWarmupLog");
  if (_tmplWarmTimer) { clearTimeout(_tmplWarmTimer); _tmplWarmTimer = null; }
  try {
    const snap = await api(`/jobs/${encodeURIComponent(jobId)}?offset=${_tmplWarmOffset}`);
    if (box && snap && Array.isArray(snap.lines) && snap.lines.length) {
      box.textContent += snap.lines.join("\n") + "\n";
      box.scrollTop = box.scrollHeight;
      snap.lines.forEach((ln) => {
        const m = /拉取中 (\d+)%/.exec(ln);
        if (m) tmplWarmupProgress(Number(m[1]), `拉取中 ${m[1]}%`);
      });
    }
    if (snap && snap.offset != null) _tmplWarmOffset = snap.offset;
    if (snap && snap.status === "running") {
      _tmplWarmTimer = setTimeout(() => tmplWarmupPoll(jobId), 1500);
    } else if (snap) {
      if (box) { box.textContent += `\n[结束] 状态：${snap.status}\n`; box.scrollTop = box.scrollHeight; }
      if (snap.status === "success") tmplWarmupProgress(100, "已完成");
      else tmplWarmupProgress(0, "已结束");
      tmplCacheLoad();
    }
  } catch (e) {
    if (box) box.textContent += `\n[进度查询中断] ${e.message || e}\n`;
  }
}

// 初始化传输中心交互
function initTransferCenter() {
  const pick = $("tcPickFile"); if (pick) pick.addEventListener("click", () => $("tcFileInput").click());
  const fi = $("tcFileInput");
  if (fi) fi.addEventListener("change", () => {
    $("tcFileName").textContent = fi.files[0] ? fi.files[0].name : "未选择";
    tcAutoAlignType();   // 按文件扩展名自动对齐「类型」（当前类型不匹配时才改）
  });
  const up = $("tcUploadBtn"); if (up) up.addEventListener("click", tcUpload);
  const dl = $("tcDownloadBtn"); if (dl) dl.addEventListener("click", tcDownload);
  const chk = $("tcCheckPresetsBtn"); if (chk) chk.addEventListener("click", tcCheckPresets);
  // 「管理预设源」弹窗：打开 / 关闭 / 增行 / 删行 / 保存 / 点遮罩关闭
  const mgr = $("tcManagePresetsBtn"); if (mgr) mgr.addEventListener("click", openPresetModal);
  const pmClose = $("presetModalClose"); if (pmClose) pmClose.addEventListener("click", () => $("presetModal").classList.add("hidden"));
  const pmCancel = $("presetCancel"); if (pmCancel) pmCancel.addEventListener("click", () => $("presetModal").classList.add("hidden"));
  const pmSave = $("presetSave"); if (pmSave) pmSave.addEventListener("click", tcSavePresets);
  const pmReset = $("presetReset"); if (pmReset) pmReset.addEventListener("click", tcResetPresets);
  const pmAdd = $("presetAdd"); if (pmAdd) pmAdd.addEventListener("click", () => {
    // 新增行：先收集当前所有输入（保留用户已填内容），尾部追加一行后重新渲染，保证序号连续
    const box = $("presetList");
    if (!box) return;
    const list = [];
    box.querySelectorAll(".preset-row").forEach((r) => {
      list.push({
        label: (r.querySelector(".preset-label").value || "").trim(),
        url: (r.querySelector(".preset-url").value || "").trim(),
      });
    });
    list.push({ label: "", url: "" });
    renderPresetRows(list);
  });
  const pmList = $("presetList");
  if (pmList) pmList.addEventListener("click", (e) => {
    const del = e.target.closest(".preset-del");
    if (del) { const row = del.closest(".preset-row"); if (row) row.remove(); reindexPresetRows(); }
  });
  const pmMask = $("presetModal");
  if (pmMask) pmMask.addEventListener("click", (e) => { if (e.target === pmMask) pmMask.classList.add("hidden"); });
  const tog = $("tcToggle"); if (tog) tog.addEventListener("click", (e) => tcToggleCenter(e.currentTarget));
  const togDl = $("tcToggleDl"); if (togDl) togDl.addEventListener("click", (e) => tcToggleCenter(e.currentTarget));
  const togSc = $("tcToggleSc"); if (togSc) togSc.addEventListener("click", (e) => tcToggleCenter(e.currentTarget));
  const togLxc = $("tcToggleLxc"); if (togLxc) togLxc.addEventListener("click", (e) => tcToggleCenter(e.currentTarget));
  // 「获取 LXC 模板」：选模板带出地址、切分类只筛列表（不重新拉取）、切下载源重列、下载 / 刷新索引
  const lv = $("tcLxcVersion"); if (lv) lv.addEventListener("change", tcSyncLxcUrl);
  const lsec = $("tcLxcSection"); if (lsec) lsec.addEventListener("change", () => { lxcSectionFilter = lsec.value; tcRenderLxcPicker(); });
  const lm = $("tcLxcMirror"); if (lm) lm.addEventListener("change", () => tcLoadLxcTemplates(false));
  const ld = $("tcLxcDownloadBtn"); if (ld) ld.addEventListener("click", tcDownloadLxc);
  const lr = $("tcLxcRefreshBtn"); if (lr) lr.addEventListener("click", () => tcLoadLxcTemplates(true));
  // 「镜像 / 模板」可搜索下拉：必须先建组件，下面的 load 才有地方灌数据
  tcCloudPicker = createSearchSelect("tcDownloadVersion");
  tcLxcPicker = createSearchSelect("tcLxcVersion");
  tcOnTypeChange();
  tcLoadCloudPresets();
  tcWireCloudPresets();
  tcLoadCloudMirrors();
  tcLoadCloudStorages();
  const cm = $("tcCloudMirror"); if (cm) cm.addEventListener("change", tcSyncCloudUrl);
  tcLoadLxcStorages();
  tcLoadLxcTemplates(false);
  tcLoadClusterSources();
  tcLoadContentStorages();
  // 缓存加速面板：节点/刷新/预热为静态元素，只绑定一次；条目上的删除与「清空」在渲染时绑定
  const togCache = $("tcToggleCache"); if (togCache) togCache.addEventListener("click", (e) => tcToggleCenter(e.currentTarget));
  const cNode = $("tmplCacheNode"); if (cNode) cNode.addEventListener("change", tmplCacheLoad);
  const cRef = $("tmplCacheRefresh"); if (cRef) cRef.addEventListener("click", tmplCacheLoad);
  const wRun = $("tmplWarmupRun"); if (wRun) wRun.addEventListener("click", tmplWarmupRun);
  const wToggle = $("tmplWarmupLogToggle"); if (wToggle) wToggle.addEventListener("click", () => {
    const pre = $("tmplWarmupLog");
    if (!pre) return;
    pre.classList.toggle("hidden");
    wToggle.textContent = pre.classList.contains("hidden") ? "展开" : "收起";
  });
  tmplWarmupLoadTemplates();
  const scSel = $("tcContentStorage"); if (scSel) scSel.addEventListener("change", tcLoadStorageContent);
  const scBtn = $("tcRefreshContent"); if (scBtn) scBtn.addEventListener("click", tcLoadStorageContent);
  // 批量删除操作条（静态元素，仅绑定一次）
  const scSelAll = $("scSelectAll"); if (scSelAll) scSelAll.addEventListener("change", () => {
    const box = $("tcContentList"); if (!box) return;
    box.querySelectorAll(".sc-chk").forEach((cb) => { cb.checked = scSelAll.checked; });
    updateScSelection();
  });
  const scCancel = $("scBatchCancel"); if (scCancel) scCancel.addEventListener("click", () => {
    const box = $("tcContentList"); if (!box) return;
    box.querySelectorAll(".sc-chk").forEach((cb) => { cb.checked = false; });
    updateScSelection();
  });
  const scDel = $("scBatchDelete"); if (scDel) scDel.addEventListener("click", tcBatchDelete);
  tcRenderActivity();
  tcResumeCloudDownloads();
  tcResumeLxcDownloads();
  tcPromptInterruptedUpload();
  selectTcPane("upload");
}

// 左侧菜单切换传输中心子模块：高亮菜单项、显隐对应面板（其余折叠避免占用空间）
let _tcPane = "upload";
function selectTcPane(id) {
  _tcPane = id;
  document.querySelectorAll("#tcNav .cat-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.tc === id));
  document.querySelectorAll("#tcModules [data-tc-pane]").forEach((s) => {
    const show = s.dataset.tcPane === id;
    s.classList.toggle("hidden", !show);
    // 切回该面板时清掉可能的折叠态，保证正文可见
    if (show) s.classList.remove("tc-collapsed");
  });
  // 切到「缓存加速」时按需加载（节点列表可能已变化，进来重新拉一次最稳）
  if (id === "cache") tmplCacheLoad();
}

// 刷新页面后，若上次有上传被中断（sessionStorage 仍记着、但 JS 上下文已销毁、文件句柄丢失），提示用户重新选择文件
// 注：浏览器刷新会销毁 File 句柄，上传无法像下载那样自动续传，故只做友好提示
function tcPromptInterruptedUpload() {
  let rec = null;
  try { rec = JSON.parse(sessionStorage.getItem("pve-uploading") || "null"); } catch (e) {}
  if (!rec || !rec.name) return;
  try { sessionStorage.removeItem("pve-uploading"); } catch (e) {}
  toast(`上次上传「${rec.name}」被中断，请重新选择文件`, true);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initTransferCenter);
else initTransferCenter();


// 历史导出目录：列出所选存储中已导出的磁盘镜像，每行带「下载 + 删除」
async function loadExportHistory() {
  const listEl = document.getElementById("exportHistoryList");
  if (!listEl) return;
  const sel = document.querySelector('#toolForm select[data-param="storage"]');
  const storage = sel ? sel.value : "";
  if (!storage) {
    listEl.innerHTML = '<span class="iso-progress-text">（选择「导出到存储」后显示该存储已导出的磁盘镜像）</span>';
    return;
  }
  listEl.innerHTML = '<span class="iso-progress-text">加载中…</span>';
  try {
    const data = await api(`/disks/exported?storage=${encodeURIComponent(storage)}`);
    const files = (data && data.files) || [];
    if (!files.length) {
      listEl.innerHTML = '<span class="iso-progress-text">该存储暂无已导出文件</span>';
      return;
    }
    listEl.innerHTML = "";
    files.forEach((f) => {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:8px;align-items:center;margin:4px 0";
      const label = document.createElement("span");
      label.style.cssText = "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
      label.textContent = `${f.name}  (${fmtBytes(f.size)})`;
      const dl = document.createElement("a");
      dl.className = "btn btn-sm";
      dl.innerHTML = '<svg class="ico"><use href="#ico-download"></use></svg>下载';
      dl.href = `/api/disks/download?storage=${encodeURIComponent(storage)}&name=${encodeURIComponent(f.name)}`;
      dl.download = f.name;
      const del = document.createElement("button");
      del.className = "btn btn-sm danger";
      del.innerHTML = '<svg class="ico"><use href="#ico-trash"></use></svg>删除';
      del.onclick = async () => {
        if (!(await appConfirm(`确认删除导出文件 ${f.name}？此操作不可恢复`, { danger: true }))) return;
        try {
          const r = await api(`/disks/delete?storage=${encodeURIComponent(storage)}&name=${encodeURIComponent(f.name)}`, "POST");
          if (r && r.ok) { row.remove(); toast(`已删除 ${f.name}`); }
          else toast("删除失败", true);
        } catch (e) { toast("删除失败: " + e.message, true); }
      };
      row.append(label, dl, del);
      listEl.appendChild(row);
    });
  } catch (e) {
    listEl.innerHTML = `<span class="iso-progress-text">加载失败: ${escapeHtml(e.message)}</span>`;
  }
}

// 云镜像后台下载的常驻提示条：独立于工具弹窗 DOM，关闭弹窗后仍可继续显示下载进度
// 下载中带「取消」按钮：写入取消标记并通知后端中止，后端会清理半成品 .part 文件
const cloudDlBanners = {};
function showCloudDlBanner(name, onCancel) {
  let b = cloudDlBanners[name];
  if (!b) {
    const el = document.createElement("div");
    el.className = "toast cloud-dl-banner";   // 复用 .toast 样式，但不走自动消失逻辑
    el.style.zIndex = 210;
    const text = document.createElement("span");
    text.className = "cloud-dl-text";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cloud-dl-cancel";
    btn.textContent = "取消";
    el.appendChild(text);
    el.appendChild(btn);
    document.body.appendChild(el);
    b = cloudDlBanners[name] = { el, text, btn, cancelRequested: false, onCancel: null };
    btn.addEventListener("click", () => {
      b.cancelRequested = true;
      if (b.onCancel) b.onCancel();
    });
  }
  if (onCancel) b.onCancel = onCancel;
  b.el.classList.remove("hidden", "leaving");
  return b;
}
function setCloudDlBannerText(name, text, isError = false, showCancel = true) {
  const b = cloudDlBanners[name];
  if (!b) return;
  b.text.textContent = text;
  b.el.classList.toggle("error", !!isError);   // 仅切换 .error，避免每次刷新重播入场动画
  b.btn.style.display = showCancel ? "" : "none";
}
function hideCloudDlBanner(name, delay = 3000) {
  const b = cloudDlBanners[name];
  if (!b) return;
  setTimeout(() => {
    b.el.classList.add("leaving");
    setTimeout(() => { b.el.remove(); delete cloudDlBanners[name]; }, 300);
  }, delay);
}

// 云镜像部署表单内联：上传本地云镜像到导入目录（分片上传，落地 /var/lib/vz/cloud-images，与磁盘导入共享目录）


// 删除当前下拉中选中的 vBIOS ROM
async function deleteSelectedVbios() {
  const sel = document.querySelector('select[data-options-from="vbios_roms"]');
  const name = sel ? sel.value : "";
  if (!name) return toast("请先在 vBIOS ROM 下拉中选择要删除的文件", true);
  if (!(await appConfirm(`确定删除 vBIOS ROM ${name} 吗？删除后需重新上传。`, { danger: true }))) return;
  try {
    await api("/vbios", "DELETE", { name });
    toast(`已删除 ${name}`);
    await fillDynamicOptions("vbios_roms");
  } catch (e) {
    toast("删除失败: " + e.message, true);
  }
}

// 把共享存储里的 vBIOS 主副本同步到集群其它节点的 /usr/share/kvm/（PVE romfile 只认本机该目录）
async function syncVbiosToCluster() {
  try {
    const r = await api("/api/vbios/sync", "POST");
    const ok = (r.ok || []).length;
    const failed = (r.failed || []).length;
    if (ok === 0 && failed === 0) return toast("没有可同步的 vBIOS（先上传 ROM）");
    if (failed === 0) return toast(`已同步到 ${ok} 个节点`);
    toast(`成功 ${ok} 个，失败 ${failed} 个：${r.failed.join("; ")}`, true);
    await fillDynamicOptions("vbios_roms");
  } catch (e) {
    toast("同步失败: " + e.message, true);
  }
}
