"""HTTP 处理入口（HandlerMixin）：各域接口的实现方法，以及它们依赖的模块级函数与常量。

以 mixin 形式与 ``RoutingMixin`` / ``OpsMixin`` 等一起组成 ``ApiHandler``，靠 ``self`` 访问
HTTP 基础设施（``HttpUtilMixin``）与 SSE（``SseMixin``）；路由表在 ``routers/`` 里用方法名
字符串登记，``dispatch`` 用 ``getattr`` 在实例上查找，跨 mixin 透明。
"""

import gzip
import json
import lzma
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse

from concurrent.futures import ThreadPoolExecutor
from core import paths
from core.pve import pve_api, pve_api as backend, storage_provision
from core.tools import toolbox
from core.version import version_info
from core.web import jobs
from core.web.host_util import _is_cluster_node, _local_outbound_ip
from core.web.probe import _SSH_CTRL, _local_access_pred, _run_func_on_node, _run_func_on_node_args, list_available_kernels, list_grub_backups, list_installed_kernels, list_net_backups, list_network_bridges, list_network_vlans, list_pci_generic, list_pci_gpus, list_pci_nics, list_pci_storage, list_phys_nics, list_usb_devices, list_vfio_orphans
from core.web.vbios import VBIOS_TARGET_DIR, _add_vbios_manifest, _link_vbios, _read_vbios_manifest, _vbios_is_managed, _vbios_manifest_path, _vbios_remove_remote, _vbios_shared_dir, _vbios_sync_to_nodes, _vbios_target_blocked, init_vbios_local
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BACKUP_TMP_DIR = Path("/var/tmp/pvewebtools-backup")

MAX_BACKUP_SIZE = 256 * 1024 ** 3   # 单备份文件上限 256GB

_backup_lock = threading.Lock()

DISK_TMP_DIR = Path("/var/tmp/pvewebtools-disk")

VZTMPL_TMP_DIR = Path("/var/tmp/pvewebtools-vztmpl")

MAX_VZTMPL_SIZE = 64 * 1024 ** 3   # 单模板上限 64GB

_vztmpl_lock = threading.Lock()

MAX_DISK_SIZE = 256 * 1024 ** 3   # 单磁盘镜像上限 256GB

_disk_lock = threading.Lock()

_CLOUD_COMP_EXTS = (".xz", ".gz", ".zst")

CLOUD_MIRRORS = (
    {"id": "ustc", "label": "中科大 USTC", "host": "mirrors.ustc.edu.cn"},
    {"id": "tuna", "label": "清华 TUNA", "host": "mirrors.tuna.tsinghua.edu.cn"},
    {"id": "aliyun", "label": "阿里云", "host": "mirrors.aliyun.com"},
)

LXC_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*\.tar\.(?:xz|zst|gz)$")

APLINFO_PATH = Path("/usr/share/perl5/PVE/APLInfo.pm")

DEFAULT_CT_SOURCE = "http://download.proxmox.com"

LXC_TEMPLATE_MIRROR_IDS = ("ustc", "tuna")

_LXC_TEMPLATE_MIRROR_LABELS = {"ustc": "中科大 USTC", "tuna": "清华 TUNA"}

_cluster_src_cache = {"ts": 0.0, "data": None}

_cluster_src_lock = threading.Lock()

_CLUSTER_SRC_TTL = 120  # 检测走 SSH，结果缓存 2 分钟，避免每次面板加载都扫集群

LXC_SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

TURNKEY_DOMESTIC_ROOTS = ("https://mirrors.ustc.edu.cn",)

TURNKEY_OFFICIAL_ROOT = "https://mirror.turnkeylinux.org"

TURNKEY_MIRROR_HOSTS = ("ustc.edu.cn",)

LXC_FALLBACK_CT_ROOTS = (
    "https://mirrors.ustc.edu.cn/proxmox",
    "https://mirrors.tuna.tsinghua.edu.cn/proxmox",
    "http://download.proxmox.com",
)

LXC_TURNKEY_MIN_DEBIAN = 11

LXC_TURNKEY_VER_RE = re.compile(r"^debian-(\d+)-turnkey-")

_DOCKER_VER_TTL = 6 * 3600

_DOCKER_VER_CACHE = {"t": 0.0, "data": None}

_DOCKER_VER_LOCK = threading.Lock()

_DOCKER_VER_URLS = (
    "https://mirrors.ustc.edu.cn/docker-ce/linux/static/stable/x86_64/",
    "https://mirrors.aliyun.com/docker-ce/linux/static/stable/x86_64/",
    "https://download.docker.com/linux/static/stable/x86_64/",
)

_BACKUP_EXT_WHITELIST = (".vma", ".vma.zst", ".vma.gz", ".vma.lzo")

_VBIOS_EXT_WHITELIST = (".rom", ".bin")

VBIOS_TMP_DIR = Path("/var/tmp/pvewebtools-vbios")

MAX_VBIOS_SIZE = 16 * 1024 ** 2   # 单 ROM 上限 16MB（正常 vBIOS 仅几百 KB）

_vbios_lock = threading.Lock()

_MERGE_STATE = {}          # key -> {"state": "merging"|"done"|"error", "msg": str?}

_MERGE_LOCK = threading.RLock()

_UPLOAD_ACTIVE = {}       # key -> 正在接收的分片数；收齐且没有写入者时才能合并

_UPLOAD_SIZE_LIMITS = {
    "backup": MAX_BACKUP_SIZE, "vztmpl": MAX_VZTMPL_SIZE, "vbios": MAX_VBIOS_SIZE,
    "disk": MAX_DISK_SIZE, "cloud": MAX_DISK_SIZE,
}

_UPLOAD_INDEX_PATH = Path("/etc/pvewebtools/upload-index.json")

_upload_index_lock = threading.Lock()

DEFAULT_CLOUD_IMAGE_PRESETS = [
    {"label": "Ubuntu 24.04 (Noble)", "url": "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-amd64.img"},
    {"label": "Ubuntu 22.04 (Jammy)", "url": "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/jammy/current/jammy-server-cloudimg-amd64.img"},
    {"label": "Ubuntu 20.04 (Focal)", "url": "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/focal/current/focal-server-cloudimg-amd64.img"},
    {"label": "Ubuntu 18.04 (Bionic, EOL)", "url": "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/bionic/current/bionic-server-cloudimg-amd64.img"},
    {"label": "Debian 13 (Trixie)", "url": "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/trixie/latest/debian-13-generic-amd64.qcow2"},
    {"label": "Debian 12 (Bookworm)", "url": "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"},
    {"label": "Debian 11 (Bullseye)", "url": "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/bullseye/latest/debian-11-generic-amd64.qcow2"},
    {"label": "Fedora 44 (Cloud Base)", "url": "https://mirrors.ustc.edu.cn/fedora/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"},
    {"label": "Fedora 43 (Cloud Base)", "url": "https://mirrors.ustc.edu.cn/fedora/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"},
    {"label": "Rocky Linux 10", "url": "https://mirrors.ustc.edu.cn/rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2"},
    {"label": "Rocky Linux 9", "url": "https://mirrors.ustc.edu.cn/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2"},
    {"label": "Rocky Linux 8", "url": "https://mirrors.ustc.edu.cn/rocky/8/images/x86_64/Rocky-8-GenericCloud-Base.latest.x86_64.qcow2"},
    {"label": "AlmaLinux 10", "url": "https://mirrors.aliyun.com/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2"},
    {"label": "AlmaLinux 9", "url": "https://mirrors.aliyun.com/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2"},
    {"label": "AlmaLinux 8", "url": "https://mirrors.aliyun.com/almalinux/8/cloud/x86_64/images/AlmaLinux-8-GenericCloud-latest.x86_64.qcow2"},
    {"label": "CentOS 7.6 (GenericCloud 1811, EOL)", "url": "https://mirrors.nju.edu.cn/centos-cloud/centos/7/images/CentOS-7-x86_64-GenericCloud-1811.qcow2"},
    {"label": "CentOS 8.4 (GenericCloud, EOL)", "url": "https://mirrors.nju.edu.cn/centos-cloud/centos/8/x86_64/images/CentOS-8-GenericCloud-8.4.2105-20210603.0.x86_64.qcow2"},
    {"label": "CentOS Stream 9", "url": "https://mirrors.ustc.edu.cn/centos-cloud/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2"},
    {"label": "CentOS Stream 10", "url": "https://mirrors.ustc.edu.cn/centos-cloud/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2"},
    {"label": "Alpine 3.22 (Cloud, BIOS)", "url": "https://mirrors.ustc.edu.cn/alpine/v3.22/releases/cloud/generic_alpine-3.22.2-x86_64-bios-tiny-r0.qcow2"},
]

DEFAULT_MIRROR_REPO_SUPPORT = {
    "anolis": ["aliyun", "huawei", "ustc", "tsinghua"],
    "openeuler": ["aliyun", "huawei", "ustc", "tsinghua"],
}

DEFAULT_CLOUD_IMAGE_CONFIG = {
    "presets": DEFAULT_CLOUD_IMAGE_PRESETS,
    "mirror_repo_support": DEFAULT_MIRROR_REPO_SUPPORT,
}

class TTLCache:
    """带 TTL 的线程安全缓存：key -> 值，过期后下次 get 触发 loader 重新加载。
    把 _hw_cache/_cpu_topo_cache/_board_hw_cache 三处重复的时间戳+字典样板收敛成统一实现，
    并补上原裸字典缺失的锁（后台预热线程与前端请求可能并发写同一缓存）。"""
    def __init__(self, ttl, lock=None):
        self.ttl = ttl
        self._store = {}
        self._lock = lock or threading.Lock()
        self._refreshing = set()   # stale-while-revalidate：正在后台重算的 key，避免并发重复计算

    def get(self, key, loader, stale_ok=False):
        """命中且未过期直接返回值；否则调用 loader() 重算并回填。
        loader 抛异常时：若有旧值则降级返回（fail-soft），否则向上抛。
        stale_ok=True 时（只给「过期也能用」的只读下拉数据）过期不阻塞：
        立即返回旧值，并起后台线程重算，下一次请求就能拿到新值。"""
        now = time.time()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
            stale = hit[1] if hit is not None else None
            start_bg = stale is not None and stale_ok and key not in self._refreshing
            if start_bg:
                self._refreshing.add(key)
        if start_bg:
            def _bg(k=key):
                val = None
                try:
                    val = loader()
                except Exception:  # noqa: BLE001
                    val = None
                with self._lock:
                    if val is not None:
                        self._store[k] = (time.time(), val)
                    self._refreshing.discard(k)
            threading.Thread(target=_bg, daemon=True).start()
            return stale
        try:
            val = loader()
        except Exception:
            if stale is not None:
                return stale
            raise
        with self._lock:
            self._store[key] = (now, val)
        return val


_hw_cache = TTLCache(300)          # node -> (时间戳, 信息 dict)

_cpu_topo_cache = TTLCache(300)

_board_hw_cache = TTLCache(600)    # node -> (时间戳, 信息 dict)

_nodes_cache = {"ts": 0.0, "data": None}

_nodes_cache_lock = threading.Lock()

_nodes_preheating = False       # 标记预热进行中，避免重复执行 collect_nodes()

_nodes_refreshing = False       # 标记后台全量重采进行中（手动刷新 ?fresh=1 触发），避免连续点击叠加 SSH 探测


_disks_baseline = {}   # node -> set(盘名)

_disks_lost = {}       # node -> {盘名: 首次消失时间戳}

DISKS_LOST_TTL = 600

_EFI_PART_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"

_MS_RESERVED_TYPE = "e3c9e316-0b5c-4db8-817d-f92df00215ae"

_WIN_RECOVERY_TYPE = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"


def edition_caps():
    """版本能力描述（供 /api/edition 与「加入节点」入口判断共用，避免前后端口径漂移）。

    - edition：工具集档位（lite，用于版本胶囊展示）
    - runtime：同 edition（保留字段以兼容前端）
    - max_nodes / enforced：0 / False —— 不限制机器数
    - can_join：True —— 允许创建 / 加入集群
    """
    return {
        "edition": str(getattr(toolbox, "EDITION", "") or ""),
        "runtime": str(getattr(toolbox, "EDITION", "") or ""),
        "max_nodes": 0,
        "enforced": False,
        "can_join": True,
    }


def _cloud_image_dir():
    """云镜像/磁盘上传主落盘目录：pvewebtools 共享存储根下的 cloud-images/
    （NFS 共享、跨节点可见、目录名自描述；非 PVE 保留名故不被存储扫描）。
    通过 storage_path("pvewebtools") 稳健取存储根，不再依赖 import 内容反推。
    共享存储不可用（单节点/未配 NFS）时回退到项目根下 cloud-images 目录。"""
    root = backend.storage_path("pvewebtools")
    if root:
        return root / "cloud-images"
    return Path(storage_provision.DEFAULT_DIR_PATH) / "cloud-images"


def _disk_image_dir():
    """普通磁盘镜像（无 cloud-init，用于 qm importdisk 磁盘导入）落盘目录：
    pvewebtools 共享存储根下的 disk-images/。与 cloud-images/ 平级、用途不同：
    cloud-images 存含 cloud-init 的云镜像（供云镜像建机），disk-images 存普通磁盘镜像。
    通过 storage_path("pvewebtools") 稳健取存储根。共享存储不可用时回退到项目根下 disk-images 目录。"""
    root = backend.storage_path("pvewebtools")
    if root:
        return root / "disk-images"
    return Path(storage_provision.DEFAULT_DIR_PATH) / "disk-images"


def _allowed_disk_ext(name):
    """磁盘镜像上传允许的扩展名（与 PVE/云迁移常用格式一致）"""
    return name.lower().endswith((".qcow2", ".raw", ".img", ".vmdk", ".vhdx", ".vdi", ".vpc"))


def _is_cloud_compressed(name):
    """判断 name 是否为「单层压缩的单一磁盘镜像」（如 xxx.qcow2.xz）。
    选项 B：下载/上传均不自动解压，压缩包原样保留，云镜像装机/磁盘导入时再现场解压。"""
    low = name.lower()
    for e in _CLOUD_COMP_EXTS:
        if low.endswith(e):
            return _allowed_disk_ext(low[: -len(e)])
    return False


def _decompress_file(src, dst):
    """把压缩包 src 解压为磁盘镜像 dst（仅用于 .xz/.gz/.zst 单层压缩的单一磁盘镜像）。
    解压失败抛异常，由调用方转成友好错误；解压成功后调用方负责删除源文件。
    按文件头魔数识别压缩格式，不受临时文件名（如 .part / .qcow2.part）影响。"""
    with open(src, "rb") as f:
        magic = f.read(6)
    if magic[:6] == b"\xfd\x37\x7a\x58\x5a\x00":          # .xz
        with lzma.open(src, "rb") as fin, open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
    elif magic[:2] == b"\x1f\x8b":                          # .gz
        with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
    elif magic[:4] == b"\x28\xb5\x2f\xfd":                 # .zst
        try:
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                dctx.copy_stream(fin, fout)
        except ImportError:
            # 退回到系统 zstd 命令（PVE 自带），避免强依赖第三方模块
            with open(dst, "wb") as fout:
                subprocess.run(["zstd", "-d", "-f", "-c", src], stdout=fout, check=True)
    else:
        raise ValueError("不支持的压缩格式")


def cloud_image_import_path(storage):
    """云镜像后台下载的目标目录：指定 storage 时落到该存储根下的 cloud-images/ 子目录；
    未指定或非法存储则回退到默认导入目录（_cloud_image_dir()，优先共享存储）。返回 (dest_dir: Path, storage_label: str)。
    同时识别 images 内容存储(cloud-images)与 import 内容存储(/import)，
    使方案 B 工作区(pvewebtools, 仅 import 内容)也能作为云镜像落地位置。"""
    if storage:
        try:
            root = backend.disk_dirs().get(storage)
            if root is not None:
                return root / "cloud-images", storage
            iroot = backend.import_dirs().get(storage)
            if iroot is not None:
                return iroot.parent / "cloud-images", storage
        except Exception:
            pass
    return _cloud_image_dir(), "__import__"


def cloud_image_mirrors():
    """可选下载源列表：均为域名替换式镜像站，首项（中科大 USTC）即前端默认选中项。"""
    return [dict(m) for m in CLOUD_MIRRORS]


def apply_cloud_mirror(url, mirror):
    """按所选下载源改写 URL 域名（保留路径与查询串）；default/未知 mirror 原样返回。
    仅接受 http/https 且带路径的 URL，其余一律原样返回（交由调用方校验报错）。"""
    mid = (mirror or "").strip().lower()
    if mid in ("", "default"):
        return url
    host = next((m["host"] for m in CLOUD_MIRRORS if m["id"] == mid), "")
    if not host:
        return url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.path:
        return url
    return f"https://{host}{p.path}" + (f"?{p.query}" if p.query else "")


def _normalize_ct_base(url):
    """把 APLInfo.pm 里读到的地址规整成 CT 源根（去掉尾部 /images 与 /）。

    模板实际下载地址为 <CT源根>/images/<section>/<文件名>（如 images/system/、images/mail/），
    而 APLInfo.pm 里可能已带 /images 后缀，统一剥掉避免拼出 .../images/images/xxx。"""
    u = (url or "").strip().rstrip("/")
    if u.lower().endswith("/images"):
        u = u[: -len("/images")].rstrip("/")
    return u


def lxc_ct_source():
    """读取本机当前生效的 CT 模板源（APLInfo.pm 中的地址）。

    「更换软件源和 LXC 源」工具会把这里的官方地址替换成所选镜像站，
    故本函数读到的即用户当前真正使用的源；读不到时回落官方源。"""
    try:
        text = APLINFO_PATH.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return DEFAULT_CT_SOURCE
    urls = re.findall(r"https?://[A-Za-z0-9._\-]+(?::\d+)?[A-Za-z0-9._\-/]*", text)
    if not urls:
        return DEFAULT_CT_SOURCE
    # 优先取带 proxmox 的地址（镜像站路径也含 /proxmox），避免误取注释里的其它域名
    hit = next((u for u in urls if "proxmox" in u.lower()), urls[0])
    return _normalize_ct_base(hit) or DEFAULT_CT_SOURCE


