"""硬件探测与节点采集。本机分发到各模块的现有实现，远端执行自包含脚本 collect.py。"""

import copy
import json
import os
import re
import socket
import subprocess
import threading
import time

from importlib import import_module
from core.pve import pve_api as backend
from core.web.host_util import _shared_storages
from pathlib import Path


_SSH_CTRL = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/pvewebtools-ssh-%C", "-o", "ControlPersist=300"]

_AMD_DGPU_RE = re.compile(
    r"\brx \d|\bradeon pro\b|\bradeon vii\b|\bnavi \d|\bpolaris\b|\btonga\b|\bfiji\b|\bhawaii\b"
    r"|\bvega (?:10|12|20|56|64)\b|\bfrontier\b|\bfirepro\b|\binstinct\b|\bwx \d|\br[579] \d"
    r"|\bhd [5-9]\d{3}\b"
)

_AMD_IGPU_RE = re.compile(
    r"\bradeon graphics\b|\bradeon vega \d{1,2}\b|\bradeon \d{3}m\b"
    r"|\b(?:raven|picasso|renoir|lucienne|cezanne|barcelo|van gogh|rembrandt|phoenix|hawk point"
    r"|mendocino|raphael|dragon range|granite ridge|strix|krackan)\b"
)

_GPU_PASSTHROUGH_HINTS = {
    "1002:67e3": {  # AMD Radeon Pro WX 4100 (Polaris 11)
        "opts": "pcie=1",
        "hint": "该卡直通易报 Code 43，建议加 pcie=1、设为主显卡(x-vga=1)，并给 CPU 加 hidden=1 隐藏虚拟化",
    },
}

_INTEL_DGPU_RE = re.compile(
    r"\barc a\d|\barc b\d|\barc pro\b|\bbattlemage\b|\bdata center gpu\b"
    r"|\bponte vecchio\b|\bdg1\b|\bdg2\b|\bats-m\b"
)

# ── Intel 核显代次映射（可查询）────────────────────────────────────────────────
# 键为 PCI 设备 ID（小写 4 位十六进制），值为该代次的平台范围与 legacy-igd 路线建议。
# 数据源：Linux 内核 i915 驱动设备表（include/drm/i915_pciids.h，v6.10 的 INTEL_*_IDS 宏；
#         Xe2 / Lunar Lake 段据内核 xe 驱动补充）。用途：识别核显代次，决定是否推荐
#         「显示输出（vga=none + legacy-igd）」路线，或只建议「仅计算」纯直通。
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

_INTEL_IGPU_INDEX = {i: g for g in INTEL_IGPU_GENERATIONS for i in g["ids"]}

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

_PCI_GENERIC_EXCLUDE = {"01", "02", "03", "06", "08", "0b"}

_PCI_MSIX_INCOMPAT = {
    "8086:f1a6",   # Intel SSD DC P4101/Pro 7600p/760p/E 6100p Series（实测直通失败）
}


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


def _pci_class_tag(classcode):

    """按 PCI 类代码给出类别标签（直通下拉里区分设备类型，避免只看地址猜）。"""

    c = (classcode or "").lower()
    for pre, tag in _PCI_CLASS_TAGS:
        if c.startswith(pre):
            return tag
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


def _collect_source():
    """读取自包含采集脚本 collect.py 的源码，供经 SSH 喂给远端节点执行。

    源码部署时 collect.py 与本文件同目录；打包分发时它被放进资源目录根，运行时从
    RESOURCE_DIR 读取。
    该脚本零第三方依赖，只用到标准库 + 节点自带 CLI（lspci/lsusb/ip/lsblk/pvesh 等），
    因此远端节点不需要安装本工具或 proxmoxer 也能返回真实硬件列表。
    """
    cand = Path(__file__).resolve().parent / "collect.py"
    if cand.is_file():
        return cand.read_text(encoding="utf-8")
    try:
        from core.paths import RESOURCE_DIR
    except Exception:  # noqa: BLE001
        RESOURCE_DIR = None
    for base in (RESOURCE_DIR,):
        if not base:
            continue
        for rel in ("collect.py", "core/web/collect.py"):
            p = Path(base) / rel
            if p.is_file():
                return p.read_text(encoding="utf-8")
    raise RuntimeError("找不到自包含采集脚本 collect.py，无法在远端节点采集硬件（请重新构建并部署二进制）")


