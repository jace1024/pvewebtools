# pvewebtools · 一键安装 Kubernetes 模块方案

> 目标：在 pvewebtools 工具箱中新增「一键安装 k8s」能力，支持**单节点**（all-in-one 演示）与**多节点集群**（1 master + N worker）两种形态，使用 `kubeadm` 部署，联网拉取，默认 Calico + containerd。
>
> 本文件为**设计/工作清单**，不含实现代码。状态：方案已评审、待实现。

---

## 1. 背景与现状

pvewebtools 的工具系统是**数据驱动**的：

- 每个工具是 `core/tools/tools_*.py` 里 `TOOLS` 数组的一条记录；
- 实现是 `core/tools/scripts/*.sh`（经 `PT_*` 环境变量注入参数，不拼接命令，防注入）；
- 前端按 `category` 自动渲染，后端统一经 `POST /api/tools/<id>/run` 执行；
- 已有「一键优化」先例（`compose` 把多段子工具串成组合任务）。

**关键前提（已查证 `core/web/jobs.py`）：** 现有任务系统是「一个工具 = 一段脚本 = 一个 job = 派发到单个节点」。指定 `node` 时经 **PVE 集群免密 SSH**（`pve_api.ssh_host(node)` → `root@<集群节点IP>`）在目标节点执行；不指定则在运行 pvewebtools 的本机执行。同节点串行、跨节点并行。

> ⚠️ 该 SSH 通道**只适用于 PVE 集群物理节点**（依赖 `pvecm` 互信）。k8s 节点是 **PVE 下的 VM**，不能直接复用。

---

## 2. 已确认的设计决策

| 项 | 决策 |
|---|---|
| k8s 节点身份 | **PVE 下的 VM**（非集群物理节点）→ 走新增「目标主机 SSH」通道，不复用 `pve_api.ssh_host` |
| 部署方式 | `kubeadm` |
| 拉取方式 | 联网；apt 走阿里云 kubernetes 源，容器镜像走 `registry.aliyuncs.com/google_containers` |
| CNI | Calico（可选 Flannel） |
| 容器运行时 | containerd |
| 免密策略（T0.3） | **自动推送公钥 → 手动免密 → 兜底** 三层降级 |

**VM 清单与 IP 获取：** 复用既有 `pve_api.list_guests_with_ips()`（返回运行中的 VM 含 `ip` / `name` / `vmid` / `onboot`），编排层据此把选中 VM 映射为执行目标。

---

## 3. 总体架构

### 3.1 单节点 k8s（零框架改动）

完全契合现有「单工具 = 单节点 = 单 job」模型：

- 新增工具 `k8s_allinone`，`category=advanced`，`risk=high`；
- 选一个执行 VM（前端从 VM 列表选，经目标主机通道在该 VM 内执行）；
- 脚本内完成：系统前置 → 装 kubeadm/kubelet/kubectl → containerd → `kubeadm init` → 去污点 → 装 CNI → 写 `root/.kube/config`。

### 3.2 多节点集群（需新增编排层）

普通工具只能产 1 个 job。k8s 集群必须拆成**三个阶段、跨多 VM** 编排，因此新增一个独立的「集群安装编排」入口，而非普通工具。

| 阶段 | 目标 VM | 动作 | 依赖 |
|---|---|---|---|
| ① prepare | 所有选中 VM（并行） | 装 containerd + kubeadm 套件 + 系统调优（关 swap / 模块 / sysctl） | 无 |
| ② init | 仅 master | `kubeadm init` + 装 CNI + **生成 join 命令** | ① 完成 |
| ③ join | 各 worker（并行） | 用 ② 产出的 `kubeadm join ... --token ... --hash ...` 加入 | ② 完成 |

阶段 ②→③ 的 **join 命令是数据传递**（init 产出、join 消费）：在 master 上 init 后追加一步 `kubeadm token create --print-join-command --certificate-key …`，编排层解析该步输出得到 join 命令，再分发给各 worker。

每个子步骤仍是**标准 job**（独立日志、SSE 流、可 kill、受节点闸门约束），可观测性不丢失。

---

## 4. 工作清单

> 标号沿用 `T` 序号；每项含【产出 / 依赖 / 验收 / 风险】。

### M0 · VM 执行通道与免密（前置，阻塞多节点，最高风险）

