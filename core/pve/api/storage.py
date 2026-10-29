from __future__ import annotations
from typing import Any
"""存储：列表与配置增删改、存储内容浏览与移动删除、备份文件归档识别。"""

import re
import shlex
import shutil
import socket
import subprocess
import threading
import time

from pathlib import Path
from core.pve.api.base import (  # noqa: F401
    PveError,
    _run_pvesh,
)

MOVABLE_CONTENT_TYPES = ("iso", "vztmpl", "backup", "snippets", "import")
PROJECT_CONTENT_SUBDIR = {"vbios": "vbios", "vgpu": "vgpu-drivers"}
_CONTENT_SUBDIR = {
    "iso": "template/iso",
    "vztmpl": "template/cache",
    "backup": "dump",
    "snippets": "snippets",
    "images": "images",
    "import": "import",
}
_STORAGE_NAME_RE = re.compile(r"^[\w\-]+$")
_STORAGE_SET_IMMUTABLE = {
    "storage", "type", "path", "server", "export",
    "vgname", "thinpool", "pool", "portal", "target",
    "datastore", "monhost", "fs-name",
}


def list_storages() -> Any:
    """集群内全部存储的用量，每个节点上的每块存储一条"""
    data = _run_pvesh("get", "/cluster/resources", ["--type", "storage"])
    return data or []


def list_storage_list() -> Any:
    """列出本机所有已启用存储（含内容类型），供「存储内容」选择器使用。

    存储定义读 /etc/pve/storage.cfg（pmxcfs 内存文件，毫秒级）；读不到才回退 pvesh。
    与 pvesh 版口径一致：跳过 disable，content 缺省按 PVE 默认「images,rootdir」。
    """
    entries = _parse_storage_cfg()
    if entries is not None:
        out = []
        for e in entries:
            if str(e.get("disable", "")) not in ("", "0", "false", "no"):
                continue
            sid = str(e.get("storage", ""))
            if not sid:
                continue
            out.append({
                "storage": sid,
                "type": str(e.get("type", "")),
                "content": str(e.get("content") or _STORAGE_DEFAULT_CONTENT),
            })
        return out
    node = socket.gethostname().split(".")[0]
    data = _run_pvesh("get", f"/nodes/{node}/storage") or []
    out = []
    for s in data:
        if s.get("disabled"):
            continue
        out.append({
            "storage": str(s.get("storage", "")),
            "type": str(s.get("type", "")),
            "content": str(s.get("content", "")),
        })
    return out


_node_avail_cache = {}        # node -> (时间戳, set|None)
_node_avail_lock = threading.Lock()
_NODE_AVAIL_TTL = 30          # active 是分钟级事实：30s 内复用，避免每个下拉都让 PVE 逐存储算 status


def invalidate_node_available_cache(node: Any = None) -> Any:
    """存储增删改/挂载状态变化后丢弃「节点可用存储」缓存。"""
    with _node_avail_lock:
        if node is None:
            _node_avail_cache.clear()
        else:
            _node_avail_cache.pop(str(node), None)


def node_available_storages(node: Any = None) -> Any:
    """某节点上 PVE 认为可用的存储 id 集合；无法判定时返回 None（调用方据此「不过滤」）。

    「存储能否在该节点使用」是节点级事实，判据取 PVE 为该节点算出的两个字段：
    - disabled=1：存储被全局禁用（/etc/pve/storage.cfg 里 disable）；
    - active=0：该节点无法激活它（NFS 未挂载、本地目录不存在等）。
    active 字段缺失时按 1 处理（不过滤），避免老版本 PVE 上误伤。
    返回 None 表示探测失败，此时宁可多列几项，也不要把下拉整个清空
    （曾出现外节点采集失败静默成「无可用选项」的坑）。node 为空表示本机。

    active 只能由 PVE 算出（storage.cfg 里没有），因此这里仍需一次 pvesh；
    但结果按节点缓存 30s：一次页面加载里「建机选存储 / 容器选存储 / 存储内容」会共用同一次探测。
    失败不缓存（下次仍重试），保持「宁可多列不要清空」的原有语义。
    """
    node = str(node or socket.gethostname().split(".")[0])
    now = time.time()
    with _node_avail_lock:
        hit = _node_avail_cache.get(node)
        if hit is not None and now - hit[0] < _NODE_AVAIL_TTL:
            return hit[1]
    try:
        data = _run_pvesh("get", f"/nodes/{node}/storage", quiet=True)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list):
        return None
    val = {str(s.get("storage", "")) for s in data
           if not s.get("disabled") and s.get("active", 1)}
    with _node_avail_lock:
        _node_avail_cache[node] = (now, val)
    return val


def _parse_options_kv(options: list) -> Any:
    """解析前端传入的 `options` 字符串（k=v,k=v），返回额外的 pvesh 参数列表。
    忽略空段/注释（以#开头），值原样转为字符串；键仅允许字母数字下划线短横线。"""
    extra = []
    if not options:
        return extra
    for seg in str(options).split(","):
        seg = seg.strip()
        if not seg or seg.startswith("#"):
            continue
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not re.match(r"^[\w\-]+$", k):
            continue
        extra += ["--" + k, v]
    return extra


