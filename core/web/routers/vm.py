"""虚拟机 / 容器（guest）域路由与实现（M2/T2.6 第三批路由表化 + 深化第三批实现迁入）。

覆盖虚机/容器列表与详情、部署默认值（模板 / 云镜像建机）、电源操作与属性修改。

本域**自包含**：只依赖 ``core.pve.pve_api``（``backend``）、``core.web.jobs`` 与处理器基础
设施（``_read_body`` / ``_json`` / ``_error`` / ``self.path``），不 import ``core.web.server``。
电源操作下发后由 ``jobs.track_event`` 在后台跟踪状态变化，跟踪实现（``_wait_guest_status`` /
``_track_guest_power``）也放在本模块——它们只服务于本域，跟着一起走。

顺序说明：``guests/<node>/<type>/<vmid>/<action>`` 是通配兜底项，与 ``onboot`` /
``description`` / ``rename`` 同为 5 段。后三者是全字面模板、落在精确表里，而精确表先于带参
模板命中（见 ``routers/dispatch.py`` 的匹配语义），所以通配项不会抢先吞掉具体动作
——旧 if 链靠书写顺序保证同一件事，迁移后不依赖顺序。
"""

import time
from urllib.parse import parse_qs, urlparse

from core.pve import pve_api as backend
from core.web import jobs


def _wait_guest_status(job, node, gtype, vmid, expect, timeout):
    """轮询虚机状态直到变为 expect；状态变化才记日志，连续查不到提前退出。"""
    deadline = time.time() + timeout
    last, errs = None, 0
    while time.time() < deadline:
        try:
            cur = str(backend.get_guest_status(node, gtype, vmid).get("status", ""))
            errs = 0
        except Exception:
            errs += 1
            if errs >= 3:  # 连续查不到，虚机可能不存在
                job.append("[错误] 无法获取虚机状态，虚机可能不存在")
                return False
            time.sleep(3)
            continue
        if cur != last:
            arrow = f"{last} → " if last else ""
            job.append(f"[状态] {time.strftime('%H:%M:%S')} {gtype}/{vmid} 状态：{arrow}{cur}")
            last = cur
        if cur == expect:
            return True
        time.sleep(3)
    return False


def _track_guest_power(job, node, gtype, vmid, action, action_cn, upid):
    """电源操作下发后跟踪实际状态变化，返回是否达到目标状态。"""
    target = f"{gtype}/{vmid}"
    job.append(f"[开始] {time.strftime('%Y-%m-%d %H:%M:%S')} 已向 PVE 提交{action_cn}指令"
               f"（UPID: {upid}），开始跟踪状态变化")
    if action == "reboot":
        # 重启：先等停下，再等回到运行
        if not _wait_guest_status(job, node, gtype, vmid, "stopped", 180):
            job.append(f"[警告] {target} 3 分钟内未停止，重启可能未生效（缺少虚机代理？）")
            return False
        if not _wait_guest_status(job, node, gtype, vmid, "running", 180):
            job.append(f"[警告] {target} 重启后 3 分钟内未恢复运行")
            return False
        job.append(f"[完成] {target} 重启完成")
        return True
    expect = "running" if action == "start" else "stopped"
    if _wait_guest_status(job, node, gtype, vmid, expect, 300):
        job.append(f"[完成] {target} {action_cn}完成，状态已变为 {expect}")
        return True
    job.append(f"[警告] {target} 5 分钟内未达到 {expect}，请手动检查")
    return False


def _guests(handler, _params):
    """GET /api/guests  虚机/容器列表（附带运行中虚机的 IP）。

    IP 由代理/配置/DHCP 探测得到，后端缓存 10 秒；``?fresh=1`` 穿透该短缓存
    （手动刷新要求拿最新状态）。
    """
    query = parse_qs(urlparse(handler.path).query)
    return handler._json(backend.list_guests_with_ips(force=bool(query.get("fresh"))))


def _snapshots(handler, params):
    """GET /api/guests/<vmid>/snapshots  指定虚机的快照列表（快照下拉数据源）。"""
    return handler._json(backend.list_snapshots(params["vmid"]))