- **T0.1 目标主机抽象**
  - `jobs.submit / _run_script / _kill / _cleanup` 增加 `target_host`（ip, port, user）参数；传入时 ssh 目标改为 `root@<vm_ip>`，复用现有 SSH 选项、`PT_JOB_ID` 注入、远端强杀、tmp 回收逻辑。
  - 产出：`core/web/jobs.py`。
  - 依赖：无。
  - 验收：对任意 VM IP 跑脚本，日志经 SSE 回流、可被 kill。
  - 风险：需保证现有「PVE 节点」分支与新增「VM 目标主机」分支互斥清晰。
- **T0.2 VM 清单映射**
  - 复用 `list_guests_with_ips()`，把选中的 VM 映射为 `(ip, name, vmid)` 传给编排层。
  - 产出：编排层调用点。
- **T0.3 免密配置（三层降级）**
  - **层 1 自动推送公钥（首选）：** 工具 `k8s_ssh_setup`（可独立跑，也作为集群安装预检）。确保运行机有 `/root/.ssh/id_rsa.pub`（无则 `ssh-keygen` 生成）→ `sshpass -p '<VM root 口令>' ssh-copy-id root@<vm_ip>` 推送 → 推送后 `ssh -o BatchMode=yes` 探测免密生效。运行机需装 `sshpass`（脚本内 `apt-get install -y sshpass`）。
  - **层 2 手动免密（自动失败引导）：** 自动推送失败时，向导显示运行机公钥全文，引导用户手动贴进各 VM 的 `/root/.ssh/authorized_keys`；提交/执行前统一做 `BatchMode` 连通性探测，全部通过才放行，不通过则停在预检页列出不通 VM + 建议。
  - **层 3 兜底（只报错引导，不自动做）：** 探测也失败且用户无法配免密时，提示退路 A（QEMU guest-agent 通道 `qm guest exec`，标注未实现需额外评估）与退路 B（先保证运行机↔VM 同网段、放行 22、手动免密后再来）。兜底**不静默绕过**。
  - ⚠️ **安全约束：** VM root 口令为 `password` 类型参数，**必须排除出 `_param_summary` 日志摘要**（否则明文落 job 日志）；口令只经 `PT_*` 环境变量注入，不写脚本、不回显。
  - 依赖：T0.1。
  - 验收：层 1 能推送并探测通过；层 1 失败时层 2 引导清晰；层 2/3 在不通时正确拦截而非静默继续。

### M1 · 单节点 k8s（可独立先行）

- **T1.1 工具注册：** `core/tools/tools_k8s.py` 加 `k8s_allinone`（`category=advanced`, `risk=high`），参数 `version`(text, 正则 `v\d+\.\d+\.\d+`)、`cni`(select: calico/flannel)、`runtime`(select: containerd)；挂入 pro 模块。
- **T1.2 脚本** `scripts/k8s_allinone.sh`：系统前置 → 装 kubeadm/kubelet/kubectl（apt 走阿里云 kubernetes 源）→ containerd 配置 → `kubeadm init --pod-network-cidr` → 去 master 污点 → 装 Calico/Flannel → 写 `root/.kube/config`。
- **T1.3 验证：** 单台 VM 跑通，`kubectl get nodes` 显示 Ready。
- 依赖：T0.1（VM 通道）。
- 验收：单节点集群就绪且可调度 Pod。

### M2 · 多节点编排后端（核心新增）

- **T2.1 路由：** 新增 `core/web/routers/k8s.py`，注册 `POST /api/k8s/cluster/install`，body `{master_vm, worker_vms[], version, cni, runtime, registry}`。在 `HANDLERS` 接入分发器。
- **T2.2 编排核心** `core/tools/k8s_orch.py`：
  - 阶段① prepare：对 `[master]+workers` 每台并行 submit `k8s_prepare` 子 job（`target_host=VM ip`），等全部 success；
  - 阶段② init：对 master submit `k8s_init`，init 后追加 `kubeadm token create --print-join-command`，解析输出得 join 命令；
  - 阶段③ join：对每台 worker submit `k8s_join`，注入 `PT_JOIN_CMD`；
  - 任一阶段失败 → 中止并汇总失败节点；返回各子 job id 供前端轮询。
