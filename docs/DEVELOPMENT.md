# PVE WebTools 开发文档（Developer Guide）

面向**二次开发 / 重构维护**的指南。面向最终用户的功能说明见仓库根 `README.md`。

> 本工具**零第三方依赖**，仅用 Python 标准库；运行环境为 PVE 宿主机自带的系统 Python（Debian 12/13）。
> 开发机同样只需标准库 + `pytest` / `ruff`（测试/CI 用）。

---

## 1. 仓库结构与运行形态

```
core/            产品核心（Web 服务、工具箱、PVE 接口、前端静态资源）
scripts/         工程脚本（部署 / lint）
tests/           pytest 用例
docs/            开发文档
install.sh       一键安装（同步目录 + 注册 systemd）
```

程序以**源码直跑**方式部署（`python3 -m core.web.server`），没有二进制编译流程，
因此新增 Python 模块**无需**登记任何编译清单。

---

## 2. 后端架构（`core/` 分层）

```
HTTP 请求
  │
  ▼
core/web/server.py        # 服务器骨架：BaseHTTPRequestHandler 子类 + SilentHTTPServer
  │  do_GET/POST/DELETE/PUT
  ▼
core/web/routing.py        # RoutingMixin：路径切分 + 登录门禁 + 写闸口 + 统一异常映射入口
  │  _dispatch_guarded(method, parts)
  ▼
core/web/routers/          # 路由表分发（HANDLERS 协议）
  │  route(handler, method, parts) → 命中则回写响应
  ▼
core/web/routers/<domain>.py   # 各域实现（system/node/vm/storage/network/toolbox/cluster/...）
core/web/handlers.py           # 长尾 / 历史路由（仍在迁移收尾）
```

支撑模块（`core/web/`）：

- `http_util.py`：`_send_json` / `_error` / `_read_body` / `_json`、gzip/ETag/缓存头/Set-Cookie 通道
- `gate.py`：`_gate_write` 写操作公共闸口（当前放行，预留给跨接口的写保护）
- `static_srv.py`：首页版本注入、静态文件服务、gzip 缓存
- `sse.py`：任务日志流式推送 `_handle_job_stream`
- `errors.py`：**异常 → HTTP 状态码**唯一映射（`EXC_STATUS` / `guard_error`）；未预期异常回 500 中性文案，细节只进服务端日志
- `template_cache.py`：模板缓存目录管理
- `ops.py` / `maintenance.py`：节点缓存后台刷新、上传分片清理等
- `boot.py`：`main()` 进程启动入口（pvesh 门槛、后台线程、HTTP 服务拉起）
- `auth_pve.py` / `auth_session.py`：登录门禁（PVE 账号校验 + HMAC 会话 Cookie）

### 路由协议（`routers/`）

每个域模块暴露：

```python
HANDLERS = {
    "get":  {"edition": "_handle_edition"},
    "post": {"node/join-init": "_handle_node_join_init"},
}
```

值可以是 `ApiHandler` 上的方法名（字符串），也可以是域模块内的 `fn(handler, params)` 函数。

新增一域 = (1) 写 `routers/<domain>.py` 暴露 `HANDLERS`；(2) 在 `routers/__init__.py` 的
`_ROUTERS` 元组追加一行。`dispatch.merge` 会合并并检查**重复登记**。

### 工具箱系统（`core/tools/`）

- `toolbox.py` 持有 `REGISTRY`（`{id: {category, risk, params, script, ...}}`）与分类表
  `CATEGORIES`。`EDITION = "lite"`，装载 7 个分类模块。
- 工具脚本**外置**：声明了 `script_file` 的工具，正文从 `core/tools/scripts/{id}.sh` 读取
  （见 `toolbox._resolve_script`）。
- **安全约束**：用户输入**不拼接进脚本**，只通过 `PT_*` 环境变量注入
  （服务端按 `params` 的 `type/pattern/options` 校验后再注入），避免命令注入。
- 写配置前先备份，块状配置用 `PVEWEBTOOLS BEGIN/END` 标记幂等管理。
- `risk`：`info`=只读、`low`=普通（一次确认）、`high`=高危（需输入确认词，服务端校验）。
- 公共 bash 常量集中在 `toolbox_common.py`（`COMMON_HELPERS` / `KARGS_PRESETS` / `MIRRORS` 等）。
- **下拉数据源的节点相关性**必须登记在 `toolbox.py` 的
  `NODE_SCOPED_SOURCES` / `GUEST_OWNED_SOURCES` / `GUEST_NODE_SOURCES` / `NODE_AGNOSTIC_SOURCES`，
  否则前端不会联动（由 `scripts/lint_tool_sources.py` 与启动自检兜底）。

### PVE 接口（`core/pve/`）

`pve_api.py` 已按资源拆分到 `core/pve/api/`（`node` / `vm` / `storage` / `network` /
`access` / `base` / `cluster` / `schedule` / `passthrough`）。所有 PVE 调用走 `pvesh` CLI 封装。