def _storage_pvesh_args(params: list) -> Any:
    """将前端提交的存储参数字典转为 pvesh 的 extra_args 列表。
    - content 多选: 逗号分隔
    - options 字段（NFS/CIFS 挂载选项）原样作为 --options 的值
    - 空值不传；但 content 例外：空字符串也要显式传入（--content ""），表示清空内容类型。
      注意 dir/nfs 存储的 content 可选，若直接跳过（不传 --content），PVE 会回退到插件默认=全部内容类型，
      与「取消所有内容」的意图相反，故 content 必须显式透传（含空串）。
    """
    args = []
    if not params:
        return args
    for k, v in params.items():
        if k == "content":
            # content 空串也显式传 --content ""，用于清空内容类型
            args += ["--content", "" if v is None else str(v)]
            continue
        if v is None or v == "":
            continue
        s = str(v).strip()
        if not s:
            continue
        args += ["--" + k, s]
    return args


def _cfg_truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _cfg_default_content(stype: str) -> str:
    """存储未显式声明 content 时 PVE 插件的默认内容类型。"""
    return "backup" if stype == "pbs" else _STORAGE_DEFAULT_CONTENT


_CFG_INT_KEYS = ("disable", "shared", "port")
# storage.cfg 里可能含明文口令（CIFS/PBS 的 password）；pvesh 的存储配置接口从不返回它，
# 这里读文件也必须剔掉，否则「编辑存储」接口会把口令原样吐给前端。
_CFG_SECRET_KEYS = ("password",)


def _storage_cfg_entry(e: Any) -> Any:
    """单个 storage.cfg 段落 → 与 pvesh GET /storage（及 /storage/<sid>）口径一致的 dict。

    表格与编辑表单共用同一份口径：
    - path：网络存储未显式写时按 PVE 默认 /mnt/pve/<sid>；
    - content：未声明时按插件默认（pbs=backup，其余 images,rootdir）；
    - disable / shared / port：pvesh 返回数字，这里同样转成 0/1（shared 缺省时按插件默认补齐）。
    """
    sid = str(e.get("storage", "") or "")
    stype = str(e.get("type", ""))
    out = {"storage": sid, "type": stype}
    for k, v in e.items():
        if k not in ("storage", "type") and k not in _CFG_SECRET_KEYS:
            out[k] = v
    out["content"] = str(e["content"]) if "content" in e else _cfg_default_content(stype)
    out["path"] = str(e.get("path") or (f"/mnt/pve/{sid}" if stype in ("nfs", "cifs", "glusterfs") else ""))
    for k in _CFG_INT_KEYS:
        if k in out:
            out[k] = 1 if _cfg_truthy(out[k]) else 0
    if "shared" not in out:
        out["shared"] = 1 if stype in _SHARED_DEFAULT_TYPES else 0
    return out


def list_storage_cfg() -> Any:
    """列出数据中心所有存储的完整配置（存储管理表格用）。

    优先读 /etc/pve/storage.cfg（pmxcfs 内存文件，毫秒级，省掉一次 pvesh），字段口径对齐
    原来的 pvesh GET /storage（见 _storage_cfg_entry 的默认值说明）。

    注意这里的 path 刻意返回「PVE 认为的挂载路径」，而不是 storage_path() 对 pvewebtools-nfs
    的 export 特例 —— 表格里的 path 与「编辑」表单必须同一口径。
    """
    entries = _parse_storage_cfg()
    if entries is None:
        return _list_storage_cfg_pvesh()
    out = []
    for e in entries:
        d = _storage_cfg_entry(e)
        if not d["storage"]:
            continue
        out.append({
            "storage": d["storage"],
            "type": d["type"],
            "content": d["content"],
            "path": d["path"],
            "server": str(d.get("server", "") or ""),
            "export": str(d.get("export", "") or ""),
            "vgname": str(d.get("vgname", "") or ""),
            "thinpool": str(d.get("thinpool", "") or ""),
            "pool": str(d.get("pool", "") or ""),
            "portal": str(d.get("portal", "") or ""),
            "target": str(d.get("target", "") or ""),
            "datastore": str(d.get("datastore", "") or ""),
            "username": str(d.get("username", "") or ""),
            "fs-name": str(d.get("fs-name", "") or ""),
            "monhost": str(d.get("monhost", "") or ""),
            "disable": bool(d.get("disable")),
            "shared": bool(d.get("shared")),
        })
    return out


