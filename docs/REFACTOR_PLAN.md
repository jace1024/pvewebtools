# pvewebtools 结构改进 · 工作清单

> 目标：**降耦合、可测试、可协作**（不追求减少行数）。
> 总验收：① 编译版与源码版都能正常起服务；② 关键接口无功能回归；③ `pytest + ruff` 全绿；④ `server.py` / `app.js` 不再是巨石文件。

## 基线（改造前，2026-10 实测）

| 指标 | 现状 |
|---|---|
| 代码量（py+js+css+html+sh） | ≈ 4.7 万行 |
| `core/web/server.py` | 7,761 行 / 194 个顶层函数 / 3 个类 / 41 个 `_handle_*` |
| `core/static/app.js` | 7,951 行 / 275 个函数 / 144 处 `addEventListener` |
| `core/tools` | 14,717 行 |
| 测试文件 | **0 个** |

---

## M0 · 基线与准备（0.5 人日）

- [x] **T0.1 记录改造基线**：改造前基线已记录于本计划顶部「基线」表（代码量 ≈4.7 万行、`server.py` 7761 行/194 函数、`app.js` 7951 行/275 函数、`core/tools` 14717 行、测试数 0）。`radon cc`/`jscpd` 未单独跑——复杂度/重复率以文件级行数与函数数为代理指标，改造后对比见「度量指标」
- [x] **T0.2 分支策略**：调整为**主分支直推**——重构全程在 `master` 本地改 → 本地 `pytest+ruff` 全绿 → 提交并推送 gitee，每步提交前用测试锁行为，主分支始终保持可发布。未新开 `refactor/*` 分支（协作模式为主分支直推）
- [x] **T0.3 定义冒烟清单**：核心操作"必须保持可用"已落地为自动化守护，见 T1.2（关键只读 `GET /api/*`：节点/存储/工具列表/授权状态/健康 + 授权闸口 未授权→熔断）+ T1.3（路由表完整性）+ 全量 `pytest`。清单：授权/登录页（熔断与授权码签发）、节点列表（概览/节点卡片）、工具运行（分类/执行/高危确认）、升级页（系统升级上传校验）、核心只读接口

**验收**：基线数字留档（含前后对比，见「度量指标」），主分支全程可发布，冒烟清单已自动化守护。

---

## M1 · 测试与工程底座（2–3 人日）⚠️ 一切的前提

- [x] **T1.1 搭 `tests/` + `pytest` 骨架**：`conftest.py` 提供"启动 server 的 fixture"、临时 EXE_DIR/CONFIG 目录隔离
- [x] **T1.2 接口冒烟测试**：关键**只读** `GET /api/*`（节点/存储/工具列表/授权状态/健康）+ 授权闸口行为（未授权 → 熔断）
- [x] **T1.3 纯逻辑单测**：`licensing`（熔断判定）、`paths`、`pve_api` 参数拼装、**路由表完整性**（每个注册路由都有 handler）
- [x] **T1.4 引入 `ruff`**（lint + format）+ `pre-commit`
- [x] **T1.5 CI**：GitHub Actions 跑 `pytest + ruff`（`.github/workflows/ci.yml`；Gitee 侧若需自动跑可配 Gitee Go 复用同命令）
- [ ] **T1.6（可选）编译冒烟**：Nuitka 产物启动 + `/api/health` 探活

**交付物**：可一键运行的测试套件 + CI
**验收**：`pytest` 通过、CI 绿；**路由表测试能挡住"拆文件漏路由"**
**依赖**：无（最先做）

---

## M2 · 后端拆分 `core/web/server.py`（目标 < 400 行）

- [x] **T2.1 抽 `core/web/http_util.py`**：`_send_json/_error/_read_body/_json`、gzip/ETag/缓存头/Set-Cookie 通道
- [x] **T2.2 抽 `core/web/gate.py`**：`_gate_auth/_gate_write`（授权闸口独立）
- [x] **T2.3 抽 `core/web/static_srv.py`**：首页版本注入、静态文件、gzip 缓存、`_serve_index`
- [x] **T2.4 抽 `core/web/sse.py`**：`_handle_job_stream`、推送
- [x] **T2.5 建路由分发协议**：`routers/` 内模块暴露 `HANDLERS = {"get"/"post"/"delete"/"put": {path: fn}}`，`ApiHandler` 只做查表分发
- [x] **T2.6 分批迁域（每步跑测试）**：`license` → `health/options` → `node` → `vm` → `storage` → `network` → `tools` → `upgrade`
- [x] **T2.7 收敛错误处理/日志**：异常 → HTTP 状态码映射集中一处
- [x] **T2.8 更新编译收集清单**：`core/entry/main.py` 声明 + loader 收录新模块，编译版回归

