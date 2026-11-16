# 贡献指南（CONTRIBUTING）

感谢你考虑为 **PVE WebTools（开源版）** 做贡献！这是一个用于 Proxmox VE（PVE）运维的轻量 Web 工具箱，
代码以 Python 标准库 + 原生 JS 实现，无第三方运行时依赖。

## 开发环境

```bash
# 需要 Python 3.9+，仓库本身零依赖，无需 pip install
pip install -r requirements-dev.txt   # 仅开发期：pytest / ruff
```

本地启动（需在一台 PVE 宿主机或能访问 PVE 的环境；没有 `pvesh` 会拒绝启动）：

```bash
python -m core.web.server              # 默认 8899 端口；--no-auth 可关闭登录门禁
```

## 代码风格

- Python 全部通过 `ruff` 检查：`ruff check .`（配置见 `ruff.toml`）。
- bash 脚本：`python scripts/lint_shell.py`。
- 前端为原生 JS（非 ES module），`index.html` 的内联 `onclick` 依赖全局函数名，
  新增交互请用 classic script 并保持函数挂在全局作用域。
- 提交前请跑测试：`pytest`。

## 新增一个工具

1. 在对应分类文件（`core/tools/tools_<分类>.py`）的 `TOOLS` 里追加一条声明
   （`id / category / name / desc / risk / params / script_file`）。
2. 在 `core/tools/scripts/<id>.sh` 写 bash 实现；用户参数只通过 `PT_` 环境变量读取，
   **不要**把用户输入拼进脚本文本。
3. 若参数用了新的 `options_from` 动态数据源，必须在 `core/tools/toolbox.py` 的
   `NODE_SCOPED_SOURCES` / `GUEST_OWNED_SOURCES` / `GUEST_NODE_SOURCES` /
   `NODE_AGNOSTIC_SOURCES` 里登记归属，否则 `scripts/lint_tool_sources.py` 会报错，
   前端也会退化成"不联动"。
4. 写配置前请用 `toolbox_common.py` 里的备份辅助做写前备份（`/etc/pvewebtools/backup`）。

## 提交流程

1. Fork 本仓库并创建特性分支。
2. 保持改动聚焦；一次 PR 只做一件事。
3. 确保 `ruff check .` 与 `pytest` 通过。
4. 发起 Pull Request，描述改动动机与测试情况（涉及真机验证的请注明环境：PVE 版本 / 架构）。

## 贡献许可

本项目以 [GNU GPL v3.0](./LICENSE) 发布（另提供商业许可，见
[LICENSE-COMMERCIAL.md](./LICENSE-COMMERCIAL.md)）。提交贡献即表示你同意：

1. 你拥有所提交内容的版权，或已取得版权人的授权；
2. 你的贡献以 GPL-3.0 授权给本项目及其使用者；
3. 你同意维护者可在未来将本项目（含你的贡献）以其它条款再许可，包括商业许可
   —— 这一条用于保留双许可的灵活性，请确认后再提交。

提交时请在 commit message 末尾带一行 `Signed-off-by: 名字 <邮箱>`（`git commit -s` 会自动添加），
表示你同意 [Developer Certificate of Origin](https://developercertificate.org/)。

## 文案

后端用户可见文案集中在 `core/web/messages.py`，前端对应 `core/static/zh.js`，
两者 key 必须一致（由 `tests/test_messages.py` 校验）。新增文案请走字典而非硬编码。
