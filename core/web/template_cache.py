"""模板缓存（缓存加速）的采集与清理，提供节点分发、容量水位、条目清单与过期清理。"""

import os
import shutil
import socket
import time

from concurrent.futures import ThreadPoolExecutor
from core.pve import pve_api, pve_api as backend
from core.web.handlers import _backup_template_dir
from core.web.probe import _run_func_on_node_args
from pathlib import Path


TEMPLATE_CACHE_DIRNAME = "pvewebtools-cache"


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
    """路径所在挂载点是否网络存储（口径与工具箱 pw_is_remote_path 一致）。"""
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
    """本机模板缓存根目录；无可用落点时返回 None。

    选择顺序与 bash 侧 pw_cache_root 一致：local(dir) 存储根 → /var/lib/vz → /var/tmp，
    且必须落在非网络存储上——缓存落在共享存储上等于没缓存，正是要避开的那个老问题。
    """
    root = None
    try:
        p = pve_api.storage_path("local")
        if p and Path(p).is_dir():
            root = str(p)
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
    """按来源路径推断缓存条目类别，供面板行内显示类型标签。

    缓存本身不记「用途」，但来源路径足以区分这几类数据源：
    cloud-images/ → 云镜像、template/cache/ → LXC 模板；dump/ 下再按项目约定分模板与备份
    （pvewebtools 共享存储的 dump/ 在本工具里就是模板目录，口径同 _node_backup_files 的
    template 标记，所以同一个目录里的文件在建机下拉里叫模板、在还原下拉里叫备份）。
    认不出来的返回空串，界面就不显示标签。
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
    """本机缓存概览：根目录、占用、上限、可用空间与条目列表（供「缓存加速」面板按节点展示）。

    state：ok=正常缓存（有 meta）/ orphan=没有 meta 的残留文件 / partial=拉取中的中间文件。

    dump_local：本节点上的模板目录是否就在本机盘，也就是这台机器是共享存储的**导出端**
    还是客户端。导出端读模板本身就是本地 IO，缓存毫无意义（bash 侧 pw_cache_ensure 会
    因此直接返回 local 而不落缓存），界面必须据此给出准确说明，否则会显示成
    「暂无缓存，首次建机时会自动拉取」，与事实相反。
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
    # 受管条目排在前面，各自按最后使用时间由新到旧
    items.sort(key=lambda x: (x["state"] != "ok", -int(x.get("last_used") or 0)))
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = None
    return dict(base, root=str(root), root_ok=True, total=total, free=free, items=items, limit_mb=limit_mb)


def _cache_entry_name_ok(name):
    """缓存条目名合法性：必须是缓存根下的纯文件名（禁路径分隔符 / 上级引用 / 隐藏文件）。"""
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
    """清空本机模板缓存（缓存文件 + meta + 锁）；拉取中的 .part 中间文件保留，交给下次 GC。"""
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
                    continue          # 拉取中的中间文件，删掉会让正在跑的任务失败
                size = ent.stat().st_size
                ent.unlink()
                count += 1
                if not only_suffix:
                    freed += size
            except OSError:
                continue
    return {"count": count, "freed": freed}


def template_cache_dispatch(node, func_name, args=()):
    """本机（包括显式节点名）调用本模块实现，外节点通过 SSH 执行 collect.py。"""
    return _run_func_on_node_args(node, func_name, args, timeout=60)


def template_cache_nodes():
    """集群各节点的缓存概览（供「缓存加速」面板构建节点下拉）。

    只关心「该节点是否需要缓存」：共享存储服务端的模板目录就在本机盘上，读模板本就是
    本地 IO，缓存没有意义——界面据此把这些节点从下拉里去掉，避免误选。
    逐节点取数：本机直接算，外节点并行 SSH；单个节点失败只标记该节点，
    不让一台离线机器拖垮整个面板。
    """
    local = socket.gethostname().split(".")[0]
    names = [str(n.get("node")) for n in backend.list_nodes()] or [local]

    def _one(name):
        try:
            info = template_cache_dispatch(None if name == local else name, "_node_template_cache")
        except Exception as e:  # noqa: BLE001
            return {"node": name, "error": str(e)[:160]}
        return {"node": name,
                "dump_local": bool(info.get("dump_local")),
                "root_ok": bool(info.get("root_ok")),
                "total": int(info.get("total") or 0),
                "count": len(info.get("items") or [])}

    if len(names) == 1:
        return {"nodes": [_one(names[0])], "local": local}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
        return {"nodes": list(ex.map(_one, names)), "local": local}


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


