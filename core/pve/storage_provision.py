#!/usr/bin/env python3
from __future__ import annotations
from typing import Any
"""启动时自动确保 pvewebtools 工作区存储存在（可选由本机兼任 NFS server）。

纯标准库实现，仅在 PVE 宿主机（root）运行。任意步骤失败仅打印告警、不阻断启动。
复用 pve_api 的 create_storage_cfg / list_storage_cfg（pvesh 封装），不重复造轮子。

设计要点：
- 配置驱动（config/storage.conf）：WORKSPACE_TYPE=nfs|dir|留空(自动)；
- 幂等：已存在且类型/content 一致则跳过；旧 nfs 残留统一转 dir（仅改元数据，文件原地保留），不丢文件。
- 未配 NFS 时自动退回本地 dir，保证工作区永远在；
- NFS_SERVER_MODE=1（默认开）时本机安装并导出 NFS server，pvewebtools 自动成为 NFS 共享存储；
  关闭则退回本地 dir。导出目录=WORKSPACE_PATH，项目跑在哪台、哪台就是 NFS server。
- PVE 存储注册类型**恒为 dir**，路径恒为 WORKSPACE_PATH（server=本地目录；client=同路径 NFS 挂载，由本工具在 OS 层完成）。
  不按节点角色切换 nfs/dir，避免共享 storage.cfg 被 server(dir)/client(nfs) 互相改写；NFS 挂载由本工具负责，PVE 只认目录。
- pvewebtools 根下固定子目录（由 ensure_workspace_dirs() 统一创建，缺则建、存在则跳过）：
  dump/ 存放虚机模板（PVE backup 内容目录）、cloud-images/ 存放云镜像、disk-images/ 存放磁盘镜像、
  vbios/ 存放显卡 vBIOS。
  cloud-images/disk-images/vbios 为项目自管目录；
  dump/ 为 PVE backup 内容目录，虚机模板直接落于此处供 PVE 识别。
  所有项目初始化（启动 / 节点接入）均复用 ensure_workspace_dirs()，不走分散创建。
- pvewebtools 默认声明 备份(backup) / 容器模板(vztmpl) 两类内容（见 DEFAULT_CONTENT，可由配置覆盖）；
  不声明 images/rootdir 以免 PVE 在根下另建 images/private 目录与项目自管目录混淆。
"""
import ipaddress
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from core import paths
from core.pve import pve_api

_CONFIG = Path(paths.RESOURCE_DIR) / "config" / "storage.conf"

WORKSPACE_SID = "pvewebtools"
# pvewebtools 默认声明的 PVE 内容类型：备份(backup) + 容器模板(vztmpl)。
# - backup：备份文件落 <根>/dump，PVE 可识别；
# - vztmpl：容器模板落 <根>/template/cache，配合「获取 LXC 模板」下载后 PVE 立即可见（pveam list / 建 CT 下拉），
#   且 pvewebtools 是集群共享 NFS 存储，模板一次下载全节点可用。
# 不声明 images/rootdir（会另建 images/private 目录，与项目自管目录混淆）；
# 留空（""）表示不声明，此时 PVE 会退回插件默认（dir/nfs 默认=全部内容类型）；
# 可用 config/storage.conf 的 CONTENT 覆盖。
DEFAULT_CONTENT = "backup,vztmpl"
DEFAULT_DIR_PATH = "/var/lib/vz/pvewebtools"
# 启动时是否把本机 local 存储的内容类型设为全量（1=开，默认）；0=完全不碰 local 配置
DEFAULT_LOCAL_ALL_CONTENT = "1"
# local 存储要启用的全量内容类型；import 为较新 PVE 才支持，被拒绝时会自动退回不含 import 的列表
DEFAULT_LOCAL_CONTENT = "images,rootdir,vztmpl,iso,backup,snippets,import"

# pvewebtools 根下的固定子目录；项目启动时由 ensure_workspace_dirs() 统一创建（缺则建、存在则跳过）
# - dump/ 为 PVE backup 内容目录，虚机模板直接落于此；
# - template/cache/ 为 PVE vztmpl 内容目录，LXC 模板下载落于此（提前建好，避免首次下载时目录缺失）；
# - cloud-images/disk-images/vbios 为项目自管目录，避开 PVE 其它内容目录名。
WORKSPACE_SUBDIRS = ("dump", "cloud-images", "disk-images", "vbios", "template/cache")


