from __future__ import annotations
from typing import Any
"""节点与硬件信息（节点表、状态、磁盘、网卡、GPU、内存/CPU/主板 DMI 解析）。"""

import json
import re
import socket
import subprocess
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from core.pve.api.base import (  # noqa: F401
    PveError,
    _corosync_node_ips,
    _run_pvesh,
    mark_node_reachable,
    mark_node_unreachable,
    node_unreachable,
    ssh_host,
    ssh_host_opts,
)
from core.pve.api.passthrough import (  # noqa: F401
    _hostpci_bds,
    _hostpci_mdev_bds,
    _rdm_passthrough_devs,
)

CPU_FREQ_TTL = 30
DISKS_CACHE_TTL = 30
GPU_CACHE_TTL = 600
NIC_CACHE_TTL = 30
TEMP_CACHE_TTL = 30
_BOARD_HW_SCRIPT = """
echo "BOARD_VENDOR=$(dmidecode -s baseboard-manufacturer 2>/dev/null | head -1)"
echo "BOARD_PRODUCT=$(dmidecode -s baseboard-product-name 2>/dev/null | head -1)"
CHIP=$(lspci 2>/dev/null | awk -F': ' '/ISA bridge/{print $2; exit}')
[ -z "$CHIP" ] && CHIP=$(lspci 2>/dev/null | awk -F': ' '/Host bridge/{print $2; exit}')
echo "CHIPSET=$CHIP"
if [ -e /dev/ipmi0 ]; then echo HAS_IPMI=1; else echo HAS_IPMI=0; fi
PW=0
for p in /sys/class/hwmon/hwmon*/pwm1; do [ -w "$p" ] && PW=1; done
echo "HAS_PWM=$PW"
echo "VIRT=$(systemd-detect-virt 2>/dev/null | grep -v '^none$' | head -n1)"
"""
_CPU_FREQ_SCRIPT = r"""
n=0; s=0; m=0; x=0
for c in /sys/devices/system/cpu/cpu[0-9]*; do
  f="$c/cpufreq/scaling_cur_freq"
  [ -f "$f" ] || continue
  v=$(cat "$f")
  case "$v" in ''|*[!0-9]*) continue;; esac
  [ "$v" -gt 0 ] || continue
  v=$((v / 1000))
  n=$((n + 1)); s=$((s + v))
  if [ "$m" -eq 0 ] || [ "$v" -lt "$m" ]; then m=$v; fi
  if [ "$v" -gt "$x" ]; then x=$v; fi
done
if [ "$n" -gt 0 ]; then
  echo "avg=$((s / n))"
  echo "min=$m"
  echo "max=$x"
  echo "cores=$n"
else
  awk '/^cpu MHz/{s+=$4; n++; if($4>m)m=$4; if(!min||$4<min)min=$4} END{if(n)printf "avg=%d\nmin=%d\nmax=%d\ncores=%d\n", s/n, min, m, n}' /proc/cpuinfo
fi
"""
_CPU_SENSOR_CHIPS = ("coretemp", "k10temp", "cpu_thermal", "zenpower")
_CPU_TEMP_FALLBACK = r"""
for h in /sys/class/hwmon/hwmon*; do
  n=$(cat "$h/name" 2>/dev/null)
  case "$n" in coretemp|k10temp|zenpower|cpu_thermal)
    t=$(cat "$h/temp1_input" 2>/dev/null)
    [ -n "$t" ] && break
    ;;
  esac
done
if [ -z "${t:-}" ]; then
  for z in /sys/class/thermal/thermal_zone*/temp; do
    [ -r "$z" ] && { t=$(cat "$z" 2>/dev/null); break; }
  done
fi
echo "${t:-}"
"""
_DMI_JUNK = {"to be filled by o.e.m.", "default string", "none", "not specified",
             "not applicable", "unknown", "system product name"}
