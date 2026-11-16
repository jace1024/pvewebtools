"""ApiHandler 的操作方法层（OpsMixin）：集群/节点操作、工具运行与参数校验、后台下载线程、存储内容删除。

这些是「处理入口调用的动作」而非 HTTP 入口本身；与 ``handlers.HandlerMixin`` 同为
``ApiHandler`` 的基类，跨 mixin 可达。
"""

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request

from core.pve import pve_api, pve_api as backend, ssh, storage_provision
from core.tools import toolbox
from core.web import jobs
from core.web.handlers import _backup_dirs_merged, _decompress_file, _list_template_backup_files, _node_backup_files, _storage_role, collect_nodes_cached, list_backup_storages, list_byid_disks, list_disk_storages, list_docker_versions, list_image_storages, list_imported_disks, list_lxc_storages, list_phys_disks, list_vbios_roms, list_vm_disks
from core.web.host_util import _is_cluster_node, _local_outbound_ip, _norm_confirm
from core.web.maintenance import _download_error_text
from core.web.probe import _SSH_CTRL, _local_access_pred, _run_func_on_node_args, list_available_kernels, list_grub_backups, list_installed_kernels, list_net_backups, list_network_bridges, list_network_vlans, list_pci_generic, list_pci_gpus, list_pci_nics, list_pci_storage, list_phys_nics, list_usb_devices, list_vfio_orphans
from core.web.vbios import _read_vbios_manifest
from pathlib import Path


_REMOTE_LOCAL_IPS = "ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1"