- **T2.3 数据源登记：** 若新增 vm 类下拉，补进 `toolbox.py` 分类表，避免 `scripts/lint_tool_sources.py` 报 unknown source。
- 依赖：M0、T1.2（脚本逻辑复用）。
- 验收：1 master + 2 worker 全流程跑通，看门狗/失败汇总正确。

### M3 · 三阶段脚本（复用单节点逻辑）

- **T3.1 `k8s_prepare.sh`**（所有节点共用）：关 swap、加载 `br_netfilter`/ipvs 模块、sysctl、装 containerd（`SystemdCgroup=true` + 镜像加速）、装 kubeadm/kubelet/kubectl、`kubelet enable`。
- **T3.2 `k8s_init.sh`**（master）：`kubeadm init --apiserver-advertise-address=<vm_ip>`、可选去污点、装 CNI、生成并回传 join 命令。
- **T3.3 `k8s_join.sh`**（worker）：消费 `PT_JOIN_CMD` 执行 `kubeadm join`，确保 containerd/kubelet 已起。
- 依赖：T0.1。

### M4 · 前端集群向导

- **T4.1 数据源：** 新增 `/api/k8s/vms`（或复用 `/api/guests` 带 ip），前端用**多选**（master 单选 + workers 多选）。
- **T4.2 向导页：** 选 master/workers、版本、CNI、运行时、镜像加速器；展示每台机器三阶段进度（轮询各子 `/api/jobs/<id>`）；完成汇总。
- **T4.3 提交前连通性预检：** 探测运行机→各 VM 的 `BatchMode` 连通性（呼应 T0.3），失败给明确引导。
- 依赖：T2.1。
- 验收：向导选点、进度、预检提示均可用。

### M5 · 联网镜像加速

- **T5.1 apt 源：** 写 `kubernetes.list` 指向 `mirrors.aliyun.com/kubernetes` + 导入 GPG。
- **T5.2 容器镜像：** `kubeadm init --image-repository registry.aliyuncs.com/google_containers`；CNI 清单（Calico）镜像地址替换为国内可达源（脚本内 imageMapping 处理）。
- 依赖：无。

### M6 · 测试与验收

- **T6.1 单测：** jobs 的 host 通道、编排依赖传递（mock 节点）、join 命令解析正则。
- **T6.2 路由表测试** 覆盖 `/api/k8s/*`（沿用 `tests/` 既有路由表守护风格）。
- **T6.3 端到端（手动）：** ①单 VM 单节点；②1 master + 2 worker，`kubectl get nodes` 全 Ready，跨节点 Pod 调度正常。
- 依赖：全部。

---

## 5. 依赖与排期

```
T0.1 → T0.2 → T0.3(免密/连通性)
                 ├─▶ T1.1/T1.2/T1.3 (单节点, 可独立交付)
                 └─▶ T2.1 → T2.2 → T3.1/T3.2/T3.3 → T4.1/T4.2/T4.3 → T5 → T6
```

| 模块 | 估算 | 阻塞项 |
|---|---|---|
| M0 VM 通道 + 免密 | 1.5–2.5 人日 | **T0.3 层 1 网络可达性（最大不确定性）** |
| M1 单节点 | 0.5 人日 | T0.1 |
| M2 编排后端 | 1.5 人日 | M0 / T1.2 |
| M3 三阶段脚本 | 1 人日 | T0.1 |
| M4 前端向导 | 1.5 人日 | T2.1 |
| M5 镜像 | 0.5 人日 | 无 |
| M6 测试 | 1 人日 | 全部 |
| **合计** | **≈ 8–9 人日** | |

---

## 6. 风险与待办

1. **T0.3 网络可达性（最高）：** 若运行机无法触达 VM（网段隔离/22 未放行），SSH 通道整体失效，需退回 QEMU guest-agent（更大改动）。建议最先验证。
2. **口令日志安全：** `k8s_ssh_setup` 的 VM 口令参数必须排除出 job 日志摘要。
3. **VM 内关 swap / 改内核：** 在 VM 内做不影响 PVE 宿主，但需与 VM 内其他负载确认无冲突。
4. **CNI 镜像国内可达性：** Calico 镜像需可靠国内源，脚本内 imageMapping 兜底。

---

## 7. 不在本方案范围

- 高可用控制面（多 master / load-balancer）；
- 离线物料打包（本次明确联网拉取）；
- 集群生命周期管理（扩缩容 / 升级 / 卸载）——可作为后续模块。
