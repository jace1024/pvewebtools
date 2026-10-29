# PVE WebTools 开发文档（Developer Guide）

面向**二次开发 / 重构维护**的指南。面向最终用户的功能说明见仓库根 `README.md`；重构计划与里程碑见 `docs/REFACTOR_PLAN.md`。

> 本工具**零第三方依赖**，仅用 Python 标准库；运行环境为 PVE 宿主机自带的系统 Python（Debian 12/13）。开发机 likewise 只需标准库 + `pytest` / `ruff`（测试/CI 用）。

---

## 1. 双仓结构：`core/` 与 `console/`

| 目录 | 角色 | 是否进编译产物 | CI |
|------|------|----------------|-----|
| `core/` | **产品核心**：Web 服务、工具箱、授权、PVE 接口、前端静态资源 | 是（Cython + Nuitka） | `ci.yml`（pytest + ruff） |
| `console/` | **打包 / 授权管理台**：密钥管理、二进制编译、授权签发 Web 页面 | 否（独立部署在打包宿主机） | `console-ci.yml`（bash/python 语法） |

二者在仓库内平级共存，但**关注点解耦**：编译时 `console/` 把 `core/` 复制进 `work/` 工作区再注入 `build-scripts/`，源码目录保持纯净。`console/` 的授权/签名逻辑通过 `PYVE_SRC` 环境变量或默认同级 `../pvewebtools` 定位产品源码里的 `signlib.py` / `corelib.py`。

`console/` 自身文档：`console/README.md`（部署/使用）、`console/BUILD.md`（二进制构建）、`console/LICENSE.md`（一机一码授权）。

---

## 2. 后端架构（`core/` 分层）

```
HTTP 请求
  │
  ▼
core/web/server.py        # 服务器骨架：BaseHTTPRequestHandler 子类 + SilentHTTPServer
  │  do_GET/POST/DELETE/PUT
  ▼
core/web/routing.py        # RoutingMixin：路径切分 + 写闸口 + 统一异常映射入口
  │  _dispatch_guarded(method, parts)
  ▼
core/web/routers/          # 路由表分发（HANDLERS 协议）
  │  route(handler, method, parts) → 命中则回写响应
  ▼
core/web/routers/<domain>.py   # 各域实现（license/node/vm/storage/network/toolbox/...）
core/web/handlers.py           # 长尾 / 历史路由（仍在迁移收尾）
```

支撑模块（`core/web/`）：

- `http_util.py`：`_send_json` / `_error` / `_read_body` / `_json`、gzip/ETag/缓存头/Set-Cookie 通道
- `gate.py`：`_gate_write` 写操作授权闸口（授权熔断逻辑集中处）
- `static_srv.py`：首页版本注入、静态文件服务、gzip 缓存
- `sse.py`：任务日志流式推送 `_handle_job_stream`
- `errors.py`：**异常 → HTTP 状态码**唯一映射（`EXC_STATUS` / `guard_error`）；未预期异常回 500 中性文案，细节只进服务端日志
- `template_cache.py`：模板缓存目录管理
- `ops.py` / `maintenance.py`：节点/授权后台刷新循环、上传分片清理等
- `boot.py`：`main()` 进程启动入口（pvesh 门槛、后台线程、HTTP 服务拉起）

### 路由协议（`routers/`）

每个域模块暴露：

```python
HANDLERS = {
    "get":  {"license/status": _status},
    "post": {"license/token": _token_add},
}
```

新增一域 = (1) 写 `routers/<domain>.py` 暴露 `HANDLERS`；(2) 在 `routers/__init__.py` 的 `_ROUTERS` 元组追加一行。`dispatch.merge` 会合并并检查**重复登记**。处理函数签名统一为 `fn(handler, params)`，`handler` 提供 `_json` / `_error` / `_read_body` 等基础设施。

> `routers/__init__.py` **不参与 Cython 编译**（作数据文件分发），只放 import 合并这层薄胶水。

### 工具箱系统（`core/tools/`）