_NICS_SCRIPT = """
for i in /sys/class/net/*; do
  N=${i##*/}
  if [ -e "$i/device" ] || [ -e "$i/bonding" ] || [ -e "$i/bridge" ]; then
    ST=$(cat "$i/operstate" 2>/dev/null || echo unknown)
    SP=$(cat "$i/speed" 2>/dev/null || echo 0)

    if [ -e "$i/wireless" ] || [ -e "$i/phy80211" ]; then WL=1; else WL=0; fi

    # 收集 IPv4 地址（含 /掩码），多地址空格分隔，管理口判定用

    IPS=$(ip -o -4 addr show "$N" 2>/dev/null | awk '{print $4}' | tr '\n' ' ')

    # 桥/聚合：内核 speed 取所有 UP 端口的最大值，会把 VM 的 tap/veth 虚拟口（固定报 10000Mb/s）
    # 也算进去，导致 1G 物理口的桥显示成 10G。改按“物理成员口”（排除 tap/veth/vnet/fw*）重算：
    # 取各物理成员口当前 up 的较大速率；无物理成员则记 0（前端显示 —）。MEM 供前端展示成员。
    MEM=""
    PORTS=""
    if [ -e "$i/bridge" ]; then
      PORTS=$(ls /sys/class/net/$N/brif/ 2>/dev/null)
    elif [ -e "$i/bonding" ]; then
      PORTS=$(cat $i/bonding/slaves 2>/dev/null)
    fi
    if [ -n "$PORTS" ]; then
      PSP=0
      for p in $PORTS; do
        case "$p" in tap*|veth*|vnet*|fwln*|fwpr*|fwbr*) continue;; esac
        MEM="${MEM:+$MEM,}$p"
        pst=$(cat /sys/class/net/$p/operstate 2>/dev/null)
        psp=$(cat /sys/class/net/$p/speed 2>/dev/null)
        case "$psp" in ''|*[!0-9]*) continue;; esac
        [ "$pst" = "up" ] || continue
        if [ "$psp" -gt "$PSP" ]; then PSP=$psp; fi
      done
      [ -n "$MEM" ] && SP=$PSP
    fi
    echo "$N $ST $SP $WL ${MEM:--} $IPS"

  fi

done

# 被 vfio-pci 接管的网络设备：驱动已剥离，接口从 /sys/class/net 消失，
# 但物理网卡仍在（class 0200 以太网 / 0280 无线）。这里只输出"驱动是 vfio-pci"这一事实，
# 究竟是「被某台 VM 直通」还是「只是被 vfio 接管、无人使用」，交给 Python 侧按 VM 配置判定。
# 输出格式：@vfio 真实驱动模块 BDF 无线(1/0) 描述
for p in $(lspci -D 2>/dev/null | awk '/Network controller|Ethernet controller/{print $1}'); do
  # 未绑定时输出空（不能用 readlink -f：链接不存在时它会把路径原样回显，得到 "driver"）
  DRV=""
  if [ -e "/sys/bus/pci/devices/$p/driver" ]; then
    DRV=$(basename "$(readlink "/sys/bus/pci/devices/$p/driver" 2>/dev/null)" 2>/dev/null)
  fi
  if [ "$DRV" = "vfio-pci" ]; then
    MOD=$(lspci -knn -s "$p" 2>/dev/null | sed -n 's/.*Kernel modules: //p' | tr -d ' \\t' | tr ',' ' ' | awk '{print $1}')
    # 无线判定：class 0280（Network controller）为无线，0200（Ethernet controller）为有线
    if lspci -nn -s "$p" 2>/dev/null | grep -q 'Network controller'; then W=1; else W=0; fi
    # 原始描述交给 Python 侧 _friendly_nic_name 精简为友好型号名
    DESC=$(lspci -nn -s "$p" 2>/dev/null | sed 's/^[0-9a-fA-F:.]* //')
    echo "@vfio $MOD $p $W $DESC"
  fi
done
"""
_NIC_FRIENDLY_NAMES = {
    "8086:a370": "Intel CNVi WiFi",
    "8086:095a": "Intel 7265 WiFi",
    "8086:24fd": "Intel 8265 WiFi",
    "8086:2723": "Intel AX200 WiFi",
    "8086:2725": "Intel AX210 WiFi",
    "8086:7af0": "Intel AX201 WiFi",
}
_SKIP_DISK_PREFIXES = ("zram", "loop", "ram", "nbd")
_SSH_CTRL = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/pvewebtools-ssh-%C", "-o", "ControlPersist=300"]
_disks_cache = {}    # node -> (时间戳, 结果 list)
_freq_cache = {}   # node -> (时间戳, 结果 dict)
_gpu_cache = {}   # node -> (时间戳, 列表)
_nic_cache = {}   # node -> (时间戳, 网卡列表)
_temp_cache = {}   # node -> (时间戳, 结果 dict)


def list_nodes() -> Any:
    data = _run_pvesh("get", "/nodes")
    return data or []


def cluster_members() -> Any:
    """集群配置里的节点名列表（corosync.conf 的 nodelist）；非集群 / 读取失败返回 []。

    只读一个文件、不起子进程，且集群失仲裁（只读）时同样可用 —— 用于「pvesh 的节点列表
    在降级时缺失节点甚至返回空表」的兜底补齐（见 server._merge_cluster_nodes）。"""
    return sorted(_corosync_node_ips().keys())


_KNOWN_NODES_TTL = 10
_known_nodes_cache = {"ts": 0.0, "names": None}
_known_nodes_lock = threading.Lock()


def _local_node_dirs() -> Any:
    """pmxcfs 里的节点配置目录名集合（每个成员节点一个目录，离线节点同样有）；读不到返回空集。"""
    try:
        return {p.name for p in Path("/etc/pve/nodes").iterdir() if p.is_dir()}
    except OSError:
        return set()


