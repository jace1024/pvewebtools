"""vBIOS 物料清单与集群同步（物料层）。

管理 /usr/share/kvm 下的 vBIOS ROM 主副本与清单文件，以及向集群各节点同步/删除。
被 transfer（上传/删除/列表）与 node（加机时铺软链）两个域共用，故独立成模块。
"""

import os
import re
import shlex
import shutil
import socket
import subprocess

from core.pve import pve_api as backend
from core.web.probe import _SSH_CTRL
from pathlib import Path


VBIOS_TARGET_DIR = Path("/usr/share/kvm")

VBIOS_LEGACY_MANIFEST = VBIOS_TARGET_DIR / ".pvewebtools-uploaded"


def _vbios_shared_dir():
    """vBIOS 主副本目录（pvewebtools 共享存储根下的 vbios/，NFS 共享，跨节点可见）。
    通过 storage_path("pvewebtools") 稳健取存储根。
    共享存储不可用（单节点/未配 NFS）时返回 None，vBIOS 退化为单节点本地模式。"""
    root = backend.storage_path("pvewebtools")
    if root:
        return root / "vbios"
    return None


def _vbios_manifest_path():
    """vBIOS 清单路径：优先共享存储（集群统一），不可用则回退本机。"""
    d = _vbios_shared_dir()
    return d / ".manifest" if d else VBIOS_LEGACY_MANIFEST


def _link_vbios(name, master_path):
    """确保 /usr/share/kvm/<name> 是指向 master_path 的软链。
    master_path 建议为 NFS 共享主副本的绝对路径（全集群同路径挂载，故一致）。
    返回 True 表示成功建立软链。PVE romfile 只认本机 /usr/share/kvm 下的相对文件名，
    软链让它「看起来」是本机文件，实则指向 NFS 主副本，实现「上传一次、全集群共享」。

    安全边界：/usr/share/kvm/ 下混有大量 QEMU 系统固件（vgabios-*.bin / efi-*.rom /
    seabios.bin 等），只允许覆盖「本工具自己建过的软链」或「清单里记录过的本工具文件」；
    清单外的真实文件一律拒绝，防止上传同名文件时把宿主机系统固件替换掉。"""
    try:
        VBIOS_TARGET_DIR.mkdir(parents=True, exist_ok=True)
        if _vbios_target_blocked(name):
            return False
        target = VBIOS_TARGET_DIR / name
        if target.is_symlink():
            target.unlink()   # 本工具之前建的软链，可安全重链
        os.symlink(str(master_path), str(target))
        return True
    except OSError:
        return False


def _read_vbios_manifest():
    """读取本工具上传过的 vBIOS 文件名清单（每行一个，集群共享）"""
    try:
        p = _vbios_manifest_path()
        if not p.is_file():
            return []
        names = [ln.strip() for ln in p.read_text(errors="ignore").splitlines()]
        return [n for n in names if n and re.fullmatch(r"[\w][\w.\-]{0,99}", n)]
    except Exception:
        return []


def _add_vbios_manifest(name):
    """把文件名追加进共享清单（去重），供下拉只显示本工具上传的文件"""
    try:
        names = _read_vbios_manifest()
        if name not in names:
            names.append(name)
        _vbios_manifest_path().write_text("\n".join(names) + "\n", encoding="utf-8")
    except OSError:
        pass   # 清单写失败不阻断上传，文件本身已落盘


def _read_legacy_vbios_manifest(path):
    """读取升级前本机 /usr/share/kvm 下的旧清单（兼容迁移用）"""
    try:
        names = [ln.strip() for ln in path.read_text(errors="ignore").splitlines()]
        return [n for n in names if n and re.fullmatch(r"[\w][\w.\-]{0,99}", n)]
    except Exception:
        return []


def _vbios_is_managed(name):
    """该文件名是否为本工具管理过（新共享清单或旧本机清单里记录过）。

    用于写路径的安全边界：/usr/share/kvm/ 下混有大量 QEMU 系统固件
    （vgabios-*.bin / efi-*.rom / seabios.bin 等），覆盖或删除前必须确认
    「这是本工具自己放进去的文件」，否则可能误伤宿主机系统固件。
    """
    if name in _read_vbios_manifest():
        return True
    return name in _read_legacy_vbios_manifest(VBIOS_LEGACY_MANIFEST)