def _load_config() -> Any:
    cfg = {
        "WORKSPACE_SID": WORKSPACE_SID,
        "WORKSPACE_TYPE": "",
        "NFS_SERVER": "",
        "NFS_EXPORT": "",
        "WORKSPACE_PATH": DEFAULT_DIR_PATH,
        "CONTENT": DEFAULT_CONTENT,
        "LOCAL_SID": "local",
        "LOCAL_ALL_CONTENT": DEFAULT_LOCAL_ALL_CONTENT,
        "LOCAL_CONTENT": DEFAULT_LOCAL_CONTENT,
        "NFS_SERVER_MODE": "1",
        "LAN_CIDR": "192.168.0.0/16",
    }
    if _CONFIG.is_file():
        for raw in _CONFIG.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def _is_true(v: Any) -> Any:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _existing_storages() -> Any:
    try:
        return {s.get("storage"): s for s in pve_api.list_storage_cfg()}
    except Exception:
        return {}


def _resolve_content(cfg: Any) -> Any:
    """pvewebtools 默认声明的 PVE 内容类型（DEFAULT_CONTENT：images/rootdir/backup/vztmpl）。
    留空表示交给 PVE 插件默认；可用配置 CONTENT 覆盖。"""
    return cfg.get("CONTENT", DEFAULT_CONTENT)


def _norm_content(content: str) -> Any:
    """把 content 串归一化为排序后的集合（用于比较是否一致）。"""
    return set(c for c in (content or "").split(",") if c)


def _ensure_dir_subdirs(path: str, content: str) -> None:
    """dir 类型存储：确保各 content 对应的标准子目录存在（PVE 激活时也会建，这里兜底）。
    映射必须与 PVE 实际布局一致，否则会凭空多建无用目录：
    iso→template/iso、vztmpl→template/cache、backup→dump、snippets→snippets、
    images→images、rootdir→private（容器 rootfs，与 local 下的 private/ 同款约定）、import→import。"""
    sub = {
        "iso": "template/iso", "vztmpl": "template/cache", "backup": "dump",
        "images": "images", "rootdir": "private", "snippets": "snippets",
        "import": "import",
    }
    base = Path(path)
    for c in _norm_content(content):
        d = sub.get(c)
        if d:
            try:
                (base / d).mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                pass


def _host_ip_in_cidr(cidr: Any) -> Any:
    """返回本机落在 cidr 内的第一个非回环 IPv4；找不到则返回任意非回环 IPv4；都没有返回 ""。"""
    net = None
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except Exception:
        net = None
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        out = ""
    any_ip = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3].split("/")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except Exception:
            continue
        if ip.is_loopback:
            continue
        if net and ip in net:
            return addr
        if not any_ip:
            any_ip = addr
    return any_ip


def _resolve_base(cfg: Any) -> Any:
    """解析并归一化存储基础参数 (wtype, server, export, path)，不含 sid 与 content。
    与 pvewebtools 完全一致，保证按子目录注册的附属存储跨节点同构。"""
    wtype = (cfg.get("WORKSPACE_TYPE") or "").lower()
    if wtype not in ("nfs", "dir"):
        # 自管 NFS server 开 → 本机即 NFS server，直接走 nfs；否则有 NFS_SERVER 才 nfs，否则 dir
        if _is_true(cfg.get("NFS_SERVER_MODE", "0")):
            wtype = "nfs"
        else:
            wtype = "nfs" if cfg.get("NFS_SERVER") else "dir"

    path = cfg.get("WORKSPACE_PATH") or DEFAULT_DIR_PATH
    server = cfg.get("NFS_SERVER")
    export = cfg.get("NFS_EXPORT")
    # 本机兼任 NFS server 时，NFS_SERVER 未填则自动用本机 IP
    if wtype == "nfs":
        if not server and _is_true(cfg.get("NFS_SERVER_MODE", "0")):
            ip = _host_ip_in_cidr(cfg.get("LAN_CIDR") or "0.0.0.0/0")
            if ip:
                server = ip
        # 客户端未显式给 NFS_EXPORT 时，导出路径取 server 端默认导出根（/var/lib/vz/pvewebtools），
        # 而非本机挂载点 path（如 /mnt/pve/pvewebtools），否则会去挂载一个服务端并不存在的 server:本机路径。
        # 服务端默认导出根见 ensure_nfs_server()，与 DEFAULT_DIR_PATH 一致。
        if not export:
            export = DEFAULT_DIR_PATH
    return wtype, server, export, path


def _sub_path(base_path: str, subdir: Any) -> Any:
    """根目录下的子目录绝对路径（dir 存储用）。"""
    return str(Path(base_path) / subdir) if subdir else base_path


