"""任务日志实时推送（SSE）。

连接即先回放已有输出，之后逐行推送新增，任务终态时推 ``event: done``；替代前端 800ms
轮询，断流（服务重启）由前端回退轮询兜底。以 mixin 形式供 ``server.ApiHandler`` 多继承，
依赖由本模块自行 import（``jobs`` / ``_sanitize_text`` / ``json`` / ``parse_qs`` /
``urlparse``）。
"""

import json
from urllib.parse import parse_qs, urlparse

from core.web import jobs
from core.web.http_util import _sanitize_text


class SseMixin:
    # ---- SSE（任务日志实时推送）辅助 ----
    # 抽成类方法，而不是 _handle_job_stream 里的局部函数：该方法有两条路径（内存任务 / 落盘兜底），
    # 若把 send/enc 定义在第二条路径处，第一条路径引用它们会运行时 NameError。

    @staticmethod
    def _sse_enc(s):
        """压成单行，避免换行破坏 SSE 帧"""
        return str(s).replace("\r", " ").replace("\n", " ")

    def _sse_begin(self):
        """发送 SSE 响应头（文本流 + 关闭中间缓冲/压缩）

        SSE 没有 Content-Length，流的结束由「关闭连接」界定。服务端已启用 HTTP/1.1 长连接，
        若不显式声明关闭，推完 ``event: done`` 后套接字会保持打开，浏览器 EventSource 便一直
        等下一帧（表现为任务已结束但日志连接不释放）。``Connection: close`` 会同时把
        ``close_connection`` 置 True，推完即断开，与原有 HTTP/1.0 行为一致。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self._cache_control = "no-cache"   # 由 end_headers 统一发出，避免重复 Cache-Control
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()

    def _sse_send(self, ev=None, data=""):
        """写一帧 SSE（可选 event: 行 + data: 行）。任务日志同理过出口净化（见 _sanitize_text）"""
        data = _sanitize_text(data)
        if ev:
            self.wfile.write(("event: " + ev + "\n").encode("utf-8"))
        self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _handle_job_stream(self, job_id):
        """SSE 实时推送任务日志：连接即先回放已有输出，之后逐行推送新增，任务终态时推 event: done。

        替代前端 800ms 轮询：省去每条日志一次 HTTP 往返；断流（服务重启）由前端回退轮询兜底。
        两条路径都必须自己发 SSE 响应头（含落盘兜底那条）。"""
        job = jobs.get_raw(job_id)
        if job is None:
            # 内存中已无（已结束且被淘汰）：从落盘日志兜底回放全量并立即 done
            snap = jobs.load_log(job_id)
            if snap is None:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._sse_begin()
            try:
                for ln in (snap.get("lines") or []):
                    self._sse_send(data=self._sse_enc(ln))
                self._sse_send(ev="done", data=json.dumps(
                    {"status": snap.get("status"), "returncode": snap.get("returncode")},
                    ensure_ascii=False))
            except (BrokenPipeError, OSError, ConnectionAbortedError):
                pass
            return
        # from：前端首拉已取走前 N 行，SSE 只回放该索引之后新增的行，避免重复输出
        start = 0
        try:
            qs = parse_qs(urlparse(self.path).query)
            start = int(qs.get("from", ["0"])[0])
        except (ValueError, IndexError):
            start = 0
        if start < 0:
            start = 0
        self._sse_begin()
        try:
            last = start
            while last < len(job.lines):   # 回放 from 之后的已存在输出
                self._sse_send(data=self._sse_enc(job.lines[last]))
                last += 1
            while True:
                job._new_data.wait(timeout=1.0)   # append 会 set；超时则检查终态
                job._new_data.clear()
                cur = len(job.lines)
                while last < cur:
                    self._sse_send(data=self._sse_enc(job.lines[last]))
                    last += 1
                if job.status != "running":
                    self._sse_send(ev="done", data=json.dumps(
                        {"status": job.status, "returncode": job.returncode}, ensure_ascii=False))
                    break
        except (BrokenPipeError, OSError, ConnectionAbortedError):
            return
