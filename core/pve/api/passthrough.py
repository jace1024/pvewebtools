from __future__ import annotations
from typing import Any
"""硬件直通解析：从 VM 配置里提取 hostpci / mdev / RDM 设备（供虚机详情与
节点硬件占用标注共用）。"""

import re

from pathlib import Path
from core.pve.api.base import (  # noqa: F401
    _conf_dir_signature,
)

_passthrough_cache = {"sig": None, "data": {}, "kind": {}}


def _passthrough_map() -> Any:
    """读取所有 VM 配置里的 hostpci（PCIe 直通）设备，返回 vmid -> 是否有直通。
    与 _onboot_map 同理读本地 pmxcfs；结果按配置目录签名缓存。RDM 盘（/dev/disk/by-id 引用）也算直通。
    同时填 _passthrough_kind() 用的类型表（同一次扫描、同一份缓存）。"""
    sig = _conf_dir_signature()
    if _passthrough_cache["sig"] == sig and _passthrough_cache["data"]:
        return _passthrough_cache["data"]
    pt, kind = {}, {}
    qemu_dir = Path("/etc/pve/qemu-server")
    try:
        confs = list(qemu_dir.glob("*.conf"))
    except OSError:
        return pt
    for conf in confs:
        vmid = conf.stem
        if not vmid.isdigit():
            continue
        try:
            text = conf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hp_lines = re.findall(r"^hostpci\d+:[^\n]*", text, re.M)
        has_rdm = bool(re.search(r"^(scsi|virtio|sata|ide)\d+:.*(/dev/|disk/by-id)", text, re.M))
        pt[vmid] = 1 if (hp_lines or has_rdm) else 0
        # 类型细分：hostpci 行里带 mdev= 的是 vGPU 切分（物理卡仍归宿主 nvidia 驱动），
        # 不带 mdev 的才是物理直通；两者都有则是 mixed
        has_mdev = any("mdev=" in ln for ln in hp_lines)
        has_plain = any("mdev=" not in ln for ln in hp_lines)
        if has_mdev and has_plain:
            kind[vmid] = "mixed"
        elif has_mdev:
            kind[vmid] = "vgpu"
        elif has_plain or has_rdm:
            kind[vmid] = "pci"
        else:
            kind[vmid] = ""
    _passthrough_cache["sig"] = sig
    _passthrough_cache["data"] = pt
    _passthrough_cache["kind"] = kind
    return pt


def _passthrough_kind() -> Any:
    """vmid -> 直通类型："vgpu"（仅 vGPU）/ "pci"（物理直通或 RDM 盘）/ "mixed"（两者都有）/ ""（无）。
    供前端把「含直通盘，不支持克隆」按场景细分（vGPU 虚机克隆出去会撞同一个 mdev 实例）。"""
    _passthrough_map()          # 命中/触发同一次扫描，顺带填好 kind 缓存
    return _passthrough_cache["kind"]


def _hostpci_bds() -> Any:
    """收集所有 VM 配置里 hostpci 引用的 PCI 设备（bus:device，去掉 function 与 domain 前缀）。

    注意：这里把「物理直通」与「vGPU 切分（mdev=）」都算作「被 VM 引用」，网卡那种
    "有人用就别动"的场景正合适；显卡要再分档时用 _hostpci_mdev_bds() 取 vGPU 子集。

    用途：区分两种容易混淆的状态——
      1. 真的被某台 VM 直通（配置里有 hostpci）→「直通占用」；
      2. 只是驱动还挂在 vfio-pci 上、当前没有任何 VM 引用它 →「vfio 占用」（宿主不可用）。
    第 2 种在"从 VM 配置里删除 hostpci"后非常常见：PVE 不会自动把设备还给原驱动，
    /sys/.../driver 仍指向 vfio-pci，只看驱动就会一直误报「直通占用」。"""
    bds = set()
    qemu_dir = Path("/etc/pve/qemu-server")
    try:
        confs = list(qemu_dir.glob("*.conf"))
    except OSError:
        return bds
    for conf in confs:
        try:
            text = conf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"^hostpci\d+:\s*([0-9a-fA-F:.]+)", text, re.M):
            bdf = m.group(1).split(",")[0].strip()
            bd = re.sub(r"\.\d+$", "", bdf)      # 去掉 function（01:00.0 → 01:00）
            bds.add(bd.replace("0000:", ""))     # 去掉 domain 前缀
    return bds


def _hostpci_mdev_bds() -> Any:
    """收集「以 vGPU（mdev=）方式」挂给 VM 的 PCI 设备（bus:device）。

    hostpci 行里带 mdev= 的是 NVIDIA vGPU 切分：物理卡仍被宿主 nvidia 驱动接管，
    只是切了一份 mdev 实例给虚机，并不是被直通独占；不带 mdev= 的才是物理直通。
    概览「显卡」据此分两档显示：vGPU 占用 / 直通占用。"""
    bds = set()
    try:
        confs = list(Path("/etc/pve/qemu-server").glob("*.conf"))
    except OSError:
        return bds
    for conf in confs:
        try:
            text = conf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"^hostpci\d+:[^\n]*mdev=[^\n]*", text, re.M):
            bdf = re.match(r"hostpci\d+:\s*([0-9a-fA-F:.]+)", m.group(0))
            if not bdf:
                continue
            bd = re.sub(r"\.\d+$", "", bdf.group(1).strip())
            bds.add(bd.replace("0000:", ""))
    return bds


def _rdm_passthrough_devs() -> Any:
    """收集所有 VM 配置里被「块映射直通(RDM)」引用的真实设备名集合。
    形如 scsi1: /dev/disk/by-id/nvme-INTEL_xxx 或 /dev/disk/by-id/ata-xxx，
    解析 by-id 软链得到真实块设备名（nvme1n1/sda 等），供磁盘状态判断使用。
    读不到任何配置时返回空集合（不视为错误）。"""
    devs = set()
    qemu_dir = Path("/etc/pve/qemu-server")
    try:
        confs = list(qemu_dir.glob("*.conf"))
    except OSError:
        return devs
    for conf in confs:
        try:
            text = conf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"/dev/disk/by-id/([\w.\-]+)", text):
            link = Path("/dev/disk/by-id") / m.group(1)
            try:
                devs.add(link.resolve().name)
            except OSError:
                continue
    return devs