def _sub_export(export: Any, subdir: Any) -> Any:
    """根导出路径下的子导出路径（NFS 存储用，拼成 posix 路径）。"""
    return export.rstrip("/") + "/" + subdir if subdir else export


def _ensure_one_storage(cfg: Any, sid: Any, subdir: Any, content: str) -> None:
    """确保单个工作区存储存在且 content 正确。

    subdir 为空表示使用根目录（pvewebtools）；否则在根下创建子目录（如 dump）。

    统一注册为 `dir` 类型、路径恒为 WORKSPACE_PATH：
      - server 节点：WORKSPACE_PATH 即本地真实目录；
      - client 节点：同一 WORKSPACE_PATH 是 NFS 挂载点（由 ensure_mount 在 OS 层挂好）。
    这样无论 server 还是 client，写入共享 storage.cfg 的都是 `dir <同一路径>`，
    跨节点一致、不再有 server(dir)/client(nfs) 互相改写的 ping-pong；NFS 由本工具挂载，
    PVE 只需认得「一个目录」，也不会去挂载自身。
    幂等：已存在且类型/content 一致则跳过；旧 nfs 残留统一转 dir（仅改元数据，文件原地保留）。
    """
    wtype, server, export, base_path = _resolve_base(cfg)
    # 存储类型恒为 dir：NFS 挂载已独立于本函数（见 ensure_mount / node_join_mount_nfs），
    # 不复用 wtype 的 nfs 分支，避免把共享 storage.cfg 改写成 nfs（client 会把 server 的 dir 条目改写）。
    wtype = "dir"
    existing = _existing_storages()

    sub_path = _sub_path(base_path, subdir)
    sub_export = _sub_export(export, subdir)

    def _params_for(t: Any) -> Any:
        # content 为空时不向 pvesh 传 --content（空串会被 PVE 拒绝：
        # "storage does not support content type 'none'"）；留空让 PVE 取默认值即可。
        # 默认只声明 backup：会被后端存储扫描命中（如 backup_dirs 拿到 <根>/dump），
        # 前端工具下拉用 isSharedStorage 屏蔽本存储，避免与各工具的目标存储混淆。
        params = {"storage": sid, "type": t}
        if t == "nfs":
            params["server"] = server
            params["export"] = sub_export
        else:
            params["path"] = sub_path
        if content:
            params["content"] = content
        return params

    if sid in existing:
        cur = existing[sid]
        if cur.get("type") == wtype:
            # 类型一致：比对 content，不一致则更新（避免旧 content 被永久跳过）。
            # content 为空表示「不管理内容类型」，直接跳过更新（空串传入 pvesh 会报错）。
            if not content or _norm_content(cur.get("content")) == _norm_content(content):
                print(f"[workspace] 存储 {sid}({wtype}) 已存在且 content 一致, 跳过")
                return
            try:
                pve_api.update_storage_cfg(sid, {"content": content})
                print(f"[workspace] 存储 {sid}({wtype}) content 已更新为: {content}")
                _ensure_dir_subdirs(sub_path, content)
            except Exception as e:  # noqa: BLE001
                print(f"[workspace] 更新 {sid} content 失败: {e}; 跳过")
            return
        # 类型不符（多为旧 nfs 残留或跨节点不一致）：统一转 dir 重新注册（仅改元数据，文件原地保留）
        print(f"[workspace] {sid} 当前为 {cur.get('type')}, 统一转为 dir(文件原地保留)...")
        try:
            pve_api.delete_storage_cfg(sid)
            pve_api.create_storage_cfg(_params_for("dir"))
            _ensure_dir_subdirs(sub_path, content)
            print(f"[workspace] 已转换 {sid} -> dir({sub_path})")
        except Exception as e:  # noqa: BLE001
            print(f"[workspace] 转换 {sid} -> dir 失败: {e}; 保留原存储, 请手动处理")
        return

    # 新建
    try:
        Path(sub_path).mkdir(parents=True, exist_ok=True)
        pve_api.create_storage_cfg(_params_for("dir"))
        _ensure_dir_subdirs(sub_path, content)
        print(f"[workspace] 已创建本地 dir 存储 {sid} -> {sub_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] 创建本地 dir 存储 {sid} 失败: {e}")


