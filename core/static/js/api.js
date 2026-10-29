// 请求层（M3/T3.1 从 app.js 抽出）：统一超时 + 有限重试 + 错误清洗。
//
// 只依赖浏览器 fetch 与同文件的 cleanErr；被 app.js 与各页面代码调用。

// ---- API 封装 ----

// 请求层：统一超时 + 有限重试（网络抖动 / 瞬时 5xx / 超时自动恢复，避免界面无谓报错）。
// GET 类可重试服务端 5xx；POST 等写操作仅在「网络层失败 / 超时」时重试，避免重复提交导致重复执行脚本。
const REQ_TIMEOUT_MS = 15000;
const REQ_RETRIES = 2;
const REQ_RETRY_DELAY = 300;                 // 基础退避，乘以 (attempt+1)
const REQ_RETRYABLE_SERVER = new Set([408, 429, 500, 502, 503, 504]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, method = "GET", payload = null, opts = null) {
  const isRead = method === "GET" || method === "HEAD";
  // opts 可选：{ timeout, retries }。个别慢接口（首次采集要等 pvesh / 外部命令）可单独放宽超时，
  // 或关掉重试避免「超时 15s + 重试 2 次」把用户拖在加载态近一分钟。
  const timeoutMs = (opts && opts.timeout) || REQ_TIMEOUT_MS;
  const retries = opts && opts.retries != null ? opts.retries : REQ_RETRIES;
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const reqOpts = { method, signal: ctrl.signal };
      if (payload !== null) {
        reqOpts.headers = { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" };
        reqOpts.body = JSON.stringify(payload);
      } else {
        // 读请求也带 CSRF 头：同源无预检，且个别接口可能带副作用（如强制刷新缓存）
        reqOpts.headers = { "X-Requested-With": "XMLHttpRequest" };
      }
      const resp = await fetch("/api" + path, reqOpts);
      clearTimeout(timer);
      let body;
      try { body = await resp.json(); } catch (_) { body = {}; }
      if (!resp.ok) {
        const msg = cleanErr((body && body.error) || `HTTP ${resp.status}`);
        if (isRead && attempt < retries && REQ_RETRYABLE_SERVER.has(resp.status)) {
          lastErr = new Error(msg);
          await sleep(REQ_RETRY_DELAY * (attempt + 1));
          continue;
        }
        throw new Error(msg);
      }
      return body.data;
    } catch (e) {
      clearTimeout(timer);
      // 超时（AbortError）与纯网络失败（TypeError）都只重试读请求：
      // 写操作（POST）超时不代表请求没送达——服务端可能已在执行，重发会导致破坏性操作重复执行。
      const netFail = (isRead && e.name === "AbortError") || ((e instanceof TypeError) && isRead);
      if (attempt < retries && netFail) {
        lastErr = e;
        await sleep(REQ_RETRY_DELAY * (attempt + 1));
        continue;
      }
      throw e;
    }
  }
  throw lastErr || new Error("请求失败");
}

// 下拉选项等只读数据的前端短缓存：同一 path（含查询参数）在 ttl 内复用结果，
// 避免切换工具 / 切换节点 / 依赖联动时反复拉取相同数据（后端 TTLCache 之上再省一层网络往返）。
// fillDynamicOptions 内通过 `const api = cachedApi` 把本函数的只读下拉请求都走此缓存，
// 不影响其它含状态的轮询/提交请求。
const _optionCache = new Map();
async function cachedApi(path, ttl = 5000, opts = null) {
  const now = Date.now();
  const hit = _optionCache.get(path);
  if (hit && now - hit.t < ttl) return hit.v;
  const v = await api(path, "GET", null, opts);
  _optionCache.set(path, { t: now, v });
  return v;
}

// 错误信息的最终过滤：剥除任何残留的命令行 / 代码 / 路径片段，
// 保证界面上只呈现友好提示或明确指示（不暴露 pvesh 命令等实现细节）
function cleanErr(msg) {
  return String(msg)
    .replace(/\[命令\][^\n]*/g, "")
    .replace(/^\s*pvesh[^\n]*/gim, "")
    .replace(/\/nodes\/[\w./-]*/g, "")
    .replace(/\bpvesh\b/gi, "")
    .replace(/\n{2,}/g, "\n")
    .replace(/\s{2,}/g, " ")
    .trim();
}