def lxc_template_mirrors():
    """可选下载源：[{id, label, base}]。

    首项固定为「跟随本机」——直接用 APLInfo.pm 里配置的源（与「更换软件源和 LXC 源」联动）；
    其余为 LXC_TEMPLATE_MIRROR_IDS 中实测确实提供 CT 模板的国内镜像站。
    官方源不再单独列出：本机未换源时「跟随本机」即等效官方源，换源后「跟随本机」自动跟随本机配置，
    无需再保留一个固定官方源选项（避免与「跟随本机」在官方配置下重复）。"""
    cur = lxc_ct_source()
    out = [{"id": "follow", "label": f"跟随本机（{cur}）", "base": cur}]
    for mid in LXC_TEMPLATE_MIRROR_IDS:
        m = toolbox.MIRRORS.get(mid) or {}
        base = (m.get("ct") or "").strip()
        if not base:
            continue
        out.append({"id": mid, "label": _LXC_TEMPLATE_MIRROR_LABELS.get(mid, mid),
                    "base": _normalize_ct_base(base)})
    return out


def lxc_template_base(mirror=""):
    """按所选下载源 id 解析 CT 源根地址；未知 id 一律回落「跟随本机」。"""
    for m in lxc_template_mirrors():
        if m["id"] == (mirror or "follow"):
            return m["base"]
    return lxc_ct_source()


def _ssh_read_ct_source(node):
    """SSH 到远端节点读取其 APLInfo.pm 中的 CT 模板源；失败返回 (None, error)。

    与工具执行同链路（免密 SSH + ControlMaster 复用），集群节点不可达时降级而非抛错。"""
    if not node or not node.replace("-", "").replace("_", "").isalnum():
        return None, "非法节点名"
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
             *backend.ssh_host_opts(node),
             f"root@{backend.ssh_host(node)}", f"cat {shlex.quote(str(APLINFO_PATH))} 2>/dev/null || true"],
            capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return None, "读取超时"
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:120]
    text = r.stdout
    urls = re.findall(r"https?://[A-Za-z0-9._\-]+(?::\d+)?[A-Za-z0-9._\-/]*", text)
    if not urls:
        return None, "未读取到源地址"
    hit = next((u for u in urls if "proxmox" in u.lower()), urls[0])
    return _normalize_ct_base(hit) or DEFAULT_CT_SOURCE, None


def lxc_cluster_sources(force=False):
    """检测集群各节点的 CT 模板源是否一致。

    本机直接读 APLInfo.pm；其它节点走免密 SSH。
    返回 {local_node, local_source, nodes:[{node,source,ok,error}],
          consistent, inconsistent:[node...], multi_node}。force=True 跳过缓存。"""
    with _cluster_src_lock:
        if not force and _cluster_src_cache["data"] is not None and \
           time.time() - _cluster_src_cache["ts"] < _CLUSTER_SRC_TTL:
            return _cluster_src_cache["data"]
    local_node = socket.gethostname().split(".")[0]
    local_src = lxc_ct_source()
    nodes = []
    try:
        cluster = backend.list_nodes() or []
    except Exception:
        cluster = []
    for n in cluster:
        name = str(n.get("node") or "").strip()
        if not name or name == local_node:
            nodes.append({"node": name or local_node, "source": local_src, "ok": True, "error": None})
        else:
            src, err = _ssh_read_ct_source(name)
            nodes.append({"node": name, "source": src, "ok": err is None, "error": err})
    distinct = {x["source"] for x in nodes if x["ok"] and x["source"]}
    consistent = len(distinct) <= 1
    inconsistent = [x["node"] for x in nodes
                    if x["ok"] and x["source"] and x["source"] != local_src]
    data = {
        "local_node": local_node,
        "local_source": local_src,
        "nodes": nodes,
        "consistent": consistent,
        "inconsistent": inconsistent,
        "multi_node": len(nodes) > 1,
    }
    with _cluster_src_lock:
        _cluster_src_cache["ts"], _cluster_src_cache["data"] = time.time(), data
    return data


def lxc_batch_swap(mirror, security):
    """对集群每个节点（含本机）提交 change_sources 任务，实现「一键批量换源」。

    复用 change_sources 工具的脚本与 env 展开逻辑（镜像/安全源地址一致），
    逐节点提交任务（本机本地执行，其余节点走免密 SSH）；远程节点走授权闸口。
    返回 {jobs:[{node, ok, job_id?/error}], total}。"""
    if mirror not in ("ustc", "tuna", "official", "auto"):
        raise toolbox.ToolError(f"非法镜像源: {mirror}")
    if security not in ("official", "mirror"):
        raise toolbox.ToolError(f"非法安全源: {security}")
    tool = toolbox.get_tool("change_sources")
    clean = {"mirror": mirror, "security": security}
    script = toolbox.build_script(tool)
    env = toolbox.build_env(tool, clean)
    try:
        cluster = backend.list_nodes() or []
    except Exception as e:
        raise toolbox.ToolError(f"无法获取集群节点: {e}")
    local_node = socket.gethostname().split(".")[0]
    out = []
    for n in cluster:
        name = str(n.get("node") or "").strip()
        if not name:
            continue
        if name != local_node and not name.replace("-", "").replace("_", "").isalnum():
            out.append({"node": name, "ok": False, "error": "非法节点名"})
            continue
        node = None if name == local_node else name
        jid = jobs.submit("change_sources", f"批量换源 · {name}", script=script, env_params=env, node=node)
        out.append({"node": name, "ok": True, "job_id": jid})
    return {"jobs": out, "total": len(out)}


def _lxc_mirror_root(base):
    """把 CT 源根（如 https://mirrors.ustc.edu.cn/proxmox）规整成镜像站根（https://mirrors.ustc.edu.cn）"""
    m = re.match(r"^(https?://[^/]+)", (base or "").strip())
    return m.group(1).rstrip("/") if m else ""


def _lxc_turnkey_roots(base):
    """TurnKey 模板（<镜像根>/turnkeylinux/images/proxmox/<文件名>）的候选镜像根，按优先级排列：
    先放所选源所在的镜像站（就近、通常更快），再依次补其它国内镜像站，最后回落 TurnKey 官方。
    各站覆盖的版本不同，必须逐个尝试才能覆盖 debian-10 这类只在部分站留存的老模板。"""
    roots = []
    host_root = _lxc_mirror_root(base)
    if host_root and any(h in host_root.lower() for h in TURNKEY_MIRROR_HOSTS):
        roots.append(host_root)
    for r in TURNKEY_DOMESTIC_ROOTS:
        if r not in roots:
            roots.append(r)
    roots.append(TURNKEY_OFFICIAL_ROOT)
    return roots


def _lxc_template_urls(base, section, name):
    """模板下载候选地址列表（首项为主用地址，其余为回退地址，供下载线程逐个尝试）：
    常规分类位于 <CT源根>/images/<section>/<文件名>，如 .../images/system/debian-13-*.tar.zst
    （镜像站与官方源结构一致，缺 images/ 这级会 404）；turnkeylinux 分类改走各镜像根的 TurnKey 目录
    <镜像根>/turnkeylinux/images/proxmox/（Proxmox/镜像站的 images/ 下已不含该段文件，见上）。
    常规分类同样给出回退源（LXC_FALLBACK_CT_ROOTS），所选源缺该文件时自动换源重试。"""
    sec = (section or "").strip().lower()
    if not LXC_SECTION_RE.match(sec):
        sec = "system"
    if sec == "turnkeylinux":
        return [r.rstrip("/") + "/turnkeylinux/images/proxmox/" + name
                for r in _lxc_turnkey_roots(base)]
    roots = [base.rstrip("/")]
    roots += [r.rstrip("/") for r in LXC_FALLBACK_CT_ROOTS if r.rstrip("/") != base.rstrip("/")]
    return [r + "/images/" + sec + "/" + name for r in roots]


def _lxc_template_url(base, section, name):
    """模板主用下载地址（供接口回显与单地址调用；完整候选列表见 _lxc_template_urls）"""
    return _lxc_template_urls(base, section, name)[0]


def _resolve_lxc_section(name):
    """按模板名在本机 pveam available 输出中查其所属 section（如 system / mail）。"""
    try:
        for it in list_lxc_templates().get("items", []):
            if it.get("name") == name:
                return (it.get("section") or "").strip().lower()
    except Exception:
        pass
    return ""


def _host_arch():
    """本机 CPU 架构（amd64/arm64），用于在模板下拉中标注架构不符的条目。"""
    try:
        return subprocess.run(["dpkg", "--print-architecture"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pveam_bin():
    return "pveam" if shutil.which("pveam") else "/usr/sbin/pveam"


def _lxc_template_available(section, name):
    """索引条目是否真的有可下载文件；当前只处理 TurnKey 老版本（debian-10 及更早）已被上游清理的情形。"""
    if (section or "").strip().lower() != "turnkeylinux":
        return True
    m = LXC_TURNKEY_VER_RE.match((name or "").strip())
    return not m or int(m.group(1)) >= LXC_TURNKEY_MIN_DEBIAN


def list_lxc_templates(refresh=False, mirror=""):
    """列出 pveam 中可下载的 CT 模板。

    走本机 pveam（而非固定清单）：列表随上游实时变化，且索引地址由 PVE 自身维护，
    因此「更换软件源和 LXC 源」切到国内镜像后，这里列出的同样是镜像站上的模板。
    refresh=True 时先执行 pveam update 刷新索引（上游新模板才会出现）。
    返回 {source, host_arch, mirrors, items, error}；pveam 不可用时不抛异常，
    由 error 字段告知前端（非 PVE 环境或 pveam 缺失时 UI 仍可用）。"""
    base = lxc_template_base(mirror)
    error = ""
    ran_update = False

    def _available():
        p = subprocess.run([_pveam_bin(), "available"], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "pveam available 失败").strip().splitlines()[-1])
        items = []
        for line in p.stdout.splitlines():
            cols = line.split()
            if len(cols) < 2:
                continue
            section, name = cols[0], cols[1]
            arch = cols[2] if len(cols) > 2 else ""
            if not LXC_TEMPLATE_NAME_RE.match(name):
                continue
            items.append({"section": section, "name": name, "arch": arch,
                          "url": _lxc_template_url(base, section, name)})
        return items

    try:
        if refresh:
            # 刷新索引：上游新增/更新模板后才可见；失败不影响后续用旧索引列
            up = subprocess.run([_pveam_bin(), "update"], capture_output=True, text=True, timeout=180)
            ran_update = True
            if up.returncode != 0:
                error = (up.stderr or up.stdout or "pveam update 失败").strip().splitlines()[-1]
        items = _available()
        if not items and not refresh:
            # 首次打开（索引尚未下载过）自动刷新一次，避免下拉空白
            subprocess.run([_pveam_bin(), "update"], capture_output=True, text=True, timeout=180)
            ran_update = True
            items = _available()
    except FileNotFoundError:
        items, error = [], "本机未找到 pveam，请确认运行在 PVE 宿主机上"
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        items, error = [], str(e)

    host_arch = _host_arch()
    # 过滤与本机架构不符的模板（如本机 amd64 时不列 arm64 模板）：架构是硬约束，
    # 下错架构的模板建 CT 必失败；host_arch 取不到（非 PVE 环境）时不过滤，全部保留。
    items = [it for it in items
             if not (host_arch and it.get("arch") and it["arch"] != host_arch)]
    # 再剔除上游已下架、镜像上已无文件的条目（TurnKey 的 debian-10 段），并回报数量供前端提示
    kept = [it for it in items if _lxc_template_available(it.get("section"), it.get("name"))]
    hidden_stale = len(items) - len(kept)
    return {"source": base, "host_arch": host_arch, "mirrors": lxc_template_mirrors(),
            "items": kept, "updated": ran_update, "error": error, "hidden_stale": hidden_stale}


def list_lxc_downloaded():
    """列出本机已下载的 LXC 模板（各支持 vztmpl 存储的 template/cache 下的 .tar.*），
    供「创建 LXC 容器」的模板来源下拉。返回 [{storage, name, size}]。"""
    out = []
    try:
        dirs = backend.vztmpl_dirs()
    except Exception:
        return out
    for sid, d in sorted(dirs.items()):
        root = Path(d)
        if not root.is_dir():
            continue
        try:
            files = sorted(root.iterdir())
        except OSError:
            continue
        for f in files:
            if f.is_file() and LXC_TEMPLATE_NAME_RE.match(f.name):
                try:
                    size = f.stat().st_size
                except OSError:
                    size = None
                out.append({"storage": sid, "name": f.name, "size": size})
    return out


def _docker_ver_fetch():
    html = ""
    for url in _DOCKER_VER_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pvewebtools"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", "replace")
            if "docker-" in html:
                break
        except Exception:
            continue
    vers = set(re.findall(r"docker-(\d+\.\d+\.\d+)\.tgz", html))
    if not vers:
        return None
    by_major = {}
    for v in vers:
        major, minor, patch = (int(x) for x in v.split("."))
        by_major.setdefault(major, []).append((minor, patch, v))
    out = []
    for major in sorted(by_major, reverse=True):
        minor, patch, final = max(by_major[major])
        out.append({"major": str(major), "final": final})
    return out


def _list_docker_versions_base():
    """Docker 引擎全部稳定版按大版本分组的最终稳定版（[{major, final}]，major 降序），结果缓存 6h。"""
    with _DOCKER_VER_LOCK:
        now = time.time()
        if _DOCKER_VER_CACHE["data"] and now - _DOCKER_VER_CACHE["t"] < _DOCKER_VER_TTL:
            return _DOCKER_VER_CACHE["data"]
        stale = _DOCKER_VER_CACHE["data"]
        data = _docker_ver_fetch()
        if data is not None:
            _DOCKER_VER_CACHE["t"] = now
            _DOCKER_VER_CACHE["data"] = data
            return data
        return stale or []


def list_docker_versions():
    """Docker 引擎稳定版清单（按大版本分组的最终稳定版）。

    数据源为 docker-ce 仓库 static/stable 目录列表（与发行版无关），结果缓存 6 小时；
    抓取失败时回退上次缓存（即使过期），仍无则返回 []（前端给出可读占位）。"""
    return _list_docker_versions_base()


def lxc_template_dest(storage):
    """CT 模板落盘目录：<存储根>/template/cache（PVE 约定的容器模板目录）。"""
    try:
        dirs = backend.vztmpl_dirs()
    except Exception:
        dirs = {}
    d = dirs.get(storage)
    if d is None:
        raise pve_api.PveError(
            f"存储 {storage} 不支持容器模板(vztmpl)内容"
            if dirs else "未找到支持容器模板(vztmpl)内容的存储")
    return Path(d)


def _free_space(path, layers=6, timeout=1.0):
    """带超时的剩余空间探测：返回可用字节数，探测不到/超时返回 None。

    网络存储（NFS/CIFS）上的 statvfs 会走网络，存储卡住时 `shutil.disk_usage` 可能阻塞
    几十秒，把整个下拉请求一起拖死（前端 15s 超时后还会重试，最终表现成「加载很慢 + 无可用存储」）。
    这里把探测丢进后台线程并限时等待，拿不到就放弃（前端只是不显示「可用 xx」），
    同时沿用原有「目标目录尚未创建时逐级向上找可用文件系统」的语义。"""
    probe = Path(path)
    for _ in range(max(1, layers)):
        box = {}

        def _probe(p):
            try:
                box["free"] = shutil.disk_usage(p).free
            except OSError:
                pass

        t = threading.Thread(target=_probe, args=(probe,), daemon=True)
        t.start()
        t.join(timeout)
        if box.get("free") is not None:
            return box["free"]
        if probe == probe.parent:
            break
        probe = probe.parent
    return None


def list_vztmpl_storages():
    """列出本机支持 vztmpl 内容的存储（供「获取 LXC 模板」选目标存储），附剩余空间。"""
    try:
        dirs = backend.vztmpl_dirs()
    except Exception:
        return []
    out = []
    for sid, d in sorted(dirs.items()):
        # 模板目录可能尚未创建（从未下载过），逐级向上取可用文件系统的剩余空间（带超时）
        out.append({"storage": sid, "path": str(d), "free": _free_space(d)})
    return out


_cloud_storages_cache = TTLCache(300)   # 目标存储下拉：5 分钟内复用，过期先返回旧值再后台刷新


def list_cloud_storages():
    """列出可作为云镜像下载目标的存储（images/import 内容，落 <存储根>/cloud-images/），附剩余空间。

    首项固定为 pvewebtools 共享存储（storage 为空 → cloud_image_import_path 回落 _cloud_image_dir()，
    即 pvewebtools 根下 cloud-images/），与「获取 LXC 模板」的目标存储命名保持一致。

    结果带 5 分钟缓存 + stale-while-revalidate：过期瞬间先返回上次结果（打开传输中心永远秒回），
    后台线程再重算。存储定义读 /etc/pve/storage.cfg（毫秒级），不再为这个下拉跑 pvesh。
    """
    return _cloud_storages_cache.get("local", _compute_cloud_storages, stale_ok=True)


def _compute_cloud_storages():
    out = []
    try:
        d = _cloud_image_dir()
        out.append({"storage": "", "label": "pvewebtools", "free": _free_space(d)})
    except Exception:
        out.append({"storage": "", "label": "pvewebtools", "free": None})
    try:
        dir_maps = backend.disk_and_import_dirs()
    except Exception:
        dir_maps = ({}, {})
    for dirs in dir_maps:
        for sid, root in sorted(dirs.items()):
            if any(x["storage"] == sid for x in out):
                continue
            out.append({"storage": sid, "label": sid, "free": _free_space(root)})
    return out