def ensure_workspace_storage(cfg: Any) -> None:
    """确保工作区存储存在（nfs 或 dir）。
    pvewebtools：工具自管文件根，默认声明 备份(backup)/容器模板(vztmpl)（见 DEFAULT_CONTENT，可由配置覆盖）。
    其下固定子目录（dump/cloud-images/disk-images/vbios）由 ensure_workspace_dirs() 统一创建，
    此处只负责把 pvewebtools 注册成 PVE 存储（供 storage_path() 定位根目录）。"""
    # pvewebtools（根目录 + 默认四类内容类型）
    _ensure_one_storage(cfg, WORKSPACE_SID, "", _resolve_content(cfg))


def ensure_local_content(cfg: Any = None) -> None:
    """把 local 存储的内容类型设为全量（默认开启，幂等）。

    背景：后端的存储扫描全部按 content 过滤（pve_api._storage_dirs：dir 型 + content 命中），
    因此 local 只有声明了对应内容类型才会出现在工具下拉与扫描结果里。全量打开后，
    local 可作为磁盘镜像 / 备份 / ISO / 容器模板 / snippets 的统一落点。

    由 LOCAL_ALL_CONTENT 控制总开关（默认 1，设为 0 则完全不碰 local 配置），
    LOCAL_SID 指定存储 id（默认 local），LOCAL_CONTENT 指定内容列表。
    content 与目标一致时不做任何写入；存储不存在/已禁用时跳过；失败仅告警，不阻断启动。"""
    if cfg is None:
        cfg = _load_config()
    if not _is_true(cfg.get("LOCAL_ALL_CONTENT", DEFAULT_LOCAL_ALL_CONTENT)):
        return
    sid = (cfg.get("LOCAL_SID") or "local").strip()
    try:
        cur = pve_api.get_storage_cfg(sid) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[local] 读取存储 {sid} 配置失败: {e}")
        return
    if not cur.get("type"):
        print(f"[local] 未找到存储 {sid}，跳过内容类型设置")
        return
    if cur.get("disable"):
        print(f"[local] 存储 {sid} 已禁用，跳过内容类型设置")
        return
    want = str(cfg.get("LOCAL_CONTENT") or DEFAULT_LOCAL_CONTENT).strip()
    if _norm_content(cur.get("content")) == _norm_content(want):
        print(f"[local] 存储 {sid} content 已是全量, 跳过")
        return
    # 老版本 PVE 不支持 import 内容类型：先按全量列表设置，失败再退回不含 import 的列表
    fallback = ",".join(c for c in want.split(",") if c.strip() and c.strip() != "import")
    for candidate in (want, fallback):
        if not candidate:
            continue
        try:
            pve_api.update_storage_cfg(sid, {"content": candidate})
            print(f"[local] 存储 {sid} content 已设置为: {candidate}")
            return
        except Exception as e:  # noqa: BLE001
            print(f"[local] 设置 {sid} content={candidate} 失败: {e}")
    print(f"[local] 存储 {sid} 内容类型设置未成功，跳过（不影响启动）")

def ensure_workspace_dirs(cfg: Any = None) -> None:
    """项目初始化核心：确保 pvewebtools 根目录及其固定子目录存在（缺则创建，幂等）。

    统一走此函数，所有项目初始化（启动 / 节点接入 / NFS 挂载后）均复用，避免目录散落创建。
    固定子目录分工：
      - dump/         ：传输中心上传的虚机模板（PVE backup 内容目录）
      - cloud-images/ ：含 cloud-init 的云镜像（供云镜像建机 / 制作直接可用）
      - disk-images/  ：普通磁盘镜像（无 cloud-init，供 qm importdisk 磁盘导入 / 制作云镜像原料）
      - vbios/        ：显卡 vBIOS 主副本（跨节点软链到 /usr/share/kvm）
    """
    if cfg is None:
        cfg = _load_config()
    root = Path(cfg.get("WORKSPACE_PATH") or DEFAULT_DIR_PATH)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] 创建根目录失败: {root}: {e}")
        return
    for sub in WORKSPACE_SUBDIRS:
        try:
            (root / sub).mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            print(f"[workspace] 创建子目录失败: {root / sub}: {e}")
    print(f"[workspace] 工作区目录已就绪: {root} ({', '.join(WORKSPACE_SUBDIRS)})")


