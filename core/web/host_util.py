"""本机与集群节点识别辅助（M2/T2.6 深化：从 server.py 抽出的跨域共享层）。

这些函数被多个域模块（cluster / node / transfer / toolbox 等）共用，且不依赖 HTTP 层，
所以先落在共享模块里——否则域模块要用它们就得反向 import server，形成循环导入。
"""

import re
import socket

from core.pve import pve_api as backend
from core.pve import storage_provision


def _norm_confirm(s):
    """高危确认词归一化：忽略所有空白字符与大小写差异（与前端 normConfirm 同口径）。
    工具名常含空格与英文（如「一键安装 Hammer 同步服务器」），要求逐字（含空格/大小写）
    精确对照纯属折磨且易静默输错；忽略空白与大小写后仍必须完整输入整个名称，
    防误点/防自动化的效果不变。"""
    return re.sub(r"\s+", "", str(s or "")).lower()


def _shared_storages():
    """能被本机与目标节点当作「同一份数据」的存储 id 集合。

    两类：
    - PVE 标了 shared=1 的存储（NFS / Ceph 等网络存储）；
    - 项目工作区存储 pvewebtools：集群内各节点由本工具挂同一 NFS 导出（见 storage_provision /
      节点管理页的「修复 NFS 客户端」），同路径即同一份数据 —— 它在 PVE 里注册成 dir 型且不带
      shared，只按 shared 判会把「跨节点管理共享存储内容」这条主线退化成只读。
    local / local-lvm / local-zfs 等节点独占存储不在其中：它们 id 各节点同名，但内容各节点独立。"""
    out = {str(storage_provision.WORKSPACE_SID)}
    try:
        for s in backend.list_storage_cfg():
            if s.get("shared"):
                out.add(str(s.get("storage")))
    except Exception:  # noqa: BLE001
        pass
    return out


def _is_cluster_node(node):
    """node 是否为当前集群的合法节点名。

    供下载 / 删除等「node 会作为 SSH 目标」的接口复用：node 未白名单校验时，
    ssh_host 查不到 IP 会原样返回 node，等于允许 ssh 到任意主机名。"""
    if not node:
        return False
    if not node.replace("-", "").replace("_", "").isalnum():
        return False
    return node in backend.known_node_names()


def _local_outbound_ip():
    """本机用于对外通信的 IPv4（UDP connect 仅设置路由，不实际发包）。失败返回空串。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