def _image_virtual_size(path):
    """读取镜像的虚拟磁盘大小（字节），用于在下拉中展示「默认系统盘大小」。
    .qcow2/.raw/.vmdk 等通过 `qemu-img info` 的 virtual-size 获取；
    .xz/.gz/.zst 等压缩包无法在不解压时获知，返回 None（前端回落显示文件大小）。
    任何异常均返回 None，避免影响镜像列表加载。"""
    try:
        out = subprocess.run(
            ["qemu-img", "info", "--output=json", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return None
        info = json.loads(out.stdout or "{}")
        vs = info.get("virtual-size")
        return int(vs) if vs else None
    except Exception:
        return None


def _classify_os(name, distro=""):
    """按文件名 + 发行版元数据粗分镜像操作系统：windows / linux / unknown。
    用于「云镜像制作」按操作系统类型筛选源镜像（文件名通常带 win/ubuntu 等，或 .meta.json 记录了 distro）。"""
    text = f"{distro} {name}".lower()
    win_kw = ("win", "windows", "cloudbase", "w2k", "ws20", "windows server")
    if any(k in text for k in win_kw):
        return "windows"
    lin_kw = ("linux", "ubuntu", "debian", "centos", "rocky", "alma", "fedora",
              "opensuse", "suse", "arch", "kali", "oracle", "rhel", "alpine",
              "openeuler", "euler", "redhat", "gentoo", "mint", "tlinux", "anolis")
    if any(k in text for k in lin_kw):
        return "linux"
    return "unknown"


def list_imported_disks(os_filter=None, kind=None, storage_filter=None):
    """列出已就绪的磁盘镜像，供导入/部署工具下拉选择。
    共享存储 pvewebtools 下分两个独立目录（kind 区分）：
      - cloud-images/：含 cloud-init 的云镜像（供云镜像建机/制作）
      - disk-images/ ：普通磁盘镜像（供 qm importdisk 磁盘导入，如 openEuler 等无 cloud-init 的系统）
    各存储的 <根>/cloud-images 视作云镜像（kind="cloud"）、<根>/import 视作普通磁盘（kind="disk"）一并扫描。
    storage 字段标记文件位置："__import__"=pvewebtools 共享目录，其余为具体存储 id。
    kind：传 "cloud" 只列云镜像、"disk" 只列普通磁盘、"None" 两者都列（传输中心总览用）。
    每条附 kind 字段，便于前端分组与下载/删除时路由。
    vsize 为镜像虚拟磁盘大小（字节），压缩包或不可识别时为 None（此时用 size 文件大小回落）。
    os_filter="linux" 仅保留 Linux 及无法识别的镜像（归入 Linux 制作）；
    os_filter="windows" 仅保留 Windows 镜像；其余（如 None）不过滤（云镜像建机可选任意系统）。
    storage_filter：传具体存储 id 时只返回该存储下的条目（供传输中心「存储内容」跟随所选存储）；
    pvewebtools 的条目在结果里以 "__import__" 标记，故两者视为同一存储。"""
    root = backend.storage_path("pvewebtools")
    locations = []
    if root and kind in (None, "cloud"):
        locations.append((root / "cloud-images", "__import__", "cloud"))
    if root and kind in (None, "disk"):
        locations.append((root / "disk-images", "__import__", "disk"))
    try:
        for sid, d in backend.disk_dirs().items():
            locations.append((d / "cloud-images", sid, "cloud"))
        for sid, d in backend.import_dirs().items():
            locations.append((d, sid, "disk"))
            locations.append((d.parent / "cloud-images", sid, "cloud"))
    except Exception:
        pass
    out = []
    seen = set()
    for d, st, knd in locations:
        try:
            d.mkdir(parents=True, exist_ok=True)
            rd = d.resolve()
            for p in d.iterdir():
                if p.is_file() and (_allowed_disk_ext(p.name) or _is_cloud_compressed(p.name)):
                    rp = p.resolve()
                    if rp in seen:
                        continue
                    seen.add(rp)
                    entry = {"name": p.name, "size": p.stat().st_size,
                             "vsize": _image_virtual_size(p), "storage": st, "kind": knd}
                    meta_path = d / (p.name + ".meta.json")
                    if meta_path.is_file():
                        try:
                            m = json.loads(meta_path.read_text(encoding="utf-8"))
                            entry["label"] = m.get("label") or p.name
                            entry["distro"] = m.get("distro") or ""
                            entry["version"] = m.get("version") or ""
                        except (OSError, ValueError):
                            pass
                    out.append(entry)
        except OSError:
            continue
    if storage_filter:
        want = "pvewebtools" if storage_filter in ("", "__import__") else storage_filter
        out = [e for e in out if ("pvewebtools" if e["storage"] == "__import__" else e["storage"]) == want]
    # 按操作系统筛选（仅「云镜像制作」按类型分流；其它场景 os_filter=None 不过滤）
    if os_filter == "windows":
        out = [e for e in out if _classify_os(e["name"], e.get("distro", "")) == "windows"]
    elif os_filter == "linux":
        out = [e for e in out if _classify_os(e["name"], e.get("distro", "")) in ("linux", "unknown")]
    if kind:
        out = [e for e in out if e["kind"] == kind]
    return sorted(out, key=lambda x: (x["storage"], x["name"]))


def _resolve_import_disk(storage, name, kind=None):
    """解析导入目录中的磁盘镜像真实路径（防路径穿越）。
    storage 为具体存储 id 时额外尝试该存储的 cloud-images/ 与 import/；
    默认/__import__ 按 kind 取 pvewebtools 的 cloud-images（kind=cloud）或 disk-images（kind=disk）。"""
    if not name or not re.fullmatch(r"[\w][\w.\- ]*", name):
        raise pve_api.PveError("非法文件名")
    if not (_allowed_disk_ext(name) or _is_cloud_compressed(name)):
        raise pve_api.PveError("不允许的文件类型")
    candidates = []
    if not storage or storage == "__import__":
        root = backend.storage_path("pvewebtools")
        if root is None:
            raise pve_api.PveError("共享存储不可用")
        d = root / ("disk-images" if kind == "disk" else "cloud-images")
        candidates.append(d)
    else:
        try:
            d = backend.disk_dirs().get(storage)
            if d is not None:
                candidates.append(d / "cloud-images")
            idir = backend.import_dirs().get(storage)
            if idir is not None:
                candidates.append(idir)
        except Exception:
            pass
    for d in candidates:
        path = d / name
        if path.is_file():
            return path
    raise pve_api.PveError("磁盘镜像不存在")


def _merge_disk_chunks(name, count, dest_dir, kind="disk"):
    """磁盘分片收齐后合并为最终镜像；全局锁防并发重复合并。返回是否完成。
    dest_dir 为目标落盘目录（cloud-images 或 disk-images，由上传 kind 决定）。"""
    tmp = _upload_tmp_dir(kind, name, "")
    with _disk_lock:
        if not tmp.is_dir():
            return False
        parts = _completed_upload_parts(tmp, count)
        if not parts:
            return False
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        target = Path(dest_dir) / name
        staging = target.with_name(target.name + ".part")
        try:
            with open(staging, "wb") as out:
                for p in parts:
                    with open(p, "rb") as pf:
                        shutil.copyfileobj(pf, out, 1024 * 1024)
            _check_upload_merged_size(tmp, staging)
            if staging.stat().st_size > MAX_DISK_SIZE:
                raise ValueError("磁盘镜像超过 256GB 上限")
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
        return True


def _pci_bd(addr):
    """PCI 地址归一化成 bus:device（去 domain 前缀与 .function），用于比对 VM 配置里的写法。"""
    s = (addr or "").strip()
    if not s:
        return ""
    s = re.sub(r"^[0-9a-fA-F]{4}:", "", s)
    return s.split(".")[0].lower()



def _upload_name_ok(kind, name):
    """上传、续传查询和秒传登记共用文件名规则，不放宽固件/驱动物料的限制。"""
    if not isinstance(name, str) or not name:
        return False
    if kind in ("backup", "disk", "cloud"):
        return re.fullmatch(r"[\w][\w.\- ]*", name) is not None
    if kind == "vbios":
        return re.fullmatch(r"[\w][\w.\-]{0,99}", name) is not None
    if kind == "vztmpl":
        return _allowed_vztmpl_ext(name)
    return False


def _upload_roots():
    """disk/cloud 的类型由目录持久化，重启后不再猜测同一分片属于哪个目标。"""
    return (("backup", BACKUP_TMP_DIR), ("vztmpl", VZTMPL_TMP_DIR),
            ("disk", DISK_TMP_DIR / "disk"), ("cloud", DISK_TMP_DIR / "cloud"),
            ("vbios", VBIOS_TMP_DIR))


def _upload_tmp_dir(kind, name, storage):
    """各上传类型的分片目录；磁盘与云镜像即使同名也互不混用。"""
    if kind == "backup":
        return BACKUP_TMP_DIR / f"{storage}__{name}"
    if kind == "vztmpl":
        return VZTMPL_TMP_DIR / f"{storage}__{name}"
    if kind in ("disk", "cloud"):
        return DISK_TMP_DIR / kind / name
    if kind == "vbios":
        return VBIOS_TMP_DIR / name
    return None


def _read_upload_meta(tmp):
    try:
        meta = json.loads((tmp / ".upload.json").read_text(encoding="utf-8"))
        if (isinstance(meta, dict) and meta.get("version") == 1
                and type(meta.get("count")) is int and meta["count"] > 0
                and _upload_name_ok(meta.get("kind"), meta.get("name"))
                and isinstance(meta.get("storage"), str)
                and (meta.get("size") is None
                     or type(meta["size"]) is int and meta["size"] > 0)):
            return meta
    except (OSError, ValueError):
        pass
    return None


def _completed_upload_parts(tmp, count):
    """只承认完整接收后原子发布的规范分片，必须恰好覆盖 0..count-1。"""
    if count <= 0:
        return []
    parts = {}
    for p in tmp.glob("part*"):
        match = re.fullmatch(r"part(\d+)", p.name)
        if match and p.is_file():
            index = int(match.group(1))
            if 0 <= index < count and p.name == f"part{index:06d}":
                parts[index] = p
    if len(parts) != count:
        return []
    return [parts[i] for i in sorted(parts)]


def _check_upload_merged_size(tmp, staging):
    meta = _read_upload_meta(tmp)
    if meta and meta["size"] is not None and staging.stat().st_size != meta["size"]:
        raise backend.PveError("上传文件大小不符，请重新上传")


def _receive_upload_chunk(rfile, length, kind, name, storage, index, count, raw_size=""):
    """接收完整分片后才发布 partNNNNNN；半片、正在写入的片均不可续传或合并。"""
    if (not _upload_name_ok(kind, name) or length <= 0
            or count <= 0 or not 0 <= index < count):
        raise backend.PveError("上传分片参数错误")
    try:
        size = int(raw_size or 0)
    except (TypeError, ValueError):
        size = 0
    meta = {"version": 1, "kind": kind, "name": name, "storage": storage or "",
            "count": count, "size": size if size > 0 else None}
    key = _merge_key(kind, name, storage)
    tmp = _upload_tmp_dir(kind, name, storage)
    staging = None
    with _MERGE_LOCK:
        if _MERGE_STATE.get(key, {}).get("state") == "merging":
            raise backend.PveError("文件正在合并，请稍后再试")
        tmp.mkdir(parents=True, exist_ok=True)
        previous = _read_upload_meta(tmp)
        if previous is None:
            # 旧版本 part 文件可能只写了一半，缺少可信元数据时重新接收，不猜测完整性。
            for p in tmp.glob("part*"):
                if p.is_file():
                    p.unlink()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=tmp,
                                             prefix=".meta-", delete=False) as f:
                meta_path = Path(f.name)
                json.dump(meta, f, ensure_ascii=False)
            try:
                meta_path.replace(tmp / ".upload.json")
            finally:
                meta_path.unlink(missing_ok=True)
            _MERGE_STATE.pop(key, None)
        elif previous != meta:
            raise backend.PveError("同名文件已有不同的未完成上传，请更换文件名或等待残片清理")
        _UPLOAD_ACTIVE[key] = _UPLOAD_ACTIVE.get(key, 0) + 1
        tmp.touch()
    try:
        with tempfile.NamedTemporaryFile(dir=tmp, prefix=".incoming-", delete=False) as f:
            staging = Path(f.name)
            remaining = length
            while remaining:
                buf = rfile.read(min(remaining, 1024 * 1024))
                if not buf:
                    raise backend.PveError("分片传输不完整，请重试上传")
                f.write(buf)
                remaining -= len(buf)
        with _MERGE_LOCK:
            staging.replace(tmp / f"part{index:06d}")
            tmp.touch()
    finally:
        try:
            if staging is not None:
                staging.unlink(missing_ok=True)
        finally:
            with _MERGE_LOCK:
                active = _UPLOAD_ACTIVE[key] - 1
                if active:
                    _UPLOAD_ACTIVE[key] = active
                else:
                    _UPLOAD_ACTIVE.pop(key, None)


def _upload_received_indexes(kind, name, storage):
    """返回某上传目标已落盘的分片索引列表（升序），供前端断点续传判断可跳过的分片"""
    tmp = _upload_tmp_dir(kind, name, storage)
    if not tmp or not tmp.is_dir():
        return []
    meta = _read_upload_meta(tmp)
    if not meta:
        return []
    out = []
    for p in tmp.glob("part*"):
        m = re.fullmatch(r"part(\d+)", p.name)
        if m and p.is_file():
            index = int(m.group(1))
            if 0 <= index < meta["count"] and p.name == f"part{index:06d}":
                out.append(index)
    return sorted(out)


def _fmt_size(n):
    """字节数转人类可读（GB/MB/KB），用于上限提示"""
    for unit in ("GB", "MB", "KB"):
        step = 1024 ** {"GB": 3, "MB": 2, "KB": 1}[unit]
        if n >= step:
            return "%.0f%s" % (n / step, unit)
    return "%dB" % n


def _upload_size_error(kind, raw_size):
    """分片上传的总大小校验。raw_size 是前端带的总字节数（老客户端不带该参数时跳过，
    退回合并阶段的兜底检查）。超限时返回提示文案，否则返回 None。"""
    try:
        size = int(str(raw_size or "0"))
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    limit = _UPLOAD_SIZE_LIMITS.get(kind)
    if limit and size > limit:
        return "文件超过 %s 上限（当前 %s）" % (_fmt_size(limit), _fmt_size(size))
    return None


def _merge_key(kind, name, storage=""):
    return "%s|%s|%s" % (kind, storage or "", name)


def _chunks_complete(kind, name, storage, count):
    """分片是否已全部落盘（后台合并的触发条件）"""
    tmp = _upload_tmp_dir(kind, name, storage)
    if not tmp or not tmp.is_dir():
        return False
    try:
        meta = _read_upload_meta(tmp)
        return bool(meta and meta["count"] == count and _completed_upload_parts(tmp, count))
    except OSError:
        return False


def _do_merge(kind, name, storage, count):
    """按上传类型分派到对应的分片合并函数（后台合并与启动兜底共用）。
    disk/cloud 上传的是单层压缩包时，合并后顺带解压出镜像并删掉压缩源 —— 与下载链路同一套行为；
    这里按文件名后缀自动判断，启动兜底补齐合并时也走同一条路。"""
    if kind == "backup":
        return _merge_backup_chunks(storage, name, count)
    if kind == "vztmpl":
        return _merge_vztmpl_chunks(storage, name, count)
    if kind == "vbios":
        return _merge_vbios_chunks(name, count)
    dest = _disk_image_dir() if kind == "disk" else _cloud_image_dir()
    ok = _merge_disk_chunks(name, count, dest, kind=kind)
    comp_ext = next((e for e in _CLOUD_COMP_EXTS if name.lower().endswith(e)), None)
    if ok and comp_ext:
        target = Path(dest) / name
        final = target.with_name(name[: -len(comp_ext)])
        try:
            _decompress_file(str(target), str(final))
            target.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            # 解压失败：清掉半个产物、保留压缩源便于排查
            final.unlink(missing_ok=True)
            raise backend.PveError("镜像解压失败，请确认文件为有效的单层压缩磁盘镜像")
    return ok


def _maybe_merge_async(kind, name, storage, count):
    """分片收齐时把合并丢到后台线程并返回 True；未收齐返回 False。
    已在合并中的重复请求也返回 True（让前端的轮询照常进行）。状态写 _MERGE_STATE 供
    /api/upload/status 查询；进程重启会丢状态，此时由 _resume_incomplete_uploads 补齐。"""
    key = _merge_key(kind, name, storage)
    with _MERGE_LOCK:
        cur = _MERGE_STATE.get(key)
        if cur and cur.get("state") == "merging":
            return True
        if _UPLOAD_ACTIVE.get(key):
            return False
        if not _chunks_complete(kind, name, storage, count):
            return False
        _MERGE_STATE[key] = {"state": "merging"}

    def _run():
        try:
            ok = _do_merge(kind, name, storage, count)
            state = {"state": "done"} if ok else {"state": "error", "msg": "合并未完成"}
        except Exception as e:  # noqa: BLE001
            state = {"state": "error", "msg": str(e)}
        with _MERGE_LOCK:
            _MERGE_STATE[key] = state

    threading.Thread(target=_run, daemon=True, name="merge-%s" % kind).start()
    return True


def _merge_state(kind, name, storage=""):
    """后台合并状态：{"state": "merging"|"done"|"error", "msg": str}；空 dict 表示未触发后台合并"""
    with _MERGE_LOCK:
        st = _MERGE_STATE.get(_merge_key(kind, name, storage))
        return dict(st) if st else {}


def _upload_target_path(kind, name, storage):
    """上传完成后的最终文件路径（秒传登记与校验用）。压缩包已解压时返回解压后的镜像路径。

    防路径穿越：name 会拼进各上传目录（含 /usr/share/kvm 等系统目录）做 is_file/stat 探测，
    必须白名单校验（不含 / \\ 等路径分隔符），否则能构造 ../../etc/shadow 探测任意文件。
    """
    if not _upload_name_ok(kind, name):
        return None
    if kind == "backup":
        d = _backup_dirs_merged().get(storage)
        return (d / name) if d else None
    if kind == "vztmpl":
        try:
            return lxc_template_dest(storage) / name
        except Exception:  # noqa: BLE001
            return None
    if kind in ("disk", "cloud"):
        dest = Path(_disk_image_dir() if kind == "disk" else _cloud_image_dir())
        comp = next((e for e in _CLOUD_COMP_EXTS if name.lower().endswith(e)), None)
        return dest / (name[: -len(comp)] if comp else name)
    if kind == "vbios":
        return VBIOS_TARGET_DIR / name
    return None


def _blocked_cloud_url(url):
    """SSRF 防护：返回 True 表示该下载 URL 应被拒绝。

    云镜像下载接受用户（含自定义预设源）提供的 URL，若不加限制，宿主机会被诱导去
    GET 任意地址——127.0.0.1 探测本机服务、169.254.169.254 读取云元数据等。
    只拦截「明确指向宿主自身 / 链路本地 / 未指定 / 保留地址」的目标；私网
    （192.168 / 10 / 172.16）放行，因为内网镜像站是合法场景。
    """
    import ipaddress
    try:
        host = urllib.parse.urlparse(url).hostname
    except ValueError:
        return True
    if not host:
        return True
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return (ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or ip.is_reserved or ip.is_multicast)
    return False


def _upload_index_read():
    try:
        d = json.loads(_UPLOAD_INDEX_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _upload_index_write(d):
    """原子写清单（失败静默：秒传只是加速手段，不影响正常上传）"""
    try:
        _UPLOAD_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _UPLOAD_INDEX_PATH.with_name(_UPLOAD_INDEX_PATH.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        tmp.replace(_UPLOAD_INDEX_PATH)
    except OSError:
        pass


def upload_instant_hit(kind, name, storage, quick_hash, size):
    """秒传判定：清单有同 key 且指纹/大小一致，并且目标文件确实存在、大小对得上。
    返回 (是否命中, 命中的路径或未命中原因)。"""
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return False, "大小参数无效"
    if not quick_hash or size <= 0:
        return False, "缺少指纹"
    with _upload_index_lock:
        rec = _upload_index_read().get(_merge_key(kind, name, storage))
    if not rec or rec.get("hash") != quick_hash or int(rec.get("size") or 0) != size:
        return False, "无匹配指纹"
    p = Path(rec.get("path") or "")
    try:
        # 用登记时记下的「目标文件大小」比对：上传 .xz/.gz/.zst 压缩包时目标会是解压后的镜像，
        # 两者大小不同，不能拿前端报的上传大小去量目标文件
        if not p.is_file() or p.stat().st_size != int(rec.get("target_size") or 0):
            return False, "目标文件不存在或大小不符"
    except OSError:
        return False, "目标文件不可读"
    return True, str(p)


def upload_instant_register(kind, name, storage, quick_hash, size):
    """登记指纹：仅在目标文件确实存在且大小一致时记录（避免把不存在的目标写进清单）。"""
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return False
    if not quick_hash or size <= 0:
        return False
    p = _upload_target_path(kind, name, storage)
    try:
        if not p or not p.is_file():
            return False
        target_size = p.stat().st_size
    except OSError:
        return False
    with _upload_index_lock:
        d = _upload_index_read()
        d[_merge_key(kind, name, storage)] = {
            "hash": quick_hash, "size": size, "path": str(p),
            "target_size": target_size, "ts": int(time.time())}
        _upload_index_write(d)
    return True


def _allowed_backup_ext(name):
    """备份上传扩展名校验：文件名必须以白名单后缀结尾（大小写不敏感）"""
    return any(name.lower().endswith(ext) for ext in _BACKUP_EXT_WHITELIST)


def _allowed_vbios_ext(name):
    """vBIOS 上传扩展名校验：文件名必须以白名单后缀结尾（大小写不敏感）"""
    return any(name.lower().endswith(ext) for ext in _VBIOS_EXT_WHITELIST)


def _allowed_vztmpl_ext(name):
    """LXC(CT) 容器模板上传校验：文件名须符合 PVE 模板命名（.tar.gz/.tar.xz/.tar.zst），
    与「获取 LXC 模板」下载同口径，落盘后 PVE 可直接识别。"""
    return bool(LXC_TEMPLATE_NAME_RE.match(name))


def _backup_template_dir():
    """虚机模板（传输中心上传的 .vma）存放目录：pvewebtools 共享存储根下的 dump/ 子目录。
    dump/ 即 PVE backup 内容类型的默认落盘目录（/var/lib/vz/pvewebtools/dump），
    PVE 可直接识别；通过 storage_path("pvewebtools") 取根，回退本机默认路径。"""
    root = backend.storage_path("pvewebtools")
    if root:
        return root / "dump"
    return Path(storage_provision.DEFAULT_DIR_PATH) / "dump"


def _backup_dirs_merged():
    """真实备份存储（PVE backup 内容的 dump 目录）+ 合成 pvewebtools（虚机模板目录 dump/）。

    pvewebtools 的虚机模板直接落在其 backup 内容目录 dump/（与 PVE 自身备份目录一致）；
    这里把它合成为虚拟备份位置（以 storage="pvewebtools" 命中该目录），使「上传虚机模板 /
    备份文件卡片 / 还原 / 模板快速建机」等逻辑统一走 pvewebtools，不再单独造一个存储项。"""
    dirs = dict(backend.backup_dirs())
    try:
        dirs["pvewebtools"] = _backup_template_dir()
    except Exception:  # noqa: BLE001
        pass
    return dirs


def _list_template_backup_files():
    """虚机模板（dump/ 子目录）里的文件列表，格式同 pve_api.list_backup_files。

    因 pvewebtools 已声明 backup 内容类型，dump/ 下的文件已被 pve_api.list_backup_files()
    扫描；本函数保留作为兼容入口，返回空列表，避免 /backup/files 重复列出。"""
    return []


def _merge_backup_chunks(storage, name, count):
    """备份分片收齐后合并为最终文件；全局锁防并发重复合并。返回是否完成"""
    tmp = BACKUP_TMP_DIR / f"{storage}__{name}"
    with _backup_lock:
        if not tmp.is_dir():
            return False
        parts = _completed_upload_parts(tmp, count)
        if not parts:
            return False
        dirs = _backup_dirs_merged()
        d = dirs.get(storage)
        if d is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise backend.PveError("存储不可用")
        target = d / name
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".part")
        try:
            with open(staging, "wb") as out:
                for p in parts:
                    with open(p, "rb") as pf:
                        shutil.copyfileobj(pf, out, 1024 * 1024)
            _check_upload_merged_size(tmp, staging)
            if staging.stat().st_size > MAX_BACKUP_SIZE:
                raise backend.PveError("备份文件超过 256GB 上限")
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
        return True


def _merge_vbios_chunks(name, count):
    """vBIOS 分片收齐后合并：主副本落到共享存储 / NFS（全集群同路径可见），
    本机 /usr/share/kvm/ 仅建一个指向该主副本的软链（PVE romfile 硬约束只认本机该目录、
    且只接受相对文件名 => 文件必须出现在 /usr/share/kvm 下）。单节点（无共享存储）退化为直接落本机文件。
    全局锁防并发重复合并。返回是否完成。"""
    tmp = VBIOS_TMP_DIR / name
    with _vbios_lock:
        if not tmp.is_dir():
            return False
        parts = _completed_upload_parts(tmp, count)
        if not parts:
            return False
        # 文件名严格白名单校验（防路径穿越，vBIOS 落到系统目录 /usr/share/kvm/）
        if not _upload_name_ok("vbios", name):
            shutil.rmtree(tmp, ignore_errors=True)
            raise backend.PveError("非法文件名")
        # 先合并到临时文件做大小校验
        staging = VBIOS_TMP_DIR / (name + ".merged")
        staging.unlink(missing_ok=True)
        try:
            with open(staging, "wb") as out:
                for p in parts:
                    with open(p, "rb") as pf:
                        shutil.copyfileobj(pf, out, 1024 * 1024)
            _check_upload_merged_size(tmp, staging)
            if staging.stat().st_size > MAX_VBIOS_SIZE:
                raise backend.PveError("vBIOS 文件超过 16MB 上限")
            # 落盘前先做防覆盖预检：目标被「非本工具管理」的真实文件占用（疑似 QEMU 系统固件）
            # 时立即拒绝，避免落一半才发现冲突，留下主副本/软链不一致的状态。
            if _vbios_target_blocked(name):
                raise backend.PveError("目标文件已存在且不是本工具上传的，可能是系统固件，拒绝覆盖")
            shared = _vbios_shared_dir()
            if shared:
                # 1) 主副本落 NFS 共享（全集群同一绝对路径可见）
                shared.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staging, shared / name)
                # 2) 本机 /usr/share/kvm 建软链指向 NFS 主副本（预检已过，失败则回滚主副本）
                if not _link_vbios(name, shared / name):
                    (shared / name).unlink(missing_ok=True)
                    raise backend.PveError("创建软链失败（目标被目录或其它文件占用）")
            else:
                # 单节点退化：直接落本机 /usr/share/kvm 真实文件
                target = VBIOS_TARGET_DIR / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staging, target)
        finally:
            staging.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
        # 3) 写入共享清单
        _add_vbios_manifest(name)
        return True


