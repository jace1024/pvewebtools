"""异常 → HTTP 状态码的唯一映射。

四个 ``do_*`` 各写一份 ``try/except`` 会让规则漂移——``GET``/``DELETE`` 漏了
``ToolError``，同一个异常换个 HTTP 方法就是另一种结果。更麻烦的是**未预期异常**：
它逃到 ``SilentHTTPServer.handle_error`` 时响应还没发出，连接被直接关掉，客户端只看到
``RemoteDisconnected``——既没有可读错误，服务端也只剩一行没有上下文的
``[error] 请求处理异常``（实测：``/api/nodes`` 在无 pvesh 环境下的 ``UnboundLocalError``）。

本模块把规则收敛到一处：

- 可预期异常按 :data:`EXC_STATUS` 回状态码并回显原因（与既有行为保持一致，不多改一分）；
- 未预期异常统一回 500 + 中性文案，异常类型与请求路径写服务端日志（``--debug`` 时附堆栈）；
- 客户端提前断开（取消下载 / 关页面）既不回包也不记日志。

沿用出口净化（``http_util._sanitize_text``）与「非 debug 不打栈」的既有约定：实现细节只留在
服务端日志里，不进给客户端的响应。
"""

import sys
import traceback

from core.pve import pve_api
from core.tools import toolbox
from core.web.http_util import _DEBUG_DETAIL, _DEBUG_HINT
from core.web.messages import t

__all__ = ["EXC_STATUS", "UNEXPECTED_MESSAGE", "client_gone", "guard_error",
           "log_unexpected", "status_for"]

# 可预期异常 → 状态码。顺序敏感：先匹配到的胜出，子类必须写在基类之前。
EXC_STATUS = (
    (toolbox.ToolError, 500),      # 工具拒绝执行（依赖缺失、参数非法…）
    (pve_api.PveError, 500),       # pvesh 调用失败
)

# 未预期异常给客户端的文案：不含异常类型与堆栈，细节只进服务端日志
UNEXPECTED_MESSAGE = t("err_internal")


def status_for(exc):
    """可预期异常 → 状态码；不属于任何一类时返回 ``None``（调用方按未预期处理）。"""
    for exc_type, status in EXC_STATUS:
        if isinstance(exc, exc_type):
            return status
    return None


def client_gone(exc):
    """客户端提前断开：不必回包，也不必记日志（取消下载 / 关页面是正常操作）。"""
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))


def log_unexpected(exc, method, parts):
    """未预期异常的服务端记录：默认一行摘要（请求路径 + 异常类型），``--debug`` 时附堆栈。"""
    sys.stderr.write("[error] %s /api/%s 未预期异常：%s: %s%s\n"
                     % (method.upper(), "/".join(parts or ()), type(exc).__name__, exc, _DEBUG_HINT))
    if _DEBUG_DETAIL:
        traceback.print_exc()


def guard_error(handler, exc, method, parts):
    """统一收口：按映射回包，并保证不再二次抛出。

    可预期异常回显原因；未预期异常记日志后回 500 中性文案。回包本身失败（响应头已经发出，
    例如 SSE 已开始推流）时静默放弃——此时连接状态由客户端去发现，再抛只会污染日志。
    """
    if client_gone(exc):
        return
    status = status_for(exc)
    if status is None:
        log_unexpected(exc, method, parts)
        status, message = 500, UNEXPECTED_MESSAGE
    else:
        message = str(exc)
    try:
        handler._error(message, status)
    except Exception:  # noqa: BLE001  # 响应已发出/连接已断，回包失败无从补救
        pass
