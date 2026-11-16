"""节点硬件采集（自包含、零第三方依赖，供远端 SSH 执行）。

本模块只依赖 Python 标准库 + 节点自带 CLI（lspci / lsusb / ip / lsblk / pvesh /
zpool / pvs / findmnt），不 import 任何 core.* 包，也不需要 proxmoxer。

它被 server.py 的 _run_func_on_node 当作源码文本，经 SSH 直接喂给远端节点的
python3 执行（见 server.py），使「切换执行节点」时，即便远端没装本工具也能拿到真实
硬件列表。

模块内的函数名与 server.py 中同名函数一一对应；此处只保留「远端执行」所需的纯采集
实现（即 server.py 里 node!=None 分支要跑的逻辑），不含 _run_func_on_node 自身递归与
工具执行等无关逻辑。存储/备份类改用 pvesh CLI（与 backend 内部逻辑一致），避免引入
proxmoxer 依赖。
"""

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path


# ───────────────────────────── Intel 核显代次映射 ─────────────────────────────

# legacy_igd 档位：ok=通常可用 / rare=通常无效（成功率低，优先纯直通）/ no=不适用。
# 维护：本表与「硬件直通 → 显卡直通」脚本内的代次提示（tools_passthrough.py 的 case 分支）须同步。
INTEL_IGPU_GENERATIONS = (
    {
        "label": "Gen 2–5",
        "platforms": "i810 ~ G45 / Pineview（2004–2009 早期平台）、"
                     "Sandy Bridge / Ivy Bridge / Haswell / Broadwell（含 Ironlake、Bay Trail、Cherry Trail）",
        "legacy_igd": "ok",
        # 注意：ids 用 "字符串.split()" 构造，串内只能放设备 ID，不能写注释——
        # 注释行会被 split() 拆成 "#"、"/"、平台名等伪 ID 混进映射。
        "ids": frozenset("""
            1132 2562 2572 2582 258a 2592 2772 27a2 27ae 2972 2982 2992 29a2
            29b2 29c2 29d2 2a02 2a12 2a42 2e02 2e12 2e22 2e32 2e42 2e92
            3577 3582 358e 7121 7123 7125 a001 a011
            0102 0106 010a 0112 0116 0122 0126
            0152 0156 015a 0162 0166 016a
            0402 0406 040a 040b 040e 0412 0416 041a 041b 041e 0422 0426 042a 042b 042e
            0a02 0a06 0a0a 0a0b 0a0e 0a12 0a16 0a1a 0a1b 0a1e 0a22 0a26 0a2a 0a2b 0a2e
            0c02 0c06 0c0a 0c0b 0c0e 0c12 0c16 0c1a 0c1b 0c1e 0c22 0c26 0c2a 0c2b 0c2e
            0d02 0d06 0d0a 0d0b 0d0e 0d12 0d16 0d1a 0d1b 0d1e 0d22 0d26 0d2a 0d2b 0d2e
            1602 1606 160a 160b 160d 160e 1612 1616 161a 161b 161d 161e
            1622 1626 162a 162b 162d 162e 1632 1636 163a 163b 163d 163e
            0042 0046 0f30 0f31 0f32 0f33 22b0 22b1 22b2 22b3
        """.split()),
    },
    {
        "label": "Gen 6–9.5",
        "platforms": "Skylake / Kaby Lake / Amber Lake / Coffee Lake / Whiskey Lake / Comet Lake"
                     "（含 Apollo Lake、Gemini Lake、Cannon Lake）",
        "legacy_igd": "ok",
        "ids": frozenset("""
            1902 1906 190a 190b 190e 1912 1913 1915 1916 1917 191a 191b 191d 191e
            1921 1923 1926 1927 192a 192b 192d 1932 193a 193b 193d
            5902 5906 5908 590a 590b 590e 5912 5913 5915 5916 5917 591a 591b 591c
            591d 591e 5921 5923 5926 5927 593b 87c0 87ca
            3e90 3e91 3e92 3e93 3e94 3e96 3e98 3e99 3e9a 3e9b 3e9c
            3ea0 3ea1 3ea2 3ea3 3ea4 3ea5 3ea6 3ea7 3ea8 3ea9
            9b21 9b41 9ba2 9ba4 9ba5 9ba8 9baa 9bac 9bc2 9bc4 9bc5 9bc6 9bc8
            9bca 9bcc 9be6 9bf6
            0a84 1a84 1a85 5a84 5a85 3184 3185
            5a40 5a41 5a42 5a44 5a49 5a4a 5a4c 5a50 5a51 5a52 5a54 5a59 5a5a 5a5c
        """.split()),
    },
    {
        "label": "Gen 11–12",
        "platforms": "Ice Lake / Elkhart Lake / Jasper Lake / Tiger Lake / Rocket Lake /"
                     " Alder Lake / Raptor Lake / Meteor Lake",
        "legacy_igd": "rare",
        "ids": frozenset("""
            8a50 8a51 8a52 8a53 8a54 8a56 8a57 8a58 8a59 8a5a 8a5b 8a5c 8a5d 8a70 8a71
            4541 4551 4555 4557 4570 4571
            4e51 4e55 4e57 4e61 4e71
            9a40 9a49 9a59 9a60 9a68 9a70 9a78 9ac0 9ac9 9ad9 9af8
            4c80 4c8a 4c8b 4c8c 4c90 4c9a
            4680 4682 4688 468a 468b 4690 4692 4693
            4626 4628 462a 4636 4638 463a
            46a0 46a1 46a2 46a3 46a6 46a8 46aa
            46b0 46b1 46b2 46b3 46b6 46b8 46ba
            46c0 46c1 46c2 46c3
            46d0 46d1 46d2 46d3 46d4
            9840 9841
            a720 a721 a780 a781 a782 a783 a788 a789 a78a a78b
            a7a0 a7a1 a7a8 a7a9 a7aa a7ab a7ac a7ad
            7d40 7d41 7d45 7d51 7d55 7d60 7d67 7dd1 7dd5
        """.split()),
    },
    {
        "label": "Xe2",
        "platforms": "Lunar Lake 及更新平台的 Arc 架构核显",
        "legacy_igd": "no",
        "ids": frozenset("""
            64a0 64b0
        """.split()),
    },
)

# 设备 ID → 代次条目 的反查索引（模块加载时构建一次）
_INTEL_IGPU_INDEX = {i: g for g in INTEL_IGPU_GENERATIONS for i in g["ids"]}


def intel_igpu_generation(dev_id):
    """按 PCI 设备 ID（'9bc4' / '0x9bc4' 均可）查 Intel 核显代次；未收录（更新的平台）返回 None。"""
    key = str(dev_id or "").strip().lower()
    if key.startswith("0x"):
        key = key[2:]
    return _INTEL_IGPU_INDEX.get(key.zfill(4))


# 注意不能只按 "arc" 判定：Meteor Lake / Lunar Lake 的核显在 lspci 里写作「Intel Arc Graphics」，
# 只匹配 "arc" 会把它们误判成独显 → 用独显专有写法 "arc a<数字>" / "arc b<数字>" / "arc pro"。
_INTEL_DGPU_RE = re.compile(
    r"\barc a\d|\barc b\d|\barc pro\b|\bbattlemage\b|\bdata center gpu\b"
    r"|\bponte vecchio\b|\bdg1\b|\bdg2\b|\bats-m\b"
)
# AMD 独显特征：
#   RX 系列 / Radeon Pro/VII、Vega 独显核心与 Frontier、Navi/Polaris/Tonga/Fiji/Hawaii 代号、
#   FirePro/Instinct/WX 专业卡
_AMD_DGPU_RE = re.compile(
    r"\brx \d|\bradeon pro\b|\bradeon vii\b|\bnavi \d|\bpolaris\b|\btonga\b|\bfiji\b|\bhawaii\b"
    r"|\bvega (?:10|12|20|56|64)\b|\bfrontier\b|\bfirepro\b|\binstinct\b|\bwx \d|\br[579] \d"
    r"|\bhd [5-9]\d{3}\b"
)
# AMD 核显（APU 集显）特征：
#   通用集显命名（Radeon Graphics / Radeon Vega 3~11 这类 APU 集显 / 移动核显 660M~890M）
#   + 各代 APU 代号（Raven ~ Strix/Krackan，含桌面 APU Raphael/Granite Ridge）
_AMD_IGPU_RE = re.compile(
    r"\bradeon graphics\b|\bradeon vega \d{1,2}\b|\bradeon \d{3}m\b"
    r"|\b(?:raven|picasso|renoir|lucienne|cezanne|barcelo|van gogh|rembrandt|phoenix|hawk point"
    r"|mendocino|raphael|dragon range|granite ridge|strix|krackan)\b"
)


