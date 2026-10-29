from __future__ import annotations
from typing import Any
"""定时开关机任务（cron 读写与孤儿清理）。"""

import shutil
import socket
import subprocess

from core.pve.api.base import (  # noqa: F401
    PveError,
    ssh_host,
    ssh_host_opts,
)
from core.pve.api.node import (  # noqa: F401
    _SSH_CTRL,
)
from core.pve.api.vm import (  # noqa: F401
    list_guests,
)

SCHEDULE_CRON = "/etc/cron.d/pvewebtools-schedule"


def _read_cron(node: int) -> Any:
    """读取节点上的定时任务 cron 文件；文件不存在/SSH 失败/超时时返回空串（视为无任务）"""
    try:
        if node == socket.gethostname().split(".")[0]:
            with open(SCHEDULE_CRON, encoding="utf-8") as f:
                return f.read()
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
             *ssh_host_opts(node),
             f"root@{ssh_host(node)}", f"cat {SCHEDULE_CRON}"],
            capture_output=True, text=True, timeout=8)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _write_cron(node: int, text: str) -> None:
    """把定时任务内容写回节点的 cron 文件并重启 cron 让改动立即生效。

    写入前先备份为 .bak；远程节点走 SSH，内容经 stdin 传入（不拼进命令行，免转义问题）。
    仅清理路径使用：失败静默返回——清理失败不应让列表接口跟着报错。
    """
    try:
        if node == socket.gethostname().split(".")[0]:
            try:
                shutil.copy2(SCHEDULE_CRON, f"{SCHEDULE_CRON}.bak")
            except OSError:
                pass
            with open(SCHEDULE_CRON, "w", encoding="utf-8") as f:
                f.write(text)
            subprocess.run(["systemctl", "restart", "cron"],
                           capture_output=True, text=True, timeout=15)
        else:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *ssh_host_opts(node),
                 f"root@{ssh_host(node)}",
                 f"cp -a {SCHEDULE_CRON} {SCHEDULE_CRON}.bak 2>/dev/null; "
                 f"cat > {SCHEDULE_CRON}; systemctl restart cron"],
                input=text, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def prune_orphan_schedules(node: int) -> Any:
    """摘除指向已删除虚机的定时开关机任务块（自愈）。

    虚机被删除后，其 SCHEDULE_<vmid> 块仍残留在 cron 文件里：到点执行必然失败
    （qm start 找不到虚机，只在日志里留一行报错），前端还会一直显示这条幽灵任务。
    在读取列表前调用即可自动摘除；返回被清理的 VMID 列表，无残留时为空列表。
    """
    text = _read_cron(node)
    if not text.strip():
        return []
    # 集群内现存虚机的 VMID 集合：定时任务按 VMID 引用，VMID 已不存在即视为孤儿
    try:
        alive = {str(g.get("vmid")) for g in (list_guests() or []) if g.get("vmid")}
    except PveError:
        return []      # 虚机列表取不到时不擅自清理，避免误删正常任务
    if not alive:
        return []

    kept, orphaned, dropping = [], [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# PVEWEBTOOLS BEGIN SCHEDULE_"):
            vmid = s.rsplit("_", 1)[-1]
            dropping = vmid not in alive     # 虚机已不存在 → 整块待摘除
            if dropping:
                orphaned.append(vmid)
                continue
        elif s.startswith("# PVEWEBTOOLS END SCHEDULE_"):
            if not dropping:
                kept.append(line)
            dropping = False
            continue
        if not dropping:
            kept.append(line)
    if not orphaned:
        return []
    _write_cron(node, "\n".join(kept).rstrip("\n") + "\n")
    return orphaned


def list_schedules(node: int) -> Any:
    """解析指定节点的定时开关机任务；返回 [{vmid, gtype, start, stop}]，读不到返回空列表"""
    if not node.replace("-", "").isalnum():
        raise PveError(f"非法节点名: {node}")
    prune_orphan_schedules(node)   # 自愈：先摘掉指向已删除虚机的残留任务，避免幽灵任务常驻
    text = _read_cron(node)
    # cron 星期字段 → 友好中文名
    def days_cn(d: Any) -> Any:
        return {"*": "每天", "1-5": "工作日", "0,6": "周末"}.get(d, d)

    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# PVEWEBTOOLS BEGIN SCHEDULE_"):
            cur = {"vmid": line.rsplit("_", 1)[-1], "gtype": "", "paused": False,
                   "start_time": "", "start_days": "", "stop_time": "", "stop_days": ""}
            continue
        if line.startswith("# PVEWEBTOOLS END SCHEDULE_"):
            if cur:
                out.append(cur)
            cur = None
            continue
        if cur is None or not line:
            continue
        # 暂停的 cron 行：行首带 #（去掉后是合法 cron 行），记录 paused 状态
        paused = line.startswith("#")
        body = line[1:].strip() if paused else line
        fields = body.split()
        if len(fields) >= 9 and fields[0].isdigit():
            hhmm = f"{int(fields[1]):02d}:{int(fields[0]):02d}"
            days = fields[4]
            tool, act = fields[6].rsplit("/", 1)[-1], fields[7]
            if not cur["gtype"]:
                cur["gtype"] = "qm" if tool == "qm" else "pct"
            if paused:
                cur["paused"] = True
            if act == "start":
                cur["start_time"] = hhmm
                cur["start_days"] = days
            elif act in ("stop", "shutdown"):
                cur["stop_time"] = hhmm
                cur["stop_days"] = days
    return out