def _merge_vztmpl_chunks(storage, name, count):
    """LXC(CT) 模板分片收齐后合并为最终模板，落到所选存储的 template/cache/（PVE 约定目录，
    写进去即刻可被 pveam list / 建 CT 下拉识别）。全局锁防并发重复合并。返回是否完成。"""
    tmp = VZTMPL_TMP_DIR / f"{storage}__{name}"
    with _vztmpl_lock:
        if not tmp.is_dir():
            return False
        parts = _completed_upload_parts(tmp, count)
        if not parts:
            return False
        # 模板名严格白名单校验（防路径穿越与任意后缀落盘）
        if not _allowed_vztmpl_ext(name):
            shutil.rmtree(tmp, ignore_errors=True)
            raise backend.PveError("非法模板名")
        try:
            dest = lxc_template_dest(storage)
        except backend.PveError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
        staging = target.with_name(target.name + ".part")
        try:
            with open(staging, "wb") as out:
                for p in parts:
                    with open(p, "rb") as pf:
                        shutil.copyfileobj(pf, out, 1024 * 1024)
            _check_upload_merged_size(tmp, staging)
            if staging.stat().st_size > MAX_VZTMPL_SIZE:
                raise backend.PveError("模板文件超过 64GB 上限")
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
        return True


def _merged_preset_config():
    """合并换源/预设配置：随包资源目录的 cloud-image-presets.json 为基，持久覆盖目录的同名文件逐键叠加。

    覆盖目录见 core.paths.preset_override_dir()：二进制形态下指向安装目录（如 /opt/pvewebtools/config，持久），
    源码形态下即资源目录（core/config）。返回 {presets:[...], mirror_repo_support:{...}}；
    任何一层缺失/损坏都回退内置默认，不致命。
    """
    base = {"presets": list(DEFAULT_CLOUD_IMAGE_PRESETS),
            "mirror_repo_support": dict(DEFAULT_MIRROR_REPO_SUPPORT)}
    candidates = [paths.RESOURCE_DIR / "config" / "cloud-image-presets.json",
                  paths.preset_override_dir() / "cloud-image-presets.json"]
    for p in candidates:
        try:
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    if isinstance(d.get("presets"), list):
                        base["presets"] = d["presets"]
                    if isinstance(d.get("mirror_repo_support"), dict):
                        base["mirror_repo_support"] = d["mirror_repo_support"]
                elif isinstance(d, list):  # 兼容旧版纯数组文件（仅含预设源）
                    base["presets"] = d
        except (OSError, json.JSONDecodeError):
            pass
    return base


def load_cloud_image_presets():
    """读取可编辑的云镜像预设源（合并覆盖目录后的结果）。"""
    return _merged_preset_config()["presets"]


def save_cloud_image_presets(presets):
    """校验并持久化云镜像预设源（运维网页维护，无需手改 JSON）。

    返回 (ok, msg)；校验失败不改文件。写入持久覆盖目录（core.paths.preset_override_dir()），
    原子写（临时文件 + os.replace）并备份旧文件；整体写回对象结构，保留当前生效的 mirror_repo_support。
    """
    if not isinstance(presets, list):
        return False, "预设源必须是数组"
    cleaned = []
    seen = set()
    for i, p in enumerate(presets):
        if not isinstance(p, dict):
            return False, f"第 {i + 1} 项格式错误（应为对象）"
        label = str(p.get("label", "")).strip()
        url = str(p.get("url", "")).strip()
        if not label:
            return False, f"第 {i + 1} 项缺少名称（label）"
        if not re.match(r"^https?://", url, re.I):
            return False, f"第 {i + 1} 项地址非法（需 http/https）：{url}"
        if url in seen:
            return False, f"存在重复地址：{url}"
        seen.add(url)
        cleaned.append({"label": label, "url": url})
    try:
        out_dir = paths.preset_override_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "cloud-image-presets.json"
        if out_path.is_file():
            shutil.copy2(out_path, out_path.with_suffix(".json.bak"))
        # 保留当前生效的 mirror_repo_support（合并基），整体写回覆盖文件
        support = _merged_preset_config()["mirror_repo_support"]
        cfg = {"presets": cleaned, "mirror_repo_support": support}
        tmp = out_dir / "cloud-image-presets.tmp"
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, out_path)
    except OSError as e:
        return False, f"写入失败: {e}"
    return True, f"已保存 {len(cleaned)} 条"


def reset_cloud_image_presets():
    """一键恢复内置默认预设源（覆盖当前自定义配置，旧文件自动备份）。

    整体写回持久覆盖目录（core.paths.preset_override_dir()），含默认 presets 与默认 mirror_repo_support。
    """
    try:
        out_dir = paths.preset_override_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "cloud-image-presets.json"
        if out_path.is_file():
            shutil.copy2(out_path, out_path.with_suffix(".json.bak"))
        tmp = out_dir / "cloud-image-presets.tmp"
        tmp.write_text(json.dumps(DEFAULT_CLOUD_IMAGE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, out_path)
    except OSError as e:
        return False, f"写入失败: {e}"
    return True, "已恢复内置默认配置"


def _http_alive(url, timeout=15, allow_get=True):
    """HEAD 探活 URL（被拒 403/405 时回退 GET Range 0-0），返回 (ok, status)。

    「云镜像预设源校验」与「云镜像制作·软件源预检」共用，避免两处各写一套探活。"""
    for method in ("HEAD", "GET") if allow_get else ("HEAD",):
        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("User-Agent", "pvewebtools")
            if method == "GET":
                req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                return 200 <= code < 400, code
        except urllib.error.HTTPError as e:
            if e.code in (403, 405) and method == "HEAD":
                continue
            return False, e.code
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:120]
    return False, ""


def probe_cloud_image_presets(presets):
    """对每条预设源做 HEAD（失败回退 GET Range）探活，返回 [{label,url,ok,status}]。"""
    results = []
    for p in presets:
        url = str(p.get("url", ""))
        ok, status = _http_alive(url)
        results.append({"label": p.get("label", ""), "url": url, "ok": ok, "status": status})
    return results


def _node_cpu_topology(node):
    """从 PVE node status 取 CPU 核数/线程数与 PVE 版本，带缓存；失败返回 {}。

    被 _node_hw_info（概览）与 collect_nodes 的快字段 enrich（节点列表）共用，
    避免两处重复 get_node_status + cpuinfo 解析。"""
    def load():
        out = {}
        try:
            st = backend.get_node_status(node)
            cpu = st.get("cpuinfo", {})
            if isinstance(cpu.get("cores"), int) and cpu["cores"] > 0:
                out["cpu_cores"] = cpu["cores"]
            if isinstance(cpu.get("cpus"), int) and cpu["cpus"] > 0:
                out["cpu_threads"] = cpu["cpus"]
            pv = (st.get("pveversion") or "").split("/")
            out["pveversion"] = pv[1] if len(pv) > 1 else (st.get("pveversion") or "")
        except Exception:
            pass
        return out
    return _cpu_topo_cache.get(node, load)


def _node_hw_info(node):
    """提取节点的 CPU 型号/主频/PVE 版本/内核，带 TTL 缓存，失败降级为空"""
    def load():
        info = {}
        try:
            st = backend.get_node_status(node)
            cpu = st.get("cpuinfo", {})
            info["cpu_model"] = cpu.get("model", "")
            info["cpu_mhz"] = cpu.get("mhz", "")
            info["cpu_sockets"] = cpu.get("sockets", 1)
            info.update(_node_cpu_topology(node))
            kv = (st.get("kversion") or "").split()
            info["kversion"] = kv[1] if len(kv) > 1 else (st.get("kversion") or "")
            la = st.get("loadavg")
            if isinstance(la, list) and len(la) == 3:
                info["loadavg"] = [round(float(x), 2) for x in la]
        except Exception:
            pass
        # 检查是否在虚拟机中运行（VMware/QEMU 等），虚机内 dmidecode 返回的
        # CPU 插槽/内存插槽信息是虚拟化层伪造的，没有实际意义，跳过采集
        is_vm = False
        try:
            bh = node_board_hw(node)
            is_vm = bh.get("virt", "none") != "none"
        except Exception:
            pass
        if not is_vm:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_mem = pool.submit(lambda: backend.get_memory_hw(node))
                fut_cpu = pool.submit(lambda: backend.get_cpu_sockets(node))
                try:
                    info.update(fut_mem.result())
                except Exception:
                    pass
                try:
                    info.update(fut_cpu.result())
                except Exception:
                    pass
        return info
    return _hw_cache.get(node, load)


def node_board_hw(node):
    """主板/芯片组型号"""
    def load():
        try:
            hw = backend.get_board_hw(node)
        except Exception:
            hw = {}
        return {
            "board_vendor": hw.get("board_vendor", ""),
            "board_product": hw.get("board_product", ""),
            "chipset": hw.get("chipset", ""),
            "has_ipmi": bool(hw.get("has_ipmi")),
            "has_pwm": bool(hw.get("has_pwm")),
            "virt": hw.get("virt", "none"),
        }
    return _board_hw_cache.get(node, load)


def _node_disks_lost(node, phys_disks):
    """返回当前处于掉盘告警状态的盘名列表；枚举失败（空结果）时维持上轮告警不误报不丢失"""
    names = {d.get("name") for d in phys_disks if d.get("name")} if phys_disks else set()
    now = time.time()
    lost = {k: ts for k, ts in _disks_lost.get(node, {}).items() if now - ts < DISKS_LOST_TTL}
    if names:
        prev = _disks_baseline.get(node, set())
        for name in prev - names:
            lost.setdefault(name, now)   # 消失：记录首次时间，持续告警直到回归或超时
        for name in names:
            lost.pop(name, None)          # 回归：解除告警
        _disks_baseline[node] = names
    _disks_lost[node] = lost
    return sorted(lost)


def _node_online(n):
    """节点当前是否在线：pvesh 的 /nodes 会带 status 字段（online / offline）。

    字段缺失时按"在线"处理 —— 宁可多采一次，也不要因为字段缺失而漏掉正常节点的采集。
    离线节点一律跳过远程采集（见 collect_nodes.enrich），这是离线场景下最主要的提速点。"""
    st = str(n.get("status") or "").strip().lower()
    return st in ("", "online")