def _list_storage_cfg_pvesh() -> Any:
    """回退实现（storage.cfg 不可读时）：跑 pvesh GET /storage。"""
    data = _run_pvesh("get", "/storage") or []
    out = []
    for s in data:
        out.append({
            "storage": str(s.get("storage", "")),
            "type": str(s.get("type", "")),
            "content": str(s.get("content", "")),
            "path": s.get("path") or "",
            "server": s.get("server") or "",
            "export": s.get("export") or "",
            "vgname": s.get("vgname") or "",
            "thinpool": s.get("thinpool") or "",
            "pool": s.get("pool") or "",
            "portal": s.get("portal") or "",
            "target": s.get("target") or "",
            "datastore": s.get("datastore") or "",
            "username": s.get("username") or "",
            "fs-name": s.get("fs-name") or "",
            "monhost": s.get("monhost") or "",
            "disable": bool(s.get("disable")),
            "shared": bool(s.get("shared")),
        })
    return out


def get_storage_cfg(storage: Any) -> Any:
    """获取单个存储的完整配置（「存储管理 → 编辑」表单回填用）。

    与 list_storage_cfg 同源读 /etc/pve/storage.cfg（口径见 _storage_cfg_entry），
    省掉一次 pvesh；cfg 不可读或不含该存储时才回退 pvesh。
    """
    if not _STORAGE_NAME_RE.match(storage or ""):
        raise PveError(f"非法存储名: {storage}")
    entries = _parse_storage_cfg()
    if entries is not None:
        for e in entries:
            if str(e.get("storage", "")) == storage:
                return _storage_cfg_entry(e)
        raise PveError(f"存储不存在: {storage}")
    return _run_pvesh("get", f"/storage/{storage}") or {}


def create_storage_cfg(params: list) -> Any:
    """新增存储（pvesh create /storage）。params 必须含 storage/type，
    其余字段按所选类型透传（path/server/export/vgname/...），
    可选 'options' 字段为 k=v,k=v 形式的额外参数。"""
    if not isinstance(params, dict):
        raise PveError("参数格式错误")
    name = str(params.get("storage", "")).strip()
    stype = str(params.get("type", "")).strip()
    if not _STORAGE_NAME_RE.match(name):
        raise PveError("存储名不合法（仅允许字母、数字、下划线、短横线）")
    if not stype:
        raise PveError("请选择存储类型")
    args = ["--storage", name, "--type", stype]
    args += _storage_pvesh_args({k: v for k, v in params.items() if k not in ("storage", "type")})
    res = _run_pvesh("create", "/storage", args)
    invalidate_storage_path_cache()   # 新建存储后丢弃路径缓存，后续立刻能取到新根目录
    return res


def update_storage_cfg(storage: Any, params: list) -> Any:
    """更新存储配置（pvesh set /storage/{storage}）。"""
    if not _STORAGE_NAME_RE.match(storage or ""):
        raise PveError(f"非法存储名: {storage}")
    if not isinstance(params, dict):
        raise PveError("参数格式错误")
    # 剔除不可变的位置/身份字段（path/type/server/export/...），避免 pvesh set 报 Unknown option
    params = {k: v for k, v in params.items() if k not in _STORAGE_SET_IMMUTABLE}
    args = _storage_pvesh_args(params)
    if not args:
        raise PveError("没有可更新的字段")
    res = _run_pvesh("set", f"/storage/{storage}", args)
    invalidate_storage_path_cache(storage)
    return res


def delete_storage_cfg(storage: Any) -> Any:
    """移除存储（pvesh delete /storage/{storage}）。仅删除存储配置，不会清除实际数据。"""
    if not _STORAGE_NAME_RE.match(storage or ""):
        raise PveError(f"非法存储名: {storage}")
    res = _run_pvesh("delete", f"/storage/{storage}")
    invalidate_storage_path_cache(storage)
    return res


def list_storage_content(storage: Any) -> Any:
    """列出指定存储的已有内容条目（模板 / ISO / 备份 / 磁盘镜像等）。

    工作区存储 pvewebtools 的虚机模板直接落在 PVE backup 目录 dump/，已由 PVE 内容接口扫描；
    _workspace_vma_entries 保留兼容调用但返回空，vbios/ 仍需要补成合成条目。"""
    if not re.match(r"^[\w\-]+$", storage or ""):
        raise PveError(f"非法存储名: {storage}")
    node = socket.gethostname().split(".")[0]
    try:
        data = _run_pvesh("get", f"/nodes/{node}/storage/{storage}/content") or []
    except Exception:
        # 存储无法激活（如 pvewebtools 的 nfs 挂载点不可达）时，PVE 会报 500；
        # 直接视为无 PVE 内容，交给 vbios/ 合成条目兜底。
        data = []
    return (list(data or []) + _workspace_vma_entries(storage, data or [])
            + _workspace_vbios_entries(storage, data or [])
            + _workspace_vgpu_entries(storage, data or []))


def _workspace_vma_entries(storage: Any, existing: Any) -> list:
    """工作区存储 pvewebtools 的 dump/ 目录已由 PVE 内容接口扫描，无需再补合成条目。

    虚机模板现在直接存放在 PVE backup 内容目录 dump/（/var/lib/vz/pvewebtools/dump），
    与 PVE 自身备份使用同一目录；本函数保留作为兼容入口，返回空列表。"""
    return []