def known_node_names() -> Any:
    """集群内已知节点名集合（校验「节点是否存在」专用；纯本地读，不起 pvesh 子进程）。

    原实现是 `{n["node"] for n in list_nodes()}`，即每次校验跑一次 `pvesh get /nodes`
    （约 0.7s）；而所有带 `?node=` 的下拉接口（PCI/网卡/磁盘/备份/存储…）在取数据前都要
    校验一遍，等于每个下拉白付一次子进程开销。

    口径：/etc/pve/nodes/ 目录（每个成员节点一个，离线节点同样有）+ corosync nodelist，
    并总是包含本机 —— pmxcfs 的节点目录在集群降级时也完整，比 pvesh 的节点表更不容易缺项
    （缺项会让正常节点被误判「节点不存在」）。读不到集群信息（单机/非 PVE）才回退 pvesh。
    结果缓存 10 秒：节点增删是分钟级事实，readdir 走 pmxcfs 的 FUSE，没必要每次都做。
    """
    now = time.time()
    with _known_nodes_lock:
        hit = _known_nodes_cache["names"]
        if hit is not None and now - _known_nodes_cache["ts"] < _KNOWN_NODES_TTL:
            return hit
    names = _local_node_dirs()
    if not names:
        names = set(cluster_members() or [])
    names.add(socket.gethostname().split(".")[0])
    if len(names) <= 1:      # 只有本机（单机部署 / 读不到集群信息）：退回 pvesh 节点表
        try:
            names |= {str(n.get("node")) for n in list_nodes() if n.get("node")}
        except Exception:  # noqa: BLE001
            pass
    with _known_nodes_lock:
        _known_nodes_cache["names"] = names
        _known_nodes_cache["ts"] = now
    return names


def invalidate_known_nodes_cache() -> Any:
    """节点增删（加入/移除集群）后丢弃已知节点名缓存。"""
    with _known_nodes_lock:
        _known_nodes_cache["names"] = None
        _known_nodes_cache["ts"] = 0.0