`core/pve/ssh.py` 提供「密码 SSH」（sshpass 或 ssh+setsid），供「加入节点」连接尚未建立互信的新机器。

### 版本信息（`core/version.py`）

`version_info()` 返回 `{version, source}`，供 `/api/edition` 与设置弹窗的版本胶囊使用。
版本号读取包内 `VERSION` 文件，文件缺失时回退为「未知」。

---

## 3. 前端（`core/static/`）

- 入口 `index.html`：内联 SVG 图标库 + 各视图骨架；**末尾按序加载 classic `<script>`**
  （**非** ES module——内联 `onclick` 依赖全局作用域）。
- 脚本已拆到 `js/`（`state.js` / `ui.js` / `api.js` / `overview.js` / `settings.js` /
  `toolbox.js` / `transfer.js` / `passthrough.js` / `logs.js`）+ 入口 `app.js`。
- **静态资源接线测试**（`tests/test_static_assets.py`）强制：`static/` 下每个 `.js` 都必须被
  `index.html` 引用（漏加 `<script>` 标签会白屏且首页仍 200）。新增前端脚本**务必**在
  `index.html` 补 `<script src="...">`。
- 版本注入：首页会注入静态资源版本号，避免浏览器缓存旧版 `app.js` 导致新功能"不生效"。

### i18n 模式（`t()`）

- 后端文案集中在 `core/web/messages.py`：`MESSAGES` 字典 + `t(key)`（缺失 key 回退为 key 本身）。
- 前端同字典在 `core/static/zh.js`（`window.MESSAGES` + `window.t`），先于各模块加载。
- **前后端 key 必须一致**：`tests/test_messages.py` 校验两者 key 集合完全一致，
  且所有 `t("key")` 调用点的 key 必须存在。

---

## 4. 本地运行

```bash
cd <repo>                       # 仓库根目录（含 core/ 的那一层），不是 core/
python -m core.web.server 8899  # 默认 8899；监听 0.0.0.0
# 也可按文件路径直接运行（server.py 顶部有 sys.path 自举，无需设 PYTHONPATH）：
python /abs/path/to/core/web/server.py 8899
```

- 入口：`core/web/server.py` 的 `__main__` 分支转发到 `core/web/boot.py: main()`。
- 开关：`[端口]` 为可选整型位置参数；`--no-auth` 关闭登录门禁；`--tls` 启用 HTTPS。
- 非 PVE 环境仍会因 pvesh 门槛直接退出（`boot.py` 检查）；纯接口/逻辑测试用 pytest 的
  `base_url` 夹具，**不**调用 `server.main()`。

---

## 5. 测试与质量

```bash
python -m pytest                 # 全部用例
python -m ruff check .           # lint + import 排序
python scripts/lint_shell.py     # bash 脚本静态检查
python scripts/lint_tool_sources.py  # 工具下拉数据源登记检查
```

- `tests/conftest.py`：
  - `base_url`（session 级）：实例化 `SilentHTTPServer` 于随机端口、后台线程运行，
    **不**做 pvesh 门槛与后台循环。
  - `http`（session 级）：极薄 GET 助手 `(status, headers, body) = http("/api/health")`。
- 关键测试：接口冒烟、异常→状态码映射、纯逻辑单测（`paths` / `pve_api` 参数拼装 /
  **路由表完整性**）、静态资源接线、i18n key 一致性。

---

## 6. 部署

```bash
sudo ./install.sh [端口] [安装目录]   # 默认 8899 / /etc/pvewebtools
```

`install.sh` 同步程序目录后调用 `scripts/deploy_source.sh` 注册 systemd 服务
（`WorkingDirectory` = 安装目录，`ExecStart = python3 -m core.web.server <端口> [--tls]`）。

> **升级 = 覆盖程序目录 + `systemctl restart pvewebtools`**（没有产品内自助升级）。

---

## 7. 常见陷阱（改动后必查）

| 改动 | 必做 | 守护测试 |
|------|------|----------|
| 新增前端 `.js` | 在 `index.html` 补 `<script>` | `test_static_assets` |
| 新增 i18n 文案 | `messages.py` + `zh.js` 同步加 key | `test_messages` |
| 新增路由域 | 写 `HANDLERS` 并在 `routers/__init__._ROUTERS` 注册 | `test_routers` |
| 新增工具 | 分类文件 `TOOLS` 加条目 + `scripts/{id}.sh` | 工具加载 / `lint_tool_sources` |
| 新增下拉数据源 | 在 `toolbox.py` 分类表登记归属 | `lint_tool_sources` |
| 用户可见文案改中文 | 走 `t("key")`，勿硬编码 | `test_messages` |
| 改动 `boot.py` 启动流程 | 确认无 pvesh 时仍能给出友好退出 | 手动验 |

---

## 8. 其它

K8S 部署工具见 `core/tools/tools_vmops.py` 的 `vm_install_kubeadm`
（分类 `vmops`，脚本 `core/tools/scripts/vm_install_kubeadm.sh`），经 Guest Agent 在虚机内执行。
