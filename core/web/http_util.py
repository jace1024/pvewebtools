"""HTTP 基础设施与「响应出口净化」（结构重构 M2/T2.1 从 server.py 抽出）。

分两块，都只依赖标准库、不依赖 server.py（避免循环导入）：

1. **输出净化**：不把实现细节（堆栈 / 解释器路径 / 内部异常类型）回给前端。多处把
   ``str(e)`` 或子进程 stderr 直接拼进响应体与任务日志，会把解释器路径、内部异常类型、
   第三方包信息暴露给浏览器。这里在**响应出口**（HTTP JSON / SSE）统一擦除：命中技术特征时
   只保留调用方写好的中文前缀（如「删除失败」），完整原文写服务日志（journalctl 可查，排障
   不受影响）；带 ``--debug`` 启动时原样输出（编译产物已屏蔽该开关，见下）。

2. **HttpUtilMixin**：统一收发响应的方法（JSON / 错误 / 静态资源 / gzip / ETag / body 读取），
   以 mixin 形式供 ``server.ApiHandler`` 多继承，保持方法体与迁移前完全一致。

``MAX_BODY_SIZE`` 随 ``_read_body`` 一并迁来；``server.py`` 再从本模块 import 回去，
因此原有调用点（``_sanitize_text`` / ``_DEBUG_DETAIL`` 等）无需改动。
"""

import gzip
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# 请求体上限：JSON body 都是小体（参数/配置），异常大的声明直接拒绝，避免一次性读入大量内存
MAX_BODY_SIZE = 16 * 1024 ** 2   # 16MB


# ---- 输出净化 ----
_LEAK_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|File \"[^\"]+\", line \d+"
    r"|\[Errno \d+\]"
    r"|/(?:usr|opt)/lib/python\d(?:\.\d+)?/"
    r"|site-packages|dist-packages"
    r"|cpython-\d+"
    r"|\.py[cod]\b|\.py\b"
    r"|<class '[^']+'>"
    r"|\b(?:ModuleNotFoundError|ImportError|AttributeError|KeyError|TypeError|ValueError"
    r"|IndexError|RuntimeError|FileNotFoundError|PermissionError|IsADirectoryError"
    r"|NotADirectoryError|OSError|NameError|UnboundLocalError|ZeroDivisionError)\b"
    r"|\bsubprocess\.[A-Za-z_]+\b"
)
_LEAK_FALLBACK = "内部错误，详情请查看服务日志"
_LEAK_SUFFIX = "（详情请查看服务日志）"
_DEBUG_DETAIL = bool(os.environ.get("PVE_WEBTOOLS_DEBUG")) or ("--debug" in sys.argv)
# 编译产物（入口 core/entry/main.py 已屏蔽 --debug 与 PVE_WEBTOOLS_DEBUG，并置
# PVE_WEBTOOLS_BUILT=1）不再提示调试开关——报错保持中性，不引导客户去找调试入口。
_DEBUG_HINT = "" if os.environ.get("PVE_WEBTOOLS_BUILT") else "（加 --debug 启动可查看细节）"


def _sanitize_text(text):
    """擦除单条文本里的实现细节，返回可直接给前端/日志的文案。

    只认「高置信度的技术特征」（堆栈行、[Errno]、site-packages、内部异常类名等），
    自撰的中文业务文案（如「存储不可用」）原样保留，因此可安全地用在所有出口上。
    """
    if not isinstance(text, str) or not text:
        return text
    m = _LEAK_RE.search(text)
    if m is None:
        return text
    if _DEBUG_DETAIL:
        return text
    try:
        # 完整原文只进服务日志（root 用 journalctl -u pvewebtools 可查，排障能力不丢）
        sys.stderr.write("[detail] " + text[:2000].replace("\n", " ") + "\n")
    except Exception:  # noqa: BLE001
        pass
    head = text[:m.start()].strip(" \t\r\n:：-—,，;；.。")
    if 2 <= len(head) <= 40:
        return head + _LEAK_SUFFIX
    return _LEAK_FALLBACK