_LOCAL_COLLECTORS = {
    **{name: (__name__, name) for name in (
        "list_pci_storage", "list_network_bridges", "list_network_vlans", "list_phys_nics",
        "list_pci_nics", "list_pci_gpus", "list_pci_generic", "list_vfio_orphans", "list_usb_devices",
    )},
    **{name: ("core.web.handlers", name) for name in (
        "list_byid_disks", "list_phys_disks", "_node_backup_files", "list_disk_storages",
        "list_image_storages", "list_backup_storages",
    )},
    "list_rootdir_storages": ("core.web.handlers", "_rootdir_storages_local"),
    "list_storage_options": ("core.pve.pve_api", "list_storage_list"),
    "list_storage_content": ("core.pve.pve_api", "list_storage_content"),
    **{name: ("core.web.template_cache", name) for name in (
        "_node_template_cache", "_node_template_cache_delete", "_node_template_cache_clear",
        "_node_template_cache_expire",
    )},
}


def _local_collector(func_name):
    """按明确的函数归属查找本机实现；延迟导入避免 handlers/cache 与 probe 的循环依赖。"""
    try:
        module_name, attr = _LOCAL_COLLECTORS[func_name]
    except KeyError:
        raise ValueError(f"不支持的本机采集函数: {func_name}") from None
    return getattr(import_module(module_name), attr)


_REMOTE_CACHE_TTL = 30          # 远端只读采集：30s 内复用（一次 SSH + python 启动通常 0.5~2s）
_REMOTE_CONNECT_TIMEOUT = 5     # SSH 建连超时（与 node.py 的 SSH 采集保持一致；原来 8s 让宕机节点拖得更久）
_remote_cache = {}              # (node, func_name) -> (时间戳, data)
_remote_cache_lock = threading.Lock()


def invalidate_remote_cache(node=None):
    """丢弃远端采集缓存（指定节点或全部）；节点侧配置变更后可主动调用。"""
    with _remote_cache_lock:
        if node is None:
            _remote_cache.clear()
        else:
            for key in [k for k in _remote_cache if k[0] == str(node)]:
                _remote_cache.pop(key, None)


def _run_func_on_node(node, func_name, timeout=15):
    """在目标节点调用采集函数并返回结果；本机调用对应模块的实现，外节点通过免密 SSH
    执行内置的自包含采集脚本（collect.py）。

    供 /api/pci/*、/api/usb/devices、各节点级下拉接口使用，使前端切换「执行节点」时
    能拿到对应节点的真实设备列表。远端节点无需安装本工具（无 core 包、无 proxmoxer），
    只要能免密 SSH 且装了 lspci/lsusb/ip/lsblk/pvesh 等基础工具即可。

    远端结果按 (节点, 函数) 缓存 30s：这些函数都是「只读采集」（白名单 = _LOCAL_COLLECTORS
    里的无参采集函数，删除/清空类走带参的 _run_func_on_node_args，不进此缓存），
    一次 SSH + python 启动通常 0.5~2s，连续打开多个工具弹窗没必要重复付这个钱。

    节点宕机/网络不通时复用全项目统一的「不可达负缓存」（60s）：首次失败后 60s 内直接
    抛出明确错误，不再每次都等一遍 SSH 建连超时（原来每个下拉要各等 8s）。
    """
    local = socket.gethostname().split(".")[0]
    if not node or node == local:
        return _local_collector(func_name)()
    cacheable = func_name in _LOCAL_COLLECTORS
    key = (str(node), func_name)
    if cacheable:
        with _remote_cache_lock:
            hit = _remote_cache.get(key)
            if hit and time.time() - hit[0] < _REMOTE_CACHE_TTL:
                return copy.deepcopy(hit[1])
    if backend.node_unreachable(node):
        raise RuntimeError(f"节点 {node} 当前不可达（近期连接失败，稍后自动重试）")
    src = _collect_source()
    code = src + "\n" + (
        "import json\n"
        f"print(json.dumps({func_name}(), ensure_ascii=False))\n"
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_REMOTE_CONNECT_TIMEOUT}",
             "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
             *backend.ssh_host_opts(node),
             f"root@{backend.ssh_host(node)}", "python3", "-"],
            input=code, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as ex:
        backend.mark_node_unreachable(node)
        raise RuntimeError(f"连接节点 {node} 失败：{ex}") from None
    if out.returncode != 0:
        if out.returncode == 255:      # SSH 自身的连接/认证失败 → 记负缓存
            backend.mark_node_unreachable(node)
        raise RuntimeError(out.stderr.strip() or f"远程采集 {func_name} 失败")
    backend.mark_node_reachable(node)
    data = json.loads(out.stdout.strip())
    if cacheable:
        with _remote_cache_lock:
            _remote_cache[key] = (time.time(), data)
    return data