def _amd_igpu_by_id(dev_id):
    """AMD APU 核显的 PCI 设备 ID 段：0x9500–0x9FFF（Llano~Trinity/Richland/Kabini/Carrizo 等老 APU）、
    0x1300–0x16FF（Kaveri 之后的现代 APU：Raven/Renoir/Cezanne/Rembrandt/Phoenix/Strix 全系）。
    AMD 独显集中在 0x66xx/0x67xx/0x68xx/0x69xx/0x73xx/0x74xx 等段，与核显段不重叠。"""
    try:
        d = int(dev_id, 16)
    except ValueError:
        return False
    return 0x9500 <= d <= 0x9FFF or 0x1300 <= d <= 0x16FF


def _gpu_vendor_device(desc):
    """从 lspci 描述取 (vendor id, device id)，均为小写；pci.ids 未收录设备时按厂商名兜底，
    此时 device id 为空串（仅够做厂商分类，不足以查设备 ID 表）。"""
    low = (desc or "").lower()
    ids = re.findall(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", desc or "")
    if ids:
        return ids[-1][0].lower(), ids[-1][1].lower()
    if "intel" in low:
        return "8086", ""
    if "amd" in low or "ati" in low:
        return "1002", ""
    return "", ""


def _gpu_kind(addr, desc):
    """判别独显/核显。Intel 除 Arc/数据中心卡外一律核显；AMD 依次按「独显特征 → 核显特征 →
    APU 核显设备 ID 段 → 根总线位」判定，覆盖 Cezanne/Rembrandt/Phoenix/Strix 等 APU 核显，
    避免它们的 lspci 名称（如「Cezanne [Radeon Vega Series]」）不含 "Radeon Graphics" 而被误判为独显。"""
    low = (desc or "").lower()
    vendor, dev_id = _gpu_vendor_device(desc)
    if vendor == "8086":
        return "dgpu" if _INTEL_DGPU_RE.search(low) else "igpu"
    if vendor == "1002":
        if _AMD_DGPU_RE.search(low):
            return "dgpu"
        if _AMD_IGPU_RE.search(low) or _amd_igpu_by_id(dev_id):
            return "igpu"
        # 兜底：其余 AMD 显示设备挂在根总线（bus 00）上多为 APU 核显，其余按独显处理
        return "igpu" if addr.split(":")[1] == "00" else "dgpu"
    return "dgpu"


# 已知直通易报 Code 43 的显卡型号 -> 推荐 hostpci 参数与说明。
# 匹配用 vendor:device（lspci desc 里的 [1002:67e3]），值为 {opts, hint}。
# AMD Polaris 专业卡（WX 4100 等）：SeaBIOS + 无 pcie=1 时驱动初始化失败报 Code 43，
#   实测需 pcie=1 + x-vga=1 + cpu hidden=1 才正常。
# NVIDIA 消费卡（GTX/RTX 等）：驱动主动检测虚拟机，需 cpu hidden 隐藏 KVM，部分还需 UEFI + vBIOS。
_GPU_PASSTHROUGH_HINTS = {
    "1002:67e3": {  # AMD Radeon Pro WX 4100 (Polaris 11)
        "opts": "pcie=1",
        "hint": "该卡直通易报 Code 43，建议加 pcie=1、设为主显卡(x-vga=1)，并给 CPU 加 hidden=1 隐藏虚拟化",
    },
}


def _gpu_passthrough_hint(addr, desc):
    ids = re.findall(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", desc)
    if not ids:
        return None
    vd = f"{ids[-1][0].lower()}:{ids[-1][1].lower()}"
    return _GPU_PASSTHROUGH_HINTS.get(vd)


# 通用 PCIe 直通排除的大类：01 存储/02 网卡/03 显卡已有专用工具；06 桥接（主板桥/PCI 桥不可直通）、
# 08 系统外设（HPET/IOMMU）、0b 处理器类直通无意义且危险，一并隐藏避免误选
_PCI_GENERIC_EXCLUDE = {"01", "02", "03", "06", "08", "0b"}

# 已知 PCIe 直通（vfio）MSI-X 布局不兼容、QEMU 启动报
# "failed to add PCI capability 0x11 ... table & pba overlap" 的 NVMe 设备 vendor:device。
# 这些设备建议改用块映射 RDM（不经过 vfio/MSI-X，规避该硬件兼容问题）。
_PCI_MSIX_INCOMPAT = {
    "8086:f1a6",   # Intel SSD DC P4101/Pro 7600p/760p/E 6100p Series（实测直通失败）
}

# PCI 类代码 → 中文类别标签（长前缀在前，避免 "04" 抢匹配 "0401"）
_PCI_CLASS_TAGS = (
    ("0104", "【RAID】"), ("0106", "【SATA 控制器】"), ("0107", "【SAS 控制器】"),
    ("0108", "【NVMe 控制器】"), ("01", "【存储控制器】"),
    ("0200", "【有线网卡】"), ("0280", "【无线网卡】"), ("02", "【网络设备】"),
    ("0300", "【显卡】"), ("0302", "【3D 显卡】"), ("03", "【显示设备】"),
    ("0401", "【声卡】"), ("0403", "【声卡】"), ("04", "【多媒体】"),
    ("0604", "【PCI 桥】"), ("0600", "【主板桥】"), ("06", "【桥接】"),
    ("0c03", "【USB 控制器】"), ("0c", "【串行总线】"),
    ("0703", "【串口】"), ("07", "【通信控制器】"),
    ("08", "【系统外设】"), ("0b", "【处理器类】"), ("10", "【加密设备】"), ("12", "【处理加速器】"),
)


def _pci_class_tag(classcode):
    """按 PCI 类代码给出类别标签（直通下拉里区分设备类型，避免只看地址猜）。"""
    cc = (classcode or "").lower()
    for pref, label in _PCI_CLASS_TAGS:
        if cc.startswith(pref):
            return label
    return ""


def _iommu_group_size(addr):
    """该 BDF 所在 IOMMU 隔离组内的设备数；>1 表示直通它会连带同组其它设备。无 IOMMU 时返回 0。"""
    try:
        dev = Path(f"/sys/bus/pci/devices/{addr}/iommu_group/devices")
        if dev.is_dir():
            return len(list(dev.iterdir()))
    except OSError:
        return 0
    return 0


def _list_pci_class(class_prefix):
    """列出指定大类 PCIe 设备（01=存储，02=网络，03=显示），供直通下拉选择。
    lspci -nnD 真实格式: "<addr> <设备类名> [<classcode>]: <厂商型号> [vendor:device] ..."
    例: 0000:01:00.0 3D controller [0302]: NVIDIA Corporation GP104GL [Tesla P4] [10de:1bb3]
    设备类名(如 3D/VGA)在地址后, class code 以 [xxxx] 形式出现在描述里,
    故从整行提取 [xxxx] 形式的 4 位 hex 作为 class code, 匹配大类前缀。"""
    try:
        out = subprocess.run(["lspci", "-nnD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    result = []
    for line in out.stdout.splitlines():
        cls_m = re.search(r"\[([0-9a-fA-F]{4})\]", line)
        if not cls_m or not cls_m.group(1).lower().startswith(class_prefix):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        addr = parts[0]
        # 去掉 "地址 类名 [class]:" 前缀, 只保留厂商型号描述(如 "NVIDIA Corporation GP104GL [Tesla P4]")
        desc = line.split("]:", 1)[1].strip() if "]: " in line else line.strip()
        result.append({"addr": addr, "desc": desc,
                       "tag": _pci_class_tag(cls_m.group(1)), "group": _iommu_group_size(addr)})
    return result


def _pci_storage_host_used():
    """返回承载了宿主占用盘的 PCIe 存储控制器地址集合（0000:xx:xx.x）。

    系统盘所在的 NVMe/SATA 控制器一旦直通，宿主机立即掉系统盘，必须拦截。
    通过 /sys/block/<dev>/device 软链反查块设备所属 PCI 地址。"""
    used_pci = set()
    host_disks = _host_used_disk_names()
    if not host_disks:
        return used_pci
    for dev in host_disks:
        link = Path("/sys/block") / dev / "device"
        try:
            path = str(link.resolve())
        except OSError:
            continue
        m = re.search(r"0000:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]", path)
        if m:
            used_pci.add(m.group(0).lower())
    return used_pci


def list_pci_gpus():
    """可直通的 PCIe 显示设备（远端采集版本，无 node 参数）。"""
    gpus = _list_pci_class("03")
    for d in gpus:
        d["kind"] = _gpu_kind(d["addr"], d["desc"])
        drv = Path(f"/sys/bus/pci/devices/{d['addr']}/driver")
        try:
            d["driver"] = drv.resolve().name if drv.exists() else ""
        except OSError:
            d["driver"] = ""
        rec = _gpu_passthrough_hint(d["addr"], d["desc"])
        if rec:
            d["recommend"] = rec
        # Intel 核显：附代次（只取可 JSON 序列化的字段，不含内部 ids 集合）
        if d["kind"] == "igpu":
            vendor, dev_id = _gpu_vendor_device(d["desc"])
            gen = intel_igpu_generation(dev_id) if vendor == "8086" else None
            if gen:
                d["igpu_gen"] = {"label": gen["label"], "platforms": gen["platforms"],
                                 "legacy_igd": gen["legacy_igd"]}
    return gpus


def list_pci_storage():
    """可直通的 PCIe 存储设备（远端采集版本）。"""
    result = _list_pci_class("01")
    host_used = _pci_storage_host_used()
    for d in result:
        m = re.search(r"\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]", d.get("desc", ""))
        if m and m.group(1).lower() in _PCI_MSIX_INCOMPAT:
            d["compat"] = False
            d["compat_hint"] = "该设备 MSI-X 布局与 QEMU 直通不兼容，建议改用块映射（RDM）"
        if d.get("addr", "").lower() in host_used:
            d["host_used"] = True
    return result


def _host_used_ifnames():
    """返回宿主占用的物理网口名集合：有 IP 的网口 + vmbr 桥接成员 + bond 成员。

    这些网口直通后宿主会断网或丢失管理通道，必须置灰 + 拦截。"""
    used = set()
    try:
        out = subprocess.run(["ip", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            # 形如：2: eno1    inet 192.168.1.10/24 ...
            m = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:", line)
            if m and " inet " in line:
                used.add(m.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    # vmbr 桥接成员 / bond 成员：读 /etc/network/interfaces 的 bridge-ports 与 bond-slaves
    try:
        text = Path("/etc/network/interfaces").read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"^\s*(?:bridge-ports|bond-slaves)\s+(\S+)", text, re.M):
            for port in m.group(1).split():
                used.add(port)
    except OSError:
        pass
    return used


def _nic_link_speed(ifname):
    """读取物理网口当前协商速率（Mb/s）：读 /sys/class/net/<if>/speed。
    链路 down、驱动不支持或接口已被 vfio-pci 剥离（已直通给 VM）时读不到，返回 0。"""
    if not ifname:
        return 0
    try:
        val = int((Path("/sys/class/net") / ifname / "speed").read_text().strip())
    except (OSError, ValueError):
        return 0
    return val if val > 0 else 0


def list_pci_nics():
    """可直通的 PCIe 网卡，标注宿主物理接口名与是否被宿主占用（有 IP/桥接成员/默认路由出口）。"""
    nics = _list_pci_class("02")
    default_dev = ""
    try:
        out = subprocess.run(["ip", "-o", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5)
        m = re.search(r"dev (\S+)", out.stdout)
        if m:
            default_dev = m.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    host_used = _host_used_ifnames()
    if default_dev:
        host_used.add(default_dev)
    addr_if = {}
    net_dir = Path("/sys/class/net")
    if net_dir.is_dir():
        for ifc in sorted(net_dir.iterdir()):
            name = ifc.name
            if name == "lo" or name.startswith(("vmbr", "tap", "veth", "fwbr",
                                                "fwln", "fwpr", "bond", "vxlan")):
                continue
            try:
                addr = (ifc / "device").resolve().name
            except OSError:
                continue
            addr_if[addr] = name
    for d in nics:
        ifname = addr_if.get(d["addr"], "")
        d["ifname"] = ifname
        d["in_use"] = bool(ifname and ifname in host_used)
        d["wireless"] = (d.get("tag") == "【无线网卡】")
        d["speed"] = _nic_link_speed(ifname)
    return nics


def list_pci_generic():
    """其余可直通 PCIe 设备（采集卡/声卡/USB 控制器等，供通用直通下拉）。"""
    try:
        out = subprocess.run(["lspci", "-nnD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    # 扫全部 VM 配置的 hostpci 行，收集已直通设备地址 → vmid 列表
    used = {}
    qdir = Path("/etc/pve/qemu-server")
    if qdir.is_dir():
        for conf in sorted(qdir.glob("*.conf")):
            try:
                text = conf.read_text(errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"^hostpci\d+:\s*([0-9a-fA-F:.]+)", text, re.M):
                used.setdefault(m.group(1).lower(), []).append(conf.stem)
    result = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        addr, desc = parts
        cls = re.search(r"\[([0-9a-fA-F]{4})\]", desc)
        if not cls or cls.group(1).lower()[:2] in _PCI_GENERIC_EXCLUDE:
            continue
        drv = Path(f"/sys/bus/pci/devices/{addr}/driver")
        try:
            driver = drv.resolve().name if drv.exists() else ""
        except OSError:
            driver = ""
        result.append({"addr": addr, "desc": desc, "driver": driver,
                       "used_by": ",".join(used.get(addr.lower(), [])),
                       "tag": _pci_class_tag(cls.group(1)), "group": _iommu_group_size(addr)})
    return result


def list_vfio_orphans():
    """列出「没人用、但宿主机也用不了」的 PCI 设备，供「回收直通设备」工具处理。

    两类：
      1. state="vfio"   —— 驱动还挂在 vfio-pci 上（删掉 hostpci 后 PVE 不会自动归还）；
      2. state="unbound"—— 已被解绑但没装回驱动（上次释放时原驱动绑失败/未装），且有候选驱动可装回。
    都不含「正被某台 VM 的 hostpci 引用」的设备。
    """
    try:
        out = subprocess.run(["lspci", "-nnD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    # 被 VM 直通的 bus:device 集合（/etc/pve 是集群共享的 pmxcfs，任意节点读到的都一致）
    used_bd = set()
    qdir = Path("/etc/pve/qemu-server")
    if qdir.is_dir():
        for conf in sorted(qdir.glob("*.conf")):
            try:
                text = conf.read_text(errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"^hostpci\d+:\s*([0-9a-fA-F:.]+)", text, re.M):
                bdf = m.group(1).split(",")[0].strip()
                used_bd.add(re.sub(r"\.\d+$", "", bdf).replace("0000:", ""))
    # 原驱动模块：lspci -Dknn 的 "Kernel modules:" 行（取第一个非 vfio-pci 的）
    mods_by_addr = {}
    try:
        knn = subprocess.run(["lspci", "-Dknn"], capture_output=True, text=True, timeout=10)
        cur = ""
        for ln in knn.stdout.splitlines():
            m = re.match(r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.\d)\s", ln)
            if m:
                cur = m.group(1)
                continue
            mm = re.match(r"\s*Kernel modules:\s*(.+)", ln)
            if mm and cur:
                mods_by_addr[cur] = [x.strip() for x in mm.group(1).split(",") if x.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    # modprobe.d 里被 blacklist 的模块：原驱动被屏蔽时重启后不会自动加载
    blacklisted = set()
    md = Path("/etc/modprobe.d")
    if md.is_dir():
        for f in md.glob("*.conf"):
            try:
                for ln in f.read_text(errors="ignore").splitlines():
                    mm = re.match(r"\s*blacklist\s+(\S+)", ln)
                    if mm:
                        blacklisted.add(mm.group(1))
            except OSError:
                continue
    result = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        addr, desc = parts
        drv = Path(f"/sys/bus/pci/devices/{addr}/driver")
        try:
            driver = drv.resolve().name if drv.exists() else ""
        except OSError:
            driver = ""
        if driver not in ("vfio-pci", ""):
            continue                       # 宿主驱动正常持有 → 不用管
        if addr.split(".")[0].replace("0000:", "") in used_bd:
            continue                       # 正被某台 VM 直通 → 不能动
        # 候选原驱动：去掉 vfio-pci；framebuffer/legacy 类排到最后（多数场景不想用，且常绑不上独显）
        low = ("nvidiafb", "nouveau", "efifb", "vesafb", "simpledrm", "vga16fb", "uvesafb")
        mods = [m for m in mods_by_addr.get(addr, []) if m != "vfio-pci"]
        cands = [m for m in mods if m not in low] + [m for m in mods if m in low]
        if not driver and not cands:
            continue                       # 无驱动且没有可装回的驱动（多为本来就不需要驱动的控制类设备）
        orig = cands[0] if cands else ""
        ids = re.findall(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", desc)
        name = desc.split("]: ", 1)[-1] if "]: " in desc else desc
        name = re.sub(r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]\s*$", "", name).strip()
        try:
            override = Path(f"/sys/bus/pci/devices/{addr}/driver_override").read_text(
                errors="ignore").strip()
        except OSError:
            override = ""
        if override == "(null)":           # 内核在"未设置"时可能读回 (null)
            override = ""
        result.append({"addr": addr, "name": name, "desc": desc,
                       "state": "vfio" if driver else "unbound",
                       "orig_driver": orig, "candidates": cands,
                       "orig_blacklisted": bool(orig and orig in blacklisted),
                       "vd": f"{ids[-1][0].lower()}:{ids[-1][1].lower()}" if ids else "",
                       "driver_override": override})
    result.sort(key=lambda d: d["addr"])
    return result


def list_usb_devices():
    """可直通的 USB 设备列表（vid:pid），跳过根集线器，并标注已被哪些 VM 直通占用。"""
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    # 扫描全部 VM 配置，收集已直通的 USB 设备（host=vid:pid → vmid 列表）
    used = {}
    qdir = Path("/etc/pve/qemu-server")
    if qdir.is_dir():
        for conf in sorted(qdir.glob("*.conf")):
            try:
                text = conf.read_text(errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"^usb\d+:.*?host=([0-9a-fA-F]{4}:[0-9a-fA-F]{4})",
                                 text, re.M):
                used.setdefault(m.group(1).lower(), []).append(conf.stem)
    result = []
    for line in out.stdout.splitlines():
        m = re.match(r"^Bus (\d+) Device \d+: ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)$",
                     line.strip())
        if not m:
            continue
        if m.group(2).lower() == "1d6b":   # Linux Foundation 根集线器，非物理外设
            continue
        key = f"{m.group(2).lower()}:{m.group(3).lower()}"
        result.append({
            "id": key,
            "desc": m.group(4).strip() or "未知设备",
            "bus": m.group(1),
            "used_by": sorted(used.get(key, [])),
        })
    return result


def _parse_interfaces():
    """解析 /etc/network/interfaces 的 iface 段，返回 接口名 -> {address, ports, raw}"""
    try:
        text = Path("/etc/network/interfaces").read_text(errors="ignore")
    except OSError:
        return {}
    stanzas = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("iface "):
            parts = s.split()
            cur = parts[1] if len(parts) >= 2 else None
            if cur:
                stanzas[cur] = {"address": "", "ports": "", "raw": ""}
        elif cur and s.startswith("address "):
            stanzas[cur]["address"] = s.split(None, 1)[1]
        elif cur and s.startswith("bridge-ports "):
            stanzas[cur]["ports"] = s.split(None, 1)[1]
        elif cur and s.startswith("vlan-raw-device "):
            stanzas[cur]["raw"] = s.split(None, 1)[1]
    return stanzas


def list_network_bridges():
    """vmbr 网桥列表（名称/IP/桥接网口/在用 VM）。"""
    stanzas = _parse_interfaces()
    used = {}
    qdir = Path("/etc/pve/qemu-server")
    if qdir.is_dir():
        for conf in sorted(qdir.glob("*.conf")):
            try:
                text = conf.read_text(errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"^net\d+:.*?bridge=(\S+?)(?:,|\s|$)", text, re.M):
                used.setdefault(m.group(1), []).append(conf.stem)
    out = []
    for name in sorted(n for n in stanzas if n.startswith("vmbr")):
        d = stanzas[name]
        out.append({"name": name, "address": d["address"], "ports": d["ports"],
                    "used_by": ",".join(used.get(name, []))})
    return out


def list_network_vlans():
    """VLAN 接口列表（X.N 形式，含 IP 与所属设备）。"""
    stanzas = _parse_interfaces()
    out = []
    for name in sorted(n for n in stanzas
                       if re.match(r"^[A-Za-z0-9_-]+\.\d{1,4}$", n)):
        d = stanzas[name]
        out.append({"name": name, "address": d["address"], "raw": d["raw"]})
    return out


def list_phys_nics():
    """物理网口列表（排除 lo/虚拟接口/VLAN 子接口），供创建网桥选 bridge-ports。"""
    try:
        out = subprocess.run(["ip", "-o", "link"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    result = []
    for line in out.stdout.splitlines():
        m = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:", line)
        if not m:
            continue
        name = m.group(1)
        if name == "lo" or "." in name or name.startswith(
                ("vmbr", "tap", "veth", "fwbr", "fwln", "fwpr", "vxlan", "vnet")):
            continue
        result.append({"name": name})
    return result


def _phys_disk_of(dev_path):
    """把分区/卷路径反查成所属物理磁盘名（如 sda）；查不到返回空字符串。

    用于从 /dev/sda3、/dev/mapper/pve-root 等反查它属于哪块物理盘。"""
    if not dev_path or not dev_path.startswith("/dev/"):
        return ""
    try:
        out = subprocess.run(["lsblk", "-no", "PKNAME", dev_path],
                             capture_output=True, text=True, timeout=5)
        lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
        if lines and lines[0]:
            return lines[0]
        # 本身已是整盘（无父设备），直接取设备名
        return dev_path.rsplit("/", 1)[-1]
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _zfs_pool_disks(pool):
    """返回 ZFS 池（如 PVE 默认的 rpool）的成员物理磁盘名集合。"""
    disks = set()
    try:
        out = subprocess.run(["zpool", "list", "-v", "-H", "-P", pool],
                             capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            for col in line.split():
                if col.startswith("/dev/"):
                    d = _phys_disk_of(col)
                    if d:
                        disks.add(d)
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return disks


def _pve_system_disk_names():
    """返回 PVE 系统盘所在的物理磁盘名集合（精确识别，宁可多判不可漏判）。

    漏判=用户能格式化掉 PVE 自身，后果严重，因此多种方式交叉验证，任意命中即视为系统盘：
      1. 关键挂载点 /、/boot、/boot/efi 所在的物理盘
      2. LVM：卷组名为 pve（PVE 默认）的物理卷所在盘
      3. ZFS：根数据集所在池（rpool/ROOT/pve-1）的成员盘
    """
    disks = set()

    # 1) 关键挂载点所在盘
    for mp in ("/", "/boot", "/boot/efi"):
        try:
            out = subprocess.run(["findmnt", "-no", "SOURCE", "--target", mp],
                                 capture_output=True, text=True, timeout=5)
            src = [(l or "").strip() for l in (out.stdout or "").splitlines()]
        except (OSError, subprocess.TimeoutExpired):
            src = []
        if not src or not src[0]:
            continue
        s = src[0]
        if s.startswith("/dev/"):
            d = _phys_disk_of(s)
            if d:
                disks.add(d)
        elif "/" in s:
            # ZFS 数据集（形如 rpool/ROOT/pve-1）：取池名查成员盘
            disks.update(_zfs_pool_disks(s.split("/", 1)[0]))

    # 2) LVM：卷组名为 pve（PVE 默认卷组）的物理卷
    try:
        out = subprocess.run(["pvs", "--noheadings", "-o", "PV_NAME,VG_NAME"],
                             capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[1] == "pve":
                d = _phys_disk_of(cols[0])
                if d:
                    disks.add(d)
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return disks


def _pve_active_vgs():
    """返回 PVE 真正在用的 LVM 卷组名集合。

    PVE 安装时默认卷组名为 pve；此外任何含有「已挂载逻辑卷」的卷组也算在用。
    关键：机器上若插着其他系统（飞牛/群晖/其他 Linux）的盘，它们的卷组内核同样能识别，
    但并未被 PVE 挂载使用——不能算 PVE 在用，否则会把人家的盘误置灰。"""
    vgs = {"pve"}
    try:
        out = subprocess.run(["lvs", "--noheadings", "-o", "vg_name,lv_path"],
                             capture_output=True, text=True, timeout=5)
        lvs = []
        for line in (out.stdout or "").splitlines():
            cols = line.split()
            if len(cols) >= 2:
                lvs.append((cols[0], cols[1]))
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return vgs
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        mounts = ""
    for vg, path in lvs:
        if path and path in mounts:
            vgs.add(vg)
    return vgs


def _pve_storage_disk_names():
    """返回 PVE 真正在用的存储盘（不含系统盘）。

    判定依据是「PVE 是否实际在用」，而不是「内核能否识别其元数据」：
      1. 有挂载点的盘（PVE 已挂载）
      2. PVE 活跃卷组的物理卷（见 _pve_active_vgs）
      3. PVE 已导入的 ZFS 池成员（zpool list 能列出即已导入）
    """
    disks = set()
    # 1) 有挂载点的盘
    try:
        out = subprocess.run(["lsblk", "-lno", "NAME,MOUNTPOINTS"],
                             capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            cols = line.split(None, 1)
            if len(cols) < 2 or not cols[1].strip():
                continue
            d = _phys_disk_of("/dev/" + cols[0])
            if d:
                disks.add(d)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # 2) PVE 活跃卷组的物理卷
    active = _pve_active_vgs()
    try:
        out = subprocess.run(["pvs", "--noheadings", "-o", "pv_name,vg_name"],
                             capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[1] in active:
                d = _phys_disk_of(cols[0])
                if d:
                    disks.add(d)
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # 3) PVE 已导入的 ZFS 池成员
    try:
        out = subprocess.run(["zpool", "list", "-H", "-o", "name"],
                             capture_output=True, text=True, timeout=5)
        for pool in (out.stdout or "").splitlines():
            pool = pool.strip()
            if pool:
                disks.update(_zfs_pool_disks(pool))
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return disks


# GPT 分区类型 GUID：EFI 系统分区 / Microsoft 保留 / Windows 恢复环境
_EFI_PART_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
_MS_RESERVED_TYPE = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
_WIN_RECOVERY_TYPE = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"


def _other_os_disk_names():
    """启发式返回「可能装有其他系统」的物理磁盘名集合。

    只在分区表层面判断，不挂载、不读文件内容（安全且无副作用）：
      - 存在 EFI 系统分区（ESP），但不是 PVE 当前挂载的 /boot/efi
      - 存在 NTFS 分区（Windows 系统盘或数据盘）
      - 存在 Microsoft 保留 / Windows 恢复分区

    注意：这是启发式判断，可能误判（例如纯 NTFS 数据盘也会被标记），
    因此前端对它「只警告不禁止」，用户确认后仍可格式化。
    """
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,PARTTYPE,MOUNTPOINTS"],
            capture_output=True, text=True, timeout=10)
        tree = json.loads(out.stdout or "{}").get("blockdevices", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return set()

    # PVE 自己使用的 EFI 分区，不算「其他系统」
    pve_esp = ""
    try:
        o = subprocess.run(["findmnt", "-no", "SOURCE", "--target", "/boot/efi"],
                           capture_output=True, text=True, timeout=5)
        pve_esp = (o.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    found = set()
    for dev in tree:
        if dev.get("type") != "disk":
            continue
        name = dev.get("name") or ""
        if not name:
            continue
        hit = False
        # 整盘本身（无分区表时）与其各分区都要检查
        for t in [dev] + list(dev.get("children") or []):
            fstype = (t.get("fstype") or "").lower()
            ptype = (t.get("parttype") or "").lower()
            t_dev = "/dev/" + (t.get("name") or "")
            # ESP 但不是 PVE 当前用的那个 → 别的系统装的
            if ptype == _EFI_PART_TYPE and t_dev != pve_esp:
                hit = True
            if ptype in (_MS_RESERVED_TYPE, _WIN_RECOVERY_TYPE):
                hit = True
            # NTFS（Windows）或 LVM/ZFS/RAID 元数据：其他系统/存储池的痕迹
            # （飞牛、群晖、其他 Linux 的存储池在 PVE 眼里同样是 LVM/ZFS 成员）
            if fstype in ("ntfs", "lvm2_member", "zfs_member", "linux_raid_member"):
                hit = True
        if hit:
            found.add(name)
    return found


def _host_used_disk_names():
    """返回宿主占用（有挂载点 / LVM / ZFS / RAID 成员）的整块磁盘设备名集合。

    系统盘与 PVE 数据存储盘都有挂载点或文件系统元数据，直通它们会毁掉宿主机；
    据此在磁盘直通下拉里置灰，脚本再兜底拦截。"""
    used = set()
    try:
        out = subprocess.run(
            ["lsblk", "-bJ", "-o", "NAME,TYPE,FSTYPE,MOUNTPOINTS"],
            capture_output=True, text=True, timeout=10)
        tree = json.loads(out.stdout or "{}").get("blockdevices", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return used

    def walk(dev):
        # 递归收集该设备自身及所有子孙的 fstype 与挂载点
        fstypes = []
        mounts = []
        if dev.get("fstype"):
            fstypes.append(dev["fstype"].lower())
        for m in dev.get("mountpoints") or []:
            if m:
                mounts.append(m)
        for c in dev.get("children") or []:
            cf, cm = walk(c)
            fstypes += cf
            mounts += cm
        return fstypes, mounts

    for dev in tree:
        if dev.get("type") != "disk":
            continue
        name = dev.get("name") or ""
        if not name:
            continue
        fstypes, mounts = walk(dev)
        # 有挂载点（/、/boot、/var/lib/vz 等）或 LVM/ZFS/RAID 元数据 = 宿主占用
        if mounts or any(ft in ("lvm2_member", "zfs_member", "linux_raid_member")
                         for ft in fstypes):
            used.add(name)
    return used


def list_phys_disks():
    """物理磁盘列表（远端采集版本）。"""
    try:
        out = subprocess.run(["lsblk", "-dn", "-o", "NAME,TYPE,SIZE,MODEL"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    pve_sys = _pve_system_disk_names()
    pve_store = _pve_storage_disk_names()
    other_os = _other_os_disk_names()
    result = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 2 or parts[1] != "disk":
            continue
        if parts[0].startswith(("nbd", "loop", "ram", "zram", "dm-")):
            continue  # 排除 nbd（PVE 挂载镜像产生的网络块设备）/loop/ram/dm 虚拟设备
        name = parts[0]
        is_sys = name in pve_sys
        # PVE 存储盘：确实被 PVE 在用（挂载/自身卷组/已导入池），且不是系统盘
        is_store = name in pve_store and not is_sys
        result.append({
            "name": name,
            "size": parts[2] if len(parts) > 2 else "",
            "model": parts[3].strip() if len(parts) > 3 else "",
            # 兼容旧字段：PVE 系统盘或存储盘 = 真正被 PVE 占用
            "host_used": is_sys or is_store,
            "pve_system": is_sys,
            "pve_data": is_store,
            # 其他系统/数据：盘上有 OS 或存储池痕迹，但并非 PVE 在用（如装着飞牛的盘）
            "other_os": name in other_os and not is_sys and not is_store,
        })
    return result


def list_byid_disks():
    """列出可用于直通的整块磁盘 by-id 名（排除分区/设备映射），标注是否被 PVE 占用。

    注意：不能用「盘上有 LVM/ZFS 元数据」来判断——其他系统（如飞牛）的存储池在 PVE 眼里
    同样是 LVM/ZFS 成员，但并未被 PVE 使用，不该锁死不给直通。"""
    try:
        out = subprocess.run(["lsblk", "-dnJ", "-o", "NAME,TYPE,SIZE"],
                             capture_output=True, text=True, timeout=10)
        disks = {d["name"]: d.get("size", "")
                 for d in json.loads(out.stdout or "{}").get("blockdevices", [])
                 if d.get("type") == "disk"}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        disks = {}
    # 与「磁盘分区与格式化」保持一致：只把 PVE 真正在用的盘判为占用并置灰
    host_used = _pve_system_disk_names() | _pve_storage_disk_names()
    result = []
    byid = Path("/dev/disk/by-id")
    if not byid.is_dir():
        return result
    # 同一块盘有多种别名（ata-/scsi-/wwn- 都指向同一 /dev/sdX），
    # 按真实设备去重，只保留最易读的一个：ata > nvme > scsi > usb > wwn > 其余
    candidates = {}
    for link in sorted(byid.iterdir()):
        name = link.name
        if "-part" in name or name.startswith(("dm-uuid", "dm-name", "lvm-", "md-uuid")):
            continue
        try:
            dev = link.resolve().name   # 解析软链到真实设备名
        except OSError:
            continue
        if dev in disks:                # 只保留整块磁盘，分区/dm 自然被滤掉
            candidates.setdefault(dev, []).append(name)
    priority = ("ata-", "nvme-", "scsi-", "usb-", "wwn-")

    def rank(n):
        for i, pre in enumerate(priority):
            if n.startswith(pre):
                return i
        return len(priority)

    for dev, names in sorted(candidates.items()):
        name = sorted(names, key=lambda n: (rank(n), n))[0]
        result.append({"name": name, "dev": "/dev/" + dev, "size": disks[dev],
                       "host_used": dev in host_used})
    return result


# ───────────────────────────── 存储 / 备份（用 pvesh CLI 替代 proxmoxer） ─────────────────────────────

def _pvesh(method, path):
    """封装 pvesh 调用，返回解析后的 JSON；失败（含非 0 退出）返回 None。

    pvesh 是 PVE 自带 CLI（走本地 unix socket，root 免密），所有节点都有，无需 proxmoxer。"""
    try:
        out = subprocess.run(["pvesh", method, path, "--output-format", "json"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _node_available_storages():
    """本节点 PVE 认为可用的存储 id 集合；无法判定时返回 None（调用方据此「不过滤」）。

    「存储能否在该节点使用」是节点级事实，判据取 PVE 为该节点算出的两个字段：
    - disabled=1：存储被全局禁用（/etc/pve/storage.cfg 里 disable）；
    - active=0：本节点无法激活它（NFS 未挂载、本地目录不存在等）。
    active 字段缺失时按 1 处理（不过滤），避免老版本 PVE 上误伤。
    返回 None 表示探测失败，此时宁可多列几项，也不要把下拉整个清空。"""
    node = socket.gethostname().split(".")[0]
    data = _pvesh("get", f"/nodes/{node}/storage")
    if not isinstance(data, list):
        return None
    return {str(s.get("storage", "")) for s in data
            if not s.get("disabled") and s.get("active", 1)}


def list_storage_options():
    """本节点所有已启用存储（含 content 类型）。供传输中心「存储内容」按节点选择存储。

    跑在「被选中的那个节点」上，因此 local 等节点独占存储也能正确列出（本机 pvesh 看到的
    是执行节点的节点视图，跨节点取不到目标节点的 local）。过滤口径与 backend.list_storage_list 一致。"""
    node = socket.gethostname().split(".")[0]
    data = _pvesh("get", f"/nodes/{node}/storage") or []
    out = []
    for s in data:
        if s.get("disabled"):
            continue
        out.append({"storage": str(s.get("storage", "")),
                    "type": str(s.get("type", "")),
                    "content": str(s.get("content", ""))})
    return out


def list_storage_content(storage):
    """本节点指定存储的内容条目（PVE content 接口原始结果，不做任何合成）。

    只在「该存储对本机不可用」时才会被调用：此时文件只存在于目标节点本地盘上
    （local 等节点独占存储），本机既列不出来也操作不了，只能由目标节点汇报；故这里
    不补 vbios/vgpu 合成条目（那是共享存储本机视角的产物），前端按只读展示。"""
    if not re.match(r"^[\w\-]+$", storage or ""):
        return []
    node = socket.gethostname().split(".")[0]
    return _pvesh("get", f"/nodes/{node}/storage/{storage}/content") or []


# 内容类型 → 存储根下的实际磁盘子目录（与 pve_api._CONTENT_SUBDIR 一致，PVE 固定映射）
_CONTENT_SUBDIR = {
    "iso": "template/iso",
    "vztmpl": "template/cache",
    "backup": "dump",
    "snippets": "snippets",
    "images": "images",
    "import": "import",
}


def _storage_root_of(sid):
    """本节点上指定存储的根目录 Path；非文件型 / 未配置 / 无 path 时返回 None。"""
    for s in _pvesh("get", "/storage") or []:
        if str(s.get("storage", "")) != sid:
            continue
        if str(s.get("type", "")) not in ("dir", "nfs", "cifs", "glusterfs"):
            return None
        p = s.get("path")
        return Path(str(p)) if p else None
    return None


def _storage_content_path(storage, volid):
    """把 volid 解析为本节点上的绝对路径（限文件型存储，防路径穿越）。

    节点独占存储（local 等）的文件只存在于所在节点：本机既列不出也读不到，故路径解析必须
    由该节点自己做（各节点的 local 同 id 不同内容），server 侧只会把解析结果拿去读/删。"""
    if not re.match(r"^[\w\-]+$", storage or ""):
        raise ValueError(f"非法存储名: {storage}")
    if ":" not in (volid or ""):
        raise ValueError("非法 volid（缺少存储前缀）")
    sname, sub = volid.split(":", 1)
    if sname != storage:
        raise ValueError("volid 与存储不匹配")
    ctype, _, rel = sub.partition("/")
    subdir = _CONTENT_SUBDIR.get(ctype)
    if not subdir:
        raise ValueError(f"不支持的内容类型: {ctype}")
    if not rel:
        raise ValueError("volid 子路径为空")
    root = _storage_root_of(storage)
    if root is None:
        raise ValueError(f"存储 {storage} 不可用或不支持该内容类型")
    try:
        base = (root / subdir).resolve()
    except OSError:
        raise ValueError(f"存储 {storage} 的目录不可访问")
    p = (base / rel).resolve()
    if base != p and base not in p.parents:
        raise ValueError("非法路径（尝试穿越存储根目录）")
    return p


def _node_storage_content_info(storage, volid):
    """本节点存储内容条目的路径与大小，供 server 侧跨节点下载（先取大小，再流式取内容）。"""
    p = _storage_content_path(storage, volid)
    if not p.is_file():
        raise ValueError("文件不存在或不是普通文件")
    return {"path": str(p), "name": p.name, "size": p.stat().st_size}


def _node_delete_storage_content(storage, volid):
    """在本节点删除存储内容条目（跨节点删除时由目标节点执行，路径校验防穿越）。"""
    p = _storage_content_path(storage, volid)
    if not p.is_file():
        raise ValueError("文件不存在或不是普通文件")
    p.unlink()
    return {"deleted": volid, "path": str(p)}


def _storage_dirs(content_type):
    """返回 本节点支持指定 content 类型、且带本地路径的存储 {storage: Path(root)}；与 backend._storage_dirs 一致。

    两个关键点（都曾导致「切到其它节点后存储下拉为空」）：
    - 存储「在本节点是否可用」是节点级事实（如 local 只在本节点存在），故先取本节点已启用
      存储集合，再按 content 过滤，而不是只看集群级配置。
    - pvesh 的查询参数必须用独立参数形式传入，不能拼进 path：pvesh 不认 URL query，
      `/storage?content=images` 会被当成未知路由直接失败
      （"No 'get' handler defined for '/storage?content=images&type=dir&enabled=1'"）。
    """
    out = {}
    try:
        # 取不到节点视图时按「无可用存储」处理（沿用原有语义：宁可不列，也不列出不可用的）
        enabled = _node_available_storages() or set()
        cfg = _pvesh("get", "/storage") or []
    except Exception:
        return out
    for s in cfg:
        sid = str(s.get("storage", ""))
        stype = str(s.get("type", ""))
        if sid not in enabled or stype not in ("dir", "nfs", "cifs", "glusterfs") or not s.get("path"):
            continue
        if content_type not in str(s.get("content", "")).split(","):
            continue
        out[sid] = Path(str(s["path"]))
    return out


def _backup_template_dir():
    """虚机模板（传输中心上传的 .vma）存放目录：pvewebtools 共享存储根下的 dump/ 子目录。

    通过 pvesh 取 pvewebtools 存储的路径（nfs 优先 export），回退 None。"""
    data = _pvesh("get", "/storage") or []
    for cfg in data:
        if str(cfg.get("storage")) == "pvewebtools":
            if str(cfg.get("type", "")).lower() == "nfs":
                export = cfg.get("export")
                if export:
                    return Path(str(export)) / "dump"
            p = cfg.get("path")
            if p:
                return Path(str(p)) / "dump"
    return None


def _read_archive_head(path):
    """读取 .vma/.tar 归档开头约 1MB 原始数据（整体压缩的先解压）。
    归档内的 VM 配置块为明文且位于开头；读取失败返回空 bytes。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return b""
    decomp = None
    if head[:4] == b"\x28\xb5\x2f\xfd":   # zstd
        decomp = "zstd -d -c"
    elif head[:2] == b"\x1f\x8b":         # gzip
        decomp = "gzip -dc"
    elif head[:4] == b"\x89\x4c\x5a\x4f": # lzop
        decomp = "lzop -dc"
    try:
        if decomp is None:
            with open(path, "rb") as f:
                return f.read(1048576)
        sh = f"{decomp} {shlex.quote(str(path))} | head -c 1048576"
        r = subprocess.run(["sh", "-c", sh], capture_output=True, timeout=30)
        return r.stdout if r.returncode == 0 else b""
    except (OSError, subprocess.TimeoutExpired):
        return b""


def _probe_backup_kind(path):
    """判断备份文件类别，返回 'template' / 'vm' / 'lxc' / 'unknown'。

    - 容器备份（.tar*）直接判 'lxc'；
    - VM 备份（.vma*）进一步读取归档头部的 VM 配置，检查是否存在 template: 1：
        有 → 'template'（模板），无 → 'vm'（普通虚机）。
    - 整体压缩的 .vma（文件头非 VMA 魔数，如手动 zstd 压缩）先解压头部再判断。
    """
    name = path.name.lower()
    if ".tar" in name:
        return "lxc"
    if ".vma" not in name:
        return "unknown"
    data = _read_archive_head(path)
    if not data:
        return "unknown"
    if re.search(rb"(^|\n)\s*template:\s*1\s*(\n|$)", data, re.M):
        return "template"
    return "vm"


def _list_backup_files():
    """列出全部备份文件：[{storage, name, size, mtime, kind}]"""
    out = []
    for sid, d in _storage_dirs("backup").items():
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            name = f.name
            # 备份文件：vzdump-qemu-*.vma.* / vzdump-lxc-*.tar.*，
            # 也支持自定义重命名后的备份（<任意名>.vma.* / <任意名>.tar.*）。
            # 排除 .log / .notes / .dat（临时）等非备份文件。
            if name.endswith(".log") or name.endswith(".notes") or name.endswith(".dat"):
                continue
            if ".vma" not in name and ".tar" not in name:
                continue
            st = f.stat()
            out.append({"storage": sid, "name": name, "path": str(f),
                        "size": st.st_size, "mtime": st.st_mtime,
                        "kind": _probe_backup_kind(f)})
    # 最近备份在前
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _list_template_backup_files():
    """虚机模板（dump/ 子目录）里的文件列表，格式同 _list_backup_files。

    因 pvewebtools 已声明 backup 内容类型，dump/ 下的文件已被 _list_backup_files()
    扫描；本函数保留作为兼容入口，返回空列表，避免 /backup/files 重复列出。"""
    return []


def list_image_storages():
    """列出本节点支持 images 内容的全部存储（含 lvmthin/zfs/ceph/nfs/dir 等）。

    存储定义读集群共享的 /etc/pve/storage.cfg（任意节点可读），与 server.py 本地实现一致；
    类型口径放开，但可用性按本节点收窄——某存储在本节点不可用（NFS 未挂载、本地目录
    不存在等）时不该出现在下拉里（否则要等 qm importdisk / qm set 执行时才失败）。
    节点视图探测失败时退回不过滤。"""
    avail = _node_available_storages()
    cfg_path = Path("/etc/pve/storage.cfg")
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    result = []
    cur = None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            m = re.match(r"^(\S+):\s*(\S+)", line.strip())
            if m:
                cur = {"type": m.group(1), "storage": m.group(2),
                       "content": "", "disabled": False}
                result.append(cur)
            else:
                cur = None
        elif cur is not None:
            s = line.strip()
            if s.startswith("content"):
                cur["content"] = s.split(None, 1)[1] if " " in s else ""
            elif s.startswith("disable"):
                cur["disabled"] = True
    return [{"storage": r["storage"], "type": r["type"], "free": None}
            for r in result
            if not r["disabled"] and "images" in r["content"].split(",")
            and (avail is None or r["storage"] in avail)]


def list_rootdir_storages():
    """列出本节点支持容器 rootfs 的存储（content 含 rootdir 或 images）。

    供创建 LXC 容器选「系统盘存储 / 数据盘存储」下拉：容器 rootfs 既可落在 rootdir 存储，
    也可落在 images 存储（如 local-lvm）。存储定义读集群共享的 /etc/pve/storage.cfg
    （与 list_image_storages 同源，差别只在内容类型过滤），可用性同样按本节点收窄
    —— 本节点无法激活的存储不该出现在下拉里（否则要等 pct create 执行时才失败）。
    节点视图探测失败时退回不过滤。"""
    avail = _node_available_storages()
    cfg_path = Path("/etc/pve/storage.cfg")
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    result = []
    cur = None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            m = re.match(r"^(\S+):\s*(\S+)", line.strip())
            if m:
                cur = {"type": m.group(1), "storage": m.group(2),
                       "content": "", "disabled": False}
                result.append(cur)
            else:
                cur = None
        elif cur is not None:
            s = line.strip()
            if s.startswith("content"):
                cur["content"] = s.split(None, 1)[1] if " " in s else ""
            elif s.startswith("disable"):
                cur["disabled"] = True
    return [{"storage": r["storage"], "type": r["type"], "free": None}
            for r in result
            if not r["disabled"] and ({"rootdir", "images"} & set(r["content"].split(",")))
            and (avail is None or r["storage"] in avail)]


def list_disk_storages():
    """列出本机支持 images 内容的 dir 型存储（供导入导出选存储下拉）。"""
    try:
        dirs = _storage_dirs("images")
    except Exception:
        return []
    result = []
    for sid, d in dirs.items():
        free = None
        try:
            free = shutil.disk_usage(d).free
        except OSError:
            pass
        result.append({"storage": sid, "path": str(d), "free": free})
    return result


def list_backup_storages():
    """列出本机支持 backup 内容的 dir 型存储（供备份选存储下拉）。"""
    try:
        dirs = _storage_dirs("backup")
    except Exception:
        return []
    result = []
    for sid, d in dirs.items():
        if sid == "pvewebtools":
            # pvewebtools 声明了 backup 内容类型（见 storage_provision.DEFAULT_CONTENT），
            # 从而以真实存储身份（pvewebtools -> <根>/dump）进入 dirs。此处跳过真实项，
            # 统一用下方合成项（指向 dump/ 子目录），避免前端「目标存储」出现两个 pvewebtools。
            continue
        free = None
        try:
            free = shutil.disk_usage(d).free
        except OSError:
            pass
        result.append({"storage": sid, "path": str(d), "free": free})
    # 合成 pvewebtools：虚机模板目录（pvewebtools 根下 dump/，即 PVE backup 目录）；
    # 传输中心上传虚机模板 / 还原 / 模板快速建机统一走该存储，不另造存储项
    try:
        td = _backup_template_dir()
        result.append({"storage": "pvewebtools", "path": str(td), "free": None, "template": True})
    except Exception:  # noqa: BLE001
        pass
    return result


def _node_backup_files():
    """备份文件列表（还原/模板建机下拉数据源）。"""
    files = list(_list_backup_files())
    for f in files:
        if f.get("storage") == "pvewebtools":
            f["template"] = True
    files += _list_template_backup_files()
    return files


# ───────────── 模板 / 镜像 / 备份的本机缓存（远端执行版，与 server.py 同名函数一一对应）─────────────
# 目录布局与「根目录选择口径」必须与工具箱 COMMON_HELPERS 的 pw_cache_* 及 server.py 完全一致，
# 否则界面看到的目录会和实际落点对不上。此处只用标准库 + pvesh。
#   <root>/<name>             缓存文件本体        <root>/.meta/<name>.meta  元数据（mtime=最后使用）
#   <root>/.locks/<name>.lock 拉取锁              <root>/<name>.part.<pid>  拉取中的中间文件
TEMPLATE_CACHE_DIRNAME = "pvewebtools-cache"
# 缓存淘汰策略（与工具箱 COMMON_HELPERS 的 pw_cache_effective_max_kb / pw_cache_purge_expired 口径一致）。
#   PT_TEMPLATE_CACHE_MAX_RATIO（默认 0.2）、PT_TEMPLATE_CACHE_MAX_AGE_DAYS（默认 30）、
#   PT_TEMPLATE_CACHE_MAX_MB（默认 0 = 不设，仅作比例之上的硬天花板）。


def _template_cache_max_ratio():
    """缓存占用比例上限（0~1），默认 0.2；口径同 bash pw_cache_max_ratio。"""
    try:
        return float(os.environ.get("PT_TEMPLATE_CACHE_MAX_RATIO", "0.2") or 0.2)
    except (ValueError, TypeError):
        return 0.2


def _template_cache_max_age_days():
    """保留天数（天），默认 30；口径同 bash pw_cache_max_age_days。"""
    try:
        return int(os.environ.get("PT_TEMPLATE_CACHE_MAX_AGE_DAYS", "30") or 30)
    except (ValueError, TypeError):
        return 30


def _template_cache_max_mb():
    """固定 MB 上限（0=不设），仅作比例之上的硬天花板；口径同 bash pw_cache_max_kb。"""
    try:
        return int(os.environ.get("PT_TEMPLATE_CACHE_MAX_MB", "0") or 0)
    except (ValueError, TypeError):
        return 0


def _template_cache_limit_mb(root):
    """有效容量上限（MB）：比例优先（盘总容量×比例），再被固定 MB 上限兜住（取小）。
    与 bash pw_cache_effective_max_kb 口径一致；root 为 None 或读不到盘时退化为固定上限（0=不限）。"""
    ratio = _template_cache_max_ratio()
    max_mb = _template_cache_max_mb()
    if ratio and ratio > 0 and root is not None:
        try:
            tot_mb = shutil.disk_usage(root).total // (1024 * 1024)
            ratio_mb = int(tot_mb * ratio)
            return min(ratio_mb, max_mb) if max_mb > 0 else ratio_mb
        except OSError:
            pass
    return max_mb


def _cache_lock_busy(lock):
    """条目是否正被并发任务占用（口径同 bash pw_cache_busy）：拿非阻塞排他锁失败即视为占用。"""
    try:
        import fcntl
        fd = os.open(str(lock), os.O_RDONLY | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def _path_is_remote_fs(path):
    """路径所在挂载点是否网络存储（与工具箱 pw_is_remote_path 口径一致）。"""
    try:
        target = str(Path(path).resolve())
    except OSError:
        target = str(path)
    best, fstype = "", ""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                f = ln.split()
                if len(f) < 3:
                    continue
                mp = f[1].replace("\\040", " ")
                if (target + "/").startswith(mp.rstrip("/") + "/") and len(mp) > len(best):
                    best, fstype = mp, f[2].lower()
    except OSError:
        return False
    return fstype in ("nfs", "nfs4", "cifs", "fuse", "ceph") or "gluster" in fstype


def _template_cache_root():
    """本机模板缓存根目录；无可用落点时返回 None（口径同 bash 侧 pw_cache_root）。"""
    root = None
    try:
        for cfg in (_pvesh("get", "/storage") or []):
            if str(cfg.get("storage")) == "local" and str(cfg.get("type")) == "dir" and cfg.get("path"):
                root = str(cfg["path"])
                break
    except Exception:  # noqa: BLE001
        root = None
    if not root and Path("/var/lib/vz").is_dir():
        root = "/var/lib/vz"
    if root and not _path_is_remote_fs(root):
        return Path(root) / TEMPLATE_CACHE_DIRNAME
    if Path("/var/tmp").is_dir() and not _path_is_remote_fs("/var/tmp"):
        return Path("/var/tmp") / TEMPLATE_CACHE_DIRNAME
    return None


def _read_cache_meta(meta):
    """解析 .meta（key=value 明文）；读取失败返回空 dict。"""
    out = {}
    try:
        for ln in meta.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        return {}
    return out


def _cache_entry_kind(src, name, dump_dir=""):
    """按来源路径推断缓存条目类别（供面板行内显示类型标签），口径与 server.py 同名函数一致。

    cloud-images/ → 云镜像、template/cache/ → LXC 模板；dump/ 下按项目约定分模板与备份
    （pvewebtools 共享存储的 dump/ 在本工具里就是模板目录）。认不出来返回空串。
    """
    s = str(src or "")
    if not s:
        return ""
    if "/cloud-images/" in s:
        return "云镜像"
    if "/template/cache/" in s:
        return "LXC 模板"
    if "/dump/" in s:
        is_tpl = bool(dump_dir) and s.startswith(str(dump_dir).rstrip("/") + "/")
        if name.startswith("vzdump-lxc-"):
            return "容器模板" if is_tpl else "容器备份"
        if name.startswith("vzdump-qemu-"):
            return "虚机模板" if is_tpl else "虚机备份"
        return "模板" if is_tpl else "备份"
    return ""


def _node_template_cache():
    """本机模板缓存概览（state: ok / orphan / partial）。

    dump_local 标记本节点上模板目录是否就在本机盘（即本机是共享存储的导出端），
    界面对导出端要显示「模板本地直读、无需缓存」，而不是「暂无缓存」。
    """
    dump_dir, dump_local = "", False
    try:
        dump = _backup_template_dir()
        if dump:
            dump_dir = str(dump)
            dump_local = Path(dump).is_dir() and not _path_is_remote_fs(dump)
    except Exception:  # noqa: BLE001
        pass
    base = {"dump_dir": dump_dir, "dump_local": dump_local,
            "max_age_days": _template_cache_max_age_days(),
            "max_ratio": _template_cache_max_ratio()}
    root = _template_cache_root()
    limit_mb = _template_cache_limit_mb(root) if root is not None else 0
    if root is None:
        return dict(base, root="", root_ok=False, total=0, free=None, items=[], limit_mb=limit_mb)
    metas = {}
    try:
        for m in (root / ".meta").glob("*.meta"):
            metas[m.name[: -len(".meta")]] = m
    except OSError:
        metas = {}
    items, total = [], 0
    try:
        entries = list(root.iterdir())
    except OSError:
        entries = []
    for ent in entries:
        name = ent.name
        try:
            if ent.is_dir() or name.startswith("."):
                continue
            st = ent.stat()
        except OSError:
            continue
        if ".part." in name:
            items.append({"name": name, "size": st.st_size, "state": "partial",
                          "last_used": int(st.st_mtime), "src": "", "fetched": 0, "kind": ""})
            continue
        meta = metas.get(name)
        info = {"name": name, "size": st.st_size, "state": "orphan" if meta is None else "ok",
                "last_used": int(st.st_mtime), "src": "", "fetched": 0, "kind": ""}
        if meta is not None:
            kv = _read_cache_meta(meta)
            info["src"] = kv.get("src", "")
            info["kind"] = _cache_entry_kind(info["src"], name, dump_dir)
            try:
                info["fetched"] = int(kv.get("fetched", "0") or 0)
            except ValueError:
                info["fetched"] = 0
            try:
                info["last_used"] = int(meta.stat().st_mtime)
            except OSError:
                pass
            total += st.st_size
        items.append(info)
    items.sort(key=lambda x: (x["state"] != "ok", -int(x.get("last_used") or 0)))
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = None
    return dict(base, root=str(root), root_ok=True, total=total, free=free, items=items, limit_mb=limit_mb)


def _cache_entry_name_ok(name):
    """缓存条目名合法性：必须是缓存根下的纯文件名。"""
    if not name or name != Path(name).name or name.startswith("."):
        return False
    return "/" not in name and "\\" not in name


def _node_template_cache_delete(name):
    """删除本机模板缓存中的一条（缓存文件 + 其 .meta）。"""
    if not _cache_entry_name_ok(name):
        raise RuntimeError("缓存文件名不合法")
    root = _template_cache_root()
    if root is None:
        raise RuntimeError("本节点未启用缓存加速")
    hit = False
    for p in (root / name, root / ".meta" / (name + ".meta")):
        try:
            if p.is_file():
                p.unlink()
                hit = True
        except OSError as e:
            raise RuntimeError(f"删除失败: {e}") from e
    if not hit:
        raise RuntimeError(f"缓存条目不存在: {name}")
    return {"deleted": name}


def _node_template_cache_clear():
    """清空本机模板缓存（缓存文件 + meta + 锁）；拉取中的 .part 保留给下次 GC。"""
    root = _template_cache_root()
    if root is None:
        raise RuntimeError("本节点未启用缓存加速")
    count, freed = 0, 0
    for d, only_suffix in ((root, ""), (root / ".meta", ".meta"), (root / ".locks", ".lock")):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for ent in entries:
            try:
                if not ent.is_file() or ent.name.startswith("."):
                    continue
                if only_suffix and not ent.name.endswith(only_suffix):
                    continue
                if not only_suffix and ".part." in ent.name:
                    continue
                size = ent.stat().st_size
                ent.unlink()
                count += 1
                if not only_suffix:
                    freed += size
            except OSError:
                continue
    return {"count": count, "freed": freed}


def _node_template_cache_expire():
    """按「保留天数」清理过期缓存条目（与大小无关）：meta 最后使用时间超过 N 天的删除。
    被并发任务占用的条目跳过，绝不删别人正在读的文件。"""
    root = _template_cache_root()
    if root is None:
        raise RuntimeError("本节点未启用缓存加速")
    age = _template_cache_max_age_days()
    if age <= 0:
        return {"count": 0, "freed": 0, "note": "未启用保留天数策略（PT_TEMPLATE_CACHE_MAX_AGE_DAYS<=0）"}
    cutoff = time.time() - age * 86400
    meta_dir = root / ".meta"
    count, freed = 0, 0
    try:
        metas = list(meta_dir.glob("*.meta"))
    except OSError:
        metas = []
    for m in metas:
        try:
            if m.stat().st_mtime < cutoff:
                name = m.name[: -len(".meta")]
                lock = root / ".locks" / (name + ".lock")
                if _cache_lock_busy(lock):
                    continue
                f = root / name
                sz = 0
                if f.is_file():
                    try:
                        sz = f.stat().st_size
                    except OSError:
                        sz = 0
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    m.unlink()
                except OSError:
                    pass
                if not m.exists():
                    count += 1
                    freed += sz
        except OSError:
            continue
    return {"count": count, "freed": freed}
