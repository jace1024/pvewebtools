#!/usr/bin/env python3
"""PVE Web 管理工具入口。

纯标准库实现（http.server），无任何第三方依赖，直接在 PVE
宿主机上用系统 Python 运行：  python3 -m core.web.server [端口]

仅限 PVE 宿主机部署：未检测到 pvesh 时拒绝启动。
"""

import json
import os
import re
import shlex
import shutil
import lzma
import gzip
import urllib.request
import urllib.error
import urllib.parse
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---- 包路径引导：允许 `python3 core/web/server.py` 直接以文件方式启动 ----
# 直接按文件路径运行时，sys.path[0] 是 core/web/（而非仓库根），且 Python 会把 core / core.web
# 当作「命名空间包」(无 __init__、报错显示 unknown location) 提前缓存进 sys.modules，导致
# `from core import paths` 解析到被错误缓存的 core。这里在非模块启动（__package__ 为空）时把仓库根
# 补进 sys.path[0]，并清除可能被错误缓存的 core* 条目，强制按新路径重新解析。
# 注意：`-m core.web.server` 形式 __package__ 非空，不走此分支（模块方式本就能正确解析）。
if __package__ in (None, ""):
    _PKG_ROOT = str(Path(__file__).resolve().parents[2])
    if _PKG_ROOT not in sys.path:
        sys.path.insert(0, _PKG_ROOT)
    for _m in ("core", "core.web", "core.web.server"):
        sys.modules.pop(_m, None)

from core import paths
from core.pve import pve_api, storage_provision
from core.tools import toolbox
from core.web import jobs

backend = pve_api