def _run_func_on_node_args(node, func_name, args=(), timeout=30):
    """在目标节点调用 collect.py 中「带参数」的同名函数；本机直接调用。

    与 _run_func_on_node 的唯一差别是支持传参：参数经 json.dumps 序列化后作为字面量
    注入远端代码（不拼命令行、不拼 shell），因此参数内容天然免疫注入。
    """
    local = socket.gethostname().split(".")[0]
    if not node or node == local:
        return _local_collector(func_name)(*args)
    src = _collect_source()
    payload = json.dumps(list(args), ensure_ascii=False)
    code = src + "\n" + (
        "import json\n"
        f"print(json.dumps({func_name}(*json.loads({payload!r})), ensure_ascii=False))\n"
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_REMOTE_CONNECT_TIMEOUT}",
             "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
             *backend.ssh_host_opts(node),
             f"root@{backend.ssh_host(node)}", "python3", "-"],
            input=code, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as ex:
        backend.mark_node_unreachable(node)
        raise RuntimeError(f"连接节点 {node} 失败：{ex}") from None
    if out.returncode != 0:
        if out.returncode == 255:
            backend.mark_node_unreachable(node)
        raise RuntimeError(out.stderr.strip() or f"远程执行 {func_name} 失败")
    backend.mark_node_reachable(node)
    return json.loads(out.stdout.strip())


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


def list_pci_storage(node=None):
    """可直通的 PCIe 存储设备（NVMe/SATA/SCSI/RAID 控制器等），
    对已知 MSI-X 不兼容的设备打 compat=False 标记（前端高亮提示改用 RDM）；
    对承载系统盘的控制器打 host_used=True（前端置灰 + 脚本拦截，直通会掉系统盘）。

    指定 node 时从目标节点采集；未指定或等于本机时读本地。
    """
    if node:
        return _run_func_on_node(node, "list_pci_storage")
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


def _ver_key(v):
    """内核版本排序键：数字段按数值比，非数字段按字典序，避免 int/str 混比报错"""
    return [(int(x), "") if x.isdigit() else (-1, x) for x in re.split(r"[.-]", v)]


def list_installed_kernels(node=None):
    """指定节点已装内核版本（/boot/vmlinuz-*），标注运行中；供「设默认启动」下拉。

    未指定或等于本机时读本机 /boot；集群其他节点走免密 SSH（与工具执行同链路）。
    """
    local = socket.gethostname().split(".")[0]
    try:
        if node and node != local:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_REMOTE_CONNECT_TIMEOUT}", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", "uname -r; ls /boot"],
                capture_output=True, text=True, timeout=15)
            lines = out.stdout.splitlines()
            running = lines[0].strip() if lines else ""
            names = lines[1:]
        else:
            out = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=5)
            running = out.stdout.strip()
            names = os.listdir("/boot")
        vers = sorted({n[len("vmlinuz-"):] for n in names if n.startswith("vmlinuz-")},
                      key=_ver_key, reverse=True)
        return [{"version": v, "running": v == running} for v in vers]
    except Exception:
        return []


def _running_kernel_signed(node=None):
    """节点运行中内核是否签名版：按 /boot/vmlinuz-$(uname -r) 的属主包名判定。

    探测失败返回 None（调用方不过滤、保持全量展示），不误伤无 signed 变体的老系统。
    """
    local = socket.gethostname().split(".")[0]
    cmd = ("dpkg -S \"/boot/vmlinuz-$(uname -r)\" 2>/dev/null"
           " | head -n1 | awk -F': ' '{print $1}' || true")
    try:
        if node and node != local:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_REMOTE_CONNECT_TIMEOUT}", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", cmd],
                capture_output=True, text=True, timeout=15)
        else:
            out = subprocess.run(["bash", "-c", cmd],
                                 capture_output=True, text=True, timeout=15)
        owner = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        return owner.endswith("-signed") if owner else None
    except Exception:
        return None


