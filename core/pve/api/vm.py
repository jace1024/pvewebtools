from __future__ import annotations
from typing import Any
"""虚机与容器（guests / snapshots / 配置 / 电源操作 / 部署默认值）。"""

import copy
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from core.pve.api.base import (  # noqa: F401
    GUEST_TYPES,
    PVESH_TIMEOUT,
    PveError,
    _conf_dir_signature,
    _run_pvesh,
    _validate,
    mark_node_reachable,
    mark_node_unreachable,
    node_unreachable,
    ssh_host,
    ssh_host_opts,
)
from core.pve.api.node import (  # noqa: F401
    _SSH_CTRL,
)
from core.pve.api.passthrough import (  # noqa: F401
    _passthrough_kind,
    _passthrough_map,
)
from core.pve.api.storage import (  # noqa: F401
    _read_archive_head,
)

GUESTS_CACHE_TTL = 10  # 与前端 10 秒自动刷新对齐；手动刷新 force=True 穿透缓存
IP_CACHE_TTL = 10    # 与前端自动刷新周期相当，避免每次刷新都打一轮 agent
POWER_ACTIONS = {"start", "stop", "shutdown", "reboot", "suspend", "resume"}
_INTERNAL_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
)
_PVE_VM_DEFAULTS = {"cores": "1", "memory": "512", "cpu": "kvm64", "bios": "seabios", "machine": "i440fx"}
_SWEEP_MAX_HOSTS = 1022   # 超过该规模的大子网（>/20）不扫，避免请求路径卡太久
_SWEEP_WORKERS = 64
_desc_cache = {"sig": None, "data": {}}
_guests_cache = {"ts": 0.0, "data": None}
_guests_cache_lock = threading.Lock()
_guests_cond = threading.Condition(_guests_cache_lock)   # 缓存锁 + 「正在采集」等待队列
_guests_loading = False                                  # 已有请求在采：后来者不重复 fork pvesh
_ip_lock = threading.Lock()
_ip_cache = {}          # vmid -> [ip, ...]：运行中虚机 IP 探测结果（TTL 缓存于 _ip_cache_time）
_ip_cache_time = 0.0    # 上次刷新 _ip_cache 的时间戳；首屏（=0）视为过期，触发探测
_onboot_cache = {"sig": None, "data": {}}


def list_guests(force: bool = False) -> Any:
    """集群内全部 VM/LXC 概览，含状态与资源占用。

    短 TTL 缓存（默认 10 秒，与前端自动刷新对齐）避免高频轮询反复 fork pvesh；
    force=True（手动刷新）绕过缓存强制重采。返回深拷贝，防止调用方就地修改污染缓存。

    单飞（single-flight）：/cluster/resources 是 PVE 里较重的聚合接口，缓存过期瞬间可能有
    多个请求（多标签页、前端轮询 + 手动刷新）同时到达。以前是持锁跑 pvesh —— 后来者会排队，
    一旦 PVE 卡住全部请求一起卡到 30s 超时。现在改成：
      - 已有请求在采 + 手上有旧数据 → 直接返回旧数据，不排队；
      - 已有请求在采 + 没有旧数据（首屏并发）→ 等那一次采集完成再返回；
      - 都没有 → 自己采，采完唤醒等待者。
    """
    global _guests_loading
    with _guests_cond:
        now = time.time()
        hit = (not force and _guests_cache["data"] is not None
               and now - _guests_cache["ts"] < GUESTS_CACHE_TTL)
        if hit:
            return copy.deepcopy(_guests_cache["data"])
        if _guests_loading:
            stale = _guests_cache["data"]
            if stale is not None:
                return copy.deepcopy(stale)      # 宁可给旧数据，也不让请求排到超时
            _guests_cond.wait(timeout=PVESH_TIMEOUT)   # 首屏并发：等正在跑的那次
            return copy.deepcopy(_guests_cache["data"] or [])
        _guests_loading = True
    try:
        data = _run_pvesh("get", "/cluster/resources", ["--type", "vm"]) or []
    except Exception:
        with _guests_cond:
            _guests_loading = False
            _guests_cond.notify_all()
        raise
    with _guests_cond:
        _guests_cache["data"] = data
        _guests_cache["ts"] = time.time()   # 从「拿到数据」开始计时，避免把采集耗时算进 TTL
        _guests_loading = False
        _guests_cond.notify_all()
    return copy.deepcopy(data)