def _node_mgmt_ip(node):
    """节点管理口 IP（与 /api/nodes 同一采集来源，即前端节点行上展示的那个 IP）；取不到返回 ""。"""
    try:
        for n in collect_nodes_cached() or []:
            if str(n.get("node")) == node:
                return str(n.get("ip") or n.get("address") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _join_blocked_reason():
    """「创建集群 / 加入节点」的准入拦截点；返回非空字符串即拒绝并回显该原因。

    当前没有额外限制，恒放行。新增限制（例如只允许特定网段、特定集群名）在此处实现即可，
    两个入口共用同一判断。
    """
    return ""


class OpsMixin:
    """从 ApiHandler 拆出的操作方法：集群/节点操作、工具运行、后台下载线程、存储删除。"""

    # 云镜像与 LXC 模板各用一张表，互不干扰（同名文件不会串进度）
    _cloud_dl = {}
    # 取消标记集合：取消请求写入 name，下载线程在下一个分片读取后停止并清理半成品 .part
    _cloud_dl_cancel = set()
    _lxc_dl = {}
    _lxc_dl_cancel = set()

    def _delete_backup(self, storage, name):
        """删除备份文件：文件名白名单校验 + 存储校验，防路径穿越"""
        if not name or not re.fullmatch(r"[\w][\w.\- ]*", name):
            raise pve_api.PveError("非法文件名")
        dirs = _backup_dirs_merged()
        d = dirs.get(storage)
        if d is None:
            raise pve_api.PveError("存储不可用或未启用 backup 内容")
        path = d / name
        if not path.is_file():
            raise pve_api.PveError("备份文件不存在")
        try:
            path.unlink()
        except OSError as e:
            raise pve_api.PveError(f"删除失败: {e}")

    def _stream_remote_storage_content(self, node, storage, volid):
        """跨节点下载节点独占存储里的文件：先让目标节点解析路径与大小，再经 SSH cat 流式取回。

        路径必须由目标节点解析：各节点的 local 是同 id 不同内容，本机解析会指到本机那份。"""
        try:
            info = _run_func_on_node_args(node, "_node_storage_content_info", (storage, volid), timeout=20)
        except Exception as e:  # noqa: BLE001
            return self._error(f"节点 {node} 定位文件失败：{e}", 502)
        if not isinstance(info, dict) or not info.get("path"):
            return self._error(f"节点 {node} 上未找到该文件", 404)
        try:
            proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", "cat", "--", str(info["path"])],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            return self._error(f"连接节点 {node} 失败：{e}", 502)
        from urllib.parse import quote
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(str(info.get('name') or 'download'))}")
        self.send_header("Content-Length", str(int(info.get("size") or 0)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            shutil.copyfileobj(proc.stdout, self.wfile, 1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _delete_storage_content(self, storage, volid, node=None):
        if not storage or not volid:
            raise pve_api.PveError("缺少 storage 或 volid")
        # node 会作为 SSH 目标，必须白名单校验为集群成员，否则可 ssh 到任意主机
        if node and not _is_cluster_node(node):
            raise pve_api.PveError("非法节点名")
        local = socket.gethostname().split(".")[0]
        if node and node != local and not _local_access_pred(node)(storage):
            # 节点独占存储（local 等）：文件只在该节点盘上，本机删除会误伤本机同名存储，
            # 必须由目标节点自行解析路径并删除（路径校验也在那边做）
            try:
                return _run_func_on_node_args(node, "_node_delete_storage_content", (storage, volid), timeout=30)
            except Exception as e:  # noqa: BLE001
                raise pve_api.PveError(f"节点 {node} 删除失败：{e}")
        backend.delete_storage_content(storage, volid)
        return {"deleted": volid}

    @staticmethod
    def _cloud_image_thread(url, dest, name, decompress=False, meta=None):
        """后台线程：流式下载云镜像到导入目录（复用通用下载逻辑）。"""
        OpsMixin._bg_download(url, dest, name, OpsMixin._cloud_dl,
                                OpsMixin._cloud_dl_cancel, decompress, meta)

    @staticmethod
    def _lxc_template_thread(urls, dest, name):
        """后台线程：流式下载 CT 模板到 <存储>/template/cache（复用通用下载逻辑，无需解压）。
        urls 为候选下载地址（turnkeylinux 类模板会带多个镜像源，逐个回退）；
        并启用内容校验，避免把「返回 200 的 HTML 错误页/站点首页」当模板落盘。"""
        OpsMixin._bg_download(urls, dest, name, OpsMixin._lxc_dl, OpsMixin._lxc_dl_cancel,
                                validate=OpsMixin._looks_like_template)

    @staticmethod
    def _looks_like_template(resp):
        """判断响应是否真的是一个 CT 模板文件（而非镜像站返回的 HTML 错误页/前端首页）。
        部分镜像站（如 SDU）对任意路径都回 200 + 站点首页，若只看状态码会"下载成功"一个几 KB 的网页。
        判据：Content-Type 不能是 html/json/text；声明长度要么未知、要么不小于 1MB
        （PVE 模板最小也有数十 MB，几 KB/几百 KB 的响应一定是错误页）。"""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype or "application/json" in ctype or "text/plain" in ctype:
            return False
        try:
            clen = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            clen = 0
        return not (clen and clen < 1024 * 1024)

    @staticmethod
    def _open_first_url(urls, timeout=30, retry_timeout=15, validate=None):
        """依次尝试候选地址，返回首个通过校验的 (响应, 实际地址)；全失败则抛出最后一个异常。
        首个地址用完整超时（正常情况就该命中），回退地址用较短超时，避免多源回退长时间卡住。
        validate(resp) 返回 False 时关闭该响应并继续尝试下一个源（用于剔除伪装成 200 的错误页）。"""
        last = None
        for i, u in enumerate(urls):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "pvewebtools"})
                resp = urllib.request.urlopen(req, timeout=timeout if i == 0 else retry_timeout)
            except Exception as e:  # noqa: BLE001  含 HTTPError/URLError/超时：一律换下一个源
                last = e
                continue
            if validate is not None:
                try:
                    ok = bool(validate(resp))
                except Exception:  # noqa: BLE001  校验本身出错时按不可用处理，继续回退
                    ok = False
                if not ok:
                    last = ValueError(f"该源返回的不是有效模板文件（疑似错误页）: {u}")
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
            return resp, u
        raise last if last is not None else ValueError("没有可用的下载地址")

    @staticmethod
    def _bg_download(urls, dest, name, registry, cancel_set, decompress=False, meta=None, validate=None):
        """通用后台下载线程：流式落 .part 并实时更新进度，完成后原子改名；
        decompress=True 时下载完成后解压并删除压缩源文件；meta 非空则写侧车元数据。
        urls 为候选地址列表（也可传单个字符串）：只在「打开」阶段回退换源，
        一旦开始接收数据便不再换源（避免多个源的分片拼成坏文件）。
        validate(resp) 可选，用于在回退阶段剔除伪装成 200 的错误页。
        进度写入传入的 registry，取消标记读传入的 cancel_set（云镜像/LXC 模板各自一套）。"""
        if isinstance(urls, str):
            urls = [urls]
        urls = [u for u in urls if u]
        try:
            registry[name] = {"total": 0, "done": 0, "status": "downloading", "error": ""}
            resp, used_url = OpsMixin._open_first_url(urls, validate=validate)
            registry[name]["url"] = used_url
            with resp:
                content_length = resp.headers.get("Content-Length")
                total = int(content_length) if content_length is not None else 0
                registry[name]["total"] = total
                tmp = dest + ".part"
                done = 0
                with open(tmp, "wb") as f:
                    while True:
                        # 先停止读取，离开 with 关闭文件后再清理半成品（Windows 不允许删除打开的文件）。
                        if name in cancel_set:
                            break
                        buf = resp.read(1024 * 1024)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        registry[name]["done"] = done
            if name in cancel_set:
                cancel_set.discard(name)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                registry[name] = {"total": 0, "done": 0, "status": "cancelled", "error": ""}
                return
            # HTTPResponse.read(size) 遇到提前 EOF 不一定抛异常，已知长度必须完整接收后才能发布。
            if content_length is not None and done != total:
                raise ValueError(f"下载不完整：预期 {total} 字节，实际收到 {done} 字节")
            # 下载完成：需解压则先解压到目标名，再删除压缩源文件（.part）；否则原子重命名落盘
            if decompress:
                _decompress_file(tmp, dest)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            else:
                os.replace(tmp, dest)
            # 携带了预设元数据时写侧车文件（<name>.meta.json），供部署阶段直接使用准确版本
            if meta:
                try:
                    Path(dest + ".meta.json").write_text(
                        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            registry[name]["status"] = "done"
            cancel_set.discard(name)   # 清理残留取消标记，避免影响下次的同名下载
        except Exception as e:  # noqa: BLE001
            cancel_set.discard(name)
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
            # 失败原因整理成人话：区分"各源都没有该模板（疑似下架）"与"网络/权限问题"
            registry[name] = {"total": 0, "done": 0, "status": "error",
                              "error": _download_error_text(e, urls)}

    def _run_tool(self, tool_id):
        """参数校验→高危确认词校验→节点校验→提交异步任务"""
        tool = toolbox.get_tool(tool_id)
        # 占位卡片（disabled:True）没有可执行内容，先拒绝，给出比「缺少脚本」更清楚的提示
        if tool.get("disabled"):
            raise toolbox.ToolError(f"{tool['name']}：该模块尚未开放，敬请期待")
        body = self._read_body()
        node = self._resolve_node(body.get("node"))
        clean = toolbox.validate_params(tool, body.get("params"))
        # 动态下拉服务端复核：校验 options_from 参数是否真在运行时选项集内（拦截伪造/越权值）
        self._verify_dynamic_options(tool, body.get("params"), node)
        # 虚机/容器归属复核：qm/pct 只能操作本节点虚机，选错执行节点必须拦下（否则静默假成功）
        self._verify_guest_ownership(tool, body.get("params"), node)
        # 高危操作必须原样输入工具名确认，防误点；
        # action_risk 存在时按所选操作取风险（如内核配置管理的“查看”只读免确认词）
        eff_risk = tool["risk"]
        if tool.get("action_risk"):
            eff_risk = tool["action_risk"].get(clean.get("action", ""), tool["risk"])
        if eff_risk == "high" and _norm_confirm(body.get("confirm")) != _norm_confirm(tool["name"]):
            raise toolbox.ToolError(f"高危操作需输入确认词：{tool['name']}")
        script = toolbox.build_script(tool)
        env = toolbox.build_env(tool, clean)
        job_id = jobs.submit(tool_id, tool["name"], script=script,
                             env_params=env, node=node)
        return self._json({"job_id": job_id})

    def _verify_dynamic_options(self, tool, raw_params, node):
        """服务端复核动态下拉参数：确认提交值真在运行时选项集内。
        无法获取选项集（如节点临时不可达）时静默跳过，回退 pattern 校验。"""
        raw_params = raw_params or {}
        for p in tool["params"]:
            if p.get("type") != "select" or not p.get("options_from"):
                continue
            val = str(raw_params.get(p["name"], "") or "").strip()
            if not val:
                continue
            allowed = self._resolve_option_values(p["options_from"], node, raw_params)
            # None=数据源取不到（节点不可达等）；空集=在该节点上确实没采到任何候选值
            #（如 lspci 不可用）。两者都只代表「无法确认合法值」，不作否定判决：
            # 否则本机采集异常时，下拉里明明选得出的值也会被误报「不在可选范围内」。
            if not allowed:
                continue
            # 多选（如虚机多选）：逐一校验逗号/空格分隔的每一个值是否都在合法集合内
            if p.get("multiple"):
                for v in re.split(r"[,\s]+", val):
                    if v and v not in allowed:
                        raise toolbox.ToolError(f"参数 {p['label']} 取值非法：{v} 不在可选范围内")
            elif val not in allowed:
                raise toolbox.ToolError(f"参数 {p['label']} 取值非法：{val} 不在可选范围内")

    def _verify_guest_ownership(self, tool, raw_params, node):
        """复核「虚机/容器（VMID）」类参数是否属于执行节点；不属于则直接拒绝提交。

        为什么必须有这一道：qm/pct 是**节点本地**命令（`/etc/pve/qemu-server/` 只含本节点虚机），
        而 VMID 下拉是**集群级**列表。执行节点与所选虚机不在同一台时，脚本里 `qm status <vmid>`
        查不到该虚机，会走「[跳过] 不存在」分支并计为成功 —— 表现为「界面显示删除/备份成功，
        其实什么都没做」。这里在提交前拦下并说明它属于哪个节点，避免静默假成功。

        覆盖 select（单值）与 checkbox multiple（逗号分隔多值，如批量删除）；取不到虚机列表时
        静默跳过（fail-open，与 _verify_dynamic_options 同口径：不确定就不否定）。"""
        if not tool.get("params"):
            return
        _get = lambda o, k: o[k] if isinstance(o, dict) else getattr(o, k, None)
        # node 为空串/None 都表示「本机执行」（见 _resolve_node），解析成真实节点名才能比对归属
        target = node or socket.gethostname().split(".")[0]
        raw_params = raw_params or {}
        for p in tool["params"]:
            # 归属分类集中在 toolbox 的分类表里（GUEST_OWNED_SOURCES），此处不再散写字面量
            if p.get("options_from") not in toolbox.GUEST_OWNED_SOURCES:
                continue
            raw = str(raw_params.get(p["name"], "") or "").strip()
            if not raw:
                continue
            ids = [x for x in re.split(r"[,\s]+", raw) if x]
            if not ids:
                continue
            try:
                owners = {str(_get(g, "vmid")): str(_get(g, "node") or "")
                          for g in backend.list_guests_with_ips()}
            except Exception:  # noqa: BLE001
                return
            for vid in ids:
                owner = owners.get(vid)
                if owner is None or not owner or owner == target:
                    continue                  # 未知虚机 / 归属不明 / 就在执行节点：放行
                raise toolbox.ToolError(
                    f"{p['label']} {vid} 属于节点 {owner}，当前执行节点为 {target}："
                    f"qm/pct 只能操作所执行节点上的虚机，请把「执行节点」切换为 {owner} 后重试")

    def _resolve_option_values(self, options_from, node, raw_params=None):
        """返回 options_from 标识对应的「当前合法 value 集合」；获取失败返回 None（回退校验）。
        兼容 dict 与对象两种返回结构（_get 辅助）。

        节点级数据源（硬件/网络/磁盘/存储/备份）必须带 node 采集：前端下拉一律附 ?node=
        取「所选执行节点」的数据，若这里只采本机，切到其它节点选出的设备/磁盘会被误判为
        「不在可选范围内」而拒绝提交。node 为本机时入参已是 None（见 _resolve_node），
        与前端不带 node 的「本机」请求一致。"""
        _get = lambda o, k: o[k] if isinstance(o, dict) else getattr(o, k, None)
        raw_params = raw_params or {}
        try:
            if options_from in ("guests", "guests_all"):
                guests = backend.list_guests_with_ips()
                if options_from == "guests":
                    guests = [g for g in guests if _get(g, "type") == "qemu"]
                return {str(_get(g, "vmid")) for g in guests}
            if options_from == "deploy_sources":
                files = list(backend.list_backup_files())
                for f in files:
                    if f.get("storage") == "pvewebtools":
                        f["template"] = True
                files += _list_template_backup_files()
                return {"file:" + str(_get(b, "path")) for b in files if _get(b, "template")}
            if options_from == "disk_upload":
                return {f"{_get(f, 'storage')}::{_get(f, 'name')}" for f in list_imported_disks(kind="disk")}
            if options_from == "cloud_images":
                return {f"{_get(f, 'storage')}::{_get(f, 'name')}" for f in list_imported_disks(kind="cloud")}
            if options_from == "disk_storages":
                return {_get(s, "storage") for s in list_disk_storages(node)}
            if options_from == "image_storages":
                return {_get(s, "storage") for s in list_image_storages(node)}
            if options_from == "backup_storages":
                return {_get(s, "storage") for s in list_backup_storages(node)}
            if options_from == "lxc_storages":
                return {_get(s, "storage") for s in list_lxc_storages(node)}
            if options_from == "backups":
                return {_get(b, "name") for b in _node_backup_files(node)}
            if options_from == "disks":
                return {_get(d, "name") for d in list_phys_disks(node)}
            if options_from == "vm_disks":
                vmid = raw_params.get("vmid")
                if not vmid:
                    return None
                g = next((x for x in backend.list_guests_with_ips()
                          if str(_get(x, "vmid")) == str(vmid)), None)
                if not g:
                    return None
                return {_get(d, "key") for d in list_vm_disks(_get(g, "node"), "qemu", vmid)}
            if options_from == "device":
                # mode 决定取值形态（pcie→addr / 块映射→by-id name）；取并集放行更安全
                vals = set()
                try:
                    vals |= {_get(d, "addr") for d in list_pci_storage(node)}
                except Exception:
                    pass
                try:
                    vals |= {_get(d, "name") for d in list_byid_disks(node)}
                except Exception:
                    pass
                return vals or None
            if options_from == "nic":
                return {_get(d, "addr") for d in list_pci_nics(node)}
            if options_from == "gpu":
                return {_get(d, "addr") for d in list_pci_gpus(node)}
            if options_from == "pci_generic":
                return {_get(d, "addr") for d in list_pci_generic(node)}
            if options_from == "vfio_devices":
                return {_get(d, "addr") for d in list_vfio_orphans(node)}
            if options_from == "usb":
                return {_get(d, "id") for d in list_usb_devices(node)}
            if options_from == "docker_versions":
                # 「latest」为前端合成选项（装仓库最新），与大版本线一并放行
                return {"latest"} | {str(d.get("major")) for d in list_docker_versions()}
            if options_from == "vbios_roms":
                return {_get(r, "name") for r in list_vbios_roms()}
            if options_from == "bridges":
                return {_get(b, "name") for b in list_network_bridges(node)}
            if options_from == "phys_nics":
                return {_get(n, "name") for n in list_phys_nics(node)}
            if options_from == "vlans":
                return {_get(v, "name") for v in list_network_vlans(node)}
            if options_from in ("kernels", "kernels_available"):
                if options_from == "kernels":
                    return {_get(k, "version") for k in list_installed_kernels(node)}
                return {_get(k, "name") for k in list_available_kernels(node)}
            if options_from == "grub_backups":
                return {_get(b, "name") for b in list_grub_backups(node)}
            if options_from == "net_backups":
                return {_get(b, "name") for b in list_net_backups(node)}
            return None
        except Exception:
            # 数据源获取失败（如节点不可达）时不阻断合法提交，回退 pattern 校验
            return None

    def _reboot_node(self, node):
        """重启节点：需 body 携带确认词，sleep 10 后 shutdown -r now（shutdown 自身只支持分钟级）"""
        body = self._read_body()
        # 服务端强校验确认词：浏览器 confirm 可能被自动化/误触绕过
        if _norm_confirm(body.get("confirm")) != _norm_confirm("重启节点"):
            raise toolbox.ToolError("高危操作需输入确认词：重启节点")
        if not node.replace("-", "").replace("_", "").isalnum():
            raise toolbox.ToolError(f"非法节点名: {node}")
        known = backend.known_node_names()
        if node not in known:
            raise toolbox.ToolError(f"节点不存在: {node}")
        cmd = 'sleep 10 && shutdown -r now "pvewebtools: 应用直通配置重启"'
        if node == socket.gethostname().split(".")[0]:
            # start_new_session 等价 setsid 脱离会话；用 Popen 不等待子进程，
            # 否则 HTTP 响应会被 10 秒延迟阻塞，前端倒计时无法立即开始
            subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", f"setsid sh -c '{cmd}' >/dev/null 2>&1 &"],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                raise toolbox.ToolError(r.stderr.strip() or "重启指令下发失败")
        jobs.record_event("node_reboot", f"节点重启：{node}", node, True,
                          [f"[已排程] {node} 将于约 10 秒后重启"])
        return self._json({"scheduled": node})

    def _shutdown_node(self, node):
        """关机节点：需 body 携带确认词「关机节点」，sleep 10 后 shutdown -h now。

        排程方式与 _reboot_node 相同（本机 Popen / 远端免密 SSH，均不阻塞 HTTP 响应）。
        项目没有远程开机（WoL）能力，控制机自身关机后工具箱与 WebUI 都不可用，
        前端对控制机额外警示，恢复必须到机器现场开机。
        """
        body = self._read_body()
        if _norm_confirm(body.get("confirm")) != _norm_confirm("关机节点"):
            raise toolbox.ToolError("高危操作需输入确认词：关机节点")
        if not node.replace("-", "").replace("_", "").isalnum():
            raise toolbox.ToolError(f"非法节点名: {node}")
        known = backend.known_node_names()
        if node not in known:
            raise toolbox.ToolError(f"节点不存在: {node}")
        cmd = 'sleep 10 && shutdown -h now "pvewebtools: 节点关机"'
        if node == socket.gethostname().split(".")[0]:
            subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
                 *backend.ssh_host_opts(node),
                 f"root@{backend.ssh_host(node)}", f"setsid sh -c '{cmd}' >/dev/null 2>&1 &"],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                raise toolbox.ToolError(r.stderr.strip() or "关机指令下发失败")
        jobs.record_event("node_shutdown", f"节点关机：{node}", node, True,
                          ["[已排程] 将于约 10 秒后关机", "无远程开机能力，恢复需到机器现场开机"])
        return self._json({"scheduled": node})

    @staticmethod
    def _require_cluster_node(node, action):
        """校验节点名合法且在当前集群内，否则抛 ToolError（避免对集群外的机器误动作）"""
        if not node.replace("-", "").replace("_", "").isalnum():
            raise toolbox.ToolError(f"非法节点名: {node}")
        known = backend.known_node_names()
        if node not in known:
            raise toolbox.ToolError(f"节点不在集群中，无法{action}：{node}")

    def _run_node_script(self, node, script, timeout=300):
        """在目标节点执行同一段 bash：本机直接本地执行（无需 ssh 回自己），其余节点走免密 SSH。
        返回 CompletedProcess；超时由 subprocess 抛 TimeoutExpired，由调用方翻译成友好文案。"""
        if node == socket.gethostname().split(".")[0]:
            return subprocess.run(["bash", "-s"], input=script,
                                  capture_output=True, text=True, timeout=timeout)
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", *_SSH_CTRL,
             *backend.ssh_host_opts(node),
             f"root@{backend.ssh_host(node)}", "bash", "-s"],
            input=script, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _storage_init_remote_script(path, server, export, names):
        """构造远端接入共享存储的 bash 脚本（挂 NFS + 铺 vBIOS 软链），幂等；参数全 shlex.quote 防注入。

        被 _join_init_remote（密码 SSH）与 _fix_node_storage（免密 SSH）共用，避免两处重复脚本。
        vBIOS 主副本位于共享存储 <挂载点>/vbios/（与 _vbios_shared_dir() 同一布局；旧版的
        <挂载点>/.pvewebtools/vbios/ 早已迁移，仍按旧路径写会永远建不出软链）。
        脚本开头自查本机地址：含 <server> 即本机就是导出端，直接跳过——绝不把导出目录挂到自己身上。"""
        vb_block = "\n".join(
            "[ -f %s/vbios/%s ] && ln -sf %s/vbios/%s /usr/share/kvm/%s" % (
                shlex.quote(path), shlex.quote(n), shlex.quote(path), shlex.quote(n), shlex.quote(n))
            for n in names
        ) or "true"
        return (
            "set +e\n"
            f"if {_REMOTE_LOCAL_IPS} | grep -qx {shlex.quote(server)}; then\n"
            f"  echo 'SKIP-SELF: {server} 是本机地址，本机即共享存储导出端，跳过挂载（请用「修复共享存储」）'\n"
            "  exit 0\n"
            "fi\n"
            f"mkdir -p {shlex.quote(path)}\n"
            f"if ! mountpoint -q {shlex.quote(path)}; then\n"
            # 优先带 nconnect=8（内核 ≥5.3 + NFSv4.1+：单流吞吐常被单 TCP 连接限制）；
            # 失败自动退回普通挂载，并按「实际挂上的选项」写 fstab —— 优化参数绝不能拖累共享存储接入
            f"  if mount -t nfs -o defaults,_netdev,nconnect=8 {shlex.quote(server + ':' + export)} {shlex.quote(path)}; then\n"
            f"    echo '已挂载 {path}（nconnect=8 多连接）'\n"
            f"    grep -qF {shlex.quote(server + ':' + export)} /etc/fstab || "
            f"printf '%s\n' {shlex.quote(server + ':' + export + ' ' + path + ' nfs defaults,_netdev,nconnect=8 0 0')} >> /etc/fstab\n"
            "  else\n"
            "    echo 'WARN-NCONNECT: 带 nconnect=8 挂载失败（内核<5.3 / 服务端 NFS<4.1 / 内核未启用多连接），改用普通挂载'\n"
            f"    mount -t nfs {shlex.quote(server + ':' + export)} {shlex.quote(path)} || mount {shlex.quote(path)}\n"
            f"    grep -qF {shlex.quote(server + ':' + export)} /etc/fstab || "
            f"printf '%s\n' {shlex.quote(server + ':' + export + ' ' + path + ' nfs defaults,_netdev 0 0')} >> /etc/fstab\n"
            "  fi\n"
            "fi\n"
            # 挂载没成功就到此为止：不能继续铺 vBIOS 软链（软链会指向空目录），更不能 echo DONE，
            # 否则调用方只按退出码 0 判定「共享存储接入完成」，而节点上其实什么都没挂上。
            f"if ! mountpoint -q {shlex.quote(path)}; then\n"
            f"  echo 'MOUNT-FAILED: 共享存储 {server}:{export} 未挂载成功（导出端不可达 / 未导出 / fstab 异常）'\n"
            "  exit 1\n"
            "fi\n"
            + vb_block + "\n"
            "echo DONE\n"
        )

    @staticmethod
    def _nfs_server_init_remote_script(export_dir, cidr, server):
        """构造远端「确保本机为 NFS 导出端」的 bash 脚本（装服务 + 写 /etc/exports + 导出 + 起服务），
        幂等，参数全 shlex.quote 防注入；等效于 storage_provision.ensure_nfs_server()，
        供「服务端导出掉线 / 事后才装好 NFS」时在导出端节点上重跑。

        同样先自查本机地址含 <server>：在客户端节点上误跑时中止，避免把已挂载的 NFS 目录再导出一次。"""
        q = shlex.quote
        log = "/var/log/pvewebtools-nfs-fix.log"
        return (
            "set +e\n"
            'export PATH="$PATH:/usr/sbin:/sbin"\n'
            f"if ! {_REMOTE_LOCAL_IPS} | grep -qx {q(server)}; then\n"
            f"  echo 'NOT-SERVER: 本机地址不含 {server}，该节点不是共享存储导出端，已中止（请用「修复共享存储」）'\n"
            "  exit 2\n"
            "fi\n"
            f"mkdir -p {q(export_dir)}\n"
            "if ! command -v exportfs >/dev/null 2>&1; then\n"
            "  echo 'NFS: 未检测到 exportfs(nfs-kernel-server)，尝试 apt 安装...'\n"
            f"  apt-get update > {q(log)} 2>&1\n"
            f"  DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-kernel-server >> {q(log)} 2>&1\n"
            "fi\n"
            "if ! command -v exportfs >/dev/null 2>&1; then\n"
            "  echo 'INSTALL-FAILED: 无法安装 nfs-kernel-server（请检查 apt 源与网络）'\n"
            f"  tail -n 3 {q(log)} 2>/dev/null\n"
            "  exit 1\n"
            "fi\n"
            f"grep -qF {q(export_dir + ' ')} /etc/exports || printf '%s\\n' '# pvewebtools managed' "
            f"{q(export_dir + ' ' + cidr + '(rw,no_root_squash,no_subtree_check,sync)')} >> /etc/exports\n"
            f"exportfs -ra >> {q(log)} 2>&1\n"
            "systemctl enable --now nfs-kernel-server >/dev/null 2>&1\n"
            "if ! systemctl is-active --quiet nfs-kernel-server; then\n"
            "  echo 'SERVICE-FAILED: nfs-kernel-server 未能启动'\n"
            "  systemctl status nfs-kernel-server --no-pager -l 2>&1 | tail -n 5\n"
            "  exit 1\n"
            "fi\n"
            f"if ! exportfs -v 2>/dev/null | grep -qF {q(export_dir)}; then\n"
            "  echo 'EXPORT-MISSING: /etc/exports 已写但导出未生效'\n"
            f"  tail -n 3 {q(log)} 2>/dev/null\n"
            "  exit 1\n"
            "fi\n"
            f"echo 'OK: {export_dir} 已导出给 {cidr}'\n"
            "echo DONE\n"
        )

    def _fix_node_storage(self, node):
        """对集群内节点重跑共享存储修复，走免密 SSH（本机则本地执行），幂等可重试。

        按目标节点的角色分流，避免对 NFS 导出端做「自己挂自己」的无意义（且有害）挂载：
        - server（该节点即导出端）：改跑服务端导出修复 _fix_nfs_server；
        - client：挂 NFS(WORKSPACE_PATH) + 铺 vBIOS 软链，复用 _storage_init_remote_script
          （与 _join_init_remote 同源，仅 SSH 认证不同：免密 vs 密码）。
        适用于「加入集群时共享存储接入失败 / 事后才配好 NFS」的补救场景。"""
        self._require_cluster_node(node, "修复共享存储")
        try:
            cfg = storage_provision._load_config()
        except Exception:
            return False, "读取存储配置失败"
        # 统一走 _resolve_base（与启动时 ensure_storage 同一套解析）：NFS_SERVER 留空而
        # NFS_SERVER_MODE=1（默认配置）时按本机 LAN IP 自动补全，否则会误报「未配置 NFS_SERVER」。
        wtype, server, export, path = storage_provision._resolve_base(cfg)
        if wtype != "nfs" or not server:
            return False, "未启用 NFS 共享存储（当前为本地 dir 模式），无需接入"
        if _storage_role(node, _node_mgmt_ip(node), cfg) == "server":
            return self._fix_nfs_server(node, cfg)
        remote = self._storage_init_remote_script(path, server, export, list(_read_vbios_manifest()))
        try:
            r = self._run_node_script(node, remote)
        except subprocess.TimeoutExpired:
            return False, "共享存储接入超时（挂载 NFS）"
        out = (r.stdout or "").strip()
        if r.returncode == 0 and "SKIP-SELF" in out:
            return False, f"{node} 本身即共享存储导出端，未执行挂载；请改用「修复共享存储」"
        if r.returncode != 0:
            return False, (r.stderr or out or "未知错误").strip()[:300]
        jobs.record_event("node_storage_fix", f"修复共享存储（客户端）：{node}", node, True,
                          [out or "共享存储接入完成"])
        return True, out or "共享存储接入完成"

    def _fix_nfs_server(self, node, cfg=None):
        """重跑 NFS 服务端导出：装 nfs-kernel-server + 写 /etc/exports + exportfs -ra + 起服务。

        与启动时 storage_provision.ensure_nfs_server() 等效（脚本见 _nfs_server_init_remote_script），
        幂等可重试。角色不是导出端时拒绝执行，避免在客户端节点上把已挂载的 NFS 目录再导出一次。"""
        self._require_cluster_node(node, "修复 NFS 服务端")
        if cfg is None:
            try:
                cfg = storage_provision._load_config()
            except Exception:
                return False, "读取存储配置失败"
        try:
            wtype, server, _export, _path = storage_provision._resolve_base(cfg)
        except Exception:
            return False, "读取存储配置失败"
        if wtype != "nfs" or not server:
            return False, "未启用 NFS 共享存储（当前为本地 dir 模式），无需修复服务端"
        if _storage_role(node, _node_mgmt_ip(node), cfg) != "server":
            return False, f"{node} 不是共享存储的导出端（导出端为 {server}）；客户端请用「修复共享存储」"
        export_dir = cfg.get("WORKSPACE_PATH") or storage_provision.DEFAULT_DIR_PATH
        cidr = cfg.get("LAN_CIDR") or "192.168.0.0/16"
        remote = self._nfs_server_init_remote_script(export_dir, cidr, server)
        try:
            r = self._run_node_script(node, remote, timeout=600)
        except subprocess.TimeoutExpired:
            return False, "NFS 服务端修复超时（安装/导出耗时过长）"
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            return False, (out or r.stderr or "未知错误").strip()[:300]
        jobs.record_event("node_nfs_server_fix", f"修复共享存储（服务端）：{node}", node, True,
                          [out or "NFS 服务端导出已就绪"])
        ok_line = next((ln for ln in out.splitlines() if ln.startswith("OK:")), "")
        return True, (ok_line[4:].strip() if ok_line else out or "NFS 服务端导出已就绪")

    def _target_run(self, target, script, timeout=30):
        """在目标集群节点上执行一段脚本：目标就是本机则本地执行，否则走集群免密 SSH。"""
        local = {socket.gethostname(), socket.gethostname().split(".")[0],
                 _local_outbound_ip(), "127.0.0.1", "localhost"}
        if target in local:
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=accept-new", "root@" + target, script]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _target_in_cluster(self, target_ip, timeout=30):
        """目标节点是否已在某个集群里：返回 (ok, 说明)。

        `pvecm add` 只能把节点加入一个**已存在的集群**，对独立单机执行必然失败；而且失败只会出现在
        对端的异步任务日志里（CLI 退出码仍为 0），用户拿到的报错很绕。所以加机前先探一次：
        ok=False 且说明为空串 = 确定没建集群；说明非空 = 探测本身失败（连不上 / 命令不可用）。"""
        probe = ('export PATH="$PATH:/usr/sbin:/sbin"; '
                 "if ! command -v pvecm >/dev/null 2>&1; then echo PVEWT-NO-PVECM; exit 0; fi; "
                 "if [ ! -f /etc/pve/corosync.conf ]; then echo PVEWT-NO-CLUSTER; exit 0; fi; "
                 "if pvecm status 2>/dev/null | grep -q 'Cluster information'; then "
                 "echo PVEWT-IN-CLUSTER; else echo PVEWT-CLUSTER-UNREADABLE; fi")
        try:
            r = self._target_run(target_ip, probe, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, "无法在目标节点 %s 上确认集群状态：%s" % (target_ip, e)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if "PVEWT-IN-CLUSTER" in out:
            return True, ""
        if "PVEWT-NO-CLUSTER" in out:
            return False, ""                       # 确定没建集群（无 corosync 配置）
        if "PVEWT-NO-PVECM" in out:
            return False, "目标节点 %s 上找不到 pvecm 命令（该地址不是 PVE 宿主机？）" % target_ip
        if "PVEWT-CLUSTER-UNREADABLE" in out:
            return False, ("目标节点 %s 有集群配置但读不到集群信息（corosync 未运行或未就绪？）"
                           "，请先确认该节点集群状态正常。" % target_ip)
        first = out.splitlines()[0] if out else "无响应"
        return False, "无法在目标节点 %s 上确认集群状态：%s" % (target_ip, first)

    def _install_new_node_key(self, new_host, new_pw, target_ip):
        """免目标密码加入：为新节点准备密钥，并把其公钥装到目标节点（借控制节点的集群互信）。"""
        script = ("mkdir -p /root/.ssh && chmod 700 /root/.ssh; "
                  "test -f /root/.ssh/id_rsa.pub || ssh-keygen -t rsa -b 3072 -N '' -f /root/.ssh/id_rsa >/dev/null 2>&1; "
                  "ssh-keyscan -H " + shlex.quote(target_ip) + " >> /root/.ssh/known_hosts 2>/dev/null; "
                  "tail -n 1 /root/.ssh/id_rsa.pub")
        r = ssh.password_ssh_run(new_host, [script], new_pw, timeout=60)
        pub = (r.stdout or "").strip()
        if r.returncode != 0 or not pub.startswith("ssh-"):
            raise toolbox.ToolError("无法为新节点准备 SSH 密钥，请改用「目标节点密码」方式加入。")
        install = ("mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                   "touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && "
                   "(grep -qF " + shlex.quote(pub) + " /root/.ssh/authorized_keys 2>/dev/null || "
                   "echo " + shlex.quote(pub) + " >> /root/.ssh/authorized_keys)")
        try:
            r2 = self._target_run(target_ip, install)
        except (OSError, subprocess.SubprocessError) as e:
            raise toolbox.ToolError("无法连接目标节点以安装公钥，请改用「目标节点密码」方式加入（%s）" % e)
        if r2.returncode != 0:
            raise toolbox.ToolError("目标节点未接受公钥，请改用「目标节点密码」方式加入：" + (r2.stderr or "")[:200])

    def _degrade_quorum(self):
        """临时取得仲裁：下调运行时预期票数到在线节点数，让失仲裁的集群重新可写。

        只改运行时票数（不写配置/不删节点/不碰虚机），corosync 重启自动失效。
        安全边界由 backend.quorum_status() 强校验：仅「在线 == 不可达」时放行，防两侧双写。"""
        body = self._read_body()
        if _norm_confirm(body.get("confirm")) != _norm_confirm("临时取得仲裁"):
            raise toolbox.ToolError("高危操作需输入确认词：临时取得仲裁")
        st = backend.quorum_status(fresh=True)      # 改票数前必须看到实时状态，不吃 5 秒缓存
        if not st.get("can_degrade"):
            raise toolbox.ToolError(st.get("reason") or "当前状态不允许临时取得仲裁")
        online = list(st.get("online") or [])
        # 用 votequorum 实时总票数而非数节点名（后者可能滞后），避免"下调了却仍不够"的空转
        target = max(1, int(st.get("votes") or len(online)))
        backend.set_expected_votes(target)
        after = self._wait_quorate()
        ok = bool(after.get("quorate"))
        jobs.record_event(
            "cluster_quorum", "临时取得仲裁（降级运行）", socket.gethostname().split(".")[0], ok,
            ["在线节点：%s" % ("、".join(online) or "无"),
             "不可达节点：%s" % ("、".join(st.get("offline") or []) or "无"),
             "预期票数：%s → %s" % (st.get("expected"), after.get("expected")),
             "集群仲裁：%s → %s" % ("有" if st.get("quorate") else "无", "有" if ok else "无"),
             "说明：仅下调运行时票数，未改集群配置；corosync 重启后自动失效，"
             "失联节点恢复后请点「恢复正常仲裁」"])
        return self._json({
            "ok": ok,
            "quorate": ok,
            "expected": after.get("expected"),
            "online": after.get("online"),
            "message": ("已临时取得仲裁，现在可以正常建机等写操作"
                        if ok else "票数已下调，但集群尚未恢复仲裁，请稍后刷新查看"),
        })

    def _wait_quorate(self, tries=4, delay=1.0):
        """降级后轮询确认是否真恢复仲裁（总等待 <5 秒，避免前端误判超时重试）。"""
        st = backend.quorum_status(fresh=True)
        for _ in range(max(0, tries)):
            if st.get("quorate"):
                break
            time.sleep(delay)
            st = backend.quorum_status(fresh=True)
        return st

    def _restore_quorum(self):
        """还原预期票数为集群节点总数，清除运行时下调。

        长期停留降级态（半数即可写）遇真实分区会双写损坏数据，故节点全在线后应显式还原。"""
        body = self._read_body()
        if _norm_confirm(body.get("confirm")) != _norm_confirm("恢复正常仲裁"):
            raise toolbox.ToolError("高危操作需输入确认词：恢复正常仲裁")
        st = backend.quorum_status(fresh=True)      # 还原前必须看到实时票数，不吃 5 秒缓存
        total = int(st.get("total_nodes") or 0)
        if not st.get("in_cluster") or total < 1:
            raise toolbox.ToolError("本机未加入集群，无需还原仲裁")
        if not st.get("needs_restore"):
            return self._json({"ok": True, "expected": st.get("expected"), "quorate": bool(st.get("quorate")),
                               "message": "预期票数已是集群节点总数，无需还原"})
        # corosync 保护：在线票数不足以支撑目标预期票数时（有节点离线），set_expected_votes 会返回
        # CS_ERR_INVALID_PARAM 并让集群立即失去仲裁。提前挡下并给友好提示，避免把底层报错抛给用户。
        offline = [str(n) for n in (st.get("offline") or [])]
        if offline:
            raise toolbox.ToolError(
                "仍有节点离线（%s）：此时恢复预期票数会使集群立即失去仲裁，corosync 会拒绝。"
                "请等该节点恢复在线后再重试；节点重新加入集群后预期票数会自动回升，通常无需手动操作。"
                % "、".join(offline))
        try:
            backend.set_expected_votes(total)
        except Exception as e:
            msg = str(e)
            if "CS_ERR_INVALID_PARAM" in msg or "expected votes" in msg.lower():
                raise toolbox.ToolError(
                    "恢复预期票数被 corosync 拒绝：当前在线票数不足以支撑目标预期票数（%d）。"
                    "请确认所有节点均已在线后再重试；节点恢复后会自动回升，无需手动操作。" % total)
            raise
        after = backend.quorum_status()
        jobs.record_event(
            "cluster_quorum", "恢复正常仲裁（还原预期票数）", socket.gethostname().split(".")[0],
            not bool(after.get("needs_restore")),
            ["集群节点总数：%d" % total,
             "预期票数：%s → %s" % (st.get("expected"), after.get("expected")),
             "集群仲裁：%s" % ("有" if after.get("quorate") else "无（请确认所有节点均已在线）")])
        return self._json({"ok": True, "expected": after.get("expected"),
                           "quorate": bool(after.get("quorate")),
                           "message": "已还原为按集群节点总数计票"})

    def _create_cluster(self):
        """在当前节点创建集群（等价 pvecm create <名>）：只适用于「还没有集群」的节点。

        「加入节点」的前提是控制节点已在集群里，所以这里补上建集群这一步：集群名创建后不可更改、
        且会生成集群 CA 并重启 pve-cluster/corosync，属高危操作，故要求手输确认词。"""
        _blocked = _join_blocked_reason()
        if _blocked:
            return self._error(_blocked, 403)
        body = self._read_body()

        # 服务端强校验确认词：集群名不可改，误点一次就要重建集群才能改名
        if _norm_confirm(body.get("confirm")) != _norm_confirm("创建集群"):
            raise toolbox.ToolError("高危操作需输入确认词：创建集群")
        name = backend.normalize_cluster_name(body.get("name"))
        local = _local_outbound_ip() or socket.gethostname().split(".")[0]
        in_cluster, why = self._target_in_cluster(local)
        if in_cluster:
            raise toolbox.ToolError("本机已在集群中，无需创建（集群名创建后不可更改）")
        if why:
            raise toolbox.ToolError(why)
        backend.create_cluster(name)
        # 建集群要拉起 corosync/pmxcfs，状态不是立刻可取：轮询确认真建成了再回包。
        # 总等待压在 10 秒内——前端单请求超时 15 秒，久等会被前端判成失败并自动重试。
        ready = False
        for _ in range(5):
            time.sleep(2)
            ready, _why = self._target_in_cluster(local)
            if ready:
                break
        jobs.record_event("cluster_create", f"创建集群：{name}",
                          socket.gethostname().split(".")[0], True,
                          [f"集群名：{name}", "已在本节点创建集群"])
        return self._json({
            "ok": True,
            "name": name,
            "ready": ready,
            "message": "" if ready else "集群已创建，但状态尚未就绪，请稍后刷新查看",
        })

    def _join_cluster(self):
        """把一台新 PVE 主机加入当前集群：中心节点 SSH（密码）到新机器，在其上执行 pvecm add。

        密码等敏感信息只用于本次 SSH 会话，绝不写入日志。"""
        _blocked = _join_blocked_reason()
        if _blocked:
            return self._error(_blocked, 403)
        import ipaddress, os, re
        body = self._read_body()
        new_host = str(body.get("new_host", "")).strip()
        new_pw = str(body.get("new_password", ""))
        target_ip = str(body.get("target_ip", "")).strip()
        if not target_ip:
            # 默认加入目标 = 本机（控制节点，已是集群成员）
            target_ip = _local_outbound_ip() or socket.gethostname().split(".")[0]
        target_pw = str(body.get("target_password", ""))
        if not all([new_host, new_pw, target_ip]):
            raise toolbox.ToolError("请完整填写：新节点地址、新节点密码、目标集群节点地址")
        # 地址合法性：仅允许安全字符，二次防注入（即使走密码 SSH，也先过滤）
        if not re.match(r"^[\w.\-:\[\]]+$", new_host) or not re.match(r"^[\w.\-:\[\]]+$", target_ip):
            raise toolbox.ToolError("节点地址格式非法（仅允许字母数字及 .-:[] ）")
        # 新节点必须用 IP：新机器尚未入集群，本机无法解析其主机名；且主机名可能与本机/现有节点重名
        try:
            ipaddress.ip_address(new_host)
        except ValueError:
            raise toolbox.ToolError("新节点地址请填写 IP（不支持主机名，避免与本机或现有节点重名）")
        if new_host in {_local_outbound_ip(), "127.0.0.1", "::1"} or socket.gethostname().split(".")[0] == new_host:
            raise toolbox.ToolError("新节点地址不能填本机（控制节点）自己的地址")
        # 前置检查：pvecm add 只能把节点加入「已存在的集群」。目标节点还是单机时，加机只会在对端
        # 异步任务里失败（CLI 退出码照样是 0），报错很绕；这里先说清楚该怎么补。
        in_cluster, why = self._target_in_cluster(target_ip)
        if not in_cluster:
            if why:
                raise toolbox.ToolError(why)
            raise toolbox.ToolError(
                "目标节点 %s 还没有加入任何集群，无法接收新节点。\n"
                "请先在控制节点上创建集群，再执行「加入节点」（集群名创建后不可更改）。"
                % target_ip)
        # 连接尚未建立互信的新节点需要“密码 SSH”（sshpass 或 ssh+setsid 均可）
        if not ssh.password_ssh_available():
            raise toolbox.ToolError("本机缺少 ssh 与 setsid，无法用密码连接新节点")
        if target_pw:
            # 方式一（PVE 原生）：把目标节点密码经 stdin 喂给 pvecm add
            remote_script = 'echo "$SSH_TARGET_PW" | pvecm add "$SSH_TARGET_IP"'
            remote_cmd = ("SSH_TARGET_PW=" + shlex.quote(target_pw) +
                          " SSH_TARGET_IP=" + shlex.quote(target_ip) +
                          " bash -c " + shlex.quote(remote_script))
        else:
            # 方式二（免目标密码）：借本集群已有的互信，把新节点公钥装到目标节点后免密 pvecm add。
            # 必须带 --use_ssh：不加时 pvecm add 走的是默认的「对端 API + 交互索要 root 密码」流程
            # （见 pvecm(1)：--use_ssh 才是「始终走 SSH 加入」），在无终端环境下它读不到密码，
            # 会立刻以 "EOF while reading password" 结束加入任务；只有 --use_ssh 才会用上刚刚
            # 铺好的免密 SSH 通道（pve02 → 目标节点），做到真正免目标密码。
            self._install_new_node_key(new_host, new_pw, target_ip)
            remote_cmd = ("SSH_TARGET_IP=" + shlex.quote(target_ip) +
                          " bash -c 'pvecm add \"$SSH_TARGET_IP\" --use_ssh'")
        try:
            r = ssh.password_ssh_run(new_host, [remote_cmd], new_pw, timeout=300)
        except subprocess.TimeoutExpired:
            raise toolbox.ToolError("加入集群超时（超过 5 分钟），请检查网络与新节点状态")
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "未知错误").strip()
            raise toolbox.ToolError("加入集群失败：" + err)
        # pvecm add 起的是「异步集群任务」，CLI 退出码 0 不代表任务成功（失败时 stdout 也常常为空，
        # 例如缺 --use_ssh 时任务以 "EOF while reading password" 结束、CLI 照样返回 0）。
        # 因此必须回到新节点确认它真的进了集群，否则会把失败当成功上报（节点管理里却一直看不到）。
        joined_ok, joined_why = self._verify_joined(new_host, new_pw)
        if not joined_ok:
            raise toolbox.ToolError("加入集群没有生效：" + joined_why)
        # 集群加入成功，自动接入共享存储（挂 NFS + 铺 vBIOS 软链）；失败仅告警，不回滚集群加入
        ok_init, msg_init = self._join_init_remote(new_host, new_pw)
        jobs.record_event("cluster_join", f"加入节点：{new_host} → {target_ip}", new_host, True,
                          [(r.stdout or "").strip() or "[已完成] 加入集群执行成功",
                           ("共享存储接入：" + msg_init) if ok_init else ("⚠ 共享存储接入未完成：" + msg_init)])
        return self._json({
            "joined": new_host,
            "output": (r.stdout or "").strip(),
            "storage_init": {"ok": ok_init, "msg": msg_init},
        })

    def _verify_joined(self, new_host, new_pw):
        """确认新节点真的已进入集群：返回 (ok, 失败原因)。

        `pvecm add` 只是启动一个异步的 clusterjoin 任务，CLI 退出码不反映该任务成败，
        且失败时 stdout 常为空（典型：没走免密 SSH 时任务以 "EOF while reading password"
        结束，CLI 仍返回 0）。所以不能只看退出码，必须回新节点以 `pvecm status` 是否给出
        "Cluster information" 作为判据。"""
        script = (
            "for i in 1 2 3 4 5; do\n"
            "  S=$(pvecm status 2>&1 | head -n 8)\n"
            "  if printf '%s\\n' \"$S\" | grep -q 'Cluster information'; then\n"
            "    echo 'PVEWT-JOIN-OK'\n"
            "    exit 0\n"
            "  fi\n"
            "  sleep 3\n"
            "done\n"
            "echo 'PVEWT-JOIN-FAIL'\n"
            "printf '%s\\n' \"$S\"\n")
        try:
            r = ssh.password_ssh_run(new_host, ["bash", "-s"], new_pw,
                                           input_text=script, timeout=60)
        except subprocess.TimeoutExpired:
            return False, "确认新节点集群状态超时（无法判定是否已加入）"
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if "PVEWT-JOIN-OK" in out:
            return True, ""
        detail = "\n".join(ln for ln in out.splitlines()
                           if ln.strip() and "PVEWT-JOIN-" not in ln).strip()
        if not detail:
            detail = ("无法连回新节点确认集群状态（SSH 退出码 %s）" % r.returncode
                      if r.returncode != 0 else "新节点仍未加入集群")
        return False, detail

    def _join_init_remote(self, new_host, new_pw):
        """加入集群成功后，SSH 到新节点接入共享存储：挂 NFS(WORKSPACE_PATH) + 铺 vBIOS 软链。

        中心化管理场景下新节点未装 pvewebtools，无法调本地 /api/node/join-init，
        故由中心节点 SSH 过去执行等效命令。失败仅返回告警，不影响已完成的集群加入。"""
        try:
            cfg = storage_provision._load_config()
        except Exception:
            return False, "读取存储配置失败，跳过共享存储接入"
        # 统一走 _resolve_base：NFS_SERVER 留空而 NFS_SERVER_MODE=1（默认配置）时按本机 LAN IP
        # 自动补全，否则这里会误判「未配置 NFS_SERVER」而跳过挂载（新节点起来后一直没共享存储）。
        wtype, server, export, path = storage_provision._resolve_base(cfg)
        if wtype != "nfs" or not server:
            return False, "未启用 NFS 共享存储（本地 dir 模式），跳过共享存储接入（仅完成集群加入）"
        names = list(_read_vbios_manifest())
        remote = self._storage_init_remote_script(path, server, export, names)
        try:
            r = ssh.password_ssh_run(new_host, ["bash", "-s"], new_pw,
                                           input_text=remote, timeout=300)
        except subprocess.TimeoutExpired:
            return False, "共享存储接入超时（挂载 NFS）"
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "未知错误").strip()[:300]
        return True, (r.stdout or "").strip() or "共享存储接入完成"

    def _resolve_node(self, node):
        """解析执行目标：留空/本机→本地执行；其他集群节点→节点名"""
        node = (node or "").strip()
        if not node:
            return None
        if not node.replace("-", "").replace("_", "").isalnum():
            raise toolbox.ToolError(f"非法节点名: {node}")
        known = backend.known_node_names()
        if node not in known:
            raise toolbox.ToolError(f"节点不存在: {node}")
        # 选的就是本机时无需 SSH，直接本地执行
        if node == socket.gethostname().split(".")[0]:
            return None
        return node

