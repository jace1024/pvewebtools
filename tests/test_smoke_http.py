"""M1.2 冒烟：服务能起、静态资源能出、未知 API 回 404。

只覆盖**不依赖 pvesh** 的路径，保证在任意开发机（Windows / 无 PVE）都能跑通。
业务接口（需要 pvesh 的）后续用 mock 补齐，见 M1 清单。

本文件验证的是路由/静态资源/错误映射，与登录门禁无关，故统一在 --no-auth 下运行
（登录门禁本身由 test_auth_*.py 单独覆盖）。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_auth_bypass():
    os.environ["PVE_WEBTOOLS_NO_AUTH"] = "1"
    yield
    os.environ.pop("PVE_WEBTOOLS_NO_AUTH", None)


def test_index_ok(http):
    status, _headers, body = http("/")
    assert status == 200
    assert b"app.js" in body


def test_index_injects_asset_version(http):
    """首页把 app.js 引用改写成带 ?v=<mtime-size> 的版本号，杜绝旧缓存。"""
    _status, _headers, body = http("/")
    assert b"app.js?v=" in body


def test_index_html_directly(http):
    status, _headers, body = http("/index.html")
    assert status == 200
    assert b"<html" in body.lower()


def test_static_app_js(http):
    status, _headers, body = http("/app.js")
    assert status == 200
    assert len(body) > 1000


def test_static_style_css(http):
    status, _headers, body = http("/style.css")
    assert status == 200
    assert len(body) > 100


def test_unknown_api_returns_404_json(http):
    status, headers, body = http("/api/__definitely_not_a_route__")
    assert status == 404
    assert "json" in headers.get("Content-Type", "")
    assert b"error" in body