def _invalidate_guests_cache() -> None:
    """虚机列表短缓存失效：重命名等写操作后调用，避免列表仍显示旧数据（否则要等 TTL 到期才更新）"""
    with _guests_cond:
        _guests_cache["data"] = None
        _guests_cache["ts"] = 0.0
        _guests_cond.notify_all()


def list_snapshots(vmid: int) -> Any:
    """指定虚机的快照列表（自动定位节点与类型），供快照下拉选择"""
    if not str(vmid).isdigit():
        raise PveError(f"非法 VMID: {vmid}")
    guest = next((g for g in list_guests() if str(g.get("vmid")) == str(vmid)), None)
    if not guest:
        raise PveError(f"虚机不存在: {vmid}")
    node, gtype = str(guest["node"]), str(guest["type"])
    _validate(node, gtype, vmid)
    data = _run_pvesh("get", f"/nodes/{node}/{GUEST_TYPES[gtype]}/{vmid}/snapshot")
    # 过滤掉 PVE 自动生成的 current 伪快照行
    return [s for s in (data or []) if s.get("name") != "current"]


def _ping_subnet_hosts() -> None:
    """逐地址 ping 各桥接接口所在子网，让在线主机进本机 ARP 表（供 MAC 反查）"""
    try:
        addrs = json.loads(subprocess.run(
            ["ip", "-j", "addr"], capture_output=True, text=True,
            timeout=5).stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return
    targets = set()
    for a in addrs:
        if not str(a.get("ifname", "")).startswith(("vmbr", "vm")):
            continue
        for info in a.get("addr_info") or []:
            if info.get("family") != "inet" or not info.get("local"):
                continue
            try:
                net = ipaddress.ip_network(
                    f"{info['local']}/{info.get('prefixlen', 24)}", strict=False)
            except ValueError:
                continue
            if net.num_addresses - 2 > _SWEEP_MAX_HOSTS:
                continue   # 大网段跳过，收益低风险高
            targets.update(str(h) for h in net.hosts())

    def ping_one(ip: str) -> None:
        # ICMP 是否被响应不重要，发起 ping 时的 ARP 解析本身就让对端 MAC/IP 进表
        try:
            subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

    with ThreadPoolExecutor(max_workers=_SWEEP_WORKERS) as ex:
        list(ex.map(ping_one, targets))


def _arp_ip_by_mac(macs: list) -> Any:
    """按 MAC 查本机 ARP 表找 VM 的 IP，无需虚机装任何代理"""
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return []
    macs = {m.lower() for m in macs}
    ips = []
    for ln in lines:
        cols = ln.split()
        # 列：IP  HW类型  标志  MAC  掩码  设备；跳过 MAC 全零的不完整条目
        if len(cols) >= 4 and cols[0] != "0.0.0.0" \
                and cols[3] != "00:00:00:00:00:00" and cols[3].lower() in macs:
            ips.append(cols[0])
    return ips


def _is_internal_ipv4(addr: str) -> Any:
    """判断是否为容器/虚机内部的 docker / 私有网段地址（应过滤，不进概览列表）。"""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return any(ip in net for net in _INTERNAL_V4)


def _qemu_ips(node: int, vmid: int) -> Any:
    """VM 取 IPv4 多级回退：虚机代理 → cloud-init 静态 IP → 本机 ARP 表按 MAC 反查"""
    # 一级：QEMU Guest Agent（需虚机已装代理，未装时接口报错走回退）
    try:
        data = _run_pvesh("get", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces", quiet=True)
    except PveError:
        data = None
    ips = []
    for itf in (data or {}).get("result") or []:
        for addr in itf.get("ip-addresses") or []:
            ip = str(addr.get("ip-address", ""))
            if addr.get("ip-address-type") == "ipv4" and ip and not _is_internal_ipv4(ip):
                ips.append(ip)
    if ips:
        return ips
    cfg = _run_pvesh("get", f"/nodes/{node}/qemu/{vmid}/config") or {}
    # 二级：cloud-init 静态配置 ipconfigN 中的 ip=
    statics = []
    for k, v in cfg.items():
        if not re.fullmatch(r"ipconfig\d+", str(k)):
            continue
        m = re.search(r"(?:^|,)ip=([^,/]+)", str(v))
        if m and m.group(1) not in ("dhcp", "manual"):
            statics.append(m.group(1))
    if statics:
        return statics
    # 三级：本机节点按网卡 MAC 查 ARP 表；先扫子网让同网段在线 VM 进表（远程节点仅能查现有表）
    if node == socket.gethostname().split(".")[0]:
        macs = []
        for k, v in cfg.items():
            if re.fullmatch(r"net\d+", str(k)):
                m = re.search(r"=([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", str(v))
                if m:
                    macs.append(m.group(1))
        if macs:
            ips = _arp_ip_by_mac(macs)
            if not ips:
                _ping_subnet_hosts()
                ips = _arp_ip_by_mac(macs)
            return ips
    return []


def _lxc_ips(node: int, vmid: int) -> Any:
    """容器取 IPv4 多级回退：配置静态 IP → PVE interfaces 端点 → pct exec 探测。
    /nodes/{node}/lxc/{vmid}/interfaces 集群感知（pvesh 自动代理到目标节点，无需 SSH），
    返回运行中容器各网卡的 inet（如 "192.168.0.117/24"），是跨节点取 DHCP 实际地址的首选；
    老版本 PVE 无此端点时回退 pct exec：本机直接执行，远程节点经 SSH
    （复用节点可达性缓存与超时机制）。"""
    cfg = _run_pvesh("get", f"/nodes/{node}/lxc/{vmid}/config") or {}
    ips = []
    dhcp = False
    for k, v in cfg.items():
        if not re.fullmatch(r"net\d+", str(k)):
            continue
        props = dict(x.split("=", 1) for x in str(v).split(",") if "=" in x)
        ip = props.get("ip", "")
        if ip == "dhcp":
            dhcp = True
        elif ip and ip != "manual" and not _is_internal_ipv4(str(ip).split("/")[0]):
            ips.append(str(ip).split("/")[0])
    if not ips:
        # 二级：PVE 7+ 的 interfaces 端点，直接拿容器网卡实际地址，跨节点零 SSH 依赖
        try:
            data = _run_pvesh("get", f"/nodes/{node}/lxc/{vmid}/interfaces", quiet=True)
        except PveError:
            data = None
        for itf in data or []:
            inet = str((itf or {}).get("inet") or "").split("/")[0]
            # 过滤回环：容器内 hostname 解析异常时可能只报 127.x，展示为无 IP 更诚实
            if inet and not _is_internal_ipv4(inet):
                ips.append(inet)
    if not ips and dhcp:
        local = socket.gethostname().split(".")[0]
        probe_cmd = ["pct", "exec", str(vmid), "--",
                     "sh", "-c", "hostname -I 2>/dev/null || hostname -i 2>/dev/null"]
        try:
            if node == local:
                proc = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=8)
            else:
                if node_unreachable(node):
                    return ips
                proc = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                     *ssh_host_opts(node),
                     f"root@{ssh_host(node)}"] + probe_cmd,
                    capture_output=True, text=True, timeout=15)
                if proc.returncode == 0:
                    mark_node_reachable(node)
        except (OSError, subprocess.TimeoutExpired):
            if node != local:
                mark_node_unreachable(node)
            return ips
        if proc.returncode == 0:
            ips = [t for t in proc.stdout.split()
                   if "." in t and not _is_internal_ipv4(t)]
    return ips


