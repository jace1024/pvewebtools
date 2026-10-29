"""启动期维护任务与后台刷新循环（M2/T2.6 深化第六批）。 进程启动时清理孤儿分片/临时目录、续传未完成的上传；运行期周期性刷新节点缓存。 这些都在 main() 里被拉起，与 HTTP 请求路径无关，独立成模块。"""

import shutil
import time
import urllib.parse

from concurrent.futures import ThreadPoolExecutor
from core.pve import pve_api as backend
from core.web import handlers
from core.web.handlers import _cloud_image_dir, _disk_image_dir, _enrich_node_slow, _nodes_cache, _nodes_cache_lock, collect_nodes
from pathlib import Path


CHUNK_SWEEP_INTERVAL = 300  # 每 5 分钟扫描一次

CHUNK_TTL = 30 * 60        # 分片目录 30 分钟无更新即判定为中断残留

NODES_REFRESH_INTERVAL = 30


def _cleanup_orphan_parts():
    """启动兜底：清理上次进程被强杀/崩溃遗留的下载半成品 *.part 与解压中间文件 *.dec.tmp。

    仅删下载通道（云镜像 / LXC 模板 / 磁盘导入）写入的 *.part 与现场解压的 *.dec.tmp 中间产物；
    分片上传的临时块走 _sweep_stale_chunks，任务级临时目录走 _cleanup_orphan_job_tmps，互不影响。
    扫描目录：cloud-images/、disk-images/、各支持 vztmpl 存储的 template/cache/。
    返回清理掉的 (目录数, 文件数)；任何异常都被吞掉，绝不阻断启动。"""
    dirs = []
    for fn in (_cloud_image_dir, _disk_image_dir):
        try:
            d = fn()
            if d and d.is_dir():
                dirs.append(d)
        except Exception:  # noqa: BLE001
            pass
    try:
        dirs.extend(backend.vztmpl_dirs().values())
    except Exception:  # noqa: BLE001
        pass

    removed = 0
    scanned = 0
    for d in dirs:
        if not d or not d.is_dir():
            continue
        scanned += 1
        try:
            for pat in ("*.part", "*.dec.tmp"):
                for p in d.glob(pat):
                    if p.is_file():
                        try:
                            p.unlink()
                            removed += 1
                        except OSError:
                            pass
        except OSError:
            pass
    if removed:
        print(f"[启动清理] 已删除 {removed} 个残留下载半成品(.part/.dec.tmp)，扫描目录 {scanned} 个")
    return scanned, removed


def _cleanup_orphan_job_tmps():
    """启动兜底：清理上次服务整体崩溃（断电 / kill -9 服务进程）来不及回收的任务临时目录。

    正常情况下任务结束或强杀时 jobs.py 会即时按 PT_JOB_ID 回收 /var/tmp/pvewebtools-job-<id>；
    这里兜底那些服务进程被强杀、脚本已终止却还没轮到回收的残留目录。
    返回删除目录数；任何异常都被吞掉，绝不阻断启动。"""
    removed = 0
    try:
        base = Path("/var/tmp")
        for d in base.glob("pvewebtools-job-*"):
            if d.is_dir():
                try:
                    shutil.rmtree(d)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    if removed:
        print(f"[启动清理] 已删除 {removed} 个残留任务临时目录(/var/tmp/pvewebtools-job-*)")
    return removed


def _download_error_text(err, urls):
    """把下载异常整理成用户看得懂的失败原因。

    pveam 的模板清单来自上游 aplinfo 索引，里面会残留已被上游删除的老模板
    （实测 debian-10 系列 TurnKey 模板在各源均已下架），此时所有源都返回 404。
    单一异常文本「HTTP Error 404」看不出所以然，这里补上"多源都试过了"与处置建议。"""
    if isinstance(err, urllib.error.HTTPError) and err.code == 404:
        if len(urls) > 1:
            return ("各下载源均无此模板文件（404）。该模板很可能已从上游下架，"
                    "建议改用较新版本的同类模板")
        return f"{err}。该模板可能已从上游下架，建议改用较新版本的同类模板"
    msg = str(err)
    return f"{msg}（已尝试 {len(urls)} 个下载源）" if len(urls) > 1 else msg