from core.web.http_util import (  # noqa: F401  # 输出净化 + HTTP 基础设施
    MAX_BODY_SIZE,
    HttpUtilMixin,
    _DEBUG_DETAIL,
    _DEBUG_HINT,
    _sanitize_out,
    _sanitize_text,
)
from core.web.gate import GateMixin  # 写操作闸口
from core.web.static_srv import StaticMixin  # 首页/静态资源
from core.web.sse import SseMixin  # 任务日志实时推送 SSE
from core.web import routers  # 路由表分发协议（各域见 routers/）
from core.web.routing import (
    RoutingMixin,
)
# 注：启动入口在 core.web.boot（见文件末尾 __main__ 分支）；这里**不能**模块级 import 它，
# 否则 boot -> server -> boot 成环。
from core.web.ops import (
    OpsMixin, _REMOTE_LOCAL_IPS, _node_mgmt_ip,
)
from core.web.maintenance import CHUNK_SWEEP_INTERVAL, CHUNK_TTL, NODES_REFRESH_INTERVAL, _cleanup_orphan_job_tmps, _cleanup_orphan_parts, _download_error_text, _enrich_nodes_slow, _nodes_refresh_loop, _resume_incomplete_uploads, _sweep_stale_chunks
from core.web.template_cache import TEMPLATE_CACHE_DIRNAME, _cache_entry_kind, _cache_entry_name_ok, _cache_lock_busy, _path_is_remote_fs, _read_cache_meta, _template_cache_limit_mb, _template_cache_max_age_days, _template_cache_max_mb, _template_cache_max_ratio, _template_cache_root
from core.web.handlers import (
    HandlerMixin, APLINFO_PATH, BACKUP_TMP_DIR, CLOUD_MIRRORS,
    DEFAULT_CLOUD_IMAGE_CONFIG, DEFAULT_CLOUD_IMAGE_PRESETS, DEFAULT_CT_SOURCE, DEFAULT_MIRROR_REPO_SUPPORT,
    DISKS_LOST_TTL, DISK_TMP_DIR, LXC_FALLBACK_CT_ROOTS, LXC_SECTION_RE, LXC_TEMPLATE_MIRROR_IDS,
    LXC_TEMPLATE_NAME_RE, LXC_TURNKEY_MIN_DEBIAN, LXC_TURNKEY_VER_RE, MAX_BACKUP_SIZE, MAX_DISK_SIZE,
    MAX_VBIOS_SIZE, MAX_VZTMPL_SIZE, TURNKEY_DOMESTIC_ROOTS, TURNKEY_MIRROR_HOSTS,
    TURNKEY_OFFICIAL_ROOT, VBIOS_TMP_DIR, VZTMPL_TMP_DIR,
    _BACKUP_EXT_WHITELIST, _CLOUD_COMP_EXTS, _CLUSTER_SRC_TTL, _DOCKER_VER_CACHE, _DOCKER_VER_LOCK,
    _DOCKER_VER_TTL, _DOCKER_VER_URLS, _EFI_PART_TYPE, _LXC_TEMPLATE_MIRROR_LABELS, _MERGE_LOCK,
    _MERGE_STATE, _MS_RESERVED_TYPE, _UPLOAD_INDEX_PATH, _UPLOAD_SIZE_LIMITS, _VBIOS_EXT_WHITELIST,
    _WIN_RECOVERY_TYPE, _allowed_backup_ext,
    _allowed_disk_ext, _allowed_vbios_ext, _allowed_vztmpl_ext, _backup_dirs_merged,
    _backup_lock, _backup_template_dir, _blocked_cloud_url, _board_hw_cache, _chunks_complete, _classify_os,
    _cloud_image_dir, _cluster_src_cache, _cluster_src_lock,
    _cpu_topo_cache, _decompress_file, _disk_image_dir, _disk_lock, _disks_baseline, _disks_lost, _do_merge,
    _docker_ver_fetch, _enrich_node_slow, _fmt_size, _host_arch, _http_alive, _hw_cache, _image_virtual_size,
    _is_cloud_compressed, _list_docker_versions_base, _list_template_backup_files, _lxc_mirror_root,
    _lxc_template_available, _lxc_template_url, _lxc_template_urls, _lxc_turnkey_roots, _maybe_merge_async,
    _merge_backup_chunks, _merge_cluster_nodes, _merge_disk_chunks, _merge_key, _merge_state,
    _merge_vbios_chunks, _merge_vztmpl_chunks, _merged_preset_config, _node_backup_files,
    _node_cpu_topology, _node_disks_lost, _node_from_query, _node_hw_info, _node_online, _nodes_cache,
    _nodes_cache_lock, _normalize_ct_base, _other_os_disk_names, _pci_bd, _phys_disk_of,
    _pve_active_vgs, _pve_storage_disk_names, _pve_system_disk_names, _pveam_bin, _remove_vbios_manifest,
    _resolve_import_disk, _resolve_lxc_section, _rootdir_storages_local, _ssh_read_ct_source,
    _storage_options, _storage_role, _template_cache_dispatch, _template_cache_nodes,
    _template_warmup_script, _upload_index_lock, _upload_index_read, _upload_index_write,
    _upload_received_indexes, _upload_size_error, _upload_target_path, _upload_tmp_dir, _vbios_lock,
    _vztmpl_lock, _zfs_pool_disks, apply_cloud_mirror, cloud_image_import_path, cloud_image_mirrors,
    collect_nodes, collect_nodes_cached, list_backup_storages, list_byid_disks, list_cloud_storages,
    list_disk_storages, list_docker_versions, list_image_storages,
    list_imported_disks, list_lxc_downloaded, list_lxc_storages, list_lxc_templates, list_phys_disks,
    list_vbios_roms, list_vm_disks,
    list_vztmpl_storages, load_cloud_image_presets, lxc_batch_swap, lxc_cluster_sources, lxc_ct_source,
    lxc_template_base, lxc_template_dest, lxc_template_mirrors, node_board_hw, probe_cloud_image_presets,
    reset_cloud_image_presets, save_cloud_image_presets, upload_instant_hit, upload_instant_register,
)
from core.web.vbios import VBIOS_LEGACY_MANIFEST, VBIOS_TARGET_DIR, _add_vbios_manifest, _link_vbios, _migrate_legacy_vbios, _read_legacy_vbios_manifest, _read_vbios_manifest, _vbios_is_managed, _vbios_manifest_path, _vbios_remove_remote, _vbios_shared_dir, _vbios_sync_to_nodes, _vbios_target_blocked, init_vbios_local
from core.web.probe import INTEL_IGPU_GENERATIONS, _AMD_DGPU_RE, _AMD_IGPU_RE, _GPU_PASSTHROUGH_HINTS, _INTEL_DGPU_RE, _INTEL_IGPU_INDEX, _PCI_CLASS_TAGS, _PCI_GENERIC_EXCLUDE, _PCI_MSIX_INCOMPAT, _SSH_CTRL, _amd_igpu_by_id, _collect_source, _gpu_kind, _gpu_passthrough_hint, _gpu_vendor_device, _host_used_disk_names, _host_used_ifnames, _iommu_group_size, _list_backup_dir, _list_pci_class, _local_access_pred, _nic_link_speed, _parse_interfaces, _pci_class_tag, _pci_storage_host_used, _run_func_on_node, _run_func_on_node_args, _running_kernel_signed, _ver_key, intel_igpu_generation, list_available_kernels, list_grub_backups, list_installed_kernels, list_net_backups, list_network_bridges, list_network_vlans, list_pci_generic, list_pci_gpus, list_pci_nics, list_pci_storage, list_phys_nics, list_usb_devices, list_vfio_orphans
from core.web.host_util import _is_cluster_node, _local_outbound_ip, _norm_confirm, _shared_storages