def _workspace_vbios_entries(storage: Any, existing: Any) -> Any:
    """工作区存储 pvewebtools 的 vbios/ 目录补成合成内容条目（content=vbios）。

    vBIOS ROM 是项目自管文件（PVE 没有该内容类型），主副本存于 <根>/vbios/，
    这里补成 volid = pvewebtools:vbios/<名> 的条目，由 storage_content_file() 解析回原路径，
    使「存储内容」可按合成分组浏览 / 下载；删除走 DELETE /api/vbios（连带清理各节点副本）。"""
    if storage != "pvewebtools":
        return []
    root = storage_path("pvewebtools")
    if not root:
        return []
    d = Path(root) / "vbios"
    if not d.is_dir():
        return []
    have = {str(e.get("volid", "")) for e in existing}
    out = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.name.endswith((".log", ".notes", ".dat", ".part", ".tmp")):
            continue
        volid = f"pvewebtools:vbios/{f.name}"
        if volid in have:
            continue
        st = f.stat()
        out.append({"content": "vbios", "volid": volid,
                    "size": st.st_size, "ctime": int(st.st_mtime), "subdir": "vbios"})
    return out


def _workspace_vgpu_entries(storage: Any, existing: Any) -> Any:
    """工作区存储 pvewebtools 的 vgpu-drivers/ 补成合成内容条目（content=vgpu）。

    vGPU 宿主机物料（.run 驱动 / .patch 解锁补丁 / .so 解锁库）是项目自管文件（PVE 无该内容类型），
    由「传输中心 → 上传文件 → vGPU 宿主机物料」统一存于 <根>/vgpu-drivers/，
    这里补成 volid = pvewebtools:vgpu/<名> 的条目，由 storage_content_file() 解析回原路径，
    使「存储内容」可按合成分组浏览 / 下载 / 删除（与 vbios/ 同构）。"""
    if storage != "pvewebtools":
        return []
    root = storage_path("pvewebtools")
    if not root:
        return []
    d = Path(root) / "vgpu-drivers"
    if not d.is_dir():
        return []
    have = {str(e.get("volid", "")) for e in existing}
    out = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.name.endswith((".part", ".tmp", ".count")):
            continue
        volid = f"pvewebtools:vgpu/{f.name}"
        if volid in have:
            continue
        st = f.stat()
        out.append({"content": "vgpu", "volid": volid,
                    "size": st.st_size, "ctime": int(st.st_mtime), "subdir": "vgpu-drivers"})
    return out


def _resolve_disk_image(root: Any, name: str) -> Any:
    """在 images 子树下按磁盘名解析真实文件。PVE 的 images/import 磁盘 volid 形如
    `vm-100-disk-0`，而实际文件位于 <root>/images/<vmid>/<name>.<ext>（带 VMID 子目录与扩展名）。
    返回解析到的 Path，找不到返回 None。"""
    img_root = root / "images"
    m = re.search(r"(?:vm|subvol|base)-(\d+)-", name)
    if m:
        vmid = m.group(1)
        cand_dir = img_root / vmid
        if cand_dir.is_dir():
            direct = cand_dir / name
            if direct.is_file():
                return direct
            with_ext = sorted(cand_dir.glob(f"{re.escape(name)}.*"))
            if with_ext:
                return with_ext[0]
    # 回退：递归搜索整个 images 树，优先精确名，其次按 name.* 匹配
    exact = sorted(img_root.rglob(name))
    if exact:
        return exact[0]
    stems = sorted(p for p in img_root.rglob(f"{re.escape(name)}.*") if p.is_file())
    if stems:
        return stems[0]
    return None