def _sanitize_out(obj):
    """递归净化响应结构里的字符串（dict / list / tuple / str），其余类型原样返回。"""
    if isinstance(obj, str):
        return _sanitize_text(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_out(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_out(v) for v in obj]
    return obj


class HttpUtilMixin:
    """HTTP 收发基础设施（由 server.ApiHandler 多继承）。

    注意 MRO：``ApiHandler(HttpUtilMixin, SimpleHTTPRequestHandler)``，故这里的
    ``end_headers`` / ``handle_one_request`` / ``handle_error`` / ``send_head`` 里对
    ``super()`` 的调用都会落到 ``SimpleHTTPRequestHandler``（或 ``BaseHTTPRequestHandler``）。
    """

    # 自定义 Server 头，不暴露 Python/BaseHTTP 实现细节
    server_version = "pvewebtools"
    sys_version = ""

    # 静态文本资源 gzip 结果缓存：键 (真实路径, mtime_ns, size)，避免每次请求重复压缩
    _gzip_cache = {}

    def end_headers(self):
        # 默认 no-store（API / SSE 等动态响应不缓存）；
        # 静态资源在发送前设置 self._cache_control 覆盖（带 ETag，命中回 304）
        self.send_header("Cache-Control", getattr(self, "_cache_control", "no-store"))
        super().end_headers()

    def handle_one_request(self):
        # 每个请求重置缓存策略（默认 no-store），避免 keep-alive 复用实例时串味
        self._cache_control = "no-store"
        return super().handle_one_request()

    def handle_error(self, request, client_address):  # noqa: ARG002
        # 客户端中途断开（取消下载/关闭页面）属正常行为；其它异常也不打印 Python 栈追踪
        # （栈里含 .py 路径与解释器细节），细节仅在 --debug 模式下输出（编译产物无此出口）。
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        if _DEBUG_DETAIL:
            super().handle_error(request, client_address)
            return
        sys.stderr.write("[error] 请求处理异常" + _DEBUG_HINT + "\n")

    def _accepts_gzip(self):
        """客户端是否在 Accept-Encoding 中声明支持 gzip（忽略 q 权重，稳健即可）"""
        ae = self.headers.get("Accept-Encoding", "")
        for token in ae.split(","):
            if token.split(";")[0].strip().lower() == "gzip":
                return True
        return False

    def _send_json(self, body, status, content_type="application/json; charset=utf-8"):
        """统一发送 JSON：客户端支持 gzip 时压缩响应体（列表/状态接口体积大，
        压缩后通常仅 10%~20%），并带 Vary: Accept-Encoding 避免中间缓存误缓存。
        Content-Length 按压缩后长度填写。

        注意：所有 send_header 必须晚于 send_response —— BaseHTTPRequestHandler
        按调用顺序拼接 _headers_buffer，若先 send_header 会把响应头写到状态行之前，
        产出畸形 HTTP 响应（浏览器表现为 Failed to fetch）。故压缩先算好，再统一发头。"""
        # 批量端点（/api/options）采集模式：只收集响应、不真正写出，
        # 由 _handle_options 把多次 GET 结果汇总成单个响应返回。
        cap = getattr(self, "_capture", None)
        if cap is not None:
            cap.append((status, body))
            return
        use_gzip = self._accepts_gzip()
        if use_gzip:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        # 出口净化：擦除响应里可能夹带的实现细节（统一在出口做，调用方无需逐处处理）
        body = json.dumps({"data": _sanitize_out(data)}, ensure_ascii=False).encode("utf-8")
        self._send_json(body, status)

    def _error(self, message, status=400):
        body = json.dumps({"error": _sanitize_text(message)}, ensure_ascii=False).encode("utf-8")
        self._send_json(body, status)

    # ---- 静态资源：ETag 条件缓存 + 按需 gzip ----

    @staticmethod
    def _is_compressible(ctype):
        return (ctype in ("application/javascript", "application/manifest+json", "image/svg+xml")
                or ctype.startswith(("text/", "application/json", "application/xml")))

    def _send_static(self, body, ctype, etag, cache_control, gzip_key=None):
        """发送静态资源：If-None-Match 命中则回 304（仅响应头、无响应体）；文本类型按需 gzip。
        ETag 由 mtime+size 生成，文件一改 ETag 即变，保证「更新后立即生效」。"""
        self._cache_control = cache_control
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        out = body
        if self._accepts_gzip() and self._is_compressible(ctype) and len(body) >= 1024:
            if gzip_key is not None:
                out = self._gzip_cache.get(gzip_key)
                if out is None:
                    out = gzip.compress(body, 6)
                    if len(self._gzip_cache) > 16:   # 简易容量控制：仅缓存最近少量文件
                        self._gzip_cache.clear()
                    self._gzip_cache[gzip_key] = out
            else:
                out = gzip.compress(body, 6)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")   # 响应随 Accept-Encoding 变化，避免中间缓存误用
        if out is not body:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)

    def send_head(self):
        """静态文件：加 ETag 条件缓存（命中回 304）与按需 gzip。
        带 ?v= 版本号（index.html 注入 mtime）的资源可长缓存；其余每次校验。"""
        path = self.translate_path(self.path)
        p = Path(path)
        if not p.is_file():
            return super().send_head()      # 目录 / 不存在：交回基类产出标准 404 等
        try:
            st = p.stat()
            body = p.read_bytes()
        except OSError:
            return super().send_head()
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        cc = ("public, max-age=31536000, immutable"
              if "v=" in urlparse(self.path).query else "no-cache")
        self._send_static(body, self.guess_type(path), etag, cc,
                          gzip_key=(path, st.st_mtime_ns, st.st_size))
        return None

    def _read_body(self):
        """读取 POST JSON body，无内容时返回空字典；超限时返回空（防内存 DoS）"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        # 请求体上限：JSON body 都是小体（参数/配置），异常大的声明直接拒绝，避免一次性读入大量内存
        if length > MAX_BODY_SIZE:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
