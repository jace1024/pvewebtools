"""路由域聚合（结构重构 M2/T2.5）。

每个路由域模块（``routers/<domain>.py``）暴露自己的 ``HANDLERS``；这里把它们合并成
``TABLES``（HTTP 方法 -> 已编译路由表），供 ``route()`` 查表分发。

本文件是包 ``__init__``，**不参与 Cython 编译**（作为数据文件随包分发），因此只放
import 与合并这层薄胶水，不放业务逻辑：分发协议在 ``routers/dispatch.py``（编译），
各路由域在 ``routers/<domain>.py``（编译）。新增一域 = 写一个域模块 + 在 ``_ROUTERS``
里追加一行（``merge`` 会合并并检查重复登记）。
"""

from core.web.routers import auth
from core.web.routers import cluster
from core.web.routers import dispatch
from core.web.routers import download
from core.web.routers import network
from core.web.routers import node
from core.web.routers import storage
from core.web.routers import system
from core.web.routers import toolbox
from core.web.routers import transfer
from core.web.routers import upgrade
from core.web.routers import vm

# 已迁入路由表的域模块（结构重构 M2/T2.6 逐批追加）
_ROUTERS = (
    auth,
    system,
    node,
    vm,
    storage,
    network,
    transfer,
    download,
    toolbox,
    cluster,
    upgrade,
)

TABLES = dispatch.merge(*_ROUTERS)


def route(handler, method, parts):
    """查表分发；命中返回 True（响应已回写），未命中返回 False（调用方回退旧分支链）。"""
    return dispatch.dispatch(handler, method, parts, TABLES)