def storage_content_file(storage: Any, volid: Any) -> Any:
    """将存储内容条目的 volid 解析为磁盘真实文件路径（仅限文件型存储 dir/nfs/cifs/glusterfs）。
    返回 (Path, 文件名)；无法解析 / 非法路径则抛 PveError。
    注：iso/vztmpl/backup/snippets 直接按子目录映射；images/import 磁盘需解析 VMID 子目录与扩展名。"""
    if not re.match(r"^[\w\-]+$", storage or ""):
        raise PveError(f"非法存储名: {storage}")
    if ":" not in (volid or ""):
        raise PveError("非法 volid（缺少存储前缀）")
    sname, sub = volid.split(":", 1)
    if sname != storage:
        raise PveError("volid 与存储不匹配")
    parts = sub.split("/", 1)
    ctype = parts[0]
    subdir = _CONTENT_SUBDIR.get(ctype) or PROJECT_CONTENT_SUBDIR.get(ctype)
    if subdir is None:
        raise PveError(f"暂不支持该内容类型: {ctype}（仅支持 iso/vztmpl/backup/snippets/images/import/vbios/vgpu）")
    if ctype in PROJECT_CONTENT_SUBDIR:
        # 项目自管内容类型（PVE 未声明该类型）：只存在于工作区存储 pvewebtools
        root = storage_path("pvewebtools") if storage == "pvewebtools" else None
    else:
        root = _storage_dirs(ctype).get(storage)
    if root is None:
        raise PveError(f"存储 {storage} 不是文件型存储或不支持内容类型 {ctype}")
    rel = parts[1] if len(parts) > 1 else ""
    if not rel:
        raise PveError("volid 子路径为空")
    root = root.resolve()

    if ctype in ("images", "import"):
        # 磁盘镜像 / 导入磁盘：实际文件带 VMID 子目录与扩展名，先在 images 树下解析
        path = _resolve_disk_image(root, rel)
        if path is None and ctype == "import":
            # import 内容也可能位于 import/ 子树（部分版本/配置）
            base = (root / _CONTENT_SUBDIR["import"]).resolve()
            direct = base / rel
            if direct.is_file():
                path = direct
            else:
                g = sorted(base.glob(f"**/{rel}"))
                if g:
                    path = g[0].resolve()
        if path is None:
            raise PveError("文件不存在或不是普通文件")
        path = path.resolve()
        if root != path and root not in path.parents:
            raise PveError("非法路径（尝试穿越存储根目录）")
        return path, path.name

    # 其余类型：直接子目录映射
    # pvewebtools 的虚机模板直接放在 PVE backup 内容目录 dump/ 下
    cand_dirs = [root / subdir]
    path = None
    for d in cand_dirs:
        base = d.resolve()
        p = (base / rel).resolve()
        if base != p and base not in p.parents:
            raise PveError("非法路径（尝试穿越存储根目录）")
        if p.is_file():
            path = p
            break
    if path is None:
        raise PveError("文件不存在或不是普通文件")
    return path, path.name


def delete_storage_content(storage: Any, volid: Any) -> None:
    """删除存储内容条目（目录扫描型：iso/vztmpl/backup/snippets/images/import）。
    这些类型 PVE 直接按文件系统呈现，删文件即生效，无需改动 PVE 配置。
    注意：磁盘镜像(images)若仍被 VM/CT 引用，请先在 VM 配置中卸载/分离，否则会导致虚拟机磁盘丢失。"""
    if not re.match(r"^[\w\-]+$", storage or ""):
        raise PveError(f"非法存储名: {storage}")
    if ":" not in (volid or ""):
        raise PveError("非法 volid")
    sname, sub = volid.split(":", 1)
    if sname != storage:
        raise PveError("volid 与存储不匹配")
    ctype = sub.split("/", 1)[0]
    if ctype not in ("iso", "vztmpl", "backup", "snippets", "images", "import", "vgpu"):
        raise PveError("暂仅支持删除 iso/vztmpl/backup/snippets/images/import/vgpu 类型")
    path, _ = storage_content_file(storage, volid)
    try:
        path.unlink()
    except OSError as e:
        raise PveError(f"删除失败: {e}")


def move_content_subdirs(ctype: Any, dst_storage: Any) -> list:
    """某内容类型在目标存储下的合法落盘目录候选（相对存储根），供「移动」时选择。

    默认是 PVE 约定目录（_CONTENT_SUBDIR），放进去 PVE 与工具各面板都能识别。
    工作区存储 pvewebtools 的 backup 即虚机模板，统一落在 dump/ 目录。"""
    return [{"subdir": _CONTENT_SUBDIR[ctype], "default": True,
             "label": f"{_CONTENT_SUBDIR[ctype]}/（PVE 约定目录）",
             "note": "PVE 约定目录，PVE 与各面板均可识别。"}]


def move_content_targets(src_storage: Any, ctype: Any) -> Any:
    """列出可把 ctype 内容从 src_storage 移到的目标存储，及其各自的合法目录候选。"""
    if ctype not in MOVABLE_CONTENT_TYPES:
        raise PveError(f"暂不支持移动该内容类型: {ctype}")
    out = []
    for sid, root in _storage_dirs(ctype).items():
        if sid == src_storage:
            continue
        out.append({"storage": sid, "path": str(root), "dirs": move_content_subdirs(ctype, sid)})
    # 兜底：工作区存储以 nfs 形式注册时 config 项不带 path，可能不在 _storage_dirs 结果里
    if src_storage != "pvewebtools" and not any(o["storage"] == "pvewebtools" for o in out):
        root = storage_path("pvewebtools")
        if root:
            out.append({"storage": "pvewebtools", "path": str(root),
                        "dirs": move_content_subdirs(ctype, "pvewebtools")})
    out.sort(key=lambda o: o["storage"])
    return out