- `toolbox.py` 持有 `REGISTRY`（`{id: {category, risk, params, script, ...}}`）。
- 工具脚本**外置**：声明了 `script_file` 的工具，正文从 `core/tools/scripts/{id}.sh` 读取（源码模式读物理文件；Nuitka 数据文件经 `importlib.resources` 读取，见 `toolbox._resolve_script`）。
- **安全约束**：用户输入**不拼接进脚本**，只通过 `PT_*` 环境变量注入（服务端按 `params` 的 `type/pattern/options` 校验后再注入），避免命令注入。
- 写配置前先备份，块状配置用 `PVEWEBTOOLS BEGIN/END` 标记幂等管理。
- `risk`：`info`=只读、`low`=普通（一次确认）、`high`=高危（需输入确认词，服务端校验）。
- 公共 bash 常量集中在 `toolbox_common.py`（`COMMON_HELPERS` / `KARGS_PRESETS` / `MIRRORS` 等）与 `toolbox_pro.py`；`toolbox_dev` 复用 pro 骨架。

### PVE 接口（`core/pve/`）

`pve_api.py` 已按资源拆分到 `core/pve/api/`（`node` / `vm` / `storage` / `network` / `access` / `base` / `cluster` / `schedule` / `passthrough`）。所有 PVE 调用走 `pvesh` CLI 封装，参数拼装集中在 `pve_api`。

### 授权与安全模型（`core/lic/`）

- 一机一码：控制机部署时激活；其它机器在「授权」分区粘贴授权码并入授权。
- **整体熔断原则**：发现未授权机器（指纹不在 `node_tokens.json`）时，直接熔断/停用整个工具箱（`gate_toolbox` / `gate_write` 返回拒绝原因，所有工具拒绝执行），**不**逐节点放行、也**不** fail-open 静默放行。唯一已知残留是「指纹采不到」时的 fail-open（可用性取舍）。
- 签名：`signlib.py`（Ed25519 纯 Python 实现，零依赖）签发；`corelib.py` 验签（随编译产物 Cython 编译，源码不暴露）。

---

## 3. 前端（`core/static/`）

- 入口 `index.html`：内联 SVG 图标库 + 各视图骨架；**末尾按序加载 classic `<script>`**（**非** ES module——内联 `onclick` 依赖全局作用域）。
- 脚本已拆到 `js/`（`state.js` / `ui.js` / `api.js` / `overview.js` / `settings.js` / `toolbox.js` / `transfer.js` / `passthrough.js` / `logs.js`）+ 入口 `app.js`。
- **静态资源接线测试**（`tests/test_static_assets.py`）强制：`static/` 下每个 `.js` 都必须被 `index.html` 引用（漏加 `<script>` 标签会白屏且首页仍 200），首页引用的资源必须能取到。新增前端脚本**务必**在 `index.html` 补 `<script src="...">`。
- 版本注入：首页会注入静态资源版本号，避免浏览器缓存旧版 `app.js` 导致新功能"不生效"。

### i18n 模式（`t()`）

- 后端文案集中在 `core/web/messages.py`：`MESSAGES` 字典 + `t(key)`（缺失 key 回退为 key 本身，防漏翻崩溃）。
- 前端同字典在 `core/static/zh.js`（`window.MESSAGES` + `window.t`），先于各模块加载。
- **前后端 key 必须一致**：`tests/test_messages.py` 校验 `zh.js` 与 `messages.py` 的 key 集合完全一致，且所有 `t("key")` 调用点的 key 必须存在于 `MESSAGES`。
- 新增用户可见文案：先在 `messages.py` 加 key，再在 `zh.js` 同步；占位符（如 `%s`）保持原样，由调用方按既有方式格式化（`t("x") % (a, b)`）。

---

## 4. 本地运行

源码模式（开发机，不校验授权、不编译）：

```bash
cd <repo>                       # 仓库根目录（含 core/ 的那一层），不是 core/
python -m core.web.server 8899  # 等价于 pvewebtools [端口]，默认 8899；监听 0.0.0.0
# 也可按文件路径直接运行（server.py 顶部有 sys.path 自举，无需设 PYTHONPATH）：
python /abs/path/to/core/web/server.py 8899
```