class SilentHTTPServer(ThreadingHTTPServer):
    """吞掉客户端中途断开（取消下载/关闭页面）产生的 BrokenPipe/连接重置异常，并避免把
    其它异常按 BaseHTTPRequestHandler 默认行为打印完整栈追踪（栈里含 .py 路径与解释器细节）。
    细节仅在 --debug 模式下输出（见 _DEBUG_HINT）。"""
    def handle_error(self, request, client_address):  # noqa: ARG002
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        if _DEBUG_DETAIL:
            super().handle_error(request, client_address)
            return
        sys.stderr.write("[error] 请求处理异常" + _DEBUG_HINT + "\n")


# 远程节点 SSH 连接复用（ControlMaster）：同一节点复用已建立的连接，避免逐项采集重复握手
# 备份文件分片上传：分片落临时目录，收齐后合并进备份存储目录
# 磁盘镜像分片上传：分片落临时目录，收齐后合并进导入目录（供 qm importdisk 使用）
# LXC/CT 容器模板分片上传：分片落临时目录，收齐后合并进所选存储的 template/cache/（PVE 约定目录，落盘即可建 CT）
# 云镜像下载支持自动解压的压缩格式（单层压缩的单一磁盘镜像）
# 云镜像下载源：切换下载源 = 保留预设 URL 路径、仅替换域名；首项即默认项。
# 默认中科大 USTC：已实测预设列表全部条目（含原阿里云 AlmaLinux、原南大 CentOS 7/8）
# 改写到中科大后均可下载。注意中科大是按需同步，个别冷门文件首次请求会 404 触发同步，重试即 200。
# ---- LXC(CT) 模板：可用列表（pveam）与后台下载 ----
# 模板文件名形如 debian-13-standard_13.6-1_amd64.tar.zst；严格校验防路径穿越与任意落盘
# PVE 的 CT 模板索引地址写在 APLInfo.pm（「更换软件源和 LXC 源」工具改的就是它）
# 可选 CT（LXC 模板）下载源：仅收录实测确实提供模板文件的镜像站（逐站探测结果）：
#   中科大 USTC  https://mirrors.ustc.edu.cn/proxmox/images/…            200，提供模板
#   清华 TUNA    https://mirrors.tuna.tsinghua.edu.cn/proxmox/images/…   200，提供模板
#   浙大 ZJU     /proxmox/… 对任意路径都回 200 + 镜像站首页 HTML（SPA 兜底），无任何模板文件
#   山东大学 SDU /proxmox/… 同上（200 + 首页 HTML）
# 后两者会把「错误页」伪装成下载成功，比诚实 404 更有害，故不再作为可选下载源列出。
# ---- 集群 CT 模板源一致性检测（供「获取 LXC 模板」下载源处给出不一致提示）----
# section 名只允许字母数字下划线连字符（拼 URL 前校验，防路径穿越）
# TurnKey 应用模板（section=turnkeylinux）不在 Proxmox 源的 images/ 下：download.proxmox.com 及其
# 国内镜像的 images/ 里都已没有 turnkeylinux/ 目录（上游下架），直接拼 <CT源根>/images/turnkeylinux/
# 必 404。该段放在 TurnKey 自己的目录：<镜像根>/turnkeylinux/images/proxmox/<文件名>。
# 实测只有两处真正镜像了该目录，且都只剩 debian-11 / debian-12（debian-10 段已整体清理）：
#   https://mirrors.ustc.edu.cn        200，225 个文件（国内，主用）
#   https://mirror.turnkeylinux.org    200，225 个文件（TurnKey 官方，最终兜底）
# 清华/浙大该路径 404（未镜像该目录），SDU 回 200 + 站点首页 HTML（SPA 兜底）——两者都不列入候选：
# 前者只是白跑一次回退，后者会拿 HTML 冒充模板，比诚实 404 的危害更大。
# 因此候选为「所选源所在镜像站（若确为有效站）→ 中科大 → TurnKey 官方」，下载时逐个回退尝试。
# 这些镜像站的 CT 源（…/proxmox）与 TurnKey 目录同站同根，可从所选源直接推导出镜像根
# 常规分类（system 等）的回退 CT 源根：所选源优先，缺文件时依次回退其它实测可用站与官方源。
# 索引里的模板偶尔会在某个镜像站缺失（各站同步进度不一），逐个回退可避免直接失败。
# TurnKey 应用模板：上游 aplinfo 索引里残留了 11 个 debian-10 老条目（collabtive / concrete5 /
# drupal8 / ezplatform / foodsoft / magento / moinmoin / mongodb / processmaker /
# revision-control / trac），这些应用已被 TurnKey 上游移除（新版索引里 0 条），实测在中科大与
# TurnKey 官方镜像上也没有对应文件，选中必然 404。这里直接从列表剔除，避免用户选到注定失败的模板。
# 其余分类（system 等）索引与镜像完全一致，不做过滤。
# ---- Docker 引擎稳定版清单（「一键安装Docker」版本下拉数据源） ----