def _sweep_stale_chunks():
    """后台清理线程：删除超时未更新的分片临时目录，回收中断上传留下的残片"""
    while True:
        time.sleep(CHUNK_SWEEP_INTERVAL)
        try:
            _resume_incomplete_uploads()
        except Exception:  # noqa: BLE001
            pass
        _sweep_stale_chunks_once()


def _upload_dir_in_use(sub):
    """调用方持有上传锁；状态的目录身份比可损坏/缺失的元数据更可靠。"""
    busy_keys = {key for key, active in handlers._UPLOAD_ACTIVE.items() if active}
    busy_keys.update(key for key, state in handlers._MERGE_STATE.items()
                     if state.get("state") == "merging")
    for key in busy_keys:
        kind, storage, name = key.split("|", 2)
        if handlers._upload_tmp_dir(kind, name, storage) == sub:
            return True
    return False


def _sweep_stale_chunks_once(now=None):
    """清理一轮过期残片；旧平铺镜像只清理，不猜测类型进行恢复。"""
    now = time.time() if now is None else now
    roots = [root for _kind, root in handlers._upload_roots()]
    roots.append(handlers.DISK_TMP_DIR)
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for sub in root.iterdir():
                # 新布局的类型目录可能很久没有新增文件，不能当成旧平铺上传删除。
                if root == handlers.DISK_TMP_DIR and sub.name in ("disk", "cloud"):
                    continue
                try:
                    with handlers._MERGE_LOCK:
                        if (not sub.is_dir() or sub.is_symlink()
                                or sub.resolve().parent != root.resolve()
                                or _upload_dir_in_use(sub)):
                            continue
                        if now - sub.stat().st_mtime > CHUNK_TTL:
                            shutil.rmtree(sub, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            continue


def _resume_incomplete_uploads():
    """启动 / 周期兜底：把「分片已收齐但因服务崩溃/重启未完成合并」的上传补齐合并。
    只恢复带可信元数据的新分片；旧 .count 无法证明分片写完或区分磁盘/云镜像，交由 TTL 清理。"""
    for kind, root in handlers._upload_roots():
        try:
            if not root.is_dir():
                continue
            for sub in root.iterdir():
                try:
                    with handlers._MERGE_LOCK:
                        if not sub.is_dir() or sub.is_symlink():
                            continue
                        meta = handlers._read_upload_meta(sub)
                        if not meta or meta["kind"] != kind:
                            continue
                        name, storage = meta["name"], meta["storage"]
                        if kind in ("backup", "vztmpl"):
                            if not storage:
                                continue
                        elif storage:
                            continue
                        if handlers._upload_tmp_dir(kind, name, storage) != sub:
                            continue
                        handlers._maybe_merge_async(kind, name, storage, meta["count"])
                except Exception:  # noqa: BLE001
                    continue
        except OSError:
            continue


def _nodes_refresh_loop():
    """后台守护线程：先快速采集节点概览（PVE API + 轻量命令，秒回）发布缓存，
    再异步补慢硬件数据（dmidecode/smartctl/sensors，秒级）二次更新缓存。
    前台请求只读缓存，首屏秒开，慢数据在后台静默补齐。"""
    # 首次给一点延迟，避免与启动时的首轮预热撞车
    time.sleep(2)
    while True:
        try:
            data = collect_nodes(fast_only=True)
            with _nodes_cache_lock:
                _nodes_cache["data"], _nodes_cache["ts"] = data, time.time()
            # 慢数据独立补齐：dmidecode 内存/CPU 插槽、主板、温度（smartctl 最慢）
            _enrich_nodes_slow(data)
            with _nodes_cache_lock:
                _nodes_cache["data"], _nodes_cache["ts"] = data, time.time()
        except Exception:
            pass  # 采集失败保留旧缓存，不打断后台循环
        time.sleep(NODES_REFRESH_INTERVAL)


def _enrich_nodes_slow(nodes):
    """后台并行补所有节点的慢硬件数据（dmidecode/smartctl/sensors），更新同一批 dict 引用"""
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        list(pool.map(_enrich_node_slow, nodes))


