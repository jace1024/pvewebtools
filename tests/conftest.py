"""pytest 公共夹具。

设计要点：
- 把仓库根加入 ``sys.path``，保证 ``import core.*`` 可用（``pytest.ini`` 的
  ``pythonpath`` 也做了同样的事，这里兜底，便于单独跑某个文件）。
- ``base_url`` 夹具：**直接**实例化 ``SilentHTTPServer``（端口 0 让内核分配），跑在
  后台线程里。**不调用** ``server.main()``——那会先做 pvesh 门槛检查（非 PVE 环境直接
  ``sys.exit``）并拉起若干后台循环，测试环境不适用。
- ``http`` 夹具：一个极薄的 GET 助手，把 HTTPError 也转成返回值，避免用例里到处 try。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def base_url():
    """启动真实 HTTP 服务（127.0.0.1:随机端口），返回 base_url，测试结束关闭。"""
    from core.web.server import ApiHandler, SilentHTTPServer

    srv = SilentHTTPServer(("127.0.0.1", 0), ApiHandler)
    host, port = srv.server_address[0], srv.server_address[1]
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def http(base_url):
    """返回 ``get(path) -> (status, headers, body)``；HTTPError 也返回而非抛出。"""
    import urllib.error
    import urllib.request

    def _get(path: str):
        req = urllib.request.Request(base_url + path)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    return _get