def move_storage_content(src_storage: Any, volid: Any, dst_storage: Any, dst_subdir: Any = None) -> Any:
    """把存储内容条目移动到另一个存储，返回 (源路径, 目标路径)。

    目标目录默认取该类型的首选合法目录（按 PVE 约定 iso→template/iso、vztmpl→template/cache、
    backup→dump、snippets→snippets、import→import）；
    也可传 dst_subdir 指定，但必须是 move_content_subdirs 给出的候选之一（避免落进 PVE 不扫描的目录）。
    同文件系统为改名（瞬时），跨文件系统为复制后删除；目标存储须声明同一内容类型，且不得已有同名文件。"""
    if not re.match(r"^[\w\-]+$", src_storage or "") or not re.match(r"^[\w\-]+$", dst_storage or ""):
        raise PveError(f"非法存储名: {src_storage} -> {dst_storage}")
    if src_storage == dst_storage:
        raise PveError("源与目标存储相同")
    if ":" not in (volid or ""):
        raise PveError("非法 volid（缺少存储前缀）")
    sname, sub = volid.split(":", 1)
    if sname != src_storage:
        raise PveError("volid 与存储不匹配")
    ctype = sub.split("/", 1)[0]
    if ctype not in MOVABLE_CONTENT_TYPES:
        raise PveError(f"暂不支持移动该内容类型: {ctype}")
    src_path, name = storage_content_file(src_storage, volid)
    dst_root = _storage_dirs(ctype).get(dst_storage)
    if dst_root is None and dst_storage == "pvewebtools":
        # 工作区存储可能以 nfs 形式注册（config 项不带 path），用 storage_path() 兜底解析根目录
        dst_root = storage_path("pvewebtools")
    if dst_root is None:
        raise PveError(f"目标存储 {dst_storage} 不可用或未启用 {ctype} 内容")
    # 目标目录：默认取该类型的首选合法目录，
    # 显式传 dst_subdir 时必须在合法候选内，避免把文件丢进 PVE 不扫描的位置
    subs = move_content_subdirs(ctype, dst_storage)
    allowed_subs = [s["subdir"] for s in subs]
    if dst_subdir:
        subdir = str(dst_subdir).strip().strip("/")
        if subdir not in allowed_subs:
            raise PveError(f"目标目录无效：{dst_storage} 的 {ctype} 仅支持 {'/'.join(allowed_subs)}")
    else:
        subdir = next((s["subdir"] for s in subs if s.get("default")), allowed_subs[0])
    dst_dir = dst_root / subdir
    dst_path = dst_dir / name
    if dst_path.exists():
        raise PveError(f"目标已存在同名文件: {dst_storage}:{ctype}/{name}")
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
    except OSError as e:
        raise PveError(f"移动失败: {e}")
    return src_path, dst_path


_STORAGE_CFG_PATH = Path("/etc/pve/storage.cfg")
_STORAGE_DIR_TYPES = ("dir", "nfs", "cifs", "glusterfs")
# PVE 对未显式声明 content 的存储按「images,rootdir」处理（与 pvesh 返回的已解析 content 口径一致）
_STORAGE_DEFAULT_CONTENT = "images,rootdir"
# PVE 插件层面 shared 默认为 1 的存储类型（cfg 里通常不显式写 shared，读文件时必须补默认，
# 否则 list_storage_cfg 会把 nfs/cifs 判成非共享，连带「跨节点管理共享存储内容」退化成只读）
_SHARED_DEFAULT_TYPES = {"nfs", "cifs", "glusterfs", "cephfs", "rbd", "iscsi", "iscsidirect", "pbs"}


def _parse_storage_cfg() -> Any:
    """解析 /etc/pve/storage.cfg → [{'type','storage','path','content','disable',...}]。

    pmxcfs 是内存文件系统，读取为微秒级；用它替代「跑 2 次 pvesh」采集存储定义，
    可把存储下拉从「秒级」降到「毫秒级」（pvesh 慢在进程启动 + PVE 逐存储计算 status，
    后者在 NFS/CIFS 上要做 statfs）。解析失败返回 None，调用方回退 pvesh。
    """
    try:
        text = _STORAGE_CFG_PATH.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    out = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 段落头（无缩进）：`dir: local`；其余为缩进属性行 `path /var/lib/vz`
        if not raw[:1].isspace():
            m = re.match(r"^([A-Za-z]+):\s*(\S+)\s*$", line)
            cur = {"type": m.group(1), "storage": m.group(2)} if m else None
            if cur:
                out.append(cur)
            continue
        if cur is None:
            continue
        parts = line.split(None, 1)
        cur[parts[0]] = parts[1].strip() if len(parts) > 1 else "1"
    return out


def _storage_root_from_entry(sid: str, entry: Any) -> Any:
    """单个 storage.cfg 段落的根目录；无本地路径（lvm/zfs/pbs 等）返回 None。

    规则与 storage_path() 完全一致：pvewebtools 的 nfs 存储优先取 export（绕开服务器侧
    /mnt/pve/<sid> 回环挂载的 Stale file handle）；网络存储缺 path 时按 /mnt/pve/<sid> 推断。
    两处共用同一规则，避免「下拉显示一个路径、实际落盘写到另一个路径」。
    """
    stype = str(entry.get("type", "")).lower()
    if sid == "pvewebtools" and stype == "nfs" and entry.get("export"):
        return Path(str(entry["export"]))
    root = entry.get("path") or (f"/mnt/pve/{sid}" if stype in ("nfs", "cifs", "glusterfs") else "")
    return Path(str(root)) if root else None


