from __future__ import annotations
from typing import Any
"""PVE 调用基础设施：pvesh 执行与错误规整、参数校验、节点连通性（ssh / 可达性标记）。

本模块是 core/pve/api 包的最底层：其余资源模块只依赖它，它不依赖任何同级模块。"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from pathlib import Path
GUEST_TYPES = {"qemu": "qemu", "lxc": "lxc"}
NODE_IP_CACHE_TTL = 30
PVESH_TIMEOUT = 30
UNREACHABLE_TTL = 60
_node_ip_cache = {"ts": 0.0, "data": {}}
_original_subprocess_run = subprocess.run
_unreachable_until = {}          # 节点名 -> 解除时间戳


def _locale_env() -> Any:
    """返回强制 C locale 的环境副本（不污染进程全局环境）。"""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["LANGUAGE"] = "C"
    return env


def _subprocess_run_with_env(*args, **kwargs) -> Any:
    kwargs.setdefault("env", _locale_env())
    return _original_subprocess_run(*args, **kwargs)


class PveError(Exception):
    """pvesh 调用失败时抛出，message 会原样返回给前端"""


def pvesh_available() -> Any:
    return shutil.which("pvesh") is not None


def _run_pvesh(method: Any, path: str, extra_args: Any = None, quiet: bool = False) -> Any:
    """执行 pvesh 并解析 JSON 输出。列表形式传参，天然免疫命令注入。
    quiet=True 时失败不留痕，用于「预期可能失败」的探测（如虚机未装 QEMU 代理时的
    agent/network-get-interfaces），避免正常场景反复刷 [pvesh 失败]。"""
    cmd = ["pvesh", method, path, "--output-format", "json"]
    if extra_args:
        cmd += extra_args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PVESH_TIMEOUT
        )
    except FileNotFoundError:
        raise PveError("未找到 pvesh 命令，请确认运行在 PVE 主机上")
    except subprocess.TimeoutExpired:
        raise PveError("PVE 接口调用超时，请稍后重试或检查节点状态。")

    if proc.returncode != 0:
        # Ctrl+C 停止服务时，SIGINT 会一并打断后台采集线程里正在跑的 pvesh 子进程：
        # 此时子进程是被信号杀死的（POSIX 下 returncode 为负信号值，或 128+N 的退出码惯例），
        # 属"被打断"而非业务失败，不作故障留痕，避免退出时误刷 "[pvesh 失败]"。
        if proc.returncode < 0 or proc.returncode == 128 + 2:
            raise PveError("操作被中断，请重试。")
        detail = proc.stderr.strip()
        # 仅服务端留痕完整命令与输出以便排查，绝不透传给前端用户
        if not quiet:
            print(f"[pvesh 失败] {' '.join(cmd)}\n{detail}", file=sys.stderr)
        raise PveError(_friendly_pvesh_error(detail, proc.returncode))
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        # 部分 create 类接口返回任务 UPID 纯文本
        return proc.stdout.strip()


def _strip_command(text: str) -> Any:
    """去掉文本中残留的命令行 / 路径片段，仅保留可读的提示信息（面向用户的最后一道过滤）。"""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("pvesh") or "pvesh " in low or low.startswith("[命令]") \
           or low.startswith("command") or "--output-format" in low or "get /nodes/" in low:
            continue
        s = re.sub(r"/nodes/[\w./-]+", "", s)
        s = re.sub(r"\bpvesh\b", "", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if s:
            out.append(s)
    return "\n".join(out) if out else "操作失败，请稍后重试或检查 PVE 节点状态。"


def _friendly_pvesh_error(detail: Any, returncode: int = 0) -> Any:
    """把 pvesh 的英文 stderr 转成面向用户的中文友好提示，绝不暴露命令行 / 代码。"""
    d = (detail or "").lower()
    if "disabled" in d:
        return "该存储未启用（已在 PVE 中禁用）。请先到「工具箱 → 存储与磁盘 → 存储管理」将其启用，再执行此操作。"
    if "not exist" in d or "no such" in d or "does not exist" in d:
        return "请求的资源不存在，可能已被删除或名称有误，请刷新后重试。"
    if "permission" in d or "denied" in d or "forbidden" in d or "eperm" in d:
        return "权限不足，请确认当前登录账户对该资源拥有操作权限。"
    if "already exist" in d or "already in use" in d:
        return "该名称 / 资源已存在，请更换名称或释放已占用的资源后再试。"
    if "timeout" in d or "timed out" in d:
        return "PVE 节点响应超时，请稍后重试或检查节点状态。"
    if not detail:
        return f"操作未成功完成（错误码 {returncode}），请稍后重试。"
    # 兜底：保留 PVE 的明确说明（非命令 / 代码），再剥掉可能残留的命令行痕迹
    return _strip_command(detail)


def _validate(node: int, gtype: Any, vmid: int) -> None:
    """路径参数白名单校验，不通过直接抛错（早返回，避免层层嵌套）"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    if gtype not in GUEST_TYPES:
        raise PveError(f"非法虚机类型: {gtype}")
    if not str(vmid).isdigit():
        raise PveError(f"非法 VMID: {vmid}")


