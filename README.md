# pvewebtools-lite

轻量的 **Proxmox VE（PVE）运维 Web 工具箱** —— 开源基础版。

把日常在 PVE 宿主机上要敲的一堆命令，收敛成一个网页：改内核启动参数、直通硬件、
建快照、迁移、批量改源、诊断救砖……都在浏览器里点几下完成，并且**每个写配置的操作都会先自动备份、可一键回滚**。

> 这是开源的 `lite` 版本，包含基础运维能力，**不含任何授权 / 商业化逻辑**，采用 GPL-3.0 协议发布。

## 特性

- **零依赖**：后端仅用 Python 3.9+ 标准库（`http.server` + 自研路由），前端是原生 HTML/CSS/JS，**无需 pip 安装任何运行时包、无需构建**。
- **基础 7 大类工具**：日常优化 / 启动与内核 / 硬件直通 / 虚机容器运维 / 网络 / 存储磁盘 / 诊断救砖。
- **登录门禁**：默认开启（引用 PVE 账号 + HMAC 会话 Cookie + CSRF 防护）；内网可信场景可用 `--no-auth` 关闭。
- **安全兜底**：所有写配置操作前自动备份，工具内提供「从备份恢复」。
- **开源免费**：以 GPL-3.0 发布，源码可自由查看、修改、再分发。

## 技术栈

| 层 | 实现 |
|---|---|
| 后端 | Python 标准库（`http.server` + 自研路由/ApiHandler），无框架、无第三方运行时依赖 |
| 前端 | 原生 HTML/CSS/JS，classic script，无打包构建 |
| 脚本 | bash（工具逻辑下沉到 `core/tools/scripts/`，跨发行版、可独立复用） |

## 快速开始

在 **PVE 宿主机**（或能访问 PVE 的环境）上：

```bash
# 运行时零依赖；仅开发期需要 pytest / ruff
pip install -r requirements-dev.txt

# 启动（默认监听 8006 端口，开启登录门禁）
python -m core.entry.loader
```

浏览器打开 `http://<宿主机IP>:8006` 即可。

- 默认需要登录（使用 PVE 的 root 或具权限的账号，HMAC 会话）。
- 内网可信场景如需关闭登录：`python -m core.entry.loader --no-auth`。

## 功能分类

| 分类 | 说明 |
|---|---|
| 日常优化 | 更新系统、去订阅弹窗、SSH 加速、换源、磁盘清理、降频解除、时间同步、重置密码、PVE 8→9 升级、根分区扩容 |
| 启动与内核 | 启动项/内核/GRUB/EFI 引导管理与诊断 |
| 硬件直通 | IOMMU/直通畅览、GPU/PCIe/NIC/磁盘 直通管理与恢复 |
| 虚机容器运维 | 虚拟机部署、云镜像建机、LXC 创建、快照、定时任务、备份、克隆、删除、内核参数、迁移、Docker 安装 |
| 网络 | 网络诊断、信息、Ping、网卡/桥接管理 |
| 存储磁盘 | 存储状态、磁盘预处理、格式转换、导出、导入、磁盘挂载、虚拟机磁盘 |
| 诊断救砖 | 服务诊断、日志检查、备份恢复 |

## 目录结构（节选）

```
core/
  entry/        启动入口（loader）
  web/          HTTP 服务、路由、各功能路由域（auth/system/cluster/...）
  pve/         PVE 平台交互（pvesh 封装 / 存储供给 / 系统升级）
  tools/       工具箱（注册表 + 各分类工具集 + scripts/ 下的 bash 实现）
  static/      前端（index.html + js/ + lang/）
tests/         pytest 用例（依赖 console/ 编译工具链的用例会自动跳过）
```

> 本仓库**不包含**原项目中的 `core/lic/`（授权模块）、`core/tools/toolbox_pro.py`、`core/tools/toolbox_dev.py`（高级玩法 / 开发版）以及 `console/`（管理台与商业编译工程）。

## 安全说明

- 登录门禁默认开启；`--no-auth` 仅用于可信内网。
- 本版本**不含机器指纹采集、一机一码、授权熔断**等任何商业化逻辑。
- 工具直接操作生产 PVE，请充分理解每个工具的影响。漏洞请按 `SECURITY.md` 私下报告。

## 许可证

[GNU GPL v3.0](./LICENSE)。你可自由使用、修改、再分发；衍生作品同样须以 GPL-3.0 开源。

## 贡献

欢迎 PR。请先阅读 [CONTRIBUTING.md](./CONTRIBUTING.md)：提交前跑 `ruff check` 与 `pytest`，新增可见文案走 `lang/` 字典而非硬编码。
