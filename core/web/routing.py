"""路由分发骨架：路径切分、选项白名单判定、登录门禁 / CSRF 校验 / 统一异常映射，以及四个 HTTP 方法的处理入口。

四个 ``_handle_*`` 只做「查表分发 + 未命中 404」，具体路由全部在 ``routers/`` 里按域声明。
"""

from urllib.parse import urlparse

from core.web import auth_session, errors, routers


class RoutingMixin:
    """HTTP 方法入口：登录门禁、CSRF 校验、写闸口与统一异常映射。"""

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
        """「分发 + 异常映射」的唯一入口。

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
        # do_DELETE 不检查 _api_parts 结果，parts 可能是 None——dispatch 会判未命中
        if routers.route(self, "delete", parts):
            return
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
        if routers.route(self, "get", parts):
            return
        self._error("未知接口", 404)

    def _handle_post(self, parts):
        if routers.route(self, "post", parts):
            return
        self._error("未知接口", 404)

    def _handle_put(self, parts):
        if routers.route(self, "put", parts):
            return
        self._error("未知接口", 404)