def _conf_dir_signature() -> Any:
    """本地 pmxcfs 配置目录的变更签名：(目录 mtime, 文件数, 最新文件 mtime)。

    onboot/passthrough 标记均读自 /etc/pve/qemu-server|/etc/pve/lxc 下的 conf 文件；
    目录 mtime 变化 = 增删文件，最新文件 mtime 变化 = 修改现有 conf。二者结合即可
    可靠感知配置变更，避免每次 /guests 全量重读几百个文件。"""
    sig = []
    for subdir in ("qemu-server", "lxc"):
        d = Path("/etc/pve") / subdir
        try:
            entries = list(d.glob("*.conf"))
            dmt = d.stat().st_mtime
            fmt = max((c.stat().st_mtime for c in entries), default=0.0)
            sig.append((dmt, len(entries), fmt))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _corosync_node_ips() -> Any:
    """从 /etc/pve/corosync.conf 解析各节点 ring0_addr -> IP，作为 /cluster/status 取不到
    节点 IP 时的兜底（集群节点 IP 的权威来源，不依赖 pvesh）。

    解析逻辑：corosync 每个节点块形如
        name: pve02
            ring0_addr: 192.168.0.113
    `ring0_addr` 跟随其所属节点的 `name`。非集群 / 文件缺失 / 解析失败返回 {}。"""
    result = {}
    path = "/etc/pve/corosync.conf"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            cur = None
            for line in fh:
                s = line.strip()
                if s.startswith("name:"):
                    cur = s.split(":", 1)[1].strip()
                elif s.startswith("ring0_addr") and cur:
                    ip = s.split(":", 1)[1].strip()
                    if cur and ip:
                        result[cur] = ip
    except OSError:
        return {}
    return result


def cluster_node_ips() -> Any:
    """集群各节点的 IP：{节点名: ip}。

    优先取自 /cluster/status（pvesh）；该来源偶发取不到 IP（例如服务进程内 pvesh 早期
    调用竞态）时，用 corosync.conf 的 ring0_addr 兜底，确保节点名总能解析成可达 IP。
    两者都取不到时返回 {}（调用方据此回退节点名）。"""
    now = time.time()
    if now - _node_ip_cache["ts"] < NODE_IP_CACHE_TTL:
        return dict(_node_ip_cache["data"])
    data = {}
    try:
        for item in (_run_pvesh("get", "/cluster/status", quiet=True) or []):
            if not isinstance(item, dict) or str(item.get("type")) != "node":
                continue
            name = str(item.get("name") or "")
            ip = str(item.get("ip") or "")
            if name and ip:
                data[name] = ip
    except Exception:  # noqa: BLE001
        data = {}
    # 兜底：/cluster/status 缺的节点 IP 用 corosync ring0_addr 补
    for name, ip in _corosync_node_ips().items():
        if name and ip and name not in data:
            data[name] = ip
    _node_ip_cache["ts"] = now
    _node_ip_cache["data"] = data
    return dict(data)


def ssh_host(node: int) -> Any:
    """SSH 到集群节点的目标主机：优先用 /cluster/status 的节点 IP，其次用 corosync
    ring0_addr，最后回退节点名。

    为什么要解析成 IP：PVE 加入集群只建立密钥互信，**不会**给各节点补 /etc/hosts；pvesh 与
    集群内部通信用 IP 不受影响，但凡 `ssh <节点名>` 的调用都会以 "Could not resolve hostname"
    失败（新建集群的典型环境）。用 IP 让工具不依赖节点名解析。"""
    node = str(node or "").strip()
    if not node:
        return node
    return cluster_node_ips().get(node) or node


def ssh_host_opts(node: int) -> Any:
    """SSH 到集群节点时附加的 host key 身份选项（HostKeyAlias）。

    host key 校验的安全强化：此前所有 SSH 都按「IP」作为 host key 身份（StrictHostKeyChecking
    的匹配键是命令行里的主机名=IP）。IP 因 DHCP/重配变化或被别的节点复用，会让 host key
    校验失效或产生 TOFU 窗口。改用 HostKeyAlias 把身份绑定到**稳定的节点名**：
      - 集群内节点互信由 pvecm 以主机名形式固化在 /root/.ssh/known_hosts，据此稳定校验；
      - 同一节点 IP 变化、或不同节点复用同一 IP，都会被正确识别（key 不匹配即拒绝）。
    调用方仍保留 accept-new 以兼容「互信尚未建立 / known_hosts 缺条目」的首次场景，
    不破坏功能；本选项仅把身份锚从易变的 IP 换成稳定的节点名。"""
    node = str(node or "").strip()
    return ["-o", "HostKeyAlias=" + node] if node else []


def node_unreachable(node: int) -> bool:
    """该节点是否处于「最近探测失败」的负缓存期内（调用方据此跳过 SSH 采集）。"""
    until = _unreachable_until.get(str(node))
    if not until:
        return False
    if time.time() >= until:
        _unreachable_until.pop(str(node), None)
        return False
    return True


def mark_node_unreachable(node: int) -> None:
    """标记节点不可达（TTL 内不再重试 SSH）。本机不参与：本机采集不走 SSH。"""
    node = str(node or "")
    if node and node != socket.gethostname().split(".")[0]:
        _unreachable_until[node] = time.time() + UNREACHABLE_TTL


def mark_node_reachable(node: int) -> None:
    """节点探测成功：解除负缓存，恢复正常采集。"""
    _unreachable_until.pop(str(node or ""), None)


def _run_sbin(cmd: str, timeout: int = 15) -> Any:
    """执行本机命令，PATH 补 /usr/sbin:/sbin（pvecm / corosync 工具默认不在服务进程 PATH 中）。
    命令不存在或执行异常返回 None，调用方据此回退到备用命令。"""
    env = _locale_env()
    env["PATH"] = (env.get("PATH") or "") + ":/usr/sbin:/sbin"
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
