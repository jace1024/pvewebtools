"""异常 → 状态码映射。

单元部分钉住映射表本身；端到端部分往路由表临时注入「会炸」的目标，确认
「未预期异常 → 500 + JSON 错误体」而不是「连接被静默关闭」。

否则会表现为：``/api/nodes`` 在无 pvesh 环境下抛 ``UnboundLocalError``，
客户端只会看到 ``urllib.error.RemoteDisconnected``（连 HTTPError 都不是，拿不到任何错误信息）。
若本文件的端到端用例抛 RemoteDisconnected，说明兜底失效。
"""

import contextlib
import json
import os
import urllib.error
import urllib.request

import pytest

from core.pve import pve_api
from core.tools import toolbox
from core.web import errors
from core.web.routers import TABLES


@pytest.fixture(autouse=True)
def _no_auth_bypass():
    """本文件验证异常→状态码映射（与登录门禁正交），统一在 --no-auth 下运行服务。"""
    os.environ["PVE_WEBTOOLS_NO_AUTH"] = "1"
    yield
    os.environ.pop("PVE_WEBTOOLS_NO_AUTH", None)


def test_status_map_covers_expected_exceptions():
    assert errors.status_for(toolbox.ToolError("缺依赖")) == 500
    assert errors.status_for(pve_api.PveError("pvesh 失败")) == 500


def test_status_map_matches_subclasses():
    """映射用的是 isinstance 语义：子类必须跟着基类走（子类须声明在基类之前）。"""
    class _SubPveError(pve_api.PveError):
        pass

    assert errors.status_for(_SubPveError("子类")) == 500


def test_status_map_returns_none_for_unexpected():
    assert errors.status_for(RuntimeError("boom")) is None
    assert errors.status_for(KeyError("boom")) is None
    assert errors.status_for(UnboundLocalError("boom")) is None


def test_client_gone_classification():
    assert errors.client_gone(BrokenPipeError())
    assert errors.client_gone(ConnectionResetError())
    assert errors.client_gone(ConnectionAbortedError())
    assert not errors.client_gone(RuntimeError())


@contextlib.contextmanager
def _temp_route(method, template, target):
    """临时注入一条路由（仅测试用）。

    ``Table`` 没有 remove API（生产不需要热插拔路由），这里直接摘精确表里那一项——
    ``template`` 全是字面段，必定落在 ``_exact`` 而不是带参桶。
    """
    table = TABLES[method]
    table.add(template, target)
    try:
        yield
    finally:
        table._exact.pop(tuple(template.split("/")), None)


def _request(base_url, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data,
                                 headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_unexpected_exception_becomes_500_json(base_url):
    """未预期异常回 500 中性文案（不回细节、也不静默断连）。"""
    def _boom(handler, _params):
        raise RuntimeError("内部炸了")

    with _temp_route("get", "__boom__", _boom):
        status, body = _request(base_url, "GET", "/api/__boom__")

    assert status == 500
    assert json.loads(body) == {"error": errors.UNEXPECTED_MESSAGE}


def test_expected_exception_keeps_reason(base_url):
    """可预期异常回显原因（不替换成中性文案）。"""
    def _refuse(handler, _params):
        raise pve_api.PveError("pvesh 调用失败：存储不存在")

    with _temp_route("get", "__refuse__", _refuse):
        status, body = _request(base_url, "GET", "/api/__refuse__")

    assert status == 500
    assert "存储不存在" in json.loads(body)["error"]


def test_tool_error_is_500_on_every_method(base_url):
    """同一个异常在四个 HTTP 方法下必须是同一结果。

    同一个异常换个 HTTP 方法就换个结果，是规则散在四处的典型后果。
    """
    def _bad(handler, _params):
        raise toolbox.ToolError("该操作不可用")

    for method, template in (("get", "__bad_get__"), ("put", "__bad_put__"),
                             ("post", "__bad_post__"), ("delete", "__bad_delete__")):
        with _temp_route(method, template, _bad):
            status, body = _request(base_url, method.upper(), "/api/" + template)
        assert status == 500, method
        assert "该操作不可用" in json.loads(body)["error"], method


def test_tool_error_maps_to_500_on_read_methods(base_url):
    """``ToolError`` 在 GET 上也要映射。"""
    def _tool_refuse(handler, _params):
        raise toolbox.ToolError("该工具不可用")

    with _temp_route("get", "__tool_refuse__", _tool_refuse):
        status, body = _request(base_url, "GET", "/api/__tool_refuse__")

    assert status == 500
    assert "该工具不可用" in json.loads(body)["error"]


class _Recorder:
    """只记录 guard_error 回了什么，不做真实回包。"""

    def __init__(self):
        self.seen = None

    def _error(self, message, status):
        self.seen = (message, status)


def test_unexpected_exception_logs_and_replies_neutral(capsys):
    """未预期异常：回中性文案 + 状态码，并把类型/路径写进服务端日志。"""
    handler = _Recorder()
    errors.guard_error(handler, RuntimeError("内部细节不该外泄"), "get", ["x"])

    assert handler.seen == (errors.UNEXPECTED_MESSAGE, 500)
    err = capsys.readouterr().err
    assert "未预期异常" in err and "RuntimeError" in err
    # 客户端文案里不得出现异常细节
    assert "内部细节不该外泄" not in handler.seen[0]


def test_client_disconnect_is_silent(capsys):
    """客户端提前断开：不回包、不记日志（取消下载 / 关页面是正常操作）。"""
    handler = _Recorder()
    errors.guard_error(handler, BrokenPipeError("客户端跑了"), "get", ["x"])

    assert handler.seen is None
    assert "未预期异常" not in capsys.readouterr().err


def test_reply_failure_is_swallowed():
    """响应头已发出（如 SSE 已开始推流）时回包会失败——不能再让它冒泡。"""
    class _Broken:
        def _error(self, message, status):
            raise ValueError("headers already sent")

    errors.guard_error(_Broken(), pve_api.PveError("pvesh 失败"), "get", ["x"])