def _vbios_target_blocked(name):
    """/usr/share/kvm/<name> 是否被「非本工具管理」的真实文件/目录占用（疑似 QEMU 系统固件）。

    本工具自己建的软链不算阻塞（可安全重链）；清单里记录过的本工具真实文件也不算。
    返回 True 表示写路径（覆盖/建链）应拒绝，防止把宿主机系统固件覆盖掉。
    """
    target = VBIOS_TARGET_DIR / name
    if target.is_symlink():
        return False
    if target.is_file():
        return not _vbios_is_managed(name)
    return target.exists()


def _migrate_legacy_vbios():
    """升级兼容：把升级前本机已上传的 vBIOS 主副本 + 清单迁移到共享存储，使其可跨节点同步。幂等。"""
    shared = _vbios_shared_dir()
    if not shared:
        return
    leg = VBIOS_LEGACY_MANIFEST
    if not leg.is_file():
        return
    shared.mkdir(parents=True, exist_ok=True)
    for n in _read_legacy_vbios_manifest(leg):
        src = VBIOS_TARGET_DIR / n
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, shared / n)
            # 原节点本机副本由真实文件转为指向 NFS 主副本的软链（统一软链模型）
            _link_vbios(n, shared / n)
        except OSError:
            continue
        _add_vbios_manifest(n)


def _vbios_sync_to_nodes():
    """把共享存储里的 vBIOS 主副本以软链形式铺到集群其它节点的 /usr/share/kvm/
    （PVE romfile 只认本机该目录）。共享存储在所有节点同路径挂载（NFS），远端直接
    ln -sf 同一绝对路径即可，无需 scp 传文件。返回 {"ok":[节点...], "failed":[原因...]}。"""
    _migrate_legacy_vbios()
    shared = _vbios_shared_dir()
    if not shared or not shared.is_dir():
        return {"ok": [], "failed": ["共享存储不可用，无法同步"]}
    names = [n for n in _read_vbios_manifest() if (shared / n).is_file()]
    if not names:
        return {"ok": [], "failed": []}
    try:
        nodes = [n.get("node") for n in (backend.list_nodes() or [])]
    except Exception as e:
        return {"ok": [], "failed": [f"无法获取节点列表: {e}"]}
    self_node = socket.gethostname().split(".")[0]
    ok, failed = [], []
    for node in nodes:
        if not node or node == self_node:
            continue
        # 远端：建目录 + 把本机 /usr/share/kvm 下的项改为指向 NFS 主副本的软链
        # （共享存储集群同路径挂载，远端 ln -sf 同一绝对路径即可，无需 scp 传文件）
        script = "set -e\nmkdir -p /usr/share/kvm\n" + "".join(
            f"ln -sf {shlex.quote(str(shared / n))} /usr/share/kvm/{shlex.quote(n)}\n"
            for n in names
        )
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *backend.ssh_host_opts(node),
               f"root@{backend.ssh_host(node)}", "bash -s"]
        try:
            r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                ok.append(node)
            else:
                failed.append(f"{node}: {r.stderr.strip()[:160]}")
        except Exception as e:
            failed.append(f"{node}: {e}")
    return {"ok": ok, "failed": failed}


def _vbios_remove_remote(names):
    """删除 vBIOS 时，尽力移除其它节点 /usr/share/kvm 下的同名软链（避免远端残留死链）。best-effort。"""
    if not names:
        return
    try:
        nodes = [n.get("node") for n in (backend.list_nodes() or [])]
    except Exception:
        return
    self_node = socket.gethostname().split(".")[0]
    script = "set -e\n" + "".join(
        f"rm -f /usr/share/kvm/{shlex.quote(n)}\n" for n in names
    )
    for node in nodes:
        if not node or node == self_node:
            continue
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
               *backend.ssh_host_opts(node),
               f"root@{backend.ssh_host(node)}", "bash -s"]
        try:
            subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=60)
        except Exception:
            pass


def init_vbios_local():
    """vBIOS 本机初始化（幂等）：1) 把旧版本机 vBIOS 迁移到共享存储；2) 把共享存储里的
    vBIOS 主副本软链到本机 /usr/share/kvm（PVE romfile 硬约束）。供启动与节点接入复用，
    确保重启 / 新节点加入后软链自动重建。"""
    try:
        _migrate_legacy_vbios()
    except Exception:  # noqa: BLE001
        pass
    linked = []
    shared = _vbios_shared_dir()
    if shared and shared.is_dir():
        for n in _read_vbios_manifest():
            if (shared / n).is_file() and _link_vbios(n, shared / n):
                linked.append(n)
    return linked

