"""M1.3 纯逻辑：响应出口的敏感信息净化（server._sanitize_text / _sanitize_out）。

这两处是所有 HTTP JSON / SSE 出口的咽喉，逻辑出错会把内部实现细节泄露给前端，
因此优先纳入单测。
"""

from core.web import server


def test_keeps_business_text():
    assert server._sanitize_text("存储不可用") == "存储不可用"
    assert server._sanitize_text("") == ""
    assert server._sanitize_text(None) is None


def test_scrubs_traceback():
    text = '删除失败\nTraceback (most recent call last):\n  File "x.py", line 3'
    out = server._sanitize_text(text)
    assert "Traceback" not in out
    assert "x.py" not in out


def test_scrubs_internal_exception_name():
    out = server._sanitize_text("创建失败 ModuleNotFoundError: No module named 'foo'")
    assert "ModuleNotFoundError" not in out


def test_sanitize_out_is_recursive():
    out = server._sanitize_out({"a": ["ok", 'File "y.py", line 9'], "b": 1})
    assert out["a"][0] == "ok"
    assert "y.py" not in out["a"][1]
    assert out["b"] == 1