def _deploy_template_config(handler, _params):
    """GET /api/deploy/template-config?src=vmid:101|file:/var/lib/vz/pvewebtools/dump/x.vma

    模板快速建机的模板默认配置（cores/memory/cpu/网桥/系统盘存储）：前端据此把「默认配置」
    显示成具体值，便于客户判断是否需要改配置。
    """
    src = parse_qs(urlparse(handler.path).query).get("src", [""])[0]
    return handler._json(backend.read_deploy_template(src))


def _deploy_cloud_defaults(handler, _params):
    """GET /api/deploy/cloud-defaults?image=<云镜像路径>

    云镜像建机（仅 Linux）脚本内置的有效默认值（1核/2G/host）：云镜像没有模板配置可读，
    故直接给脚本兜底值，供前端把「默认配置」显示成具体值。
    """
    image = parse_qs(urlparse(handler.path).query).get("image", [""])[0]
    return handler._json(backend.cloud_deploy_defaults(image))


def _config(handler, params):
    """GET /api/guests/<node>/<type>/<vmid>/config  虚机配置（详情页展示）。"""
    return handler._json(backend.get_guest_config(params["node"], params["type"], params["vmid"]))


def _status(handler, params):
    """GET /api/guests/<node>/<type>/<vmid>/status  虚机运行状态（详情页轮询）。"""
    return handler._json(backend.get_guest_status(params["node"], params["type"], params["vmid"]))


def _onboot(handler, params):
    """POST /api/guests/<node>/<type>/<vmid>/onboot  body:{enabled}  切换开机自启。"""
    body = handler._read_body()
    enabled = bool(body.get("enabled"))
    backend.set_guest_onboot(params["node"], params["type"], params["vmid"], enabled)
    return handler._json({"onboot": 1 if enabled else 0})


def _description(handler, params):
    """POST /api/guests/<node>/<type>/<vmid>/description  body:{description}  设置备注。"""
    body = handler._read_body()
    desc = str(body.get("description", ""))
    backend.set_guest_description(params["node"], params["type"], params["vmid"], desc)
    return handler._json({"description": desc})


def _rename(handler, params):
    """POST /api/guests/<node>/<type>/<vmid>/rename  body:{name}  重命名虚机。"""
    body = handler._read_body()
    name = str(body.get("name", "")).strip()
    if not name:
        return handler._error("名称不能为空", 400)
    backend.rename_guest(params["node"], params["type"], params["vmid"], name)
    return handler._json({"renamed": name})


def _action(handler, params):
    """POST /api/guests/<node>/<type>/<vmid>/<action>  虚机电源操作（通配兜底项）。

    action 取 start / shutdown / stop / reboot（未知值原样透传给 pvesh）。指令下发后由
    ``jobs.track_event`` 在后台跟踪状态变化，直到达到目标状态或超时。
    """
    node, gtype, vmid, action = params["node"], params["type"], params["vmid"], params["action"]
    action_cn = {"start": "启动", "shutdown": "关机",
                 "stop": "强制停止", "reboot": "重启"}.get(action, action)
    title = f"概览操作：{action_cn} {gtype}/{vmid}"
    try:
        upid = backend.guest_power_action(node, gtype, vmid, action)
    except Exception as e:
        # 指令下发失败也留一条日志，便于排查
        jobs.record_event("guest_action", title, node, False, [f"[错误] {e}"])
        raise
    jobs.track_event("guest_action", title, node,
                     lambda job: _track_guest_power(job, node, gtype, vmid, action, action_cn, upid))
    return handler._json({"upid": upid})


HANDLERS = {
    "get": {
        # GET /api/guests  虚机/容器列表（附带运行中虚机的 IP）
        "guests": _guests,
        "guests/<vmid>/snapshots": _snapshots,
        # GET /api/deploy/template-config?src=  模板快速建机的模板默认配置
        "deploy/template-config": _deploy_template_config,
        # GET /api/deploy/cloud-defaults?image=  云镜像建机的脚本内置默认值
        "deploy/cloud-defaults": _deploy_cloud_defaults,
        "guests/<node>/<type>/<vmid>/config": _config,
        "guests/<node>/<type>/<vmid>/status": _status,
    },
    "post": {
        "guests/<node>/<type>/<vmid>/onboot": _onboot,
        "guests/<node>/<type>/<vmid>/description": _description,
        "guests/<node>/<type>/<vmid>/rename": _rename,
        "guests/<node>/<type>/<vmid>/<action>": _action,
    },
}