def migrate_legacy_layout(cfg: Any = None) -> None:
    """目录布局迁移（幂等）：旧版 .pvewebtools/{import,vbios} -> cloud-images/ / vbios/。
    仅当共享存储可见时执行；逐个移动文件，目标已存在则保留新布局、删除旧文件避免堆积。
    属一次性升级兼容，已并入统一初始化入口 ensure_storage()。"""
    if cfg is None:
        cfg = _load_config()
    try:
        root = pve_api.storage_path("pvewebtools")
        if not root:
            return
        _move_subdir(root / ".pvewebtools" / "import", root / "cloud-images")
        _move_subdir(root / ".pvewebtools" / "vbios", root / "vbios")
        # 兼容旧版单机模式：默认导入目录曾落在 /var/lib/vz/.pvewebtools/{import,vbios}
        _move_subdir(Path("/var/lib/vz/.pvewebtools/import"), Path("/var/lib/vz/cloud-images"))
        _move_subdir(Path("/var/lib/vz/.pvewebtools/vbios"), Path("/var/lib/vz/vbios"))
    except Exception:  # noqa: BLE001
        pass


def _move_subdir(src: str, dst: str) -> None:
    """把 src 目录下的内容移动到 dst（dst 不存在则建）；src 不存在直接跳过。"""
    if not src.is_dir():
        return
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = dst / item.name
            if target.exists():
                # 新布局优先：删除旧文件避免堆积
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                except OSError:
                    pass
                continue
            try:
                item.rename(target)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def ensure_nfs_server(cfg: Any) -> None:
    """可选：本机兼任 NFS server，导出 WORKSPACE_PATH 供其它节点挂载。"""
    if not _is_true(cfg.get("NFS_SERVER_MODE", "0")):
        return
    export_dir = cfg.get("WORKSPACE_PATH") or DEFAULT_DIR_PATH
    try:
        Path(export_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        print(f"[nfs] 创建导出目录失败: {e}; 跳过自管 NFS server")
        return

    installed = False
    # 1) 安装 nfs-kernel-server（缺失时，仅走在线 apt 源）
    if shutil.which("exportfs") is None:
        print("[nfs] 未检测到 exportfs(nfs-kernel-server), 尝试 apt 安装...")
        try:
            subprocess.run(["apt-get", "update"], check=False,
                           capture_output=True, text=True, timeout=300)
            r = subprocess.run(["apt-get", "install", "-y", "nfs-kernel-server"],
                           check=False, capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and shutil.which("exportfs"):
                installed = True
                print("[nfs] installed nfs-kernel-server via apt")
        except Exception as e:  # noqa: BLE001
            print(f"[nfs] 安装异常: {e}")
    if not shutil.which("exportfs"):
        print("[nfs] 安装失败或不支持, 退回本地 dir 模式(不启用自管 NFS server)")
        return
    # 2) 写 /etc/exports（去重）
    cidr = cfg.get("LAN_CIDR") or "192.168.0.0/16"
    line = f"{export_dir} {cidr}(rw,no_root_squash,no_subtree_check,sync)\n"
    exports = Path("/etc/exports")
    text = exports.read_text(encoding="utf-8") if exports.is_file() else ""
    if export_dir not in text:
        try:
            with exports.open("a", encoding="utf-8") as f:
                f.write("\n# pvewebtools managed\n" + line)
        except Exception as e:  # noqa: BLE001
            print(f"[nfs] 写入 /etc/exports 失败: {e}; 跳过")
            return

    # 3) 导出 + 起服务
    try:
        subprocess.run(["exportfs", "-ra"], check=False,
                       capture_output=True, text=True, timeout=60)
        subprocess.run(["systemctl", "enable", "--now", "nfs-kernel-server"],
                       check=False, capture_output=True, text=True, timeout=60)
        print(f"[nfs] 已导出 {export_dir} 给 {cidr}")
    except Exception as e:  # noqa: BLE001
        print(f"[nfs] 导出/启动失败: {e}")


def _is_self_server(cfg: Any) -> Any:
    """本节点是否为 NFS server（或本地 dir 模式）：是则无需挂载客户端。"""
    wtype, server, export, path = _resolve_base(cfg)
    if wtype != "nfs":
        return True
    if not server:
        return True
    my = _host_ip_in_cidr(cfg.get("LAN_CIDR") or "0.0.0.0/0")
    return bool(my) and server == my


def _parse_mounts() -> Any:
    """解析 `mount` 输出为 {挂载点(target): 源(source)} 字典，用于精确比对挂载源。"""
    out = {}
    try:
        mt = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return out
    for line in mt.splitlines():
        if " on " not in line:
            continue
        src_part, rest = line.split(" on ", 1)
        target = rest.split(" type ", 1)[0].strip()
        out[target] = src_part.strip()
    return out


# NFS 多连接数：单流吞吐常被单 TCP 连接限制（NFSv4.1+ 的多连接可显著提速）。
# 只在新挂载时带上：内核 <5.3 或服务端不支持时，去掉该选项自动重挂（绝不能因此挂不上共享存储）。
NFS_NCONNECT = 8
NFS_BASE_OPTS = "defaults,_netdev"


def _kernel_supports_nconnect() -> Any:
    """内核 ≥5.3 才支持 NFS 客户端多连接（nconnect）；版本解析不出来按不支持处理。"""
    m = re.match(r"^(\d+)\.(\d+)", platform.release() or "")
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (5, 3)


def _mounted_with_nconnect(mountpoint: Any) -> bool:
    """当前挂载选项里是否真带上了 nconnect（防内核静默忽略，提示与实际一致）。"""
    try:
        for ln in Path("/proc/mounts").read_text(errors="ignore").splitlines():
            f = ln.split()
            if len(f) > 3 and f[1] == mountpoint and "nconnect=" in f[3]:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _fstab_set_nfs_nconnect(fstab: Any, src: str, mountpoint: Any, enable: bool) -> bool:
    """确保 fstab 里 src→mountpoint 的 nfs 行带/不带 nconnect。

    - 已有该行：只在其选项里增删 nconnect，保留其它既存选项（如 noatime）；
    - 没有该行：追加一条受管行。
    内容无变化时不写盘；写失败只告警（返回 False），不阻断挂载。
    """
    want = f"nconnect={NFS_NCONNECT}"
    pat = re.compile(r"^\s*" + re.escape(src) + r"\s+" + re.escape(mountpoint)
                     + r"\s+nfs\s+([^ \t]+)(\s+\S+\s+\S+)\s*$")
    try:
        lines = fstab.read_text(encoding="utf-8").splitlines() if fstab.is_file() else []
    except Exception as e:  # noqa: BLE001
        print(f"[mount] 读取 /etc/fstab 失败: {e}")
        return False
    out, hit = [], False
    for ln in lines:
        m = pat.match(ln)
        if not m or hit:
            out.append(ln)
            continue
        hit = True
        opts = [o for o in m.group(1).split(",") if o and not o.startswith("nconnect=")]
        if enable:
            opts.append(want)
        out.append(f"{src} {mountpoint} nfs {','.join(opts)}{m.group(2)}")
    if not hit:
        opts = NFS_BASE_OPTS + (f",{want}" if enable else "")
        out.extend(["", "# pvewebtools managed", f"{src} {mountpoint} nfs {opts} 0 0"])
    if out == lines:
        return True   # 已是目标状态，不碰文件（保持幂等、不动 mtime）
    try:
        fstab.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[mount] 写入 /etc/fstab 失败: {e}")
        return False
    return True


def _reconcile_client_mount(cfg: Any, server: Any, export: Any, mountpoint: Any) -> None:
    """确保 server:export 已挂到 mountpoint（自动纠正错误挂载 + 自动挂载）。非阻断。

    - 已正确挂载：跳过；
    - 目标挂载点挂了错误源（如导出子目录 /dump 而非根、或别的服务器/导出）：惰性卸载旧挂载后再按正确源挂载；
    - 未挂载：建挂载点、写 fstab 持久化、执行挂载；成功后自动清理同服务器子目录的遗留挂载。
    """
    src = f"{server}:{export}"
    # 解析当前挂载表为 {target: source}，用于精确比对挂载源，避免子串误判：
    # 例如 /var/lib/vz/pvewebtools 被错误挂成 .../pvewebtools/dump 时，
    # src(.../pvewebtools) 仍是其子串，子串匹配会误判为“已正确挂载”而跳过纠正。
    mounts = _parse_mounts()
    current_src = mounts.get(mountpoint)

    # 检测目标挂载点是否为陈旧(stale) NFS 句柄：目录“存在”但 stat 抛 OSError（非 ENOENT）。
    # 陈旧句柄也会出现在挂载表里、且源匹配，会被下方误判为“已正确挂载”，故必须先强制卸载。
    stale = False
    try:
        os.stat(mountpoint)
    except FileNotFoundError:
        pass  # 目录不存在，正常，稍后创建
    except OSError as e:
        if "Stale" in str(e) or e.errno in (5, 116):  # EIO / ESTALE
            stale = True
            print(f"[mount] {mountpoint} 为陈旧 NFS 句柄, 强制卸载后重挂...")
            try:
                subprocess.run(["umount", "-f", "-l", mountpoint], check=False,
                               capture_output=True, text=True, timeout=30)
            except Exception:  # noqa: BLE001
                pass
            mounts = _parse_mounts()
            current_src = mounts.get(mountpoint)
        else:
            print(f"[mount] 检测 {mountpoint} 异常({e}), 按挂载异常处理")
    # 已正确挂载且可访问（精确源匹配，排除陈旧句柄与错误子目录/错误导出）
    if not stale and current_src == src:
        print(f"[mount] {src} 已正确挂载于 {mountpoint}")
        ensure_workspace_dirs(cfg)
        return
    # 目标挂载点挂了错误源（错误子目录如 .../pvewebtools/dump、或别的导出/服务器）：
    # 惰性卸载旧挂载后再按正确源挂载（同服务器根导出，数据不丢）
    if mountpoint in mounts:
        print(f"[mount] 检测到 {mountpoint} 挂载源异常({current_src} ≠ {src}), 尝试纠正...")
        try:
            subprocess.run(["umount", "-l", mountpoint], check=False,
                           capture_output=True, text=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    try:
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        print(f"[mount] 创建挂载点失败: {e}; 跳过")
        return
    fstab = Path("/etc/fstab")
    # 新挂载时自动带 nconnect（内核 ≥5.3 才试），失败自动回退——共享存储挂不上比少个优化严重得多
    want_nc = _kernel_supports_nconnect()
    _fstab_set_nfs_nconnect(fstab, src, mountpoint, want_nc)
    # 执行挂载（优先 fstab，失败显式 mount -t nfs）
    r = subprocess.run(["mount", mountpoint], check=False, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        r = subprocess.run(["mount", "-t", "nfs", src, mountpoint], check=False,
                           capture_output=True, text=True, timeout=60)
    if r.returncode != 0 and want_nc:
        # nconnect 被拒（老内核 / 服务端 NFS<4.1 / 内核未启用多连接）时不能让共享存储挂不上：
        # 去掉该选项后重挂一次，并按「实际能挂上的选项」持久化到 fstab
        print(f"[mount] 带 nconnect={NFS_NCONNECT} 挂载失败，去掉该选项重试: {(r.stderr or '').strip()[:200]}")
        _fstab_set_nfs_nconnect(fstab, src, mountpoint, False)
        r = subprocess.run(["mount", mountpoint], check=False, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            r = subprocess.run(["mount", "-t", "nfs", src, mountpoint], check=False,
                               capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        if want_nc and _mounted_with_nconnect(mountpoint):
            print(f"[mount] 已挂载 {src} -> {mountpoint}（nconnect={NFS_NCONNECT} 多连接）")
        else:
            print(f"[mount] 已挂载 {src} -> {mountpoint}")
        ensure_workspace_dirs(cfg)
        # 清理同服务器子目录遗留挂载（如旧 /mnt/pve/dump 指向 .../pvewebtools/dump），数据已含于根挂载
        sub = f"{src}/dump"
        try:
            mt2 = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
            for line in mt2.splitlines():
                if " /mnt/pve/dump " in line and sub in line:
                    subprocess.run(["umount", "-l", "/mnt/pve/dump"], check=False,
                                   capture_output=True, text=True, timeout=30)
                    print("[mount] 已清理遗留挂载 /mnt/pve/dump")
        except Exception:  # noqa: BLE001
            pass
    else:
        print(f"[mount] 挂载失败: {r.stderr.strip()[:300]}")
        print(f"[mount] 请确认 {server} 已在 /etc/exports 导出 {export}"
              f"（注意导出根目录 {export}, 而非其子目录如 {export}/dump）")


def node_join_mount_nfs(cfg: Any = None) -> Any:
    """新节点加入集群时的 NFS 客户端挂载（供节点初始化调用）。

    把 NFS 共享导出挂到 WORKSPACE_PATH，并持久化进 /etc/fstab；之后 pvewebtools
    共享存储里的备份/云镜像即全集群可见，vBIOS 再由调用方铺软链到 /usr/share/kvm。
    本机即为 NFS server（NFS_SERVER_MODE=1）或 NFS_SERVER 指向本机时跳过——导出目录本就是本地目录。
    复用 _reconcile_client_mount()（与启动自动挂载同一逻辑）。返回 (ok: bool, msg: str)。"""
    if cfg is None:
        cfg = _load_config()
    if _is_self_server(cfg):
        return True, "本节点为 NFS server(或本地 dir), 无需挂载客户端"
    wtype, server, export, path = _resolve_base(cfg)
    if wtype != "nfs" or not server:
        return False, "未配置 NFS_SERVER, 无法挂载客户端"
    _reconcile_client_mount(cfg, server, export, path)
    try:
        if _parse_mounts().get(path) == f"{server}:{export}":
            return True, f"已挂载 {server}:{export} -> {path}"
    except Exception:  # noqa: BLE001
        pass
    return False, f"挂载 {server}:{export} -> {path} 未成功, 请检查 NFS server 导出"


def _cleanup_legacy_mounts(cfg: Any = None) -> None:
    """清理早期版本遗留的 NFS 挂载（现已统一为 dir 类型，工作区在 WORKSPACE_PATH，
    不再使用 /mnt/pve 下的 PVE 托管挂载点）。

    需清理的遗留项：
      - /mnt/pve/dump         ：旧误挂 .../pvewebtools/dump 子目录
      - /mnt/pve/pvewebtools  ：旧 PVE 以 nfs 类型托管挂载本机导出（现改为 dir 后成为僵尸/陈旧句柄）
    两者源均含 "pvewebtools"；若某挂载点恰好就是当前 WORKSPACE_PATH 则跳过（那是新设计的正规挂载）。"""
    if cfg is None:
        cfg = _load_config()
    ws_path = (cfg.get("WORKSPACE_PATH") or "").rstrip("/")
    legacy = ["/mnt/pve/dump", "/mnt/pve/pvewebtools"]
    try:
        mt = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        mt = ""
    # 检测陈旧句柄：目录存在但 stat 抛 ESTALE/EIO（出现在 mount 列表里、源匹配，却被误判为正常）
    stale_mps = set()
    for mp in legacy:
        if mp.rstrip("/") == ws_path:
            continue
        try:
            os.stat(mp)
        except FileNotFoundError:
            pass
        except OSError as e:
            if "Stale" in str(e) or e.errno in (5, 116):  # EIO / ESTALE
                stale_mps.add(mp)
    for mp in legacy:
        if mp.rstrip("/") == ws_path:
            continue
        present = any(f" {mp} " in ln for ln in mt.splitlines())
        if not present and mp not in stale_mps:
            continue
        print(f"[mount] 发现遗留挂载 {mp}, 尝试卸载...")
        try:
            subprocess.run(["umount", "-f", "-l", mp], check=False,
                           capture_output=True, text=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        # 移除 fstab 中可能残留的条目，避免重启后再次挂上
        fstab = Path("/etc/fstab")
        try:
            if fstab.is_file():
                lines = fstab.read_text(encoding="utf-8").splitlines()
                kept = [ln for ln in lines if mp not in ln]
                if len(kept) != len(lines):
                    fstab.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    print(f"[mount] 已移除 fstab 中 {mp} 残留条目")
        except Exception:  # noqa: BLE001
            pass


def ensure_mount(cfg: Any = None) -> None:
    """启动入口：确保本节点工作区挂载就绪（自动修复错误挂载 + 自动挂载）。

    本机即 NFS server 或本地 dir 模式：仅确保本地目录存在，无需挂载；
    其余情况按 config 把 server:export 挂到 WORKSPACE_PATH（幂等、写 fstab 持久化、
    若挂错位置自动纠正）。失败仅告警、不阻断启动。"""
    if cfg is None:
        cfg = _load_config()
    if _is_self_server(cfg):
        # 本机即 NFS server（或本地 dir）：工作区即本地目录，无需挂载；
        # 目录由 ensure_storage() 末尾的 ensure_workspace_dirs() 统一确保（避免重复打印）
        return
    wtype, server, export, path = _resolve_base(cfg)
    if wtype != "nfs" or not server:
        # 未配置 NFS 的本地 dir 模式：目录同样由 ensure_storage() 末尾统一确保
        return
    _reconcile_client_mount(cfg, server, export, path)


def ensure_storage() -> None:
    """启动入口：先确保 NFS server（若开启），再确保本地挂载（自动修复+自动挂载），
    然后确保工作区存储。失败不阻断启动。"""
    try:
        cfg = _load_config()
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] 读取存储配置失败: {e}")
        return
    try:
        ensure_nfs_server(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[nfs] 自管 server 异常: {e}")
    try:
        ensure_mount(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[mount] 自动挂载异常: {e}")
    try:
        _cleanup_legacy_mounts(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[mount] 清理遗留挂载异常: {e}")
    try:
        ensure_workspace_storage(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] 异常: {e}")
    try:
        ensure_workspace_dirs(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] 目录初始化异常: {e}")
    try:
        ensure_local_content(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[local] 异常: {e}")
    try:
        migrate_legacy_layout(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] 旧目录布局迁移异常: {e}")


if __name__ == "__main__":
    ensure_storage()
    sys.exit(0)