def get_node_status(node: int) -> Any:
    """单节点详情：cpuinfo、内核、PVE 版本等硬件/系统信息"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    return _run_pvesh("get", f"/nodes/{node}/status") or {}


def get_node_disks(node: int) -> Any:
    """主机全部物理盘（含空闲盘）：容量/型号/使用状态/已用字节，带 30 秒缓存"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    now = time.time()
    hit = _disks_cache.get(node)
    if hit and now - hit[0] < DISKS_CACHE_TTL:
        return hit[1]
    if node_unreachable(node):
        return []          # 该节点最近探测失败（负缓存 60 秒）：跳过，不再等一次 SSH 连接超时
    lsblk_cmd = "lsblk -b -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,ROTA,TRAN"
    df_cmd = "df -B1 --output=target,used"
    if node == socket.gethostname().split(".")[0]:
        cmd1, cmd2 = lsblk_cmd.split(), df_cmd.split()
    else:
        base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL, *ssh_host_opts(node), f"root@{ssh_host(node)}"]
        cmd1, cmd2 = (base + [f"export LC_ALL=C LANG=C; {lsblk_cmd}"],
                      base + [f"export LC_ALL=C LANG=C; {df_cmd}"])
    try:
        r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=15)
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        mark_node_unreachable(node)
        return []
    if r1.returncode != 0:
        if r1.returncode == 255:       # SSH 连接/认证失败 → 记负缓存，短时间内不再重试
            mark_node_unreachable(node)
        return []
    mark_node_reachable(node)
    try:
        tree = json.loads(r1.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        return []
    used_map = {}
    if r2.returncode == 0:
        # --output=target,used：挂载点(target)在最左且可能含空格，used 在最右为纯数字。
        # 用 rpartition 从右取 used，再 strip 还原含空格的挂载点
        # （df 以空格对齐，target 可能带前导空格；旧逻辑 line.split() 在含空格挂载点下会错位）。
        for line in r2.stdout.splitlines()[1:]:
            target, _, used = line.rpartition(" ")
            target = target.strip()
            if used.isdigit() and target:
                used_map[target] = int(used)

    def walk(dev: Any, mounts: list, fstypes: list) -> None:
        for m in dev.get("mountpoints") or []:
            if m:
                mounts.append(m)
        if dev.get("fstype"):
            fstypes.append(dev["fstype"].lower())
        for c in dev.get("children") or []:
            walk(c, mounts, fstypes)

    disks = []
    # 被 VM 块映射直通(RDM)引用的设备名集合：这些盘虽无挂载/文件系统，但已被 VM 占用，不能算空闲
    rdm_devs = _rdm_passthrough_devs()
    for dev in tree:
        if dev.get("type") != "disk":
            continue
        name = dev.get("name") or ""
        if name.startswith(_SKIP_DISK_PREFIXES):
            continue
        mounts, fstypes = [], []
        walk(dev, mounts, fstypes)
        # RDM 直通占用优先级最高：盘被 VM 直通引用后，即使宿主机上还残留文件系统/挂载点
        # （如之前当过系统盘、有分区表或 LVM 元数据），也应判为「直通占用」而非「使用中」
        if name in rdm_devs:
            state = "直通占用"
        elif mounts:
            state = "使用中"
        elif "lvm2_member" in fstypes:
            state = "LVM 使用中"
        elif fstypes:
            state = "使用中"
        else:
            state = "空闲"
        disks.append({
            "name": name,
            "size": dev.get("size") or 0,
            "model": (dev.get("model") or "").strip(),
            "rota": dev.get("rota"),
            "tran": (dev.get("tran") or "").lower(),
            "state": state,
            "used": sum(used_map.get(m, 0) for m in mounts),
        })
    disks.sort(key=lambda d: (d["state"] == "空闲", -d["size"]))
    _disks_cache[node] = (now, disks)
    return disks


def get_node_temperatures(node: int) -> Any:
    """主机温度：CPU 走 lm-sensors，磁盘走 smartctl 并行采集；带 30 秒缓存，依赖缺失时对应项缺省"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    now = time.time()
    hit = _temp_cache.get(node)
    if hit and now - hit[0] < TEMP_CACHE_TTL:
        return hit[1]
    base = [] if node == socket.gethostname().split(".")[0] else \
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL, *ssh_host_opts(node), f"root@{ssh_host(node)}"]

    def run(cmd: str, timeout: int = 15) -> Any:
        try:
            if base:
                return subprocess.run(base + ["env", "LC_ALL=C", "LANG=C"] + cmd,
                                      capture_output=True, text=True, timeout=timeout)
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    out = {}
    # CPU 温度：sensors -j 输出 JSON，取整包温度（Package id 0 / Tctl）
    chips = {}
    r = run(["sensors", "-j"])
    if r and r.returncode == 0:
        try:
            chips = json.loads(r.stdout)
        except json.JSONDecodeError:
            chips = {}
        for chip, features in chips.items():
            if not chip.lower().startswith(_CPU_SENSOR_CHIPS):
                continue
            for fname, fvals in features.items():
                t = fvals.get("temp1_input") if isinstance(fvals, dict) else None
                if isinstance(t, (int, float)):
                    out["cpu"] = round(float(t), 1)
                    break
            if "cpu" in out:
                break
    # CPU 温度兜底：未装 lm-sensors 时改读 sysfs hwmon/thermal（返回毫摄氏度）
    if "cpu" not in out:
        if node == socket.gethostname().split(".")[0]:
            r = run(["sh", "-c", _CPU_TEMP_FALLBACK])
        else:
            r = run([_CPU_TEMP_FALLBACK])
        if r and r.returncode == 0 and r.stdout.strip():
            try:
                milli = int(float(r.stdout.strip()))
                if milli > 0:
                    out["cpu"] = round(milli / 1000.0, 1)
            except ValueError:
                pass
    # 磁盘温度：逐盘 smartctl JSON 并行查询（串行逐盘是多盘主机首屏最大耗时点）
    r = run(["lsblk", "-dn", "-o", "NAME,TYPE"])
    disk_temps = {}
    if r and r.returncode == 0:
        names = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "disk" and not parts[0].startswith(_SKIP_DISK_PREFIXES):
                names.append(parts[0])

        def read_temp(name: str) -> Any:
            rs = run(["smartctl", "-A", "-j", f"/dev/{name}"], timeout=10)
            if not rs:
                return name, None
            try:
                t = (json.loads(rs.stdout).get("temperature") or {}).get("current")
            except json.JSONDecodeError:
                return name, None
            return name, (round(float(t), 1) if isinstance(t, (int, float)) else None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for name, t in pool.map(read_temp, names):
                if t is not None:
                    disk_temps[name] = t
    if disk_temps:
        out["disks"] = disk_temps
    _temp_cache[node] = (now, out)
    return out


def get_node_cpu_freq(node: int) -> Any:
    """CPU 实时频率（每核 scaling_cur_freq，MHz）：取不到时返回空 dict，前端不展示"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    now = time.time()
    hit = _freq_cache.get(node)
    if hit and now - hit[0] < CPU_FREQ_TTL:
        return hit[1]
    if node_unreachable(node):
        return {}          # 该节点最近探测失败（负缓存 60 秒）：跳过，不再等一次 SSH 连接超时
    base = [] if node == socket.gethostname().split(".")[0] else \
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL, *ssh_host_opts(node), f"root@{ssh_host(node)}"]
    try:
        if base:
            r = subprocess.run(base + [f"export LC_ALL=C LANG=C; {_CPU_FREQ_SCRIPT}"],
                               capture_output=True, text=True, timeout=10)
        else:
            r = subprocess.run(["sh", "-c", _CPU_FREQ_SCRIPT], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        mark_node_unreachable(node)
        return {}
    if r is not None:
        if r.returncode == 255:        # SSH 连接/认证失败 → 记负缓存，短时间内不再重试
            mark_node_unreachable(node)
        elif r.returncode == 0:
            mark_node_reachable(node)
    out = {}
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            if k in ("avg", "min", "max", "cores") and v.isdigit():
                out[k] = int(v)
    if not out:
        return {}
    # 时间戳用完成时刻：远程 SSH 耗时较长，避免耗时吃掉缓存有效期
    _freq_cache[node] = (time.time(), out)
    return out


def _clean_dmi(value: str) -> Any:
    """dmidecode 字段去噪：Unknown/Not Specified 这类占位值视为空"""
    v = (value or "").strip()
    if v.lower() in ("unknown", "none", "not specified", "no module installed"):
        return ""
    return v


def _mem_brand(part_number: Any, manufacturer: Any) -> Any:
    """从内存条信息提取品牌名。
    Manufacturer 字段常是 JEDEC 十六进制厂商 ID（如 04EF/U3E1），不可读；
    真正品牌多藏在 Part Number 里（如 TEAMGROUP-SD4-3200 -> TEAMGROUP）。
    优先取 Part Number 第一段（去掉型号/规格尾巴）；提取不到时退回 Manufacturer，
    但纯十六进制 ID（如 04EF）视为无效返回空。"""
    part = _clean_dmi(part_number)
    if part:
        # 常见分隔：- 空格 _ ；取第一段作为品牌，并去掉尾部数字/纯型号
        head = re.split(r"[-_\s]", part, maxsplit=1)[0].strip()
        # 品牌应是字母为主（如 TEAMGROUP/Crucial/Kingston/Samsung），纯数字/十六进制视为型号而非品牌
        if head and re.search(r"[A-Za-z]", head):
            return head.upper()
    manu = _clean_dmi(manufacturer)
    if manu:
        # Manufacturer 若是纯十六进制厂商 ID（如 04EF、U3E1 这类 JEDEC 代码）则丢弃
        if re.fullmatch(r"[0-9A-Fa-f]{2,4}", manu):
            return ""
        if manu.lower() in _DMI_JUNK:
            return ""
        return manu
    return ""


def _friendly_nic_name(desc: str) -> Any:
    """把直通网卡的 lspci 描述精简为友好型号名。
    优先用 vendor:device 映射表；未命中则做通用清理（去公司后缀/芯片组代号/编号/修订号）。"""
    if not desc:
        return ""
    m = re.search(r"\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]", desc)
    if m and m.group(1).lower() in _NIC_FRIENDLY_NAMES:
        return _NIC_FRIENDLY_NAMES[m.group(1).lower()]
    # 通用清理：去掉「Corporation/Inc./Co., Ltd.」等公司后缀与芯片组平台代号，
    # 以及 vendor:device 编号和修订号，只留厂商 + 型号核心词
    name = desc
    name = re.sub(r"\bCorporation\b|\bInc\.?|\bCo\.,?\s*Ltd\.?\b", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def get_node_nics(node: int, mgmt_ip: Any = None) -> Any:
    """物理网卡/bond 的链路与速率（up/down + Mb/s）；外节点走集群免密 SSH，带 30 秒缓存。
    mgmt_ip: 节点管理口 IP（来自 PVE /nodes 的 ip 字段），用于前端高亮管理口并附注其 IP。"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    now = time.time()
    hit = _nic_cache.get(node)
    if hit and now - hit[0] < NIC_CACHE_TTL:
        return hit[1]
    if node_unreachable(node):
        return []          # 该节点最近探测失败（负缓存 60 秒）：跳过，不再等一次 SSH 连接超时
    if node == socket.gethostname().split(".")[0]:
        cmd = ["sh", "-c", _NICS_SCRIPT]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *ssh_host_opts(node),
               f"root@{ssh_host(node)}", f"export LC_ALL=C LANG=C; {_NICS_SCRIPT}"]
    nics = []
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        proc = None
        mark_node_unreachable(node)
    if proc is not None:
        # 255 是 SSH 自身的连接/认证失败码：标记负缓存；成功则解除
        if proc.returncode == 255:
            mark_node_unreachable(node)
        elif proc.returncode == 0:
            mark_node_reachable(node)
    # VM 配置里 hostpci 引用的设备集合：用于区分「真直通占用」与「仅被 vfio 接管」
    pt_bds = _hostpci_bds()
    if proc is not None and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.startswith("@vfio"):
                # 驱动已被 vfio-pci 接管的网卡：无 /sys/class/net 接口，但物理设备仍在
                parts = line.split(None, 4)   # @vfio MOD BDF W DESC
                if len(parts) < 4:
                    continue
                bdf = parts[2]
                wireless = parts[3] == "1"
                desc = parts[4] if len(parts) > 4 else ""
                # 「直通占用」以 VM 配置为准：配置里删掉 hostpci 后，设备仍可能挂在 vfio-pci 上，
                # 那种情况标为 vfio（宿主不可用但无人直通），不再误报「直通占用」
                bd = bdf.split(".")[0].replace("0000:", "")
                nics.append({"name": bdf,
                             "state": "passthrough" if bd in pt_bds else "vfio",
                             "speed": 0, "wireless": wireless,
                             "driver": parts[1] if parts[1] else "",
                             "desc": _friendly_nic_name(desc)})
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                speed = int(parts[2])
            except ValueError:
                speed = 0
            members = [m for m in (parts[4].split(",") if len(parts) > 4 else []) if m and m != "-"]
            ips = parts[5:] if len(parts) > 5 else []
            nics.append({"name": parts[0], "state": parts[1],
                         "speed": max(speed, 0),
                         "wireless": len(parts) >= 4 and parts[3] == "1",
                         "members": members,
                         "ips": ips})
        # up 在前，直通占用次之，同状态按名称排序
        order = {"up": 0, "unknown": 1, "down": 2, "passthrough": 3, "vfio": 4}
        nics.sort(key=lambda x: (order.get(x["state"], 9), x["name"]))
    # 标记管理口（命中节点管理 IP 或默认路由出口），前端高亮 + 显示其 IP
    _mark_mgmt_nic(nics, node, mgmt_ip)
    _nic_cache[node] = (now, nics)
    return nics


def _default_route_dev(node: int) -> Any:
    """返回默认路由出口网口名（管理口常见候选）；取不到返回空串。"""
    try:
        if node == socket.gethostname().split(".")[0]:
            cmd = ["sh", "-c", "ip route show default"]
        else:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                   *ssh_host_opts(node),
                   f"root@{ssh_host(node)}", "export LC_ALL=C LANG=C; ip route show default"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            m = re.search(r"dev (\S+)", proc.stdout)
            if m:
                return m.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _mark_mgmt_nic(nics: list, node: int, mgmt_ip: str) -> None:
    """在物理/聚合网卡中标记管理口（mgmt=True）并附注 IP。
    优先按节点管理 IP 命中网卡 IP；未命中则回退到默认路由出口网口。"""
    if not nics:
        return
    target = (mgmt_ip or "").split("/")[0].strip()
    if target:
        for nic in nics:
            for ip in nic.get("ips", []):
                if ip.split("/")[0] == target:
                    nic["mgmt"] = True
                    return
    dev = _default_route_dev(node)
    if dev:
        for nic in nics:
            if nic["name"] == dev:
                nic["mgmt"] = True
                return


def _read_gpu_driver(addr: str, base: Any) -> Any:
    """读取 PCI 设备当前绑定驱动名。本机节点直接读 sysfs；远程节点经 SSH 读远端 sysfs
    （注意：不能在本机直接 Path() 读 sysfs，否则远程节点永远读不到）。"""
    sys_path = f"/sys/bus/pci/devices/{addr}/driver"
    if not base:
        # 本机
        p = Path(sys_path)
        try:
            return p.resolve().name if p.exists() else ""
        except OSError:
            return ""
    # 远程：经 SSH 用 readlink 取 driver 软链指向的驱动目录名
    try:
        r = subprocess.run(base + ["env", "LC_ALL=C", "LANG=C", "readlink", sys_path],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().rstrip("/").split("/")[-1]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def get_node_gpus(node: int) -> Any:
    """节点全部显示设备（class 03）：型号/PCI 地址/当前绑定驱动；本机 lspci，外节点走集群免密 SSH。
    概览节点卡片「显卡」标签数据源。"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    now = time.time()
    hit = _gpu_cache.get(node)
    if hit and now - hit[0] < GPU_CACHE_TTL:
        return hit[1]
    if node_unreachable(node):
        return []          # 该节点最近探测失败（负缓存 60 秒）：跳过，不再等一次 SSH 连接超时
    base = [] if node == socket.gethostname().split(".")[0] else \
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL, *ssh_host_opts(node), f"root@{ssh_host(node)}"]
    try:
        # -D 输出完整 BDF（带 0000: domain 前缀），与 /sys/bus/pci/devices/<bdf> 路径一致。
        # 注意：不能用 lspci -d ::0300 —— 它只匹配 class 0300(VGA)，会漏掉 0302(3D 控制器，
        # 如 Tesla P4)、0380(显示控制器) 等。改为全量输出后在 Python 按 class 前缀 03 过滤，
        # 与直通下拉 _list_pci_class("03") 逻辑保持一致。
        r = subprocess.run(base + ["env", "LC_ALL=C", "LANG=C", "lspci", "-nnD"],
                           capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        mark_node_unreachable(node)
        return []
    if r.returncode != 0:
        if r.returncode == 255:        # SSH 连接/认证失败 → 记负缓存，短时间内不再重试
            mark_node_unreachable(node)
        # 探测失败不写缓存：避免空结果被缓存 TTL 时长（10 分钟）导致前端长期「未探测到显卡」
        return []
    mark_node_reachable(node)
    # VM 配置里 hostpci 引用的设备集合（即使 VM 关机也算占用）：与网卡共用同一个助手
    pt_bds = _hostpci_bds()
    # 其中以 vGPU 切分（mdev=）方式挂给虚机的单独成集：这类是「vGPU 占用」而非物理直通
    mdev_bds = _hostpci_mdev_bds()
    gpus = []
    for line in r.stdout.splitlines():
        # lspci -nnD 真实格式: "0000:01:00.0 3D controller [0302]: NVIDIA ... [10de:1bb3] (rev a1)"
        # 设备类名(如 3D/VGA/Network)在地址后, class code 以 [xxxx] 形式出现在描述里,
        # 因此从整行提取 [xxxx] 形式的 4 位 hex 作为 class code, 匹配显示大类 03。
        cls_m = re.search(r"\[([0-9a-fA-F]{4})\]", line)
        if not cls_m or not cls_m.group(1).lower().startswith("03"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        addr = parts[0]
        # 去掉 "地址 类名 [class]:" 前缀, 只保留厂商型号描述(如 "NVIDIA Corporation GP104GL [Tesla P4]")
        desc = line.split("]:", 1)[1].strip() if "]: " in line else line.strip()
        driver = _read_gpu_driver(addr, base)
        bd = addr.split(".")[0].replace("0000:", "")
        # 「直通占用」只看 VM 配置：配置里删掉 hostpci 后，即使驱动还挂在 vfio-pci 上也不再算直通；
        # 那种情况标 vfio（宿主不可用但无人使用），前端显示为灰色「vfio 占用」
        gpus.append({"addr": addr, "desc": desc.strip(),
                     "driver": driver, "passthrough": bd in pt_bds,
                     "vgpu": bd in mdev_bds,
                     "vfio": driver == "vfio-pci"})
    _gpu_cache[node] = (now, gpus)
    return gpus


def _parse_dmi_memory(text: str) -> Any:
    """解析 dmidecode --type 17 输出，归纳为类型/频率/型号/已插条数/插槽总数"""
    modules, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Memory Device"):
            cur = {}
            modules.append(cur)
        elif cur is not None and ": " in line:
            k, v = line.split(": ", 1)
            cur[k] = v.strip()
    # 虚机场景：dmidecode --type 17 会为每个 vCPU NUMA 节点生成 Memory Device 条目，
    # Locator 形如 "DIMM #000" ~ "DIMM #127"，此时按 # 前基名去重避免虚报插槽数。
    # 物理机场景：每个 Memory Device 就是一根真实内存条；主板常把不同通道的条子都命名为
    # 同一 Locator（如双通道都叫 "DIMM 0"，仅 Bank Locator 区分 CHANNEL A/B），
    # 若按 Locator 去重会把两根真条子并成一根——故物理机（Locator 不含 #）不去重。
    seen = set()
    deduped = []
    for m in modules:
        loc = _clean_dmi(m.get("Locator")) or ""
        if "#" in loc:
            base = loc.split("#")[0].strip()
            if base in seen:
                continue
            seen.add(base)
        deduped.append(m)
    modules = deduped
    # 只留插了条的插槽
    used = [m for m in modules if _clean_dmi(m.get("Size"))]
    if not used:
        return {}
    m0 = used[0]
    sticks = []
    for m in used:
        # 品牌名：Manufacturer 字段常是 JEDEC 十六进制厂商 ID（如 04EF/U3E1）无意义，
        # 真正的品牌藏在 Part Number 里（如 TEAMGROUP-SD4-3200 -> TEAMGROUP）。
        # 优先从 Part Number 提取品牌，提取不到再退回 Manufacturer（但过滤掉纯十六进制 ID）。
        part = _clean_dmi(m.get("Part Number"))
        maker = _mem_brand(part, m.get("Manufacturer"))
        speed = _clean_dmi(m.get("Configured Memory Speed")) or _clean_dmi(m.get("Speed"))
        if speed and speed.split()[0].isdigit():
            speed = f"{speed.split()[0]} MT/s"
        else:
            speed = ""
        sticks.append({
            "locator": _clean_dmi(m.get("Locator")),
            "bank": _clean_dmi(m.get("Bank Locator")),
            "size": _clean_dmi(m.get("Size")),
            "maker": maker,
            "speed": speed,
        })
    out = {
        "mem_type": _clean_dmi(m0.get("Type")),
        "mem_speed": _clean_dmi(m0.get("Configured Memory Speed")) or _clean_dmi(m0.get("Speed")),
        "mem_modules": len(used),
        "mem_sticks": sticks,
    }
    if len(modules) >= len(used):
        out["mem_slots"] = len(modules)
    return out


def get_memory_hw(node: int) -> Any:
    """读内存条型号/频率：PVE API 不提供，只能 dmidecode；外节点走集群免密 SSH"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    if node == socket.gethostname().split(".")[0]:
        cmd = ["dmidecode", "--type", "17"]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *ssh_host_opts(node),
               f"root@{ssh_host(node)}", "export LC_ALL=C LANG=C; dmidecode --type 17"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_dmi_memory(proc.stdout)


def _parse_dmi_cpu(text: str) -> dict:
    """解析 dmidecode --type 4 输出，归纳为插槽总数/在位数/逐槽明细"""
    procs, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Processor Information"):
            cur = {}
            procs.append(cur)
        elif cur is not None and ": " in line:
            k, v = line.split(": ", 1)
            cur[k] = v.strip()
    # 按 Socket Designation 去重：dmidecode --type 4 会为每个逻辑处理器输出一条记录。
    # 物理机：同一插槽的多条记录 Socket Designation 完全相同，直接按 locator 去重。
    # 虚机：每个 vCPU 的 Socket Designation 形如 "CPU #NNN"（# 后序号不同），
    # 此时去掉 # 序号前缀，按槽位基名合并（所有 "CPU #*" 视为同一物理槽）。
    seen = set()
    sockets = []
    for p in procs:
        locator = _clean_dmi(p.get("Socket Designation")) or ""
        # 虚机场景：CPU #000 → 槽位基名 = "CPU"
        base = locator.split("#")[0].strip() if "#" in locator else locator
        if base in seen:
            continue
        seen.add(base)
        status = (p.get("Status") or "").lower()
        populated = "populated" in status and "unpopulated" not in status
        model = _clean_dmi(p.get("Version"))
        if model.lower() in _DMI_JUNK:
            model = ""
        if not populated and not model:
            continue
        cores, threads = p.get("Core Count"), p.get("Thread Count")
        speed = _clean_dmi(p.get("Current Speed"))
        if speed and speed.split()[0].isdigit() and int(speed.split()[0]) > 0:
            speed = f"{int(speed.split()[0])} MHz"
        else:
            speed = ""
        sockets.append({
            "locator": locator or f"CPU{len(sockets) + 1}",
            "populated": populated,
            "model": model,
            "speed": speed,
            "cores": int(cores) if cores and cores.isdigit() else None,
            "threads": int(threads) if threads and threads.isdigit() else None,
        })
    if not sockets:
        return {}
    return {
        "cpu_socket_total": len(sockets),
        "cpu_socket_used": sum(1 for s in sockets if s["populated"]),
        "cpu_sockets": sockets,
    }


def get_cpu_sockets(node: int) -> Any:
    """CPU 插槽数与逐槽明细：dmidecode --type 4；外节点走集群免密 SSH"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    if node == socket.gethostname().split(".")[0]:
        cmd = ["dmidecode", "--type", "4"]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *ssh_host_opts(node),
               f"root@{ssh_host(node)}", "export LC_ALL=C LANG=C; dmidecode --type 4"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_dmi_cpu(proc.stdout)


def get_board_hw(node: int) -> dict:
    """探测主板/芯片组型号与风扇控制能力（IPMI/可写 PWM）；外节点走集群免密 SSH"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    if node == socket.gethostname().split(".")[0]:
        cmd = ["sh", "-c", _BOARD_HW_SCRIPT]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *ssh_host_opts(node),
               f"root@{ssh_host(node)}", f"export LC_ALL=C LANG=C; {_BOARD_HW_SCRIPT}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    kv = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    board_vendor = kv.get("BOARD_VENDOR", "")
    board_product = kv.get("BOARD_PRODUCT", "")
    if board_vendor.lower() in _DMI_JUNK:
        board_vendor = ""
    if board_product.lower() in _DMI_JUNK:
        board_product = ""
    return {
        "board_vendor": board_vendor,
        "board_product": board_product,
        "chipset": kv.get("CHIPSET", ""),
        "has_ipmi": kv.get("HAS_IPMI") == "1",
        "has_pwm": kv.get("HAS_PWM") == "1",
        "virt": kv.get("VIRT", "") or "none",
    }