def _onboot_map() -> Any:
    """从本地 pmxcfs 配置（/etc/pve/qemu-server|/etc/pve/lxc）读每个虚机的 onboot 开关。
    pmxcfs 集群共享、本机直接读文件，比逐个 pvesh 查 config 快得多（无进程启动开销）。
    结果按配置目录签名缓存，配置有增删改才重读。返回 vmid -> 0/1。"""
    sig = _conf_dir_signature()
    if _onboot_cache["sig"] == sig and _onboot_cache["data"]:
        return _onboot_cache["data"]
    onboot = {}
    for subdir in ("qemu-server", "lxc"):
        d = Path("/etc/pve") / subdir
        try:
            confs = list(d.glob("*.conf"))
        except OSError:
            continue
        for conf in confs:
            vmid = conf.stem
            if not vmid.isdigit():
                continue
            try:
                text = conf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # 找 onboot: 1 即开机自启；缺省或 0 均视为未启用
            m = re.search(r"^onboot:\s*(\d+)", text, re.M)
            onboot[vmid] = 1 if (m and m.group(1) == "1") else 0
    _onboot_cache["sig"] = sig
    _onboot_cache["data"] = onboot
    return onboot


def _description_map() -> Any:
    """从本地 pmxcfs 配置（/etc/pve/qemu-server|/etc/pve/lxc）读每个虚机的备注（description）。
    与 _onboot_map 同理读本地文件、按配置目录签名缓存；返回 vmid -> 备注字符串（无则空串）。"""
    sig = _conf_dir_signature()
    if _desc_cache["sig"] == sig and _desc_cache["data"]:
        return _desc_cache["data"]
    desc = {}
    for subdir in ("qemu-server", "lxc"):
        d = Path("/etc/pve") / subdir
        try:
            files = [f for f in d.iterdir() if f.is_file() and f.suffix == ".conf"]
        except OSError:
            continue
        for conf in files:
            vmid = conf.stem
            if not vmid.isdigit():
                continue
            try:
                text = conf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(r"^description:\s?(.*)$", text, re.M)
            desc[vmid] = (m.group(1).strip() if m else "")
    _desc_cache["sig"] = sig
    _desc_cache["data"] = desc
    return desc


