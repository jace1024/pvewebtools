"""路由分发骨架（M2/T2.6 深化第九批）：路径切分、选项白名单判定、四个 HTTP 方法的处理入口。 四个 _handle_* 现在只做「查表 + 末尾 404」——具体路由全在 routers/ 里，末尾保留的旧链注释是迁移记录。 搬成独立 mixin 后 server.py 只剩服务器骨架（do_* / log_message / 继承链）。"""

from urllib.parse import urlparse

from core.web import auth_session, errors, routers


class RoutingMixin:
    """从 ApiHandler 拆出的处理入口：方法体未改动，靠 self 访问其余基础设施。"""

    # ---- HTTP 方法入口：登录门禁 + 写闸口 + 统一异常映射（具体分发在 _handle_*）----

    # 登录门禁的公开豁免端点：登录态相关、版本胶囊、授权管理（熔断后仍须能上传授权码解熔断）。
    _AUTH_PUBLIC = ("login", "logout", "auth", "edition")

    def _require_auth(self, parts):
        """登录门禁：未通过校验的 /api/* 请求一律 401。

        豁免：--no-auth 破窗通道、登录/注销/状态/版本/授权管理、以及路径无法解析（交给后续 404）。
        其余请求必须携带合法会话 Cookie。返回 False 时已回写 401，调用方应直接 return。
        """
        if auth_session.disabled():
            return True
        if not parts:
            return True  # 非 /api 或未知路径，交给后续分发/404
        if parts[0] in self._AUTH_PUBLIC:
            return True
        if auth_session.verify(self.headers.get("Cookie")):
            return True
        self._error("未登录或会话已过期", 401)
        return False

    def _csrf_ok(self, gate):
        """写操作且登录门禁开启时，必须带 ``X-Requested-With: XMLHttpRequest`` 头，
        挡掉跨站伪造写请求（简单跨站请求无法自定义头）。--no-auth 下不校验（无登录即无需 CSRF）。"""
        if not gate or auth_session.disabled():
            return True
        return (self.headers.get("X-Requested-With") or "") == "XMLHttpRequest"

    def _dispatch_guarded(self, method, parts, gate=False):
        """「分发 + 异常映射」的唯一入口（M2/T2.7）。

        异常 → 状态码的规则集中在 ``core/web/errors.py``：可预期异常回显原因，未预期异常写
        服务端日志后回 500 中性文案——以前这类异常会让连接直接断开，客户端只看到
        ``RemoteDisconnected``。``gate=True`` 时把写闸口一并纳入兜底：闸口自身出错同样不该
        表现为断连。
        """
        try:
            if not self._require_auth(parts):
                return
            if not self._csrf_ok(gate):
                self._error("请求缺少 CSRF 防护头", 403)
                return
            if gate and not self._gate_write(parts):
                return
            getattr(self, "_handle_" + method)(parts)
        except Exception as e:  # noqa: BLE001  # 统一交给 errors 判定可预期与否
            errors.guard_error(self, e, method, parts)

    def do_GET(self):
        # 首页注入静态资源版本号：杜绝浏览器缓存旧版 app.js 导致新功能"不生效"
        if urlparse(self.path).path in ("/", "/index.html"):
            return self._serve_index()
        parts = self._api_parts()
        if parts is None:
            try:
                return super().do_GET()  # 静态文件
            except (BrokenPipeError, ConnectionResetError):
                return  # 客户端提前断开，不值得报栈
        self._dispatch_guarded("get", parts)

    def do_POST(self):
        parts = self._api_parts()
        if parts is None:
            return self._error("未知接口", 404)
        self._dispatch_guarded("post", parts, gate=True)

    def do_DELETE(self):
        # 注意：这里不检查 _api_parts 结果，parts 可能是 None——dispatch 会判未命中
        self._dispatch_guarded("delete", self._api_parts(), gate=True)

    def do_PUT(self):
        parts = self._api_parts()
        if parts is None:
            return self._error("未知接口", 404)
        self._dispatch_guarded("put", parts, gate=True)

    def _handle_delete(self, parts):
        # 路由表优先（M2/T2.5）：命中即返回；未命中回退下面的旧分支链（T2.6 逐域迁走）
        # 注意 do_DELETE 不检查 _api_parts 结果，parts 可能是 None——dispatch 会判未命中
        if routers.route(self, "delete", parts):
            return
        # DELETE /api/template-cache、/api/backup
        # 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # DELETE /api/storage、/api/storage-cfg/<storage>
        # 已迁到 routers/storage.py（M2/T2.6 第四批）
        # DELETE /api/vbios 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # DELETE /api/upgrade/package 已迁到 routers/upgrade.py（M2/T2.6 第九批）
        # DELETE /api/jobs/<job_id> 已迁到 routers/toolbox.py（M2/T2.6 第八批）
        self._error("未知接口", 404)

    def _api_parts(self):
        """返回 /api/ 之后的路径段列表；非 API 请求返回 None"""
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return None
        return [p for p in path[len("/api/"):].split("/") if p]

    def _is_option_source(self, parts):
        if not parts:
            return False
        if tuple(parts) in self._OPTION_SOURCES:
            return True
        # /guests/<node>/<type>/<vmid>/config 这类带参数的详情端点
        if len(parts) == 5 and parts[0] == "guests" and parts[4] == "config":
            return True
        return False

    def _handle_get(self, parts):
        # 路由表优先（M2/T2.5）：命中即返回；未命中回退下面的旧分支链（T2.6 逐域迁走）
        if routers.route(self, "get", parts):
            return
        # GET /api/options 已迁到 routers/system.py（M2/T2.6 第二批）
        # GET /api/upload/{status,verify} 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # GET /api/guests、/api/guests/<vmid>/snapshots、/api/deploy/{template-config,cloud-defaults}
        # 已迁到 routers/vm.py（M2/T2.6 第三批）
        # GET /api/disks、disks/{byid,storages}、storages/images 已迁到 routers/storage.py（M2/T2.6 第四批）
        # GET /api/vbios/roms、/api/vgpu/{materials,gpus,types}、/api/backup/{storages,files,download}、
        # /api/template-cache{,/nodes}
        # 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # GET /api/pci{,/nics,/gpus,/generic,/vfio} 已迁到 routers/network.py（M2/T2.6 第五批）
        # GET /api/usb/devices、/api/kernels{,/available}、/api/grub/backups、/api/network/backups
        # 已迁到 routers/network.py（M2/T2.6 第五批）
        # GET /api/network/{bridges,vlans,interfaces}、/api/pci
        # 已迁到 routers/network.py（M2/T2.6 第五批）
        # GET /api/nodes 已迁到 routers/node.py，逻辑抽成 _handle_nodes（M2/T2.6 第二批）
        # GET /api/cluster/{info,quorum}、/api/schedules
        # 已迁到 routers/cluster.py 与 routers/system.py（M2/T2.6 第九批）

        # GET /api/storages{,/images,/rootdir}、/api/storage/{move-targets,<s>/content,download}、
        # /api/storage-cfg{/<s>}、/api/disks/{vm,imported}
        # 已迁到 routers/storage.py（M2/T2.6 第四批）
        # GET /api/cloud-image/{status,list,presets,mirrors,storages}
        # 已迁到 routers/download.py（M2/T2.6 第七批）
        # GET /api/lxc-template/{available,downloaded,storages}
        # GET /api/community-scripts/list、/api/docker-versions
        # 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # GET /api/lxc-template/{cluster-sources,status,list}
        # 已迁到 routers/download.py（M2/T2.6 第七批）
        # GET /api/disks/{exported,download} 已迁到 routers/storage.py（M2/T2.6 第四批）
        # GET /api/edition、/api/upgrade/{status,log}
        # 已迁到 routers/system.py 与 routers/upgrade.py（M2/T2.6 第九批）
        # GET /api/tools、/api/jobs{,/<job_id>,/<job_id>/stream}
        # 已迁到 routers/toolbox.py（M2/T2.6 第八批）
        # GET /api/guests/<node>/<type>/<vmid>/{config,status} 已迁到 routers/vm.py（M2/T2.6 第三批）
        self._error("未知接口", 404)

    def _handle_post(self, parts):
        # 路由表优先（M2/T2.5）：命中即返回；未命中回退下面的旧分支链（T2.6 逐域迁走）
        if routers.route(self, "post", parts):
            return
        # POST /api/upgrade/{upload,inspect,apply,rollback}
        # 已迁到 routers/upgrade.py（M2/T2.6 第九批）
        # POST /api/backup/upload 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # POST /api/disks/{upload,delete}、/api/storage/{move-content,move}
        # 已迁到 routers/storage.py（M2/T2.6 第四批）
        # POST /api/vbios/{upload,sync}、/api/vgpu/upload
        # 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # POST /api/lxc-template/upload 已迁到 routers/download.py（M2/T2.6 第七批）
        # POST /api/node/join-init 已迁到 routers/node.py（M2/T2.6 第二批）
        # POST /api/cloud-image/{download,presets,presets/check,presets/reset}
        # 已迁到 routers/download.py（M2/T2.6 第七批）
        # POST /api/upload/register 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # POST /api/cloud-image/cancel、/api/lxc-template/{download,batch-source-swap,cancel}
        # 已迁到 routers/download.py（M2/T2.6 第七批）
        # POST /api/template-cache/{warmup,expire} 已迁到 routers/transfer.py（M2/T2.6 第六批）
        # POST /api/tools/<tool_id>/run、/api/jobs/clear、/api/jobs/<job_id>/kill
        # 已迁到 routers/toolbox.py（M2/T2.6 第八批）
        # POST /api/nodes/<node>/{reboot,storage-fix,nfs-server-fix} 已迁到 routers/node.py（M2/T2.6 第二批）
        # POST /api/cluster/{create,join,quorum/degrade,quorum/restore}
        # 已迁到 routers/cluster.py（M2/T2.6 第九批）

        # POST /api/guests/<node>/<type>/<vmid>/{onboot,description,rename,<action>}
        # 已迁到 routers/vm.py（M2/T2.6 第三批）
        # POST /api/storage-cfg 已迁到 routers/storage.py（M2/T2.6 第四批）
        self._error("未知接口", 404)

    def _handle_put(self, parts):
        # 路由表优先（M2/T2.5）：命中即返回；未命中回退下面的旧分支链（T2.6 逐域迁走）
        if routers.route(self, "put", parts):
            return
        # PUT /api/storage-cfg/<storage> 已迁到 routers/storage.py（M2/T2.6 第四批）
        self._error("未知接口", 404)

