from __future__ import annotations
from typing import Any
"""集群与仲裁（成员表、pvecm/corosync 状态、预期票数）。"""

import copy
import json
import os
import re
import sys
import threading
import time

from core.pve.api.base import (  # noqa: F401
    PveError,
    _corosync_node_ips,
    _run_pvesh,
    _run_sbin,
)

COROSYNC_CONF_FILE = "/etc/pve/corosync.conf"
MEMBERS_FILE = "/etc/pve/.members"            # pmxcfs 维护的集群成员表（失仲裁只读时仍可读）
QUORUM_CACHE_TTL = 5
_CLUSTER_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{0,63}$")
_STATUS_FIELDS = {
    "quorate": re.compile(r"^Quorate:\s*(\S+)", re.M),
    "expected": re.compile(r"^Expected votes:\s*(\d+)", re.M),
    "votes": re.compile(r"^Total votes:\s*(\d+)", re.M),
    "quorum": re.compile(r"^Quorum:\s*(\d+)", re.M),
}
_quorum_cache = {"ts": 0.0, "data": None}
_quorum_lock = threading.Lock()


def normalize_cluster_name(name: str) -> Any:
    """校验集群名并返回归一化后的值；不合法抛 PveError（供创建集群与前端预校验同一口径）。"""
    name = str(name or "").strip()
    if not _CLUSTER_NAME_RE.match(name):
        raise PveError("集群名不合法：须字母开头，只能包含字母、数字、. - _，最长 64 个字符")
    return name


def create_cluster(name: str) -> Any:
    """在本节点创建集群（等价 pvecm create <名>）。

    仅能在「尚未加入任何集群」的节点上执行（PVE 侧对已有集群会拒绝）；集群名创建后不可更改，
    是否真的要执行由调用方负责确认。"""
    name = normalize_cluster_name(name)
    return _run_pvesh("create", "/cluster/config", ["--clustername", name])


def cluster_name() -> Any:
    """当前集群名；未加入任何集群时返回空串。

    `/cluster/status` 里 `type == "cluster"` 的那条即集群本身（独立单机没有这条），
    比解析 `pvecm status` 的文本输出稳定。"""
    try:
        data = _run_pvesh("get", "/cluster/status", quiet=True) or []
    except Exception:  # noqa: BLE001
        return ""
    for item in data:
        if isinstance(item, dict) and str(item.get("type")) == "cluster":
            return str(item.get("name") or "")
    return ""


