"""工具箱与任务队列域路由。

覆盖工具清单与运行（``tools``、``tools/<tool_id>/run``）以及任务队列（``jobs``、
``jobs/<job_id>``、``jobs/<job_id>/stream``、``jobs/<job_id>/kill``、``jobs/clear``、
``jobs/<job_id>`` 删除）。工具运行产出的就是任务、两者共用 ``core.web.jobs``，故合为一个域。

只声明数据，**不 import** ``core.web.server``（避免循环导入）：无参处理用方法名字符串，
带参处理用 ``fn(handler, params)``，参数由 ``<tool_id>`` / ``<job_id>`` 模板捕获。

``jobs/<job_id>/stream``（3 段）与 ``jobs/<job_id>``（2 段）段数不同，天然互斥；
``jobs/clear`` 是全字面 2 段、落精确表，与带参模板不冲突。
"""


def _run_tool(handler, params):
    """POST /api/tools/<tool_id>/run  body:{params:{...}, confirm:"工具名"}  运行工具。"""
    return handler._run_tool(params["tool_id"])


def _stream(handler, params):
    """GET /api/jobs/<job_id>/stream  SSE 实时日志流（替代前端轮询）。"""
    return handler._handle_job_stream(params["job_id"])


def _job_detail(handler, params):
    """GET /api/jobs/<job_id>[?offset=N]  任务详情（支持 ?offset= 增量拉取）。"""
    return handler._handle_job_detail(params["job_id"])


def _job_kill(handler, params):
    """POST /api/jobs/<job_id>/kill  强制结束运行中的任务（杀进程组，含远端节点）。"""
    return handler._handle_job_kill(params["job_id"])


def _job_delete(handler, params):
    """DELETE /api/jobs/<job_id>  删除已结束任务的日志。"""
    return handler._handle_job_delete(params["job_id"])


HANDLERS = {
    "get": {
        "tools": "_handle_tools",
        "jobs": "_handle_jobs",
        "jobs/<job_id>/stream": _stream,
        "jobs/<job_id>": _job_detail,
    },
    "post": {
        "tools/<tool_id>/run": _run_tool,
        "jobs/clear": "_handle_jobs_clear",
        "jobs/<job_id>/kill": _job_kill,
    },
    "delete": {
        "jobs/<job_id>": _job_delete,
    },
}