def _storage_cfg_dirs(content_types: Any) -> Any:
    """从 storage.cfg 生成 {ctype: {storage_id: 根目录 Path}}；读不到 cfg 时返回 None。

    筛选口径与 pvesh 版保持一致：跳过 disable、只取 dir/nfs/cifs/glusterfs、需有根目录、
    content 含目标类型。网络存储未显式写 path 时按 PVE 默认挂载点 /mnt/pve/<sid> 推断。
    """
    entries = _parse_storage_cfg()
    if entries is None:
        return None
    wanted = set(content_types)
    out = {ct: {} for ct in wanted}
    for e in entries:
        if str(e.get("disable", "")) not in ("", "0", "false", "no"):
            continue
        stype = str(e.get("type", ""))
        if stype not in _STORAGE_DIR_TYPES:
            continue
        sid = str(e.get("storage", ""))
        if not sid:
            continue
        root = _storage_root_from_entry(sid, e)
        if root is None:
            continue
        contents = set(str(e.get("content") or _STORAGE_DEFAULT_CONTENT).split(","))
        for ct in wanted & contents:
            out[ct][sid] = root
    return out


def _storage_dirs(content_type: Any) -> Any:
    """列出本机支持指定内容类型、且带本地路径的存储（dir/nfs/cifs/glusterfs）：storage_id -> 根目录 Path。
    与磁盘导出脚本的 EXPORT_DIR 解析范围保持一致（dir/nfs/cifs/glusterfs），
    避免导出到 nfs/cifs 后下载/删除/列表接口因后端只认 dir 而报"存储不可用"的隐性不一致。
    存储定义优先读 /etc/pve/storage.cfg（毫秒级），读不到才回退 pvesh。"""
    dirs = _storage_cfg_dirs((content_type,))
    if dirs is not None:
        return dirs.get(content_type, {})
    node_storages = _run_pvesh("get", f"/nodes/{socket.gethostname().split('.')[0]}/storage") or []
    enabled = {str(s["storage"]) for s in node_storages if not s.get("disabled")}
    cfg = _run_pvesh("get", "/storage") or []
    out = {}
    for s in cfg:
        sid = str(s.get("storage", ""))
        stype = str(s.get("type", ""))
        if sid not in enabled or stype not in _STORAGE_DIR_TYPES or not s.get("path"):
            continue
        if content_type not in str(s.get("content", "")).split(","):
            continue
        out[sid] = Path(str(s["path"]))
    return out


def _storage_dirs_batch(content_types: Any) -> Any:
    """一次采集同时返回多种 content 类型的目录映射：{ctype: {storage_id: Path}}。

    口径与 _storage_dirs 完全一致。优先读 /etc/pve/storage.cfg（0 次 pvesh）；
    极端情况下 cfg 不可读才回退 pvesh 采集。"""
    dirs = _storage_cfg_dirs(content_types)
    if dirs is not None:
        return dirs
    node = socket.gethostname().split(".")[0]
    node_storages = _run_pvesh("get", f"/nodes/{node}/storage") or []
    enabled = {str(s["storage"]) for s in node_storages if not s.get("disabled")}
    cfg = _run_pvesh("get", "/storage") or []
    wanted = set(content_types)
    out = {ct: {} for ct in wanted}
    for s in cfg:
        sid = str(s.get("storage", ""))
        stype = str(s.get("type", ""))
        if sid not in enabled or stype not in _STORAGE_DIR_TYPES or not s.get("path"):
            continue
        contents = set(str(s.get("content", "")).split(","))
        for ct in wanted & contents:
            out[ct][sid] = Path(str(s["path"]))
    return out


def disk_and_import_dirs() -> Any:
    """一次采集同时返回 (images 目录, import 目录)，供云镜像目标存储复用。"""
    batch = _storage_dirs_batch(("images", "import"))
    return batch["images"], {sid: d / "import" for sid, d in batch["import"].items()}


def disk_dirs() -> Any:
    """本机可存放/导出虚拟机磁盘镜像的目录：storage_id -> 根目录 Path"""
    return _storage_dirs("images")


def export_dirs() -> Any:
    """本机各磁盘导出存储的【导出专用】子目录：storage_id -> <根>/.pvewebtools/export/ Path。
    与 disk_dirs() 解耦——磁盘导出、历史列表、下载、删除都只走此目录，
    不占用存储根、也不误列 PVE 自身目录（images/iso/dump 等），历史目录更干净。
    范围与磁盘导出脚本的 EXPORT_DIR 解析（dir/nfs/cifs/glusterfs + images 内容）保持一致"""
    dirs = _storage_dirs("images")
    return {sid: d / ".pvewebtools" / "export" for sid, d in dirs.items()}