def list_available_kernels(node=None):
    """仓库中可安装的内核包（apt-cache 本地缓存，不触发联网 apt update）；
    供「安装新内核」下拉。

    过滤 -template 构建包（不是内核）；系列元包（如 proxmox-kernel-7.0）打 meta 标记；
    按节点运行中内核形态过滤：signed 系统只留签名版+元包，未签名系统只留未签名版。
    排序与「查看可用内核」脚本保持一致用 sort -V。集群其他节点走免密 SSH。
    """
    local = socket.gethostname().split(".")[0]
    cmd = ("apt-cache search --names-only '^(proxmox|pve)-kernel-[0-9]'"
           " | awk '{print $1}' | grep -vE -- '-template$' | sort -V || true")
    try:
        if node and node != local:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_REMOTE_CONNECT_TIMEOUT}", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", cmd],
                capture_output=True, text=True, timeout=30)
        else:
            out = subprocess.run(["bash", "-c", cmd],
                                 capture_output=True, text=True, timeout=30)
        names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
        meta_re = re.compile(r"^(proxmox|pve)-kernel-[0-9]+\.[0-9]+$")
        # 随运行内核形态过滤：signed 系统只显签名版+系列元包（元包依赖链拉的是签名版）；
        # 未签名系统只显未签名版（元包会拉 signed 故一并隐藏）；探测失败保持全量
        signed_sys = _running_kernel_signed(node)
        if signed_sys is True:
            names = [n for n in names if n.endswith("-signed") or meta_re.match(n)]
        elif signed_sys is False:
            names = [n for n in names if not n.endswith("-signed") and not meta_re.match(n)]
        # 同一内核的未签名版（如 proxmox-kernel-7.0.14-9-pve）在存在签名版（...-signed）时
        # 不出现在下拉里：安装链路默认优先签名版，成对展示只会造成困惑；
        # 仅有未签名版的老内核不受影响照常展示
        signed = {n for n in names if n.endswith("-signed")}
        return [{"name": n, "meta": bool(meta_re.match(n)), "signed": n in signed}
                for n in reversed(names) if n + "-signed" not in signed]  # 最新版排最前
    except Exception:
        return []


def _list_backup_dir(dirpath, node=None):
    """列出指定备份目录下的 .bak 文件，时间戳前缀字典序倒序 = 最近备份在前。

    集群其他节点走免密 SSH，与工具执行同链路；无备份/目录不存在返回空列表。
    """
    local = socket.gethostname().split(".")[0]
    try:
        if node and node != local:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_REMOTE_CONNECT_TIMEOUT}", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", f"ls -1 {dirpath} 2>/dev/null || true"],
                capture_output=True, text=True, timeout=15)
            names = out.stdout.splitlines()
        else:
            d = Path(dirpath)
            names = [p.name for p in d.iterdir()] if d.is_dir() else []
        baks = sorted((n.strip() for n in names
                       if n.strip().endswith(".bak") and "/" not in n.strip()),
                      reverse=True)
        return [{"name": n} for n in baks]
    except Exception:
        return []


def list_grub_backups(node=None):
    """GRUB 配置备份列表（/etc/pvewebtools/backup/grub），供「从备份恢复」下拉"""
    return _list_backup_dir("/etc/pvewebtools/backup/grub", node)


def list_net_backups(node=None):
    """网络配置备份列表（/etc/pvewebtools/backup/network），供接口配置管理「从备份恢复」下拉"""
    return _list_backup_dir("/etc/pvewebtools/backup/network", node)


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


def list_network_bridges(node=None):
    """vmbr 网桥列表（名称/IP/桥接网口/在用 VM），供网桥管理下拉；
    删除前前端可据 used_by 提示，脚本另有双重拦截。指定 node 时从目标节点采集。"""
    if node:
        return _run_func_on_node(node, "list_network_bridges")
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


def list_network_vlans(node=None):
    """VLAN 接口列表（X.N 形式，含 IP 与所属设备），供删除 VLAN 下拉。指定 node 时从目标节点采集。"""
    if node:
        return _run_func_on_node(node, "list_network_vlans")
    stanzas = _parse_interfaces()
    out = []
    for name in sorted(n for n in stanzas
                       if re.match(r"^[A-Za-z0-9_-]+\.\d{1,4}$", n)):
        d = stanzas[name]
        out.append({"name": name, "address": d["address"], "raw": d["raw"]})
    return out


def list_phys_nics(node=None):
    """物理网口列表（排除 lo/虚拟接口/VLAN 子接口），供创建网桥选 bridge-ports。指定 node 时从目标节点采集。"""
    if node:
        return _run_func_on_node(node, "list_phys_nics")
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


def _local_access_pred(node):
    """构造判据：某存储的内容能否由本机直读（读得到就能继续做文件操作）。

    可选情形：所选即本机，或该存储属于共享存储（目标节点与本机是同一份数据，见
    _shared_storages）且本机已激活它。节点独占存储（local 等）恒为 False：本机直读会串到
    本机自己那份同名存储上，必须由所在节点汇报，前端按只读展示。预取集合，避免逐存储 pvesh。"""
    local = socket.gethostname().split(".")[0]
    if not node or node == local:
        return lambda _storage: True
    shared = _shared_storages()
    avail = backend.node_available_storages()   # 本机可用集合；None=探测失败
    return lambda storage: bool(avail is not None and storage in shared and storage in avail)