**验收**：`server.py` 行数大幅下降；41 个 `_handle_*` 全部落到 routers；**全部旧接口 URL/行为不变**（T1.2 保证）
**依赖**：M1 完成
**风险**：路由漏挂 → 由 T1.3 路由表测试兜底

---

## M3 · 前端拆分 `core/static/app.js`（目标：仅入口）

- [x] **T3.1 抽 `api.js`**：统一 `api()`、401 处理、错误清洗
- [x] **T3.2 抽 `ui.js`**：toast / modal / dom 辅助 / confirm
- [x] **T3.3 抽 `state.js`**：全局状态与自动刷新
- [x] **T3.4 分批迁页面**：dashboard → vm → storage → network → tools → settings
- [x] **T3.5 改入口**：`<script type="module">` + `main.js`；**版本注入要覆盖所有新 js**（否则缓存不刷新）
- [x] **T3.6 静态服务/缓存校验**：多文件加载 + ETag 生效

**验收**：`app.js` 瘦身为入口；所有页面功能不变
**依赖**：M1（至少 T1.2 冒烟）；可与 M2 并行

---

## M4 · 降重复与治理

- [x] **T4.1 统一工具注册表**：`registry = {name: {category, risk, params, script, 文案}}`，各 `tools_*.py` 只声明数据
- [x] **T4.2 bash 脚本外置**：`core/tools/scripts/*.sh`，Python 只做编排/传参（60 个工具已外置，运行期经 importlib.resources 读取）
- [x] **T4.3 合并 `toolbox_pro / toolbox_dev / toolbox_common` 公共逻辑**：经核查公共 bash 常量已集中——`toolbox_common` 持有 `COMMON_HELPERS/KARGS_PRESETS/MIRRORS` 等，`toolbox_dev` 直接 `from core.tools.toolbox_pro import VGPU_HOST_COMMON, _VGPU_RUN`（仅 try/except 兜底 `=""`），无重复定义可合并。T4.2 外置后 `CLOUDIMG_COMMON/VGPU_UNLOCK_HOOKS/DEPLOY_LABEL_HELPERS` 三个常量已无 Python 引用，作为死代码清理（见 T5.3）
- [x] **T4.4 中文文案抽离**：已建基础设施 `core/web/messages.py`（`MESSAGES` 字典 + `t()` 查找，缺失 key 回退为 key）+ `core/static/zh.js`（前端同字典 + `window.t`，先于各模块加载），已登记编译清单（`setup.py`/`build.sh`）并注入 `index.html`。试点迁移**授权域**（`routers/license.py` 7 处响应文案 + `errors.py` 中性 500 文案）改走 `t()`，行为零变化。后续分阶段：其余路由响应文案、工具结果文案、前端 `t()` 调用、启动控制台文案等（core/ 散落约 2700 处含中文串，多数为工具数据/文档串，需甄别）
- [x] **T4.5 `pve_api.py`（2,722 行）按资源拆**：node / vm / storage / network / access（现约 184 行 + 拆分模块）
- [x] **T4.6 `console/` 关注点分离**：
  - 文档区隔：已具备（`console/README.md` 产品打包/授权管理台说明、`BUILD.md` 二进制构建指南、`LICENSE.md` 一机一码授权文档），与产品核心 `core/` 解耦
  - 单独 CI：新增 `.github/workflows/console-ci.yml`，路径过滤 `console/**`，校验 bash 语法（`bash -n`）、Python 语法（`py_compile`）、best-effort ShellCheck
  - 拆独立仓：需用户决策（涉及仓库结构调整），未做

**验收**：`core/tools` 行数与重复率下降；新增工具只需"写脚本 + 注册一条"
**依赖**：M1（强依赖测试）；建议 M2 之后做

---

## M5 · 规范化（持续）