def list_guests_with_ips(force: bool = False) -> Any:
    """list_guests 基础上给运行中的虚机补 ip 字段（缓存 10 秒），并补 onboot 开机自启开关。
    force=True（手动刷新）时穿透虚机列表缓存，强制重采最新状态。"""
    global _ip_cache, _ip_cache_time   # 函数内有赋值，两个都要声明，否则读取报 UnboundLocalError
    guests = list_guests(force=force)
    # 开机自启 + 直通标记：直接读本地配置，按目录签名缓存，无需每次全量重读
    ob = _onboot_map()
    pm = _passthrough_map()
    pk = _passthrough_kind()
    dm = _description_map()
    for g in guests:
        g["onboot"] = ob.get(str(g.get("vmid")), 0)
        g["has_passthrough"] = pm.get(str(g.get("vmid")), 0)
        g["passthrough_kind"] = pk.get(str(g.get("vmid")), "")
        g["description"] = dm.get(str(g.get("vmid")), "")
    with _ip_lock:
        stale = time.time() - _ip_cache_time > IP_CACHE_TTL
        if not stale:
            for g in guests:
                ips = _ip_cache.get(g.get("vmid"))
                if ips is not None:
                    g["ip"] = ips
            return guests
        running = [g for g in guests if g.get("status") == "running"]
        fresh = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_qemu_ips if g["type"] == "qemu" else _lxc_ips,
                              g["node"], g["vmid"]): g for g in running}
            for fut, g in futs.items():
                try:
                    fresh[g["vmid"]] = fut.result(timeout=PVESH_TIMEOUT) or []
                except Exception:
                    fresh[g["vmid"]] = []   # 代理未装/探测失败不阻断列表
        _ip_cache = fresh
        _ip_cache_time = time.time()
        for g in guests:
            ips = fresh.get(g.get("vmid"))
            if ips is not None:
                g["ip"] = ips
    return guests