def _host_used_ifnames():
    """返回宿主占用的物理网口名集合：有 IP 的网口 + vmbr 桥接成员 + bond 成员。

    这些网口直通后宿主会断网或丢失管理通道，必须置灰 + 拦截。"""
    used = set()
    try:
        out = subprocess.run(["ip", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            # 形如：2: eno1    inet 192.168.1.10/24 ...
            m = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:\s", line)
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


def list_pci_nics(node=None):
    """可直通的 PCIe 网卡，标注宿主物理接口名与是否被宿主占用（有 IP/桥接成员/默认路由出口）。

    宿主占用的网口直通会断网/丢管理通道，前端置灰 + 脚本拦截。
    指定 node 时从目标节点采集；未指定或等于本机时读本地。
    """
    if node:
        return _run_func_on_node(node, "list_pci_nics")
    nics = _list_pci_class("02")
    # 默认路由出口网口：直通它 = 宿主失联
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
    # 物理网卡 -> PCI 地址映射（跳过 lo 与桥/tap 等虚拟接口）
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
        # 无线网卡（含 CNVi）：虚机里通常认不到网口，软路由（爱快等）更是不支持当 WAN/LAN → 前端置灰
        d["wireless"] = (d.get("tag") == "【无线网卡】")
        d["speed"] = _nic_link_speed(ifname)
    return nics


def intel_igpu_generation(dev_id):
    """按 PCI 设备 ID（'9bc4' / '0x9bc4' 均可）查 Intel 核显代次；未收录（更新的平台）返回 None。

    返回的字典含 label / platforms / legacy_igd / ids（ids 为 frozenset，供内部比对，
    对前端输出时应只取前三个字段，避免 JSON 序列化集合失败）。"""
    key = str(dev_id or "").strip().lower()
    if key.startswith("0x"):
        key = key[2:]
    return _INTEL_IGPU_INDEX.get(key.zfill(4))


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


def list_pci_gpus(node=None):
    """可直通的 PCIe 显示设备，标注独显/核显与宿主已绑定驱动（直通前可能需屏蔽），
    并对已知直通易报 Code 43 的显卡型号给出推荐配置（前端高亮提示 + 脚本自动套用）。
    Intel 核显额外带 igpu_gen（代次与 legacy-igd 建议，取自 INTEL_IGPU_GENERATIONS）。

    指定 node 时从目标节点采集；未指定或等于本机时读本地。
    """
    if node:
        return _run_func_on_node(node, "list_pci_gpus")
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


def _gpu_passthrough_hint(addr, desc):
    ids = re.findall(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", desc)
    if not ids:
        return None
    vd = f"{ids[-1][0].lower()}:{ids[-1][1].lower()}"
    return _GPU_PASSTHROUGH_HINTS.get(vd)


def list_pci_generic(node=None):
    """其余可直通 PCIe 设备（采集卡/声卡/USB 控制器等），供通用直通下拉。

    标注宿主当前绑定驱动与已被哪个 VM 直通占用，前端据此提示。
    指定 node 时从目标节点采集；未指定或等于本机时读本地。
    """
    if node:
        return _run_func_on_node(node, "list_pci_generic")
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


def list_vfio_orphans(node=None):
    """列出「没人用、但宿主机也用不了」的 PCI 设备，供「回收直通设备」工具处理。

    两类：
      1. state="vfio"   —— 驱动还挂在 vfio-pci 上（删掉 hostpci 后 PVE 不会自动归还）；
      2. state="unbound"—— 已被解绑但没装回驱动（上次释放时原驱动绑失败/未装），且有候选驱动可装回。
    都不含「正被某台 VM 的 hostpci 引用」的设备。
    返回：BDF、精简名称、首选原驱动、候选驱动表、是否被 modprobe.d 屏蔽、driver_override。
    指定 node 时从目标节点采集；未指定或等于本机时读本地。
    """
    if node:
        return _run_func_on_node(node, "list_vfio_orphans")
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


def list_usb_devices(node=None):
    """可直通的 USB 设备列表（vid:pid），跳过根集线器，并标注已被哪些 VM 直通占用。

    指定 node 时从目标节点采集；未指定或等于本机时读本地。
    """
    if node:
        return _run_func_on_node(node, "list_usb_devices")
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