# 依次尝试的版本清单源：docker-ce/linux/static/stable/<arch>/ 目录列表。
# 静态 tgz 与发行版无关、覆盖该仓库已发布的全部稳定版；国内镜像优先。
# 上传文件扩展名白名单：仅允许镜像/备份类文件，拒绝脚本/可执行文件/任意二进制
# vBIOS ROM 上传：只允许 .rom/.bin 固件类文件（显卡直通挂载用），落到 PVE 标准目录 /usr/share/kvm/
# 本工具上传的 vBIOS 文件名清单（落盘在固件目录内的隐藏文件）。
# /usr/share/kvm/ 下还混有大量 QEMU 系统固件（vgabios-*.bin / efi-*.rom / seabios.bin 等），
# 用黑名单永远追不全；改为只认"本工具上传过的文件"，下拉就不会再出现系统固件。
#
# 跨节点：vBIOS 主副本放在 pvewebtools 共享存储的 vbios/（NFS 共享，目录名自描述），
# 清单也写在共享存储（集群统一）；本机 /usr/share/kvm/ 仅放指向主副本的软链（PVE romfile 硬约束）。
# 分片残留自动清理：后台线程定时扫描临时目录，删除超过 N 秒未更新的分片目录
# （上传中断/浏览器刷新残留的半截分片）。正常上传会持续刷新目录 mtime，不会被误删。
# ── 后台异步合并：分片收齐即响应，合并交给后台线程 ─────────────────────────────
# 以前合并在「最后一片」的请求里同步做：4GB 文件会让进度条卡在 100% 干等十几秒到一分钟。
# 现在收齐就丢给后台线程，请求立刻返回 merging=True；前端轮询 /api/upload/status 直到
# done/error。合并期间同一进程还能继续接收下一个文件的分片。
# 分片上传的单文件上限（与前端 TC_UPLOAD_TYPES[].maxSize 保持一致）。
# 前端选文件时就会拦，这里是第二道：即便绕过前端直调接口，也在「第一片」就被拒，
# 不必把几 GB 传完才在合并阶段报错。
# ── 秒传（指纹去重）：上传前先问「这个文件是否已在本节点上传过、且目标文件仍在」────────
# 指纹由前端算（大小 + 首尾 1MB 的快速哈希，非加密），服务端只负责登记与比对、不实现算法 ——
# 两端不会因算法差异而永远不命中。命中即跳过整个上传（0 字节传输）。
# 清单落本机 /etc/pvewebtools/upload-index.json：秒传仅在「本节点 + 目标文件仍存在且大小一致」
# 时生效，换节点或文件被删都只是不命中（退回正常上传），不会误判。