def backup_dirs() -> Any:
    """本机可存放备份文件的目录：storage_id -> dump 目录 Path"""
    dirs = _storage_dirs("backup")
    return {sid: d / "dump" for sid, d in dirs.items()}


def import_dirs() -> Any:
    """本机可作为 qm importdisk 源的目录（import 内容）：storage_id -> <根>/import Path。
    与 disk_dirs()(images 内容)互补，导入解析应合并两者。
    注：工作区存储 pvewebtools 默认只声明 backup（不声明 import），其 cloud-images/ 由 storage_path() 定位，
    不再依赖 import 内容来反推根目录。"""
    dirs = _storage_dirs("import")
    return {sid: d / "import" for sid, d in dirs.items()}


def vztmpl_dirs() -> Any:
    """本机支持 vztmpl（容器模板）内容的存储：storage_id -> <根>/template/cache Path。

    与 PVE 固定映射一致（volid `local:vztmpl/xxx.tar.zst` 实际位于 <根>/template/cache/），
    供「获取 LXC 模板」下载落盘使用：文件写进该目录后 PVE 即刻可见（pveam list / 建 CT 下拉）。
    目录可能尚不存在（从未下载过模板），调用方需自行 mkdir。"""
    dirs = _storage_dirs("vztmpl")
    return {sid: d / "template" / "cache" for sid, d in dirs.items()}


_storage_path_cache = {}      # sid -> (时间戳, Path)
_storage_path_lock = threading.Lock()
_STORAGE_PATH_TTL = 60        # 存储根目录是分钟级事实：60s 内复用，避免每次上传/列表都跑 pvesh


def invalidate_storage_path_cache(sid: Any = None) -> Any:
    """存储配置变更（create/update/delete）后丢弃路径缓存，避免继续用旧根目录。

    同时清掉「节点可用存储」缓存：新增/删除/启用禁用存储都会改变那份集合。
    """
    with _storage_path_lock:
        if sid is None:
            _storage_path_cache.clear()
        else:
            _storage_path_cache.pop(str(sid), None)
    invalidate_node_available_cache()


def _storage_path_from_cfg(sid: str) -> Any:
    """从 /etc/pve/storage.cfg 解析存储根目录；cfg 不可读返回 None（调用方回退 pvesh）。

    与 pvesh 版口径一致：pvewebtools 的 nfs 存储优先用 export 路径（绕开服务器侧
    /mnt/pve/<sid> 回环挂载的 Stale file handle）；网络存储缺 path 时按 /mnt/pve/<sid> 推断。
    """
    entries = _parse_storage_cfg()
    if entries is None:
        return None
    for e in entries:
        if str(e.get("storage", "")) != sid:
            continue
        return _storage_root_from_entry(sid, e)
    return None


def storage_path(sid: Any) -> Any:
    """返回指定存储的根目录绝对路径（读 /etc/pve/storage.cfg，带 60s 缓存）。

    自管共享存储 pvewebtools（nfs 类型）：PVE 会在服务器侧自动建立 /mnt/pve/<sid>
    回环挂载；服务器执行 exportfs -ra 后该回环挂载极易变 Stale file handle (errno 116)。
    而其 export 路径在服务器即真实导出目录、在各客户端即已挂载的 NFS 目录，二者都可
    稳定直访。故对 pvewebtools 的 nfs 存储优先返回 export 路径，绕开回环挂载。

    说明：该函数被上传/列表/建机等高频路径调用，原来每次都跑一次 pvesh（0.3~1.5s）；
    现在优先读 cfg（微秒级）+ 60s 缓存，存储增删改后由 invalidate_storage_path_cache 失效。
    """
    key = str(sid or "")
    now = time.time()
    with _storage_path_lock:
        hit = _storage_path_cache.get(key)
        if hit and now - hit[0] < _STORAGE_PATH_TTL:
            return hit[1]
    val = _storage_path_from_cfg(key)
    if val is None:
        # cfg 不可读（或解析异常）时才走 pvesh 兜底；该路径保持原有语义
        try:
            cfg = _run_pvesh("get", f"/storage/{key}") or {}
            if key == "pvewebtools" and str(cfg.get("type", "")).lower() == "nfs" and cfg.get("export"):
                val = Path(str(cfg["export"]))
            elif cfg.get("path"):
                val = Path(str(cfg["path"]))
        except Exception:
            val = None
    if val is not None:
        with _storage_path_lock:
            _storage_path_cache[key] = (now, val)
    return val


def _read_archive_head(path: Path) -> bytes:
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


def _probe_backup_kind(path: Path) -> str:
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


def list_backup_files() -> Any:
    """列出全部备份文件：[{storage, name, size, mtime, kind}]"""
    out = []
    for sid, d in backup_dirs().items():
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