def _merge_cluster_nodes(nodes):
    """用集群成员表补齐 pvesh 缺失的节点，避免节点管理页在集群降级时整页空白。

    背景：`pvesh get /nodes` 依赖 pmxcfs 的成员视图，集群失去仲裁 / 刚降级恢复时该视图可能
    不同步——轻则缺少离线节点，重则整表为空（现象就是节点管理页"暂无节点数据"，离线节点
    连「临时取得仲裁」按钮都点不到）。corosync.conf 的 nodelist 与 corosync 的成员信息不受影响，
    故用它们兜底补齐：补出来的条目 `placeholder=True`，前端据此只显示节点名与状态、不显示
    无意义的 0 值与操作按钮。

    正常情况（pvesh 已给全）直接返回，不做任何额外探测（quorum_status 要起子进程）。"""
    try:
        members = backend.cluster_members()
    except Exception:  # noqa: BLE001
        return nodes
    if not members or (nodes and len(nodes) >= len(members)):
        return nodes
    try:
        st = backend.quorum_status()
    except Exception:  # noqa: BLE001
        return nodes
    online = set(st.get("online") or [])
    try:
        ips = backend.cluster_node_ips()
    except Exception:  # noqa: BLE001
        ips = {}
    local = socket.gethostname().split(".")[0]
    known = {str(n.get("node")) for n in nodes}
    # 本机优先补在第一项：前端 localNodeName() 取列表首项的节点名当"本机"
    order = ([local] if local in members else []) + [m for m in members if m != local]
    for name in order:
        if name in known:
            continue
        nodes.append({
            "node": name,
            "status": "online" if name in online else "offline",
            "ip": str(ips.get(name) or ""),
            "cpu": 0, "maxcpu": 0, "mem": 0, "maxmem": 0,
            "disk": 0, "maxdisk": 0, "uptime": 0, "pveversion": "",
            "storage_role": "dir",   # 占位条目不提供「修复 NFS」按钮（状态未知，操作必然失败）
            "placeholder": True,
        })
    return nodes


def _trigger_nodes_refresh():
    """后台全量重采节点概览（含 dmidecode/smartctl/sensors 慢硬件），立即返回不阻塞请求。

    手动刷新（``?fresh=1``）以前是同步重采：慢命令经 SSH 打各节点，HTTP 线程会挂住
    数秒到十几秒，浏览器只能转圈。改为「先回旧缓存 + 后台补新」，新数据由前端下一轮
    轮询取走。同一时刻只跑一个重采任务，连续点刷新不会叠加 SSH 探测。"""
    global _nodes_refreshing
    with _nodes_cache_lock:
        if _nodes_refreshing or _nodes_preheating:
            return False
        _nodes_refreshing = True

    def _run():
        global _nodes_refreshing
        try:
            data = collect_nodes(fast_only=False)
            with _nodes_cache_lock:
                _nodes_cache["data"], _nodes_cache["ts"] = data, time.time()
        except Exception:  # noqa: BLE001
            pass           # 重采失败保留旧缓存，前端继续显示上一轮数据
        finally:
            with _nodes_cache_lock:
                _nodes_refreshing = False

    threading.Thread(target=_run, name="nodes-refresh", daemon=True).start()
    return True


def collect_nodes_cached(force=False):
    """节点概览读取：后台线程周期性预热缓存，前台读取永远秒回。

    force=True（手动刷新）不再同步等慢采集：有缓存就立即返回，同时触发后台全量重采
    （见 ``_trigger_nodes_refresh``）。缓存为空（冷启动首屏）时仍同步采一次快数据兜底，
    避免空白首屏；慢硬件随后由后台补齐。"""
    global _nodes_preheating
    with _nodes_cache_lock:
        cached = _nodes_cache["data"]
        if cached is None:
            # 首次请求且缓存为空：同步采集一次兜底（避免冷启动首屏空白）
            if _nodes_preheating and not force:
                return _nodes_cache["data"] or []
            _nodes_preheating = True
    # 命中缓存：直接回，force 只负责把重采排到后台（必须在锁外启动，锁不可重入）
    if cached is not None:
        if force:
            _trigger_nodes_refresh()
        return cached
    try:
        # 冷启动首屏只采快数据（秒回），慢硬件交给后台
        data = collect_nodes(fast_only=True)
    except BaseException:
        # 采集失败必须复位预热标记再抛出：否则后续请求会一直命中「预热中」分支、
        # 返回空列表，节点管理页就此永久空白。原先这里是 finally 里直接写
        # _nodes_cache[...] = data —— data 未赋值时先抛 UnboundLocalError，既把
        # PveError 这类可预期异常伪装成「未预期」，又让复位那行永远执行不到。
        with _nodes_cache_lock:
            _nodes_preheating = False
        raise
    with _nodes_cache_lock:
        _nodes_cache["data"], _nodes_cache["ts"] = data, time.time()
        _nodes_preheating = False
    _trigger_nodes_refresh()    # 冷启动后立刻补慢硬件（后台，不阻塞首屏）
    return data