# static 资源目录：由 paths.RESOURCE_DIR 统一解析（打包解压目录 / 源码包目录）
STATIC_DIR = paths.RESOURCE_DIR / "static"
# 默认监听端口（命令行参数缺省时用）
DEFAULT_PORT = 8899
# 运行期实际监听端口（boot.main() 启动时写入）：升级模块做健康检查与单元端口解析兜底用
RUN_PORT = DEFAULT_PORT

# 云镜像预设源：可编辑 JSON 配置（config/cloud-image-presets.json），缺省回退内置默认值。
# 设计为「项目自定义源」：运维直接改 JSON 即可增减镜像，无需动代码；文件缺失时自动物化默认配置便于编辑。
CONFIG_DIR = paths.RESOURCE_DIR / "config"
CLOUD_PRESETS_PATH = CONFIG_DIR / "cloud-image-presets.json"

# 内置默认预设（与 config/cloud-image-presets.json、static/app.js 的 CLOUD_IMAGE_PRESETS_FALLBACK 三处保持一致；
# Fedora 构建号、Alpine 小版本均已核对直链。以下 20 条地址已逐条 HEAD 实测 200，且全部落在国内镜像站：
#   - Ubuntu / Debian / Fedora / Rocky / CentOS Stream / Alpine → 中科大
#   - AlmaLinux → 阿里云
#   - CentOS 7.6 / 8.4（EOL）→ 南京大学（中科大 centos-cloud 只收录 9/10-stream、scos，7、8 目录已下架，原地址 404）
# 换源可用性表（镜像×发行版）：仅 Anolis / openEuler 路径非标、各镜像站收录情况不一，需门控；
# 其余标准发行版（ubuntu/debian/alpine/centos-stream/rocky/almalinux/fedora）五大镜像均收录，无需门控。
# 与 config/cloud-image-presets.json 的 mirror_repo_support 字段保持一致；腾讯云镜像暂未确认收录，保守排除。
# 配置文件整体结构：{presets:[...], mirror_repo_support:{...}}；旧版纯数组文件仍兼容（仅含 presets）。
def load_mirror_repo_support():
    """读取换源可用性表（镜像×发行版，合并覆盖目录后的结果）；缺省回退内置默认。运维改 JSON 即可调整，无需动脚本。"""
    return _merged_preset_config()["mirror_repo_support"]


# 节点硬件信息缓存：CPU 型号/内核版本几乎不变，没必要每次轮询都调 pvesh
# 主板/芯片组型号探测缓存：硬件信息几乎不变，长 TTL
# /api/nodes 采集要跑一串外部命令，结果短缓存：前端 10 秒一轮询，多开页面/并发请求不重复采集
# 概览采集改为「后台异步预热 + 前台只读缓存」：
# - 后台守护线程每 NODES_REFRESH_INTERVAL 秒静默采集一次，更新缓存；
# - 前端 10 秒轮询永远命中缓存、毫秒级返回，不因采集慢而阻塞；
# - 手动刷新走 force=True 同步重采最新数据。
# 采集周期与慢命令（sensors/smartctl 等秒级）匹配，避免后台过于频繁。
# 掉盘告警：对比前后两次物理盘枚举，消失的盘持续告警；回归自动解除，
# 超过 10 分钟未回归则自动消除（视为人为拔盘/直通等预期变更）
# PCI 类代码 → 中文类别标签（长前缀在前，避免 "04" 抢匹配 "0401"）
# ─── 建机/还原数据源的节点本机缓存，面板名「缓存加速」（与工具箱 COMMON_HELPERS 的 pw_cache_* 配套）───
# 缓存由建机 / 还原脚本在**节点本机盘**上维护，目录布局见 toolbox_common.pw_cache_ensure：
#   <root>/<name>               缓存文件本体（源文件的一份完整副本）
#   <root>/.meta/<name>.meta    元数据 src/size/mtime/fetched；meta 的 mtime = 最后使用时间（LRU 依据）
#   <root>/.locks/<name>.lock   拉取锁，被占用说明此刻有任务正在拉这个文件
#   <root>/<name>.part.<pid>    拉取中的中间文件（完成后原子改名，崩溃残留由下次 GC 清理）
# 界面只做「查看 / 删除」，读删都在这里完成，所以根目录解析必须与 bash 侧一致
# （local 存储根 → pvewebtools-cache），否则界面看到的目录会和真实落点对不上。