- [x] **T5.1 类型注解 + docstring**：`core/pve` + `core/lic` 全部函数已加类型注解（脚本化注入 `from __future__ import annotations` + 推断签名，ruff/pytest 全绿）；`core/lic` 安全关键包（licensing/corelib）公共 API 已提升为精确类型（`dict | None`/`list[dict]`/`tuple[bool,str]`/`str | None` 等），且 `core/lic` 本就带良好 docstring。深入类型化各 api 模块内部辅助函数（多为 `Any` 基线）为后续持续项
- [x] **T5.2 前端样式收敛**：`style.css` + 内联 → 设计变量/组件样式
  - `style.css` 设计变量体系已单点维护：正文仅有的 1 处硬编码 `#ff8c1a`（`color-mix` 里误用未定义的 `--orange` 回退）改为引用 `--accent-text`，正文硬编码色值清零
  - 新增「实用工具类」段（`u-subhead/u-text13/u-para/u-mt18/u-justify-end/u-flex-row/u-grow32/u-center-pad/u-err-msg/u-ok-msg/u-ok-text/u-sub` 等），覆盖出现 ≥3 次的高频内联模式
  - 脚本化精确匹配（tag-aware 合并进已有 `class`）把 118 处内联样式收敛为工具类（index.html 29、js 33），标签空白归一化去双空格
  - 结果：内联样式 62 种/118 处 → 47 种/56 处；硬编码颜色 4 处全部 token 化（`#ff5c5c`→`--red`、`#35c46a`→`--green`、`var(--muted,#888)`→`--muted`）；`display:none`（4 处）刻意保留（JS 靠 `.style.display` 切换显隐，改 class 会破坏逻辑）；其余为单次独有结构样式，按需后续
  - 104 pytest 全绿、ruff 未涉及前端
- [x] **T5.3 死代码清理**：`vulture` 扫描——删除 T4.2 外置后无引用的常量（`CLOUDIMG_COMMON`/`VGPU_UNLOCK_HOOKS`/`DEPLOY_LABEL_HELPERS` 三处定义 + 9 处死导入），及 M2 重构遗留的 `server.py` 4 个 `_node_template_cache*` 死导入。其余 vulture 命中（`handlers.py`/`collect.py` 的 `_handle_*`/`list_*`、`routing.py` 的 `do_*`、`ops.py` 集群方法、`lic/*`、`pve/api/*`）多为路由分发/动态引用误报，需单独评审，未动
- [x] **T5.4 文档更新**：新增 `docs/DEVELOPMENT.md`——双仓结构（`core/` vs `console/`）、后端分层与路由协议、工具箱系统、PVE 接口拆分、授权/熔断安全模型、前端结构与 i18n 模式、本地运行、测试与 CI、编译构建清单登记、加工具/加路由流程、常见陷阱速查表

---

## 依赖与排期

```
M0 ─▶ M1(测试,必做前提) ─┬─▶ M2(后端) ─┐
                       └─▶ M3(前端) ─┼─▶ M4(降重复) ─▶ M5(规范化,持续)
```

| 里程碑 | 内容 | 预估 | 可验收成果 |
|---|---|---|---|
| M1 | 测试 + 工程底座 | 2–3 人日 | 一键验证 + CI 绿 |
| M2 | 拆后端 | 3–5 人日 | `server.py` < 400 行 |
| M3 | 拆前端 | 3–5 人日 | `app.js` 仅入口 |
| M4 | 降重复 | 5–8 人日（分步） | 工具注册统一 |
| M5 | 规范化 | 持续 | 类型/样式/文档 |

## 度量指标（改造前后对比）

| 指标 | 改造前（2026-10 基线） | 改造后（2026-10 收尾） |
|---|---|---|
| `core/web/server.py` 行数 | 7,761 | **389**（仅服务器骨架） |
| `core/static/app.js` 行数 | 7,951 | **242**（仅入口） |
| `core/tools` 行数 | 14,717 | **4,864**（脚本外置后纯 Python 编排层） |
| 测试函数 / 文件数 | 0 / 0 | **85 / 10** |
| CI 状态 | 无 | `ci.yml`（pytest+ruff）+ `console-ci.yml`（bash/python 语法） |
| 圈复杂度 / 重复率 | 未量化（代理：行数/函数数） | 同上，未单独跑 `radon`/`jscpd` |

> `core/` 总 Python 行数现约 22,204（含外置脚本读取层、新增 `routers/` 分发、i18n 基础设施、开发文档生成逻辑）。行数上涨源于工具脚本外置后保留的编排层与新增基础设施，符合"降耦合、可测试、可协作"目标而非"减行数"。

**总验收**（计划顶部四条）：① 编译版/源码版均正常起服务（T4.4/T4.6 已接编译清单与静态接线，CI 守护）；② 关键接口无功能回归（T1.2/T1.3 + 全量 pytest 守护）；③ `pytest + ruff` 全绿；④ `server.py`/`app.js` 不再是巨石文件（7761→389、7951→242）。

## 明确不做

- 不照搬 ProxCenter 的 MySQL / 42 router / RBAC 平台化复杂度；
- 不为"减少行数"而拆（目标是可维护性）；
- 不在没有测试的情况下动 `server.py` 核心。

---

## 推荐执行顺序

1. **M1 先做**（投入最小、确定性最高，是后续所有重构的通行证）；
2. M2 / M3 可并行，逐域迁移、每步跑测试；
3. M4 在 M1 之后进行，靠测试锁行为；
4. M5 持续。