- 入口：`core/web/server.py` 的 `__main__` 分支转发到 `core/web/boot.py: main()`（解析端口、pvesh 门槛、后台线程、HTTP 服务）。
- 开关：`[端口]` 为可选整型位置参数；`--hostid` 仅打印本机硬件指纹后退出（用于签发许可证，无需 pvesh）。
- 部署提示：若从旧版本拷贝/部署，需确保 `core/web/server.py` 含顶部 `sys.path` 自举段（已在 master 的 `a236ddd` 提交）——否则直接脚本形式会因 `import core` 失败；用 `git pull` 拉到最新即可。

> 非 PVE 环境仍会因 pvesh 门槛直接退出（`boot.py` 检查）；纯接口/逻辑测试用 pytest 的 `http` 夹具（见下），**不**调用 `server.main()`。

---

## 5. 测试与质量

```bash
python -m pytest            # 全部用例（core/ 行为 + 路由表完整性 + 构建清单 + 静态资源接线 + i18n）
python -m ruff check core tests               # lint + import 排序
```

- `tests/conftest.py`：
  - `base_url`（session 级）：实例化 `SilentHTTPServer` 于随机端口、后台线程运行，**不**做 pvesh 门槛与后台循环，适合测试。
  - `http`（session 级）：极薄 GET 助手 `(status, headers, body) = http("/api/health")`，`HTTPError` 也返回不抛出。
- 关键测试：接口冒烟（只读 `GET /api/*`）、授权闸口（未授权 → 熔断）、纯逻辑单测（`licensing` 熔断判定 / `paths` / `pve_api` 参数拼装 / **路由表完整性**）、构建清单一致性、静态资源接线、i18n key 一致性。
- **不要**手动跑 Nuitka 编译冒烟（`T1.6` 仍在计划，属可选）。

---

## 6. 编译构建（`console/`）

编译由 `console/build-scripts/` 的 `build.sh`（Cython + Nuitka）与 `setup.py`（Cython 配置）驱动，产物经 `deploy_binary.sh` 部署为 systemd 服务，客户用 `install.sh` 一键安装。

**新增 `core/**/*.py` 必须登记编译清单**（否则 Cython 不编译 → 产物 `ModuleNotFoundError`）：

- `console/build-scripts/setup.py` 的 `EXT_MODULES`
- `console/build-scripts/build.sh` 的 `BASE_MODULES`

`tests/test_build_manifest.py` 强制两者一致且覆盖磁盘上所有 `core/**/*.py`（`EXCLUDED`：`core/entry/main.py`、`core/web/collect.py`、`core/tools/toolbox_dev.py`；`PRO_ONLY`：`core/tools/toolbox_pro.py`）。

---

## 7. 常见陷阱（改动后必查）

| 改动 | 必做 | 守护测试 |
|------|------|----------|
| 新增 `core/**/*.py` | 登记 `setup.py` + `build.sh` 清单 | `test_build_manifest` |
| 新增前端 `.js` | 在 `index.html` 补 `<script>` | `test_static_assets` |
| 新增 i18n 文案 | `messages.py` + `zh.js` 同步加 key | `test_messages` |
| 新增路由域 | 写 `HANDLERS` 并在 `routers/__init__._ROUTERS` 注册 | `test_*` 路由表完整性 / 手动验 |
| 新增工具 | `REGISTRY` 加条目 + `scripts/{id}.sh` +（如需）`_resolve_script` 依赖 | 工具加载单测 |
| 用户可见文案改中文 | 走 `t("key")`，勿硬编码 | `test_messages` |

---

## 8. 重构里程碑索引

详见 `docs/REFACTOR_PLAN.md`：

- **M1** 测试骨架 + pytest/ruff/CI（✅）
- **M2** Web 分层重构：http_util / gate / static_srv / sse / 路由表分发 / 错误处理集中（✅）
- **M3** 前端拆分（state/ui/api/各页面模块 + 入口）（✅）
- **M4** 工具注册表统一、bash 脚本外置、pve_api 拆分（✅）
- **T4.4** 中文文案抽离（基础设施 + 授权域试点；全量抽取分阶段）（✅ 基础）
- **T4.6** `console/` 关注点分离（文档区隔 + 单独 CI；拆独立仓待定）（✅）
- **M5** 规范化收尾：死代码清理（✅）、类型注解 / 样式收敛 / 开发文档（本文件即 M5.4）