def collect_nodes(fast_only=False):
    """节点列表并入各自的存储用量与硬件信息，供概览卡片展示；节点/存储列表并行拉取。
    fast_only=True 时只采集快字段（PVE API 存储/网卡/物理盘/CPU 频率），
    跳过慢硬件（dmidecode 内存/CPU 插槽、主板、温度 smartctl），供首屏秒开；
    慢字段由 _enrich_nodes_slow 后台补全。"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_nodes = pool.submit(backend.list_nodes)
        fut_storages = pool.submit(backend.list_storages)
        nodes = fut_nodes.result()
        try:
            storages = fut_storages.result()
        except Exception:
            storages = []  # 存储信息拉不到不影响节点展示，降级为无磁盘
    by_node = {}
    for s in storages:
        by_node.setdefault(s.get("node"), []).append({
            "storage": s.get("storage"),
            "type": s.get("plugintype", ""),
            "disk": s.get("disk", 0),
            "maxdisk": s.get("maxdisk", 0),
        })

    # 集群节点 IP（/cluster/status 的 ring 地址）：走 pvesh，不依赖 SSH、也不需要节点名可解析。
    # 作为「SSH 采网卡 → 管理口」拿不到时的兜底（节点名解析不了 / SSH 不通时不再显示「–」）。
    try:
        node_ips = backend.cluster_node_ips()
    except Exception:  # noqa: BLE001
        node_ips = {}

    def enrich(n):
        """单节点补全：存储/网络/物理盘/CPU 频率（快），各自降级互不影响"""
        disks = by_node.get(n.get("node"), [])
        # 大盘在前，折叠时优先露出主力存储
        disks.sort(key=lambda d: d["maxdisk"], reverse=True)
        n["disks"] = disks
        # 离线节点直接跳过全部远程采集：这些探测都走 SSH（ConnectTimeout=5），单节点串行叠加
        # 可达 20~30 秒，而采集周期只有 NODES_REFRESH_INTERVAL(30) 秒 —— 会让缓存永远追不上
        # （页面长期显示旧数据），手动刷新（?fresh=1）还会同步卡住整个请求。数据本来就取不到，
        # 给空值即可；掉盘告警用空结果调用以维持既有状态，避免把"取不到"误判成"盘没了"。
        if not _node_online(n):
            n["nics"], n["gpus"], n["phys_disks"], n["cpu_freq"] = [], [], [], {}
            n["disks_lost"] = _node_disks_lost(n.get("node"), [])
            n["remote_skipped"] = True
            if not n.get("ip"):
                n["ip"] = node_ips.get(str(n.get("node")), "")
            return
        try:
            n["nics"] = backend.get_node_nics(n.get("node"), n.get("ip"))
        except Exception:
            n["nics"] = []  # 网卡链路探测失败时前端降级提示
        # 节点 IP 复用概览「管理口」逻辑：优先取被标记为 mgmt 的网卡 IP；
        # 与概览网卡列表高亮的管理口完全一致（_mark_mgmt_nic 按管理 IP 或默认路由出口标记）。
        # /nodes 列表偶发带 ip 字段时也兼容（n.get("ip") 已优先）。
        if not n.get("ip"):
            for nic in n.get("nics", []):
                if nic.get("mgmt") and nic.get("ips"):
                    n["ip"] = str(nic["ips"][0]).split("/")[0]
                    break
        # 仍取不到（SSH 不通 / 节点名解析不了）时回退 /cluster/status 的 ring 地址，避免显示「–」
        if not n.get("ip"):
            n["ip"] = node_ips.get(str(n.get("node")), "")
        try:
            n["gpus"] = backend.get_node_gpus(n.get("node"))
        except Exception:
            n["gpus"] = []  # 显卡拉不到时前端不展示
        try:
            n["phys_disks"] = backend.get_node_disks(n.get("node"))
        except Exception:
            n["phys_disks"] = []  # 物理盘拉不到时前端降级显示 PVE 存储
        # 掉盘告警：与上轮枚举对比，消失的盘红色警示
        n["disks_lost"] = _node_disks_lost(n.get("node"), n["phys_disks"])
        # CPU 核数/线程数：复用 _node_cpu_topology（与概览 _node_hw_info 同源），归属首屏快字段
        try:
            n.update(_node_cpu_topology(n.get("node")))
        except Exception:
            pass
        try:
            n["cpu_freq"] = backend.get_node_cpu_freq(n.get("node"))
        except Exception:
            n["cpu_freq"] = {}  # 频率取不到时不展示，不影响其他块
        if not fast_only:
            _enrich_node_slow(n)

    # 多节点并行采集，避免串行叠加各节点的外部命令耗时
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        list(pool.map(enrich, nodes))
    return nodes


def _enrich_node_slow(n):
    """补单个节点的慢硬件数据：CPU 型号/内存条/CPU 插槽（dmidecode）、主板（dmidecode）、
    温度（smartctl+sensors）。各自降级互不影响，字段为空时前端自然不展示。"""
    # 离线节点：慢字段同样全靠 SSH（dmidecode / smartctl / sensors），跳过以免每轮再叠加
    # 十几秒的连接超时（该函数由后台刷新线程直接调用，不经过 collect_nodes.enrich 的跳过逻辑）
    if not _node_online(n):
        return
    try:
        n.update(_node_hw_info(n.get("node")))
    except Exception:
        pass
    # 主板/芯片组型号：复用工具弹窗的探测缓存（600 秒），探测失败不影响卡片其余内容
    try:
        bh = node_board_hw(n.get("node"))
        n["board_vendor"] = bh.get("board_vendor", "")
        n["board_product"] = bh.get("board_product", "")
        n["chipset"] = bh.get("chipset", "")
    except Exception:
        pass
    try:
        n["temps"] = backend.get_node_temperatures(n.get("node"))
    except Exception:
        n["temps"] = {}  # 温度依赖缺失时不展示，不影响其他块



def list_byid_disks(node=None):
    """列出可用于直通的整块磁盘 by-id 名（排除分区/设备映射），供前端下拉选择；指定 node 时从目标节点采集。
    标注 host_used：PVE 系统盘与 PVE 真正在用的存储盘，前端置灰 + 脚本拦截。

    注意：不能用「盘上有 LVM/ZFS 元数据」来判断——其他系统（如飞牛）的存储池在 PVE 眼里
    同样是 LVM/ZFS 成员，但并未被 PVE 使用，不该锁死不给直通。"""
    if node:
        return _run_func_on_node(node, "list_byid_disks")
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


def _node_from_query(path):
    """从请求路径的 ?node= 解析并校验集群节点名。

    返回 None 表示未指定（用本机）；返回节点名表示合法外节点；
    返回 False 表示节点名非法（调用方应返回 404）。供节点级只读接口
    （网络/磁盘/备份下拉）复用，与 /api/pci/* 的节点校验保持一致。
    """
    q = parse_qs(urlparse(path).query)
    node = (q.get("node") or [""])[0]
    if not node:
        return None
    known = backend.known_node_names()
    return node if node in known else False


def _node_backup_files(node=None):
    """备份文件列表（还原/模板建机下拉数据源）。指定 node 时从目标节点采集本地备份目录。"""
    if node:
        return _run_func_on_node(node, "_node_backup_files")
    files = list(backend.list_backup_files())
    for f in files:
        if f.get("storage") == "pvewebtools":
            f["template"] = True
    files += _list_template_backup_files()
    return files


def _template_cache_dispatch(node, func_name, args=()):
    """转发到 core.web.template_cache.template_cache_dispatch（延迟导入）。

    为什么延迟导入：缓存实现要用本模块的 _backup_template_dir，本模块又要用缓存的
    dispatch——顶层互相 import 会成环。入口层延迟导入实现层是这里的取舍（同
    ``toolbox_common`` 那处）。
    """
    from core.web import template_cache

    return template_cache.template_cache_dispatch(node, func_name, args)


def _template_cache_nodes():
    """转发到 core.web.template_cache.template_cache_nodes（延迟导入，理由同上）。"""
    from core.web import template_cache

    return template_cache.template_cache_nodes()


def _template_warmup_script(src):
    """生成「预热模板缓存」脚本：在目标节点上确保该模板已缓存到本机盘（幂等）。

    与建机/云镜像/还原共用同一个 pw_cache_use —— 同一套缓存逻辑（pw_cache_ensure）、
    同一落点、同一提示口径，连「只在缓存不可用时才做链路诊断」也一致，
    因此预热过的模板在真正建机时必定命中，不会重复拉取。

    与建机路径唯一的差别是失败语义：预热的目的就是「确保有缓存」，拿不到本机缓存
    （本机磁盘空间不足 / 源不可读）即判任务失败；建机则会退为直读远端继续建机。
    pw_cache_use 的返回码恒为 0，成败按它 stdout 第 1 行的状态（local/hit/fetched）判定。
    """
    return (
        "SRC=" + shlex.quote(src) + "\n"
        '[ -f "$SRC" ] || { echo "[错误] 模板文件不存在: $SRC"; exit 1; }\n'
        'echo "===== 预热模板缓存 ====="\n'
        'echo "模板: $SRC"\n'
        '_res=$(pw_cache_use "$SRC" 模板)\n'
        "_st=$(printf '%s\\n' \"$_res\" | sed -n 1p)\n"
        "_cf=$(printf '%s\\n' \"$_res\" | sed -n 2p)\n"
        'case "$_st" in\n'
        '  local|hit|fetched) : ;;\n'
        '  *) echo "[错误] 预热失败：模板未缓存到本机（本机磁盘空间不足或源不可读）"; exit 1 ;;\n'
        'esac\n'
        'echo "[完成] 预热完成，本节点后续建机/还原将直读本地：$_cf"\n'
        'true\n'
    )


def _remove_vbios_manifest(name):
    """从共享清单移除文件名（删除 ROM 时同步）"""
    try:
        names = _read_vbios_manifest()
        if name in names:
            names.remove(name)
            _vbios_manifest_path().write_text("\n".join(names) + "\n", encoding="utf-8")
    except OSError:
        pass


def list_vbios_roms():
    """本工具已上传的 vBIOS ROM 列表（/usr/share/kvm/），供显卡直通-romfile 下拉数据源。

    只返回清单里记录且文件仍存在的 ROM——/usr/share/kvm/ 下 QEMU 系统固件（vgabios-*.bin、
    efi-*.rom、seabios.bin 等）一律不会出现在下拉里，用户只看到自己上传的文件。
    """
    try:
        names = sorted(
            n for n in _read_vbios_manifest()
            if (VBIOS_TARGET_DIR / n).is_file()
        )
        return [{"name": n} for n in names]
    except Exception:
        return []


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


def list_phys_disks(node=None):
    """物理磁盘列表（设备名/型号/容量），供磁盘分区与格式化选盘。指定 node 时从目标节点采集。

    分类标注（前端据此决定置灰与提示）：
      pve_system：PVE 系统盘（格式化会毁掉宿主机）→ 置灰 + 脚本硬拦截
      pve_data  ：PVE 存储盘（正被 PVE 使用的数据盘）→ 置灰 + 拦截
      other_os  ：可能装有其他系统（Windows/Linux），启发式判断 → 警告但允许操作
      host_used ：上面两类 PVE 在用盘的并集，保留以兼容既有逻辑
    """
    if node:
        return _run_func_on_node(node, "list_phys_disks")
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


def list_vm_disks(node, gtype, vmid):
    """列出指定 VM 的磁盘列表（导入/导出/格式转换选盘用）；容器不支持此功能。
    返回 key/volid/size/format/path：format 从镜像文件后缀推断，path 为真实文件路径（dir 存储）。"""
    if not node or not vmid or not str(vmid).isdigit():
        return []
    if gtype == "lxc":
        return []  # 容器不支持磁盘导入导出
    try:
        cfg = backend.get_guest_config(node, gtype, vmid) or {}
    except Exception:
        return []
    # 本机 dir 型存储根目录映射，用于把 volid 解析成真实路径
    try:
        dirs = backend.disk_dirs()
    except Exception:
        dirs = {}
    disks = []
    for k, v in sorted(cfg.items()):
        # 匹配 scsi0/virtio0/sata0/ide0 等磁盘行（格式：存储:镜像名,...）
        if not re.match(r"^(scsi|virtio|sata|ide|scsihw)\d+$", k):
            continue
        # 排除 cloud-init 配置盘（PVE 自动生成，导出无意义）与 cdrom 光驱（ISO），
        # 与导出脚本的过滤保持一致，避免下拉里出现"选了却导不出来"的盘
        vstr = str(v)
        if "cloudinit" in vstr or "media=cdrom" in vstr:
            continue
        parts = str(v).split(",", 1)
        volid = parts[0]
        # volid 形如 local:100/100-disk-1.raw -> 存储=local，路径=images/100/100-disk-1.raw
        fmt = ""
        path = ""
        if ":" in volid:
            sid, rest = volid.split(":", 1)
            base = dirs.get(sid)
            if base:
                path = str(base / rest)
            name = rest.rsplit("/", 1)[-1]
            if "." in name:
                fmt = name.rsplit(".", 1)[-1].lower()
        disks.append({
            "key": k,
            "volid": volid,
            "size": parts[1] if len(parts) > 1 else "",
            "format": fmt,
            "path": path,
        })
    return disks


def list_disk_storages(node=None):
    """列出本机支持 images 内容的 dir 型存储（供导入导出选存储下拉）。指定 node 时从目标节点采集。"""
    if node:
        return _run_func_on_node(node, "list_disk_storages")
    try:
        dirs = backend.disk_dirs()
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


def list_image_storages(node=None):
    """列出目标节点支持 images 内容的全部存储（含 lvmthin/zfs/ceph/nfs/dir 等，供建机/新增磁盘选存储）。

    类型口径放开（默认 disk_storages 仅含 dir 型，会漏掉默认的 local-lvm），但可用性仍按
    执行节点收窄：某存储在该节点不可用（NFS 未挂载、本地目录不存在等）时不该出现在下拉里，
    否则要等 qm importdisk / qm set 执行时才失败。节点视图探测失败时退回不过滤。"""
    if node:
        return _run_func_on_node(node, "list_image_storages")
    avail = backend.node_available_storages()
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


def list_lxc_storages(node=None):
    """列出支持容器 rootfs 的存储（content 含 rootdir 或 images），供创建 LXC 容器选存储。

    指定 node 时从目标节点采集：存储「对该节点是否可用」是节点级事实，而容器是在执行节点上创建的
    —— 只取本机列表时，切到其它节点后可能选出该节点不存在的存储（pct create 直接失败）。
    与前端同口径排除项目共享存储 pvewebtools（它只服务于传输中心）。

    注意：本机实现刻意不叫 list_rootdir_storages —— 远端采集走 collect.py 的同名函数
    （_run_func_on_node 在本机会 globals()[func_name]），同名会递归调到自己。"""
    items = _run_func_on_node(node, "list_rootdir_storages") if node else _rootdir_storages_local()
    return [s for s in items if s.get("storage") != "pvewebtools"]


def _rootdir_storages_local():
    """本机支持容器 rootfs 的存储（逻辑与 collect.py 的 list_rootdir_storages 保持一致）。

    可用性按本节点收窄（disabled / active，与 list_image_storages 同口径）：本节点无法
    激活的存储不该出现在下拉里，否则要等 pct create 执行时才失败。探测失败时退回不过滤。"""
    avail = backend.node_available_storages()
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


def list_backup_storages(node=None):
    """列出本机支持 backup 内容的 dir 型存储（供备份选存储下拉）。指定 node 时从目标节点采集。"""
    if node:
        return _run_func_on_node(node, "list_backup_storages")
    try:
        dirs = backend.backup_dirs()
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


def _storage_options(node=None):
    """「存储内容」页存储下拉数据源：按节点列出已启用存储（含 content 类型）。

    node 为空/本机 → 本机 pvesh；外节点 → SSH 到该节点采集 —— 存储集合与「对该节点是否
    可用」都是节点级事实（local 等节点独占存储各节点内容独立），本机 pvesh 只看得到本机那份。
    每项附 local_ok：该存储的内容能否由本机直读。内容面板的删除 / 移动 / 下载都基于本机文件
    系统，故节点独占存储只能只读浏览，前端据此收起操作按钮。"""
    local = socket.gethostname().split(".")[0]
    remote = bool(node) and node != local
    items = _run_func_on_node(node, "list_storage_options") if remote else backend.list_storage_list()
    can_local = _local_access_pred(node)
    for it in items:
        it["local_ok"] = can_local(str(it.get("storage")))
    return items


def _storage_role(node, node_ip="", cfg=None):
    """节点在共享存储中的角色（决定「修复」按钮该修哪一侧）：
    - server：该节点就是 NFS 导出端（配置的 NFS_SERVER 落在它身上），只需维护服务端导出；
    - client：需把 NFS 导出挂到本机 WORKSPACE_PATH（并铺 vBIOS 软链）；
    - dir：未启用 NFS（本地 dir 模式），无共享存储可修。"""
    if cfg is None:
        try:
            cfg = storage_provision._load_config()
        except Exception:
            return "dir"
    try:
        wtype, server, _export, _path = storage_provision._resolve_base(cfg)
    except Exception:
        return "dir"
    if wtype != "nfs" or not server:
        return "dir"
    if node_ip and node_ip == server:
        return "server"
    # NFS_SERVER 未显式配置时（本机自管 NFS server），导出端就是跑本工具这台；其管理口 IP
    # 未必能从节点列表拿到，用主机名兜底，避免把导出端当客户端去「自己挂自己」。
    if not cfg.get("NFS_SERVER") and node == socket.gethostname().split(".")[0]:
        return "server"
    return "client"


class HandlerMixin:
    """从 ApiHandler 拆出的处理入口：方法体未改动，靠 self 访问其余基础设施。"""

    def _handle_storage_move_content(self):
        """把存储内容条目移动到另一个存储（跨存储搬迁）。

        源用 volid（如 local:backup/xxx.vma.zst），目标给存储 id + 可选的目标目录 subdir
        （候选由 GET /api/storage/move-targets 给出，省略则取该类型的首选合法目录）；
        同文件系统为改名（瞬时），跨文件系统为复制后删除；
        目标存储必须声明同一内容类型，且不得已有同名文件。"""
        body = self._read_body()
        storage = str(body.get("storage", ""))
        volid = str(body.get("volid", ""))
        dst = str(body.get("dst", ""))
        subdir = str(body.get("subdir", "") or "")
        if not storage or not volid or not dst:
            return self._error("参数不完整（需 storage / volid / dst）", 400)
        try:
            src_path, dst_path = backend.move_storage_content(storage, volid, dst, subdir or None)
        except backend.PveError as e:
            return self._error(str(e), 400)
        except Exception as e:  # noqa: BLE001
            return self._error(f"移动失败: {e}", 500)
        return self._json({"moved": dst_path.name, "from": str(src_path), "to": str(dst_path)})

    def _handle_storage_move(self):
        """在所选存储的各子目录间移动文件（同盘 rename）。

        支持任意存储：pvewebtools 走 dump/cloud-images/disk-images/vbios，
        其它存储（内置 local、NFS/ZFS 等）走 cloud-images/import（磁盘导入与云镜像两用目录）。
        src/dst 必须是该存储的白名单子目录且不相同；name 仅允许安全文件名（防路径穿越）。
        源文件不存在或目标已存在则报错，移动时一并移动同名的 .meta.json 侧车文件。"""
        body = self._read_body()
        storage = str(body.get("storage", ""))
        name = str(body.get("name", ""))
        src = str(body.get("src", ""))
        dst = str(body.get("dst", ""))
        if not storage or storage == "__import__":
            storage = "pvewebtools"
        if not re.match(r"^[\w\-]+$", storage):
            return self._error("非法存储名", 400)
        allowed = ("dump", "cloud-images", "disk-images", "vbios") if storage == "pvewebtools" else ("cloud-images", "import")
        if src not in allowed or dst not in allowed or src == dst:
            return self._error(f"{storage} 的源/目标子目录无效（仅支持 {'/'.join(allowed)}）", 400)
        if not re.match(r"^[\w.\-]+$", name) or ".." in name or "/" in name:
            return self._error("非法文件名", 400)
        root = backend.storage_path(storage)
        if not root:
            return self._error(f"找不到存储根: {storage}", 404)
        src_path = root / src / name
        dst_path = root / dst / name
        if not src_path.is_file():
            return self._error("源文件不存在", 404)
        if dst_path.exists():
            return self._error(f"目标已存在: {dst}/{name}", 409)
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(src_path), str(dst_path))
            meta_src = root / src / (name + ".meta.json")
            if meta_src.is_file():
                os.replace(str(meta_src), str(root / dst / (name + ".meta.json")))
        except OSError as e:
            return self._error(f"移动失败: {e}", 500)
        return self._json({"moved": name, "from": src, "to": dst})

    def _handle_nodes(self):
        """GET /api/nodes  节点列表（节点管理页 / 执行节点下拉）。

        ?fresh=1 触发后台重采（手动刷新）：立即回当前缓存、慢硬件采集转后台，新数据由前端
        下一轮轮询取走，请求本身不再等待 SSH 慢命令。pvesh 的节点表在集群降级时可能缺节点
        甚至为空（pmxcfs 成员视图不同步）：用 corosync 成员表补齐，避免节点管理页整页空白、
        离线节点连「临时取得仲裁」都点不到。并标注本机标记与共享存储角色——前端据此把
        「执行节点」下拉的本机项显示成真实节点名；共享存储角色用于一键修复时的排序与逐台动作分流。
        """
        query = parse_qs(urlparse(self.path).query)
        nodes = collect_nodes_cached(force=bool(query.get("fresh")))
        nodes = _merge_cluster_nodes(nodes)
        try:
            _cfg = storage_provision._load_config()
        except Exception:
            _cfg = None
        _local_node = socket.gethostname().split(".")[0]
        for n in nodes:
            n["local"] = (str(n.get("node")) == _local_node)
            if n.get("placeholder"):
                continue        # 占位条目状态未知，保持 "dir" 以免显示必然失败的修复按钮
            n["storage_role"] = _storage_role(str(n.get("node")), str(n.get("ip") or ""), _cfg)
        return self._json(nodes)

    def _handle_options(self):
        """批量选项端点：把工具弹窗的多个只读下拉请求合并成 1 个 GET。

        前端把本批需要的各上游 path 通过 ?path= 重复传参（含各自 query）；
        本方法复用既有 GET 分发（_handle_get），在捕获模式下逐个采集结果，
        最终一次性返回 [{ok, status, data|error}, ...]，顺序与请求 path 一一对应。
        单个子请求失败不影响其余；整批请求失败则前端回退为逐条 api() 调用。"""
        query = parse_qs(urlparse(self.path).query)
        paths = query.get("path") or []
        results = []
        old_path = self.path
        try:
            for raw in paths:
                p = raw.lstrip("/")
                # 白名单判定只看路径段，剥离 query（如 disks/imported?kind=cloud）
                subparts = [s for s in p.split("?", 1)[0].split("/") if s]
                if not self._is_option_source(subparts):
                    results.append({"ok": False, "status": 400, "error": "不允许批量该路径"})
                    continue
                self.path = "/api/" + p
                self._capture = []
                try:
                    self._handle_get(subparts)
                except Exception as e:  # noqa: BLE001
                    results.append({"ok": False, "status": 500, "error": str(e)})
                    self._capture = []
                    continue
                cap = self._capture
                self._capture = []
                if not cap:
                    results.append({"ok": False, "status": 500, "error": "无响应"})
                    continue
                status, body = cap[0]
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    payload = None
                if status == 200 and payload is not None and "data" in payload:
                    results.append({"ok": True, "status": status, "data": payload["data"]})
                else:
                    err = payload.get("error") if isinstance(payload, dict) and "error" in payload else "请求失败"
                    results.append({"ok": False, "status": status, "error": err})
        finally:
            self.path = old_path
            self._capture = None
        return self._json(results)

    def _handle_upload_status(self):
        """GET /api/upload/status?kind=&name=&storage=：返回某上传目标已落盘的分片索引，
        供前端断点续传（刷新后重新选择同一文件可跳过已传分片）。"""
        query = parse_qs(urlparse(self.path).query)
        kind = (query.get("kind") or [""])[0]
        name = (query.get("name") or [""])[0]
        storage = (query.get("storage") or [""])[0]
        if not kind or not name:
            return self._error("参数缺失", 400)
        if kind not in _UPLOAD_SIZE_LIMITS:
            return self._error("未知上传类型", 400)
        if not _upload_name_ok(kind, name):
            return self._error("非法文件名", 400)
        # 文件名/存储合法性沿用上传白名单口径，避免越权探测他人上传
        if kind == "backup":
            if not storage or storage not in _backup_dirs_merged():
                return self._error("存储不可用", 400)
        elif kind == "vztmpl":
            try:
                vdirs = backend.vztmpl_dirs()
            except Exception:  # noqa: BLE001
                vdirs = {}
            if not storage or storage not in vdirs:
                return self._error("存储不可用", 400)
        mst = _merge_state(kind, name, storage)
        return self._json({"received": _upload_received_indexes(kind, name, storage),
                           # 后台合并状态：前端据 done/error 结束轮询，merging 期间继续等
                           "merging": mst.get("state") == "merging",
                           "done": mst.get("state") == "done",
                           "error": mst.get("msg", "") if mst.get("state") == "error" else ""})

    def _handle_upload_verify(self):
        """GET /api/upload/verify?kind=&name=&storage=&hash=&size=：秒传判定（只读，不写任何文件）"""
        query = parse_qs(urlparse(self.path).query)
        hit, info = upload_instant_hit(
            (query.get("kind") or ["backup"])[0], (query.get("name") or [""])[0],
            (query.get("storage") or [""])[0], (query.get("hash") or [""])[0],
            (query.get("size") or ["0"])[0])
        return self._json({"hit": hit, "info": info})

    def _handle_upload_register(self):
        """POST /api/upload/register  body:{kind,name,storage,hash,size}
        上传完成后登记指纹，供下次秒传命中。属于加速手段，失败不报错。"""
        body = self._read_body()
        ok = upload_instant_register(body.get("kind") or "backup", body.get("name") or "",
                                     body.get("storage") or "", body.get("hash") or "",
                                     body.get("size") or 0)
        return self._json({"ok": ok})

    def _handle_storages(self):
        """GET /api/storages[?node=]  列出指定节点的已启用存储（含内容类型）。

        供「存储内容」选择器使用。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        try:
            return self._json(_storage_options(node or None))
        except Exception as e:  # noqa: BLE001
            return self._error(f"读取节点 {node} 的存储列表失败：{e}", 502)

    def _handle_image_storages(self):
        """GET /api/storages/images  支持 images 内容的全部存储（含 lvmthin/zfs/ceph 等）。

        供新增磁盘选存储；?node= 指定集群节点。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_image_storages(node or None))

    def _handle_storages_rootdir(self):
        """GET /api/storages/rootdir?node=  支持容器 rootfs 的存储（创建 LXC 容器选存储）。

        随执行节点采集：存储对哪个节点可用是节点级事实。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_lxc_storages(node or None))

    def _handle_storage_move_targets(self):
        """GET /api/storage/move-targets?ctype=&storage=  移动文件时的目标存储及各自合法目录候选。"""
        query = parse_qs(urlparse(self.path).query)
        ctype = (query.get("ctype") or [""])[0]
        src = (query.get("storage") or [""])[0]
        try:
            return self._json(backend.move_content_targets(src, ctype))
        except backend.PveError as e:
            return self._error(str(e), 400)

    def _handle_storage_content(self, storage):
        """GET /api/storage/<storage>/content[?node=]  列出指定存储的已有内容。

        内容能由本机直读（所选即本机，或共享存储且本机已激活）时就本机读：与目标节点是同一份
        数据，且能继续做文件操作；节点独占存储（local 等本机读不到，直读会串到本机同名存储）
        才 SSH 到目标节点采集，前端按只读展示。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        if _local_access_pred(node)(storage):
            return self._json(backend.list_storage_content(storage))
        try:
            return self._json(_run_func_on_node_args(node, "list_storage_content", (storage,), timeout=30))
        except Exception as e:  # noqa: BLE001
            return self._error(f"读取节点 {node} 的存储 {storage} 失败：{e}", 502)

    def _handle_storage_cfg_list(self):
        """GET /api/storage-cfg  列出数据中心所有存储完整配置（存储管理表格用）。"""
        return self._json(backend.list_storage_cfg())

    def _handle_storage_cfg_get(self, storage):
        """GET /api/storage-cfg/<storage>  获取单个存储的完整配置（编辑表单回填用）。"""
        return self._json(backend.get_storage_cfg(storage))

    def _handle_storage_cfg_create(self):
        """POST /api/storage-cfg  新增存储（body 含 storage/type 与类型相关字段）。"""
        body = self._read_body()
        return self._json({"upid": backend.create_storage_cfg(body)})

    def _handle_storage_cfg_update(self, storage):
        """PUT /api/storage-cfg/<storage>  更新存储配置（body 为要修改的字段）。"""
        body = self._read_body()
        return self._json({"upid": backend.update_storage_cfg(storage, body)})

    def _handle_storage_cfg_delete(self, storage):
        """DELETE /api/storage-cfg/<storage>  移除存储（仅删除配置，不影响实际数据）。"""
        backend.delete_storage_cfg(storage)
        return self._json({"deleted": storage})

    def _handle_storage_content_delete(self):
        """DELETE /api/storage  body:{storage, volid, node?}  删除存储内已有文件。

        带 node 时按该节点删（走远端采集路径）；不带则按本机。
        """
        body = self._read_body()
        self._delete_storage_content(str(body.get("storage", "")), str(body.get("volid", "")),
                                     str(body.get("node", "")) or None)
        return self._json({"deleted": body.get("volid")})

    def _handle_disks(self):
        """GET /api/disks  物理磁盘列表（磁盘休眠/SMART 详情检查下拉数据源）；?node= 指定集群节点。"""
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_phys_disks(node or None))

    def _handle_disks_byid(self):
        """GET /api/disks/byid  可直通整块磁盘列表（块映射直通下拉数据源）；?node= 指定集群节点。"""
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_byid_disks(node or None))

    def _handle_disk_storages(self):
        """GET /api/disks/storages  支持 images 内容的 dir 型存储列表（虚机导入导出选存储）。

        ?node= 指定集群节点。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_disk_storages(node or None))

    def _handle_disks_vm(self):
        """GET /api/disks/vm?node=&type=&vmid=  列出指定 VM 的磁盘列表（供导入导出/格式转换选盘）。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        gtype = (query.get("type") or [""])[0]
        vmid = (query.get("vmid") or [""])[0]
        return self._json(list_vm_disks(node, gtype, vmid))

    def _handle_disks_imported(self):
        """GET /api/disks/imported[?os=][&kind=cloud|disk][&storage=]  列出导入目录中已上传的磁盘镜像。

        os 用于「云镜像制作」按操作系统类型筛选源镜像；kind 区分云镜像(cloud)/普通磁盘(disk)，
        省略则两者都列；storage 供传输中心「存储内容」跟随所选存储过滤（pvewebtools 的条目
        在结果中以 __import__ 标记）。
        """
        query = parse_qs(urlparse(self.path).query)
        osf = (query.get("os") or [""])[0] or None
        kindf = (query.get("kind") or [""])[0] or None
        stf = (query.get("storage") or [""])[0] or None
        return self._json(list_imported_disks(osf, kindf, stf))

    def _handle_network_bridges(self):
        """GET /api/network/bridges  vmbr 网桥列表（网桥与 VLAN 管理下拉数据源）。

        含在用 VM 标注；?node= 指定集群节点。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_network_bridges(node or None))

    def _handle_network_vlans(self):
        """GET /api/network/vlans  VLAN 接口列表（删除 VLAN 下拉数据源）；?node= 指定集群节点。"""
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_network_vlans(node or None))

    def _handle_network_interfaces(self):
        """GET /api/network/interfaces  物理网口列表（创建网桥选 bridge-ports）；?node= 指定集群节点。"""
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_phys_nics(node or None))

    def _handle_network_backups(self):
        """GET /api/network/backups  网络配置备份列表（从备份恢复下拉数据源）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_net_backups(node or None))

    def _handle_pci_storage(self):
        """GET /api/pci  可直通 PCIe 存储设备列表（PCIe 直通下拉数据源）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_pci_storage(node or None))

    def _handle_pci_nics(self):
        """GET /api/pci/nics  可直通 PCIe 网卡列表（含宿主在用标记）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_pci_nics(node or None))

    def _handle_pci_gpus(self):
        """GET /api/pci/gpus  可直通 PCIe 显卡列表（独显直通下拉数据源，含宿主驱动标记）。

        ?node= 指定集群节点。
        """
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_pci_gpus(node or None))

    def _handle_pci_generic(self):
        """GET /api/pci/generic  其余可直通 PCIe 设备（采集卡/声卡/USB 控制器等）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_pci_generic(node or None))

    def _handle_pci_vfio(self):
        """GET /api/pci/vfio  「被 vfio 接管但无 VM 使用」的设备（释放 vfio 占用工具的数据源）。

        ?node= 指定集群节点。
        """
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_vfio_orphans(node or None))

    def _handle_usb_devices(self):
        """GET /api/usb/devices  可直通 USB 设备列表（含已直通 VM 标记）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_usb_devices(node or None))

    def _handle_kernels(self):
        """GET /api/kernels  已装内核版本列表（内核配置管理-设默认启动下拉数据源）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_installed_kernels(node or None))

    def _handle_kernels_available(self):
        """GET /api/kernels/available  仓库可装内核包列表（安装新内核下拉数据源）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_available_kernels(node or None))

    def _handle_grub_backups(self):
        """GET /api/grub/backups  GRUB 配置备份列表（从备份恢复下拉数据源）；?node= 指定集群节点。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0]
        if node:
            known = backend.known_node_names()
            if node not in known:
                return self._error("节点不存在", 404)
        return self._json(list_grub_backups(node or None))

    def _handle_backup_storages(self):
        """GET /api/backup/storages  支持 backup 内容的 dir 型存储列表（虚机备份选存储）。

        ?node= 指定集群节点。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        return self._json(list_backup_storages(node or None))

    def _handle_backup_files(self):
        """GET /api/backup/files  备份文件列表（还原时下拉选）；?node= 指定集群节点。

        备份目录是节点本地的；storage="pvewebtools" 的条目标记 template=true，
        供「模板快速建机」精确筛选。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        try:
            return self._json(_node_backup_files(node or None))
        except Exception as e:  # noqa: BLE001
            return self._error(str(e), 500)

    def _handle_backup_delete(self):
        """DELETE /api/backup  body:{name, storage}  删除备份文件。"""
        body = self._read_body()
        self._delete_backup(str(body.get("storage", "")), str(body.get("name", "")))
        return self._json({"deleted": body.get("name")})

    def _handle_template_cache_nodes(self):
        """GET /api/template-cache/nodes  各节点缓存概览。

        构建节点下拉用，界面据此过滤掉无需缓存的节点。
        """
        try:
            return self._json(_template_cache_nodes())
        except Exception as e:  # noqa: BLE001
            return self._error(f"读取节点缓存概览失败: {e}", 500)

    def _handle_template_cache(self):
        """GET /api/template-cache?node=  本机缓存（「缓存加速」面板）。

        返回缓存位置、占用水位与条目列表。
        """
        node = _node_from_query(self.path)
        if node is False:
            return self._error("节点不存在", 404)
        try:
            return self._json(_template_cache_dispatch(node, "_node_template_cache"))
        except Exception as e:  # noqa: BLE001
            return self._error(f"读取缓存信息失败: {e}", 500)

    def _handle_template_cache_delete(self):
        """DELETE /api/template-cache  body:{node,name} 删单条；body:{node,all:true} 清空该节点缓存。

        注意：do_DELETE 只兜底 PveError，节点解析的 ToolError 需在此自行转错误响应。
        """
        body = self._read_body()
        try:
            node = self._resolve_node(body.get("node"))
        except toolbox.ToolError as e:
            return self._error(str(e), 400)
        try:
            if body.get("all"):
                return self._json(_template_cache_dispatch(node, "_node_template_cache_clear"))
            return self._json(_template_cache_dispatch(
                node, "_node_template_cache_delete", (str(body.get("name", "")),)))
        except (RuntimeError, OSError) as e:
            return self._error(str(e), 400)

    def _handle_vbios_roms(self):
        """GET /api/vbios/roms  已上传的 vBIOS ROM 列表（显卡直通-romfile 下拉数据源）。"""
        return self._json(list_vbios_roms())

    def _handle_vbios_sync(self):
        """POST /api/vbios/sync  把共享存储里的 vBIOS 主副本同步到集群其它节点的 /usr/share/kvm/。"""
        return self._json(_vbios_sync_to_nodes())

    def _handle_vbios_remove(self):
        """DELETE /api/vbios  body:{name}  删除 /usr/share/kvm/ 下已上传的 vBIOS ROM。

        与 ``_handle_vbios_delete``（按名字删的实际实现，也被上传流程复用）区分开：
        本方法是 DELETE 路由的入口，负责从 body 取名字。
        """
        body = self._read_body()
        return self._handle_vbios_delete(str(body.get("name", "")))

    def _handle_docker_versions(self):
        """GET /api/docker-versions  Docker 引擎稳定版版本下拉。"""
        return self._json(list_docker_versions())

    def _handle_cloud_image_presets_get(self):
        """GET /api/cloud-image/presets  返回可编辑的云镜像预设源。

        取自 config/cloud-image-presets.json，缺省回退内置默认。
        """
        return self._json(load_cloud_image_presets())

    def _handle_cloud_image_mirrors(self):
        """GET /api/cloud-image/mirrors  可选下载源（域名替换式镜像站，首项为默认的中科大 USTC）。"""
        return self._json(cloud_image_mirrors())

    def _handle_cloud_image_storages(self):
        """GET /api/cloud-image/storages  可作云镜像下载目标的存储（images/import 内容）。"""
        return self._json(list_cloud_storages())

    def _handle_cloud_image_presets_set(self):
        """POST /api/cloud-image/presets  body:{presets:[{label,url}]}  持久化可编辑预设源。

        网页维护：原子写 + 备份旧文件。
        """
        body = self._read_body()
        ok, msg = save_cloud_image_presets(body.get("presets") if isinstance(body, dict) else None)
        if not ok:
            return self._error(msg, 400)
        return self._json({"ok": True, "msg": msg})

    def _handle_cloud_image_presets_check(self):
        """POST /api/cloud-image/presets/check  探活所有预设源可用性（HEAD/GET Range），返回逐条结果。"""
        return self._json(probe_cloud_image_presets(load_cloud_image_presets()))

    def _handle_cloud_image_presets_reset(self):
        """POST /api/cloud-image/presets/reset  一键恢复内置默认预设源（覆盖自定义，旧文件备份）。"""
        ok, msg = reset_cloud_image_presets()
        if not ok:
            return self._error(msg, 400)
        return self._json({"ok": True, "msg": msg})

    def _handle_lxc_downloaded(self):
        """GET /api/lxc-template/downloaded  本机已下载的 CT 模板（创建 LXC 容器时选源）。"""
        return self._json(list_lxc_downloaded())

    def _handle_lxc_cluster_sources(self):
        """GET /api/lxc-template/cluster-sources[?refresh=1]  集群各节点 CT 源一致性（下载源处提示）。"""
        query = parse_qs(urlparse(self.path).query)
        force = (query.get("refresh") or [""])[0] in ("1", "true", "yes")
        return self._json(lxc_cluster_sources(force=force))

    def _handle_tools(self):
        """GET /api/tools  工具清单（按分类分组，含依赖提示与参数定义）。

        附带 recommend_status：前端需要知道建议的前置工具是否成功执行过。
        """
        data = toolbox.list_tools()
        dep_ids = {r["tool"] for t in data["tools"] for r in t.get("recommend", [])}
        data["recommend_status"] = jobs.last_success_times(sorted(dep_ids))
        return self._json(data)

    def _handle_jobs(self):
        """GET /api/jobs  任务列表（执行日志总览）。"""
        return self._json(jobs.list_jobs())

    def _handle_job_detail(self, job_id):
        """GET /api/jobs/<job_id>[?offset=N]  任务详情（支持增量拉取，省流量）。"""
        query = parse_qs(urlparse(self.path).query)
        try:
            offset = int(query.get("offset", ["0"])[0] or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        snap = jobs.get(job_id, offset)
        if snap is None:
            return self._error("任务不存在", 404)
        return self._json(snap)

    def _handle_job_kill(self, job_id):
        """POST /api/jobs/<job_id>/kill  强制结束运行中的任务（杀进程组，含远端节点）。"""
        ok, msg = jobs.kill(job_id)
        if not ok:
            return self._error(msg, 400)
        return self._json({"killed": job_id})

    def _handle_jobs_clear(self):
        """POST /api/jobs/clear  body:{before_days:N}  批量清理执行日志。

        before_days 缺省（或非法）时全部清空。
        """
        body = self._read_body()
        days = body.get("before_days")
        try:
            before = time.time() - float(days) * 86400 if days else None
        except (TypeError, ValueError):
            before = None
        count = jobs.delete_bulk(before)
        return self._json({"deleted": count})

    def _handle_job_delete(self, job_id):
        """DELETE /api/jobs/<job_id>  删除已结束任务的日志（运行中的不允许删）。"""
        if not jobs.delete(job_id):
            return self._error("任务不存在或仍在运行", 400)
        return self._json({"deleted": job_id})

    def _handle_cluster_info(self):
        """GET /api/cluster/info  本机是否已建集群 / 集群名。

        节点管理页据此决定入口显隐与「加入节点」第 1 步。
        """
        _local = _local_outbound_ip() or socket.gethostname().split(".")[0]
        _ok, _why = self._target_in_cluster(_local)
        _name = ""
        if _ok:
            try:
                _name = backend.cluster_name()
            except Exception:  # noqa: BLE001
                _name = ""
        return self._json({"in_cluster": bool(_ok), "name": _name, "reason": _why})

    def _handle_cluster_quorum(self):
        """GET /api/cluster/quorum  集群仲裁状态；can_degrade 由服务端强校验，前端只透传。"""
        return self._json(backend.quorum_status())

    def _handle_edition(self):
        """GET /api/edition  版本号 + 工具集档位 + 版本能力。

        轻量接口：供首屏胶囊秒显（不触发 toolbox/jobs 重型路径）。edition / runtime 是
        工具集档位（toolbox.EDITION）；max_nodes / can_join / enforced 见 edition_caps()。
        """
        _vi = version_info()
        data = edition_caps()
        data.update({"version": _vi["version"], "source": _vi["source"]})
        return self._json(data)

    def _handle_schedules(self):
        """GET /api/schedules  定时开关机任务列表；不带 ?node= 时查本机。"""
        query = parse_qs(urlparse(self.path).query)
        node = (query.get("node") or [""])[0] or socket.gethostname().split(".")[0]
        return self._json(backend.list_schedules(node))

    def _handle_vbios_upload(self):
        """接收单个 vBIOS 分片落盘；分片收齐后自动合并到 /usr/share/kvm/"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        try:
            index = int((query.get("index") or ["-1"])[0])
            count = int((query.get("count") or ["0"])[0])
        except ValueError:
            return self._error("分片参数错误", 400)
        length = int(self.headers.get("Content-Length") or 0)
        if not name or count <= 0 or not 0 <= index < count:
            return self._error("上传参数错误", 400)
        err = _upload_size_error("vbios", (query.get("size") or [""])[0])
        if err:
            return self._error(err, 400)
        # vBIOS 落到系统目录 /usr/share/kvm/，文件名必须严格白名单，防路径穿越
        if not _upload_name_ok("vbios", name):
            return self._error("非法文件名", 400)
        if not _allowed_vbios_ext(name):
            return self._error("不允许的文件类型，仅支持 .rom/.bin", 400)
        _receive_upload_chunk(self.rfile, length, "vbios", name, "", index, count,
                              (query.get("size") or [""])[0])
        merging = _maybe_merge_async("vbios", name, "", count)
        return self._json({"ok": True, "done": False, "merging": merging})

    def _handle_vztmpl_upload(self):
        """接收单个 CT 容器模板分片落盘；分片收齐后合并到所选存储的 template/cache/。
        支持上传到任意声明了 vztmpl 内容的存储（默认项目共享存储 pvewebtools）。"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        storage = (query.get("storage") or [""])[0]
        try:
            index = int((query.get("index") or ["-1"])[0])
            count = int((query.get("count") or ["0"])[0])
        except ValueError:
            return self._error("分片参数错误", 400)
        length = int(self.headers.get("Content-Length") or 0)
        if not name or not storage or count <= 0 or not 0 <= index < count:
            return self._error("上传参数错误", 400)
        err = _upload_size_error("vztmpl", (query.get("size") or [""])[0])
        if err:
            return self._error(err, 400)
        # 模板命名校验：只接受 PVE 模板命名（.tar.gz/.tar.xz/.tar.zst），杜绝路径穿越与任意后缀落盘
        if not _upload_name_ok("vztmpl", name):
            return self._error("不允许的文件类型，仅支持 .tar.gz/.tar.xz/.tar.zst 容器模板", 400)
        # 校验存储可用（须启用 vztmpl 内容）
        try:
            dirs = backend.vztmpl_dirs()
        except Exception:  # noqa: BLE001
            dirs = {}
        if storage not in dirs:
            return self._error("存储不可用或未启用容器模板(vztmpl)内容", 400)
        _receive_upload_chunk(self.rfile, length, "vztmpl", name, storage, index, count,
                              (query.get("size") or [""])[0])
        merging = _maybe_merge_async("vztmpl", name, storage, count)
        return self._json({"ok": True, "done": False, "merging": merging})

    def _handle_vbios_delete(self, name):
        """删除本工具上传的 vBIOS ROM（本机 /usr/share/kvm/ 副本 + 共享主副本 + 清单）。

        安全边界：只允许删除清单里记录过的本工具文件，杜绝按名删除 QEMU 系统固件
        （vgabios-*.bin / efi-*.rom / seabios.bin 等）。"""
        if not name or not re.fullmatch(r"[\w][\w.\-]{0,99}", name):
            return self._error("非法文件名", 400)
        if not _vbios_is_managed(name):
            return self._error("vBIOS 文件不存在（不是本工具上传的文件）", 404)
        target = VBIOS_TARGET_DIR / name
        if not target.is_symlink() and not target.is_file():
            return self._error("vBIOS 文件不存在", 404)
        try:
            target.unlink()
            shared = _vbios_shared_dir()
            if shared:
                try:
                    (shared / name).unlink(missing_ok=True)
                except OSError:
                    pass
            _remove_vbios_manifest(name)   # 同步从清单移除
            _vbios_remove_remote([name])   # 尽力清理其它节点的软链，避免残留死链
        except OSError as e:
            return self._error(f"删除失败: {e}", 500)
        return self._json({"deleted": name})

    def _handle_node_join_init(self):
        """新节点加入集群的初始化钩子（由「加节点」流程在目标节点上调用）：
        1) 挂载 NFS 共享存储到 WORKSPACE_PATH（挂载后即全集群可见，备份/云镜像无需额外动作）；
        2) 把共享存储里的 vBIOS 主副本软链到本机 /usr/share/kvm（PVE romfile 硬约束）。
        返回 {"ok":bool,"mount":str,"vbios_linked":[...]}。"""
        try:
            ok, msg = storage_provision.node_join_mount_nfs()
        except Exception as e:  # noqa: BLE001
            return self._error(f"挂载 NFS 失败: {e}", 500)
        if not ok:
            return self._error(msg, 500)
        linked = init_vbios_local()
        return self._json({"ok": True, "mount": msg, "vbios_linked": linked})

    def _handle_storage_download(self):
        """下载存储内已有文件（iso/vztmpl/backup/snippets/images/import）：流式传输，volid 解析防路径穿越。

        带 ?node= 且该存储属于节点独占（本机读不到）时，改由目标节点定位并经 SSH 取回。"""
        query = parse_qs(urlparse(self.path).query)
        storage = (query.get("storage") or [""])[0]
        volid = (query.get("volid") or [""])[0]
        node = (query.get("node") or [""])[0]
        if not storage or not volid:
            return self._error("缺少 storage 或 volid", 400)
        # node 会作为 SSH 目标，必须白名单校验为集群成员，否则可 ssh 到任意主机
        if node and not _is_cluster_node(node):
            return self._error("非法节点名", 400)
        if node and node != socket.gethostname().split(".")[0] and not _local_access_pred(node)(storage):
            return self._stream_remote_storage_content(node, storage, volid)
        try:
            path, fname = backend.storage_content_file(storage, volid)
        except pve_api.PveError as e:
            return self._error(str(e), 400)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        from urllib.parse import quote
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(fname)}")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            try:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _handle_backup_download(self):
        """下载备份文件：流式传输，防路径穿越，文件名白名单校验"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        storage = (query.get("storage") or [""])[0]
        # 文件名只允许白名单字符，防路径穿越
        if not name or not re.fullmatch(r"[\w][\w.\- ]*", name):
            return self._error("非法文件名", 400)
        # 从备份目录定位文件
        dirs = _backup_dirs_merged()
        d = dirs.get(storage)
        if d is None:
            return self._error("存储不可用", 400)
        path = d / name
        if not path.is_file():
            return self._error("备份文件不存在", 404)
        size = path.stat().st_size
        # 流式发送，支持断点续传（Range 请求）
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        # RFC 5987 编码文件名，支持中文
        from urllib.parse import quote
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(name)}")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            try:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                return  # 客户端取消下载/关闭页面，属正常中断，不报错

    def _handle_backup_upload(self):
        """接收单个备份分片落盘；分片收齐后自动合并为最终备份文件"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        storage = (query.get("storage") or [""])[0]
        try:
            index = int((query.get("index") or ["-1"])[0])
            count = int((query.get("count") or ["0"])[0])
        except ValueError:
            return self._error("分片参数错误", 400)
        length = int(self.headers.get("Content-Length") or 0)
        if not name or not storage or count <= 0 or not 0 <= index < count:
            return self._error("上传参数错误", 400)
        err = _upload_size_error("backup", (query.get("size") or [""])[0])
        if err:
            return self._error(err, 400)
        # 文件名白名单校验，防路径穿越
        if not _upload_name_ok("backup", name):
            return self._error("非法文件名", 400)
        # 扩展名白名单：只允许备份类文件，拒绝脚本/可执行文件/任意二进制
        if not _allowed_backup_ext(name):
            return self._error("不允许的文件类型，仅支持 .vma（含 .zst/.gz/.lzo 压缩）", 400)
        # 校验存储可用
        dirs = _backup_dirs_merged()
        if storage not in dirs:
            return self._error("存储不可用或未启用 backup 内容", 400)
        _receive_upload_chunk(self.rfile, length, "backup", name, storage, index, count,
                              (query.get("size") or [""])[0])
        merging = _maybe_merge_async("backup", name, storage, count)
        return self._json({"ok": True, "done": False, "merging": merging})

    def _handle_disk_upload(self):
        """接收单个磁盘镜像分片落盘到导入目录；分片收齐后自动合并为最终镜像。
        kind=cloud 落 cloud-images/（云镜像），kind=disk 落 disk-images/（普通磁盘），默认 cloud。"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        kind = (query.get("kind") or ["cloud"])[0]
        if kind not in ("disk", "cloud"):
            return self._error("未知上传类型", 400)
        try:
            index = int((query.get("index") or ["-1"])[0])
            count = int((query.get("count") or ["0"])[0])
        except ValueError:
            return self._error("分片参数错误", 400)
        length = int(self.headers.get("Content-Length") or 0)
        if not name or count <= 0 or not 0 <= index < count:
            return self._error("上传参数错误", 400)
        err = _upload_size_error(kind, (query.get("size") or [""])[0])
        if err:
            return self._error(err, 400)
        # 文件名白名单校验，防路径穿越
        if not _upload_name_ok(kind, name):
            return self._error("非法文件名", 400)
        # 扩展名白名单：允许磁盘镜像，或单层压缩（.xz/.gz/.zst）且解压后是磁盘镜像；拒绝脚本/可执行文件
        # 压缩包在上传合并完成后由后台自动解压（见 _do_merge：解压出镜像并删除压缩源，与下载链路一致）
        comp_ext = next((e for e in _CLOUD_COMP_EXTS if name.lower().endswith(e)), None)
        decompress = False
        if comp_ext:
            base = name[: -len(comp_ext)]
            if not _allowed_disk_ext(base):
                return self._error("不允许的文件类型，仅支持磁盘镜像或其 .xz/.gz/.zst 压缩包", 400)
        elif not _allowed_disk_ext(name):
            return self._error("不允许的文件类型，仅支持 .qcow2/.raw/.img/.vmdk/.vhdx/.vdi/.vpc", 400)
        _receive_upload_chunk(self.rfile, length, kind, name, "", index, count,
                              (query.get("size") or [""])[0])
        # 合并（以及压缩包解压）整体交给后台线程：_do_merge 按文件名后缀自动判断是否解压
        merging = _maybe_merge_async(kind, name, "", count)
        return self._json({"ok": True, "done": False, "merging": merging})

    def _handle_cloud_image_download(self):
        """接收云镜像下载请求：校验 URL 与文件名，后台线程下载，立即返回（前端轮询 status）。
        可选 storage 参数：指定后下载到该存储的 cloud-images/ 子目录，否则默认导入目录。
        可选 mirror 参数：按所选镜像站改写预设 URL 的域名（保留路径），default/缺省原样下载。"""
        body = self._read_body()
        url = (body.get("url") or "").strip()
        name = (body.get("name") or "").strip()
        storage = (body.get("storage") or "").strip()
        mirror = (body.get("mirror") or "").strip()
        label = (body.get("label") or "").strip()
        distro = (body.get("distro") or "").strip()
        version = (body.get("version") or "").strip()
        source = (body.get("source") or "").strip()
        if not url:
            return self._error("缺少 url", 400)
        # 下载源：按所选镜像站改写域名（与前端地址框预览一致，服务端为准）
        url = apply_cloud_mirror(url, mirror)
        if not re.match(r"^https?://", url):
            return self._error("仅支持 http/https 链接", 400)
        # SSRF 防护：拒绝指向本机 / 回环 / 链路本地 / 未指定地址的 URL（云元数据、本机服务探测等）
        if _blocked_cloud_url(url):
            return self._error("下载地址指向本机或受保护网段，已拒绝", 400)
        meta = None
        if label or distro or version:
            meta = {"label": label, "distro": distro, "version": version,
                    "source": source or "preset", "url": url}
        if not name:
            name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path)) or "cloud-image"
        if not re.fullmatch(r"[\w][\w.\- ]*", name):
            return self._error("非法文件名", 400)
        # 选项 B：下载不自动解压，压缩包原样保留在导入目录；云镜像装机/磁盘导入时再现场解压。
        # 允许的文件名：磁盘镜像本身，或单层压缩的单一磁盘镜像（xxx.qcow2.xz 等）。
        decompress = False
        if not _allowed_disk_ext(name) and not _is_cloud_compressed(name):
            name += ".qcow2"
        if name in type(self)._cloud_dl and type(self)._cloud_dl[name]["status"] == "downloading":
            return self._error("该镜像正在下载中", 409)
        dest_dir, dest_storage = cloud_image_import_path(storage)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = str(dest_dir / name)
        # 已存在同名镜像（且不在下载中）时拒绝，避免静默覆盖已下载的镜像（与 LXC 模板下载行为一致）
        if os.path.exists(dest):
            return self._error("同名镜像已存在，请先到「存储内容」删除或换名再下载", 409)
        t = threading.Thread(target=type(self)._cloud_image_thread, args=(url, dest, name, decompress, meta), daemon=True)
        t.start()
        return self._json({"ok": True, "name": name, "storage": dest_storage})

    def _handle_cloud_image_status(self):
        """查询云镜像后台下载进度"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        return self._json(type(self)._cloud_dl.get(name, {"status": "notfound"}))

    def _handle_cloud_image_list(self):
        """列出所有后台云镜像下载任务（含已完成），供前端刷新页面后恢复进度"""
        # 后台下载线程会并发写入 _cloud_dl，迭代前先取快照，避免 "dictionary changed size during iteration"
        return self._json([{"name": k, **v} for k, v in list(type(self)._cloud_dl.items())])

    def _handle_cloud_image_cancel(self):
        """取消正在进行的云镜像后台下载：置取消标记，线程会在下个分片读取后停止并清理半成品 .part"""
        body = self._read_body()
        name = (body.get("name") or "").strip()
        if not name:
            return self._error("缺少 name", 400)
        rec = type(self)._cloud_dl.get(name)
        if not rec or rec["status"] != "downloading":
            return self._error("该镜像未在下载中", 404)
        type(self)._cloud_dl_cancel.add(name)
        return self._json({"ok": True, "cancelled": name})

    def _handle_lxc_available(self):
        """GET /api/lxc-template/available?refresh=0|1&mirror=<源id>
        列出 pveam 中可下载的 CT 模板（refresh=1 先 pveam update 刷新索引）。"""
        query = parse_qs(urlparse(self.path).query)
        refresh = (query.get("refresh") or [""])[0] in ("1", "true", "yes")
        mirror = (query.get("mirror") or [""])[0]
        return self._json(list_lxc_templates(refresh=refresh, mirror=mirror))

    def _handle_lxc_storages(self):
        """GET /api/lxc-template/storages  支持 vztmpl 内容的存储（模板落盘目标）"""
        return self._json(list_vztmpl_storages())

    def _handle_lxc_batch_swap(self):
        """POST /api/lxc-template/batch-source-swap  body:{mirror,security}
        集群各节点统一换源（含本机），实现「跟随本机」处的一键批量换源。"""
        body = self._read_body()
        mirror = str(body.get("mirror") or "ustc").strip()
        security = str(body.get("security") or "mirror").strip()
        return self._json(lxc_batch_swap(mirror, security))

    def _handle_lxc_download(self):
        """POST /api/lxc-template/download  body:{name, storage, mirror, section?}
        后台下载 CT 模板到目标存储的 template/cache/，落盘后 PVE 立即可用（pveam list / 建 CT）。"""
        body = self._read_body()
        name = (body.get("name") or "").strip()
        storage = (body.get("storage") or "").strip()
        mirror = (body.get("mirror") or "").strip()
        section = (body.get("section") or "").strip().lower()
        if not name:
            return self._error("请选择要下载的模板", 400)
        # 文件名严格校验：只接受 PVE 模板命名，杜绝路径穿越与任意后缀落盘
        if not LXC_TEMPLATE_NAME_RE.match(name):
            return self._error("非法模板名", 400)
        # section 决定 URL 中 images/ 下的一级目录（system/mail/...）；
        # 前端未传或非法时，按模板名向本机 pveam 反查，仍查不到则拒绝（拼不出正确地址）
        if not LXC_SECTION_RE.match(section):
            section = _resolve_lxc_section(name)
        if not section:
            return self._error("无法确定模板分类（section），请先刷新模板列表", 400)
        if not storage:
            return self._error("请选择目标存储", 400)
        if name in type(self)._lxc_dl and type(self)._lxc_dl[name]["status"] == "downloading":
            return self._error("该模板正在下载中", 409)
        try:
            dest_dir = lxc_template_dest(storage)
        except pve_api.PveError as e:
            return self._error(str(e), 400)
        if (dest_dir / name).exists() or (dest_dir / (name + ".part")).exists():
            return self._error(f"该存储中已存在模板 {name}", 409)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self._error(f"无法创建模板目录: {e}", 500)
        # 下载地址由服务端按「模板名 + section + 所选源」拼出，不接受客户端传入 URL，避免任意地址下载；
        # turnkeylinux 类会给出多个镜像源的候选地址（各站收录的版本不同），下载线程逐个回退尝试
        urls = _lxc_template_urls(lxc_template_base(mirror), section, name)
        t = threading.Thread(target=type(self)._lxc_template_thread,
                             args=(urls, str(dest_dir / name), name), daemon=True)
        t.start()
        return self._json({"ok": True, "name": name, "storage": storage, "url": urls[0],
                           "urls": urls})

    def _handle_lxc_status(self):
        """查询 CT 模板后台下载进度"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        return self._json(type(self)._lxc_dl.get(name, {"status": "notfound"}))

    def _handle_lxc_list(self):
        """列出所有后台 CT 模板下载任务（含已完成），供刷新页面后恢复进度"""
        # 后台下载线程会并发写入 _lxc_dl，迭代前先取快照，避免 "dictionary changed size during iteration"
        return self._json([{"name": k, **v} for k, v in list(type(self)._lxc_dl.items())])

    def _handle_lxc_cancel(self):
        """取消正在进行的 CT 模板下载：置取消标记，线程会在下个分片读取后停止并清理半成品 .part"""
        body = self._read_body()
        name = (body.get("name") or "").strip()
        if not name:
            return self._error("缺少 name", 400)
        rec = type(self)._lxc_dl.get(name)
        if not rec or rec["status"] != "downloading":
            return self._error("该模板未在下载中", 404)
        type(self)._lxc_dl_cancel.add(name)
        return self._json({"ok": True, "cancelled": name})

    def _handle_disk_download(self):
        """下载导入/导出目录中的磁盘镜像（流式，防路径穿越）。
        kind=export 从导出目录取；kind=cloud 从 cloud-images/ 取（云镜像）；kind=disk 从 disk-images/ 取（普通磁盘）。
        kind=import 视为 disk（兼容旧前端）。"""
        query = parse_qs(urlparse(self.path).query)
        kind = (query.get("kind") or ["disk"])[0]
        name = (query.get("name") or [""])[0]
        storage = (query.get("storage") or ["__import__"])[0]
        try:
            if kind == "export":
                if not name or not re.fullmatch(r"[\w][\w.\- ]*", name):
                    raise pve_api.PveError("非法文件名")
                if not _allowed_disk_ext(name):
                    raise pve_api.PveError("不允许的文件类型")
                dirs = backend.export_dirs()
                d = dirs.get(storage)
                if d is None:
                    raise pve_api.PveError("存储不可用或未启用 images 内容")
                path = d / name
            else:
                k = "cloud" if kind == "cloud" else "disk"
                path = _resolve_import_disk(storage, name, k)
        except pve_api.PveError as e:
            return self._error(str(e), 400)
        if not path.is_file():
            return self._error("磁盘镜像不存在", 404)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        from urllib.parse import quote
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(name)}")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            try:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                return  # 客户端取消下载/关闭页面，属正常中断，不报错

    def _handle_disk_list(self):
        """列出某存储导出目录中已有的磁盘镜像文件（历史导出目录），供前端常驻浏览与下载/删除"""
        query = parse_qs(urlparse(self.path).query)
        storage = (query.get("storage") or [""])[0]
        if not storage or not re.fullmatch(r"[\w][\w.\- ]*", storage):
            return self._error("非法存储名", 400)
        dirs = backend.export_dirs()
        d = dirs.get(storage)
        if d is None:
            return self._error("存储不可用或未启用 images 内容", 400)
        try:
            files = [p for p in d.iterdir() if p.is_file() and _allowed_disk_ext(p.name)]
        except OSError:
            files = []
        out = [{"name": p.name, "size": p.stat().st_size} for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)]
        return self._json({"storage": storage, "files": out})

    def _handle_disk_delete(self):
        """删除导入/导出目录中的磁盘镜像文件（防路径穿越）。
        kind=cloud 删 cloud-images/（云镜像）；kind=disk/import 删 disk-images/（普通磁盘）；kind=export 删导出目录。"""
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        storage = (query.get("storage") or ["__import__"])[0]
        kind = (query.get("kind") or ["export"])[0]
        try:
            if kind == "export":
                if not name or not re.fullmatch(r"[\w][\w.\- ]*", name):
                    return self._error("非法文件名", 400)
                if not _allowed_disk_ext(name):
                    return self._error("不允许的文件类型", 400)
                dirs = backend.export_dirs()
                d = dirs.get(storage)
                if d is None:
                    return self._error("存储不可用或未启用 images 内容", 400)
                path = d / name
            elif kind == "cloud":
                path = _resolve_import_disk(storage, name, "cloud")
            else:
                path = _resolve_import_disk(storage, name, "disk")
            if not path.is_file():
                return self._error("磁盘镜像不存在", 404)
            path.unlink()
            # 同步删除伴随的元数据侧车（<name>.meta.json）
            try:
                (path.parent / (path.name + ".meta.json")).unlink()
            except OSError:
                pass
        except pve_api.PveError as e:
            return self._error(str(e), 400)
        except OSError as e:
            return self._error(f"删除失败: {e}", 500)
        return self._json({"ok": True, "name": name})

    def _handle_template_cache_warmup(self):
        """把某个模板预热到指定节点：在目标节点本机盘上拉取一份缓存。

        只对 file: 来源的模板有意义（vmid: 模板 VM 本就落在节点本地盘，无需预热）。
        走 jobs 异步执行——拉 10G 级模板可能要十几分钟，必须能在「执行日志」里看进度、可强杀。
        """
        body = self._read_body()
        node = self._resolve_node(body.get("node"))
        tpl = str(body.get("template", "")).strip()
        if not tpl.startswith("file:"):
            raise toolbox.ToolError("预热只支持「虚机模板文件」来源；PVE 模板 VM 本身就在节点本地盘上，无需预热")
        src = tpl[len("file:"):]
        # 路径只做形态校验，真正防注入靠 shlex.quote；这两条用于拦住明显异常的输入
        if not src.startswith("/") or "\n" in src or "\x00" in src:
            raise toolbox.ToolError("模板路径不合法")
        # 延迟导入：避免 server 启动期就把工具模块全量拉起（与其它工具的按需加载保持一致）
        from core.tools.toolbox_common import COMMON_HELPERS
        script = COMMON_HELPERS + "\n" + _template_warmup_script(src)
        job_id = jobs.submit("template_cache_warmup", "预热模板缓存",
                             script=script, env_params={}, node=node)
        return self._json({"job_id": job_id})

    def _handle_template_cache_expire(self):
        """按保留天数清理指定节点的过期缓存（仅删超期条目）。"""
        body = self._read_body()
        node = self._resolve_node(body.get("node"))
        try:
            r = _template_cache_dispatch(node, "_node_template_cache_expire")
            return self._json(r)
        except (RuntimeError, OSError) as e:
            return self._error(str(e), 400)

    def _handle_node_storage_fix(self, node):
        """对已加入集群的节点重跑共享存储接入：返回 {ok,msg}，失败不抛错（前端按告警展示）"""
        ok, msg = self._fix_node_storage(node)
        return self._json({"node": node, "ok": ok, "msg": msg})

    def _handle_node_nfs_server_fix(self, node):
        """重跑 NFS 服务端导出（装服务 + 写 /etc/exports + exportfs + 起服务）：失败不抛错（前端按告警展示）"""
        ok, msg = self._fix_nfs_server(node)
        return self._json({"node": node, "ok": ok, "msg": msg})

    def _handle_node_storage_fix_all(self):
        """POST /api/nodes/storage-fix-all  一键修复集群内所有节点的共享存储。

        导出端必须先就绪、客户端才挂得上，故按角色排序（server 优先）后逐台调用
        _fix_node_storage（内部按角色自动分流）。离线 / 占位条目状态未知、SSH 必然超时，
        直接跳过并单列，避免一台把整批拖住。逐台结果一次性返回，供前端展示。
        """
        nodes = _merge_cluster_nodes(collect_nodes_cached() or [])
        try:
            cfg = storage_provision._load_config()
        except Exception:
            cfg = None
        targets, skipped = [], []
        for n in nodes:
            name = str(n.get("node") or "")
            if not name:
                continue
            # 占位条目 = pvesh 未返回该节点（集群降级时由 corosync 补出），状态未知
            if n.get("placeholder") or str(n.get("status") or "").lower() == "offline":
                skipped.append(name)
                continue
            role = _storage_role(name, str(n.get("ip") or n.get("address") or ""), cfg)
            if role == "dir":
                continue        # 本地 dir 模式：没有共享存储可修
            targets.append((0 if role == "server" else 1, name, role))
        targets.sort(key=lambda t: (t[0], t[1]))
        results = []
        for _order, name, role in targets:
            try:
                ok, msg = self._fix_node_storage(name)
            except Exception as e:  # noqa: BLE001  单台失败不影响其余节点
                ok, msg = False, str(e)[:300]
            results.append({"node": name, "role": role, "ok": ok, "msg": msg})
            jobs.record_event("node_storage_fix_all", f"一键修复共享存储 · {name}", name, ok,
                              [msg or ("修复完成" if ok else "修复失败")])
        done = sum(1 for r in results if r["ok"])
        if not results:
            msg = "没有需要修复的节点（未启用 NFS 共享存储，或节点均离线）"
        elif done == len(results):
            msg = f"全部 {done} 台节点修复完成"
        else:
            msg = f"{len(results) - done} 台节点修复失败，详见逐台结果"
        return self._json({"ok": done == len(results), "msg": msg,
                           "total": len(results), "done": done,
                           "skipped": skipped, "results": results})

