# 贡献指南（CONTRIBUTING）

感谢你考虑为 **pvewebtools-lite** 做贡献！这是一个用于 Proxmox VE（PVE）运维的轻量 Web 工具箱，代码以 Python 标准库 + 原生 JS 实现，无第三方运行时依赖。

## 开发环境

```bash
# 需要 Python 3.9+，仓库本身零依赖，无需 pip install
pip install -r requirements-dev.txt   # 仅开发期：pytest / ruff
```

本地启动（需在一台 PVE 宿主机或能访问 PVE 的环境）：

```bash
python -m core.entry.loader            # 默认 8006 端口，--no-auth 可关闭登录门禁
```

## 代码风格

- Python 全部通过 `ruff` 检查（`ruff check core tests`）。
- 前端为原生 JS（非 ES module），按 `index.html` 内联 `onclick` 依赖全局函数名，新增交互请用 classic script 并保持全局函数。
- 提交前请跑测试：`pytest`（部分用例依赖 `console/` 编译工具链，不在仓库时自动跳过）。

## 提交流程

1. Fork 本仓库并创建特性分支。
2. 保持改动聚焦；若涉及新工具，请在 `core/tools/` 的对应分类注册表登记并在 `scripts/` 提供 bash 实现。
3. 确保 `ruff` 与 `pytest` 通过。
4. 发起 Pull Request，描述改动动机与测试情况。

## 国际化

前端文案走 `static/lang/zh.js`（默认中文）；新增可见文案请走字典而非硬编码，避免破坏 i18n key 一致性测试。