def read_deploy_template(src: str) -> Any:
    """读取「模板快速建机」模板的实际生效配置，供前端把「默认配置」选项显示成具体值。

    src 形如 'vmid:<数字>'（PVE 模板 VM）或 'file:<路径>'（上传的 .vma 模板文件）。
    cores/memory/cpu/bios/machine 配置里缺省时按 PVE 默认值补齐（见 _PVE_VM_DEFAULTS），
    保证前端总能显示具体值而非空文案；读取失败则返回 {}，由前端退回中性文案。
    返回 {name, cores, memory, cpu, bios, machine, bridge, boot_storage}（仅含能取到的键）。
    """
    data = b""
    if src.startswith("vmid:"):
        # /etc/pve 是集群文件系统，任意节点可直接读该 VM 的配置
        p = Path(f"/etc/pve/qemu-server/{src[5:]}.conf")
        try:
            data = p.read_bytes() if p.is_file() else b""
        except OSError:
            data = b""
    elif src.startswith("file:"):
        data = _read_archive_head(Path(src[5:]))
    if not data:
        return {}

    def pick(key: str) -> Any:
        m = re.search(rb"(?m)^\s*" + key.encode() + rb":\s*([^\r\n]*)", data)
        return m.group(1).decode("utf-8", errors="replace").strip() if m else ""

    out = {}
    name = pick("name") or pick("hostname")
    if name:
        out["name"] = name
    for k in ("cores", "memory", "cpu", "bios", "machine"):
        v = pick(k)
        # 缺省即取 PVE 默认值（见 _PVE_VM_DEFAULTS 说明），补齐后前端文案口径统一
        out[k] = v or _PVE_VM_DEFAULTS[k]
    net0 = pick("net0")
    m = re.search(r"(?:^|,)bridge=([^,\s]+)", net0)
    if m:
        out["bridge"] = m.group(1)
    for k in ("scsi0", "virtio0", "sata0", "ide0", "efidisk0"):
        line = pick(k)
        if line:
            out["boot_storage"] = line.split(",", 1)[0].split(":", 1)[0]
            break
    return out


def cloud_deploy_defaults(image: str = None) -> dict:
    """云镜像建机（cloud_deploy）脚本内置的有效默认值，供前端把「默认配置」选项显示成具体值。

    仅服务 Linux 云镜像，默认值如下：
    - cores=1 / memory=2048(2GB) / cpu=host
    - bridge=vmbr0 / storage=local（这些参数未选时脚本的兜底值）。
    """
    del image  # 前端可能传入镜像路径，但默认值已统一为 Linux，不再按系统区分
    cores, memory = 1, 2048
    return {
        "cores": cores,
        "memory": memory,
        "cpu": "host",
        "bridge": "vmbr0",
        "storage": "local",
    }


def get_guest_config(node: int, gtype: Any, vmid: int) -> Any:
    _validate(node, gtype, vmid)
    return _run_pvesh("get", f"/nodes/{node}/{gtype}/{vmid}/config")


def get_guest_status(node: int, gtype: Any, vmid: int) -> Any:
    _validate(node, gtype, vmid)
    return _run_pvesh("get", f"/nodes/{node}/{gtype}/{vmid}/status/current")


def set_guest_onboot(node: int, gtype: Any, vmid: int, enabled: bool) -> Any:
    """设置虚机开机自启（onboot）：enabled=True 时 --onboot 1，否则 --onboot 0"""
    _validate(node, gtype, vmid)
    val = "1" if enabled else "0"
    return _run_pvesh("set", f"/nodes/{node}/{gtype}/{vmid}/config", ["--onboot", val])


def set_guest_description(node: int, gtype: Any, vmid: int, desc: str) -> Any:
    """设置虚机备注（description）。空串表示清空备注。"""
    _validate(node, gtype, vmid)
    return _run_pvesh("set", f"/nodes/{node}/{gtype}/{vmid}/config", ["--description", desc])


def rename_guest(node: int, gtype: Any, vmid: int, name: str) -> Any:
    """重命名虚机：VM 用 --name，LXC 用 --hostname（PVE 显示名随 hostname 变化）"""
    _validate(node, gtype, vmid)
    name = str(name).strip()
    if not name or len(name) > 200:
        raise PveError("名称不能为空且最长 200 字符")
    opt = "--name" if gtype == "qemu" else "--hostname"
    ret = _run_pvesh("set", f"/nodes/{node}/{gtype}/{vmid}/config", [opt, name])
    _invalidate_guests_cache()   # 名称写操作后立即失效列表缓存，避免前端刷新仍拿到旧名
    return ret


def guest_power_action(node: int, gtype: Any, vmid: int, action: Any) -> Any:
    """电源操作，返回任务 UPID"""
    _validate(node, gtype, vmid)
    if action not in POWER_ACTIONS:
        raise PveError(f"不支持的操作: {action}")
    return _run_pvesh("create", f"/nodes/{node}/{gtype}/{vmid}/status/{action}")
