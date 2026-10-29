"""系统升级域路由与实现（结构重构 M2/T2.6 第九批路由表化 + 深化第二批实现迁入）。

覆盖升级包的整包上传与安装前校验（``upgrade/upload|inspect|apply|rollback|package``）以及
只读的状态与日志（``upgrade/status|log``）。

本域**自包含**：只依赖 ``core.pve.upgrade`` 与处理器基础设施（``_read_body`` / ``_json`` /
``self.rfile`` / ``self.headers`` / ``self.path``），不 import ``core.web.server``——唯一例外
是 ``RUN_PORT``（升级状态要报服务实际监听的端口），用函数内延迟导入以避免循环导入。

``upgrade/apply`` 的替换动作交给独立的 transient unit 执行，本进程随后会被 systemctl stop
停掉，所以它只做校验 + 提交，前端轮询 ``/api/upgrade/status`` 观察结果。
"""

from urllib.parse import parse_qs, urlparse

from core.pve import upgrade


def _status(handler, _params):
    """GET /api/upgrade/status  当前版本 / 部署形态 / 升级状态 / 已上传包（只读）。"""
    from core.web import server as _server   # 延迟导入：RUN_PORT 是 server 的模块级常量

    return handler._json(upgrade.status(_server.RUN_PORT))


def _log(handler, _params):
    """GET /api/upgrade/log?lines=N  升级日志尾部（只读，行数限制在 1..2000）。"""
    q = parse_qs(urlparse(handler.path).query)
    try:
        lines = int((q.get("lines") or ["200"])[0])
    except ValueError:
        lines = 200
    return handler._json({"log": upgrade.log_tail(min(max(lines, 1), 2000))})


def _upload(handler, _params):
    """POST /api/upgrade/upload?name=xxx  整包上传升级包（raw body，边收边落盘）。

    升级包体积不大（onefile 产物通常几十 MB），不需要走分片上传那套；文件名与大小在升级
    模块内做白名单/上限校验，落盘到本机 /var/tmp。
    """
    q = parse_qs(urlparse(handler.path).query)
    name = (q.get("name") or [""])[0]
    length = int(handler.headers.get("Content-Length") or 0)
    return handler._json(upgrade.receive_upload(handler.rfile, length, name))


def _inspect(handler, _params):
    """POST /api/upgrade/inspect  body:{name, force?, allow_downgrade?}  安装前校验。"""
    body = handler._read_body()
    return handler._json(upgrade.inspect(str(body.get("name") or ""),
                                         force=bool(body.get("force")),
                                         allow_downgrade=bool(body.get("allow_downgrade"))))


def _apply(handler, _params):
    """POST /api/upgrade/apply  body:{name, force?, allow_downgrade?}  执行升级（服务会重启）。"""
    body = handler._read_body()
    return handler._json(upgrade.apply(str(body.get("name") or ""),
                                       force=bool(body.get("force")),
                                       allow_downgrade=bool(body.get("allow_downgrade"))))


def _rollback(handler, _params):
    """POST /api/upgrade/rollback  回滚到升级前的版本（服务会重启）。"""
    return handler._json(upgrade.rollback())


def _package_delete(handler, _params):
    """DELETE /api/upgrade/package  body:{name}  删除已上传的升级包。"""
    body = handler._read_body()
    upgrade.delete_package(str(body.get("name", "")))
    return handler._json({"deleted": body.get("name", "")})


HANDLERS = {
    "get": {
        "upgrade/status": _status,
        "upgrade/log": _log,
    },
    "post": {
        "upgrade/upload": _upload,
        "upgrade/inspect": _inspect,
        "upgrade/apply": _apply,
        "upgrade/rollback": _rollback,
    },
    "delete": {
        "upgrade/package": _package_delete,
    },
}