# 缓存淘汰策略（与工具箱 COMMON_HELPERS 的 pw_cache_effective_max_kb / pw_cache_purge_expired 口径一致）：
#   占用比例上限（盘总容量的比例）为主上限，保留天数为时间淘汰；二者均可在环境变量覆盖。
#   PT_TEMPLATE_CACHE_MAX_RATIO（默认 0.2）、PT_TEMPLATE_CACHE_MAX_AGE_DAYS（默认 30）、
#   PT_TEMPLATE_CACHE_MAX_MB（默认 0 = 不设，仅作比例之上的硬天花板）。


# 已知 PCIe 直通（vfio）MSI-X 布局不兼容、QEMU 启动报
# "failed to add PCI capability 0x11 ... table & pba overlap" 的 NVMe 设备 vendor:device。
# 这些设备建议改用块映射 RDM（不经过 vfio/MSI-X，规避该硬件兼容问题）。
# GPT 分区类型 GUID：EFI 系统分区 / Microsoft 保留 / Windows 恢复环境
# 设备 ID → 代次条目 的反查索引（模块加载时构建一次）
# Intel 独立显卡特征：Arc 独显（Alchemist/DG2 的「Arc A 系列」、Battlemage/BMG 的「Arc B 系列」、
# Arc Pro）、数据中心 GPU（Ponte Vecchio）、DG1、ATS-M。
# 注意不能只按 "arc" 判定：Meteor Lake / Lunar Lake 的核显在 lspci 里写作「Intel Arc Graphics」，
# 只匹配 "arc" 会把它们误判成独显 → 用独显专有写法 "arc a<数字>" / "arc b<数字>" / "arc pro"。
# AMD 独显特征（先于核显特征判定，避免「Radeon RX Vega 64」被当成 APU 核显）：
#   RX/R 系列与 HD 5xxx~9xxx（末位须非字母，故 APU 的「Radeon HD 8650G」不会被误伤）、
#   Radeon Pro/VII、Vega 独显核心与 Frontier、Navi/Polaris/Tonga/Fiji/Hawaii 代号、
#   FirePro/Instinct/WX 专业卡
# AMD APU 核显特征：
#   通用集显命名（Radeon Graphics / Radeon Vega 3~11 这类 APU 集显 / 移动核显 660M~890M）
#   + 各代 APU 代号（Raven ~ Strix/Krackan，含桌面 APU Raphael/Granite Ridge）
# 已知直通易报 Code 43 的显卡型号 -> 推荐 hostpci 参数与说明。
# 匹配用 vendor:device（lspci desc 里的 [1002:67e3]），值为 {opts, hint}。
# AMD Polaris 专业卡（WX 4100 等）：SeaBIOS + 无 pcie=1 时驱动初始化失败报 Code 43，
#   实测需 pcie=1 + x-vga=1 + cpu hidden=1 才正常。
# NVIDIA 消费卡（GTX/RTX 等）：驱动主动检测虚拟机，需 cpu hidden 隐藏 KVM，部分还需 UEFI + vBIOS。
# 通用 PCIe 直通排除的大类：01 存储/02 网卡/03 显卡已有专用工具；06 桥接（主板桥/PCI 桥不可直通）、
# 08 系统外设（HPET/IOMMU）、0b 处理器类直通无意义且危险，一并隐藏避免误选
class ApiHandler(RoutingMixin, OpsMixin, HandlerMixin, GateMixin, StaticMixin, SseMixin, HttpUtilMixin, SimpleHTTPRequestHandler):
    """路由：/api/* 走 JSON 接口，其余落到 static 目录静态文件"""

    # 连接复用：默认 HTTP/1.0 是一次请求一条 TCP 连接，首屏十几个静态资源、概览页并发的
    # 多个 API、10 秒轮询都会被放大成几十次建连（每次还要新建服务线程）。开启 HTTP/1.1 后
    # 浏览器复用同一条连接串行发请求。前提是响应能自界定长度：静态/JSON 都由
    # `_send_static` / `_send_json` 带 Content-Length，SSE 流由 `Connection: close` 界定
    # （见 sse.py），其余错误响应走 `send_error` 自带 Content-Length，因此可安全开启。
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    # 各职责按 mixin 分工，本类只把它们拼起来：
    #   HttpUtilMixin（http_util.py）—— HTTP 基础设施与响应出口净化
    #   SseMixin（sse.py）           —— 任务日志实时推送
    #   RoutingMixin（routing.py）    —— 四个 HTTP 方法入口与路由分发
    #   StaticMixin（static_srv.py）  —— 首页与静态资源服务
    #   GateMixin（gate.py）         —— 写操作公共闸口

    # 允许被 /api/options 批量代理的只读下拉数据源白名单（避免被当成任意转发代理，最小暴露面）
    _OPTION_SOURCES = {
        ("guests",), ("pci",), ("pci", "nics"), ("pci", "gpus"), ("pci", "generic"),
        ("pci", "vfio"), ("usb", "devices"), ("kernels",), ("kernels", "available"),
        ("grub", "backups"), ("network", "backups"), ("network", "bridges"),
        ("network", "vlans"), ("network", "interfaces"), ("vbios", "roms"),
        ("disks",), ("disks", "byid"), ("disks", "storages"), ("disks", "imported"),
        ("disks", "vm"), ("storages", "images"), ("backup", "storages"), ("backup", "files"),
        ("storages",), ("lxc-template", "downloaded"),   # 创建 LXC 容器：系统盘存储 / 已下载模板下拉
        ("docker-versions",),   # 一键安装Docker：引擎稳定版版本下拉
        ("nodes",),   # 迁移：目标节点下拉（集群成员表，与执行节点无关，见 toolbox.NODE_AGNOSTIC_SOURCES）
    }

    # 各域的处理入口按域登记在 routers/（vm / storage / network / transfer / download /
    # toolbox / node / cluster / system / auth），本类只保留跨域共用的实现。
    # 后台下载进度表（_cloud_dl / _cloud_dl_cancel / _lxc_dl / _lxc_dl_cancel）在
    # core/web/ops.py 的 OpsMixin，以 type(self)._cloud_dl 访问。
    def log_message(self, fmt, *args):
        # 只记录 API 与错误，避免静态资源刷屏；args 可能含 HTTPStatus 枚举，先转字符串再判断
        joined = " ".join(str(a) for a in args)
        if "/api/" in joined or "code 4" in joined or "code 5" in joined:
            super().log_message(fmt, *args)


# 远端 shell 片段：列出目标机全部 IPv4。共享存储修复脚本用它自查「本机是否即导出端」，
# 从而在导出端跳过客户端挂载（避免把导出目录挂到自己身上）、在客户端拒绝误跑服务端导出。
# 旧版目录布局迁移（.pvewebtools/{import,vbios} -> cloud-images/ / vbios/）已并入
# storage_provision.migrate_legacy_layout()，由统一初始化入口 ensure_storage() 调用。


if __name__ == "__main__":
    from core.web.boot import main   # 启动入口在 boot.py；函数内导入以免模块级成环

    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C：启动阶段（如 pvesh 探测）被中断也会走到这里，统一干净退出、不打印堆栈
        print()
        print("已停止")