def _read_cluster_members() -> Any:
    """读 /etc/pve/.members，返回 (cluster_dict, nodelist_dict)；解析失败返回 ({}, {})。

    pmxcfs 会把集群名、是否 quorate、各节点 id/ip/online 写在该文件里：比 pvesh /cluster/status
    更轻，且集群失去仲裁（只读）时依旧可读 —— 正是「已经建不了机」时最需要它的场景。"""
    try:
        with open(MEMBERS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    cluster = data.get("cluster") if isinstance(data.get("cluster"), dict) else {}
    nodes = data.get("nodelist") if isinstance(data.get("nodelist"), dict) else {}
    return cluster, nodes


def _pvecm_status() -> Any:
    """解析 `pvecm status` 的 quorate / expected / votes / quorum；非集群或解析失败返回 {}。"""
    proc = _run_sbin(["pvecm", "status"])
    text = ((proc.stdout or "") + (proc.stderr or "")) if proc else ""
    if not text:
        return {}
    out = {}
    for key, rx in _STATUS_FIELDS.items():
        m = rx.search(text)
        if m:
            out[key] = m.group(1).strip()
    if "quorate" in out:
        out["quorate"] = str(out["quorate"]).lower() in ("yes", "1", "true")
    for key in ("expected", "votes", "quorum"):
        try:
            out[key] = int(out[key])
        except (KeyError, TypeError, ValueError):
            out.pop(key, None)
    return out


def _corosync_conf_matches(pattern: str) -> Any:
    """corosync.conf 是否命中正则（探测 QDevice 设备块 / auto_tie_breaker）。"""
    try:
        with open(COROSYNC_CONF_FILE, "r", encoding="utf-8", errors="replace") as fh:
            return bool(re.search(pattern, fh.read(), re.M))
    except OSError:
        return False


def _corosync_members() -> Any:
    """当前 corosync 成员节点名列表（取自 `corosync-quorumtool -l`）；取不到返回 []。

    为什么不用 /etc/pve/.members 当主来源：那是 pmxcfs 维护的视图，集群失仲裁（只读）时可能滞后，
    而「哪个节点还在」正是本功能唯一的判据；quorumtool 直接问 corosync，最贴近事实。
    输出形如：
        Nodeid      Votes    Qdevice Name
    0x00000001          1         NR pve01 (local)
    """
    proc = _run_sbin(["corosync-quorumtool", "-l"])
    text = (proc.stdout or "") if proc else ""
    names = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or not re.match(r"^(0x[0-9a-fA-F]+|\d+)$", parts[0]):
            continue                       # 跳过标题/分隔线
        name = parts[-1]
        if name == "(local)" and len(parts) >= 4:
            name = parts[-2]               # 带 " (local)" 后缀时取前一个词
        names.append(name)
    return names


def quorum_status(fresh: bool = False) -> Any:
    """集群仲裁状态 + can_degrade（服务端强校验结论）。

    返回 in_cluster / quorate / expected / votes / quorum / total_nodes / online / offline /
    qdevice / atb / can_degrade / needs_restore / reason。带 5 秒缓存（每次起两个子进程）；
    改/还原票数需立刻见效的调用方传 fresh=True 强制重取。"""
    now = time.time()
    with _quorum_lock:
        cached = _quorum_cache["data"]
        if not fresh and cached is not None and now - _quorum_cache["ts"] < QUORUM_CACHE_TTL:
            return copy.deepcopy(cached)
    info = _quorum_status_uncached()
    with _quorum_lock:
        _quorum_cache["ts"] = time.time()
        _quorum_cache["data"] = copy.deepcopy(info)
    return info


def _quorum_status_uncached() -> Any:
    info = {
        "in_cluster": False, "quorate": False,
        "expected": 0, "votes": 0, "quorum": 0, "total_nodes": 0,
        "online": [], "offline": [], "qdevice": False, "atb": False,
        "can_degrade": False, "needs_restore": False, "reason": "",
    }
    if not os.path.exists(COROSYNC_CONF_FILE):
        info["reason"] = "本机未加入集群：独立单机不存在仲裁问题，无需处理。"
        return info
    info["in_cluster"] = True
    info["qdevice"] = _corosync_conf_matches(r"^\s*device\s*\{")
    info["atb"] = _corosync_conf_matches(r"^\s*auto_tie_breaker\s*[:\{]")

    cluster, nodelist = _read_cluster_members()
    members = _corosync_members()                           # corosync 当前成员（权威的"谁还在"）
    conf_nodes = sorted(_corosync_node_ips().keys())        # corosync.conf 里的全部集群节点
    all_nodes = sorted(set(conf_nodes) | set(nodelist.keys()) | set(members))
    if members:
        online = sorted(set(members) & set(all_nodes))
    else:
        # 兜底：quorumtool 不可用（corosync 未跑等）时用 pmxcfs 的成员表
        online = sorted(n for n, v in nodelist.items() if isinstance(v, dict) and v.get("online"))
    info["online"] = online
    info["offline"] = [n for n in all_nodes if n not in set(online)]
    info["total_nodes"] = len(all_nodes)

    st = _pvecm_status()
    info["quorate"] = bool(st.get("quorate")) if "quorate" in st else bool(cluster.get("quorate"))
    info["expected"] = int(st.get("expected") or 0) or info["total_nodes"]
    # 票数优先用 votequorum 的实时总票数（比数节点名更可靠）；取不到再退回成员数
    info["votes"] = int(st.get("votes") or 0) or len(online)
    info["quorum"] = int(st.get("quorum") or 0) or (info["expected"] // 2 + 1)
    # 运行时预期票数低于集群节点总数 = 曾被「临时取得仲裁」下调过（重启 corosync 即自动消失）
    info["needs_restore"] = bool(info["total_nodes"] and info["expected"] < info["total_nodes"])

    if info["qdevice"]:
        info["reason"] = "已部署 QDevice 仲裁设备：分区由设备安全裁决，禁止手工调整仲裁。"
    elif info["atb"]:
        info["reason"] = "已启用 auto_tie_breaker：corosync 会按确定性规则保留一侧仲裁，禁止手工调整。"
    elif info["quorate"]:
        info["reason"] = ""
    elif not info["total_nodes"] or (not members and not nodelist):
        info["reason"] = "未能读取集群成员表，无法确认在线节点数，出于安全考虑禁止手工调整仲裁。"
    elif info["votes"] < 1:
        info["reason"] = "未检测到任何在线票数（corosync 可能未正常运行），请先检查集群服务。"
    elif info["votes"] * 2 != info["total_nodes"]:
        info["reason"] = ("当前在线 %d 票、集群共 %d 台：两侧票数不等，对面仍保有仲裁，"
                          "手工降级会造成集群双写，已禁止。请先确认失联节点是真实宕机而非网络分区。"
                          % (info["votes"], info["total_nodes"]))
    else:
        info["can_degrade"] = True
        info["reason"] = ("在线 %d 票、不可达 %d 台（两侧票数相等）：对面票数低于达标门槛，"
                          "降级后不会双写，可安全执行。"
                          % (info["votes"], len(info["offline"])))
    return info


def set_expected_votes(n: int) -> Any:
    """临时设置运行时预期票数（等价 `pvecm expected <n>`，底层 corosync-quorumtool -e）。

    只改运行时值：不写 corosync.conf、不删节点、不动虚机；corosync / pve-cluster 重启后自动回到
    配置值。n 必须 ≥ 1。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise PveError("预期票数必须是整数")
    if n < 1:
        raise PveError("预期票数不能小于 1")
    last = ""
    for cmd in (["pvecm", "expected", str(n)], ["corosync-quorumtool", "-e", str(n)]):
        proc = _run_sbin(cmd)
        if proc is None:
            continue                       # 命令不存在 → 试下一个
        if proc.returncode == 0:
            # 票数已变：立即失效缓存，避免调用方在 TTL 内读到"改之前"的状态
            with _quorum_lock:
                _quorum_cache["data"] = None
            return n
        last = (proc.stderr or proc.stdout or "").strip() or last
        print(f"[pvecm 失败] {' '.join(cmd)}\n{last}", file=sys.stderr)
    raise PveError("调整仲裁票数失败：" + (last[:200] or "请确认本机 corosync / pve-cluster 服务正常"))
