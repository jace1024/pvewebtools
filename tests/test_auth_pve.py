"""auth_pve.verify_pve_credentials 的单元测试（mock PVE 响应，无需真机）。

只覆盖“凭据校验 + TFA 重试”的判定逻辑：200+ticket=成功、401+挑战头=需 TFA、
其余=失败、网络/TLS 故障=ERROR。
"""

import json
import urllib.error
import urllib.request

from core.web import auth_pve


class _FakeResp:
    """模拟成功的 HTTP 响应（支持 with 协议 + .status/.headers/.read）。"""

    def __init__(self, status, headers, payload):
        self.status = status
        self.headers = headers
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _make_httperror(code, headers, payload):
    err = urllib.error.HTTPError("https://x", code, "", headers, None)
    err.read = lambda: payload  # 默认 read 依赖 fp(None)，覆盖之
    return err


def _patch(monkeypatch, resp):
    monkeypatch.setattr(
        auth_pve.urllib.request, "urlopen",
        lambda *a, **k: resp if hasattr(resp, "status") else (_ for _ in ()).throw(resp),
    )


def test_ok_on_200_with_ticket(monkeypatch):
    # PVE 真实格式：结果包在 data 里
    resp = _FakeResp(200, {}, json.dumps({"data": {"ticket": "t", "username": "root@pam"}}).encode())
    _patch(monkeypatch, resp)
    status, data = auth_pve.verify_pve_credentials("root@pam", "pw")
    assert status == auth_pve.OK
    assert data == "root@pam"


def test_ok_flat_format_backward_compat(monkeypatch):
    # 兼容旧/平铺格式
    resp = _FakeResp(200, {}, json.dumps({"ticket": "t", "username": "root@pam"}).encode())
    _patch(monkeypatch, resp)
    status, data = auth_pve.verify_pve_credentials("root@pam", "pw")
    assert status == auth_pve.OK
    assert data == "root@pam"


def test_tfa_on_401_with_challenge_header(monkeypatch):
    headers = {"PVE-TFA-Token": "challenge123"}
    resp = _make_httperror(401, headers, b"{}")
    _patch(monkeypatch, resp)
    status, data = auth_pve.verify_pve_credentials("root@pam", "pw")
    assert status == auth_pve.TFA
    assert data == "challenge123"


def test_tfa_retry_with_otp_succeeds(monkeypatch):
    captured = {}

    def fake_urlopen(req, *a, **k):
        # Request.get_header 对大小写敏感，改读底层 headers 字典判断头是否已正确发出
        items = req.header_items()
        captured["has_tfa_header"] = any(k.lower() == "pve-tfa-token" for k, _ in items)
        captured["body"] = req.data.decode()
        return _FakeResp(200, {}, json.dumps({"data": {"ticket": "t", "username": "root@pam"}}).encode())

    monkeypatch.setattr(auth_pve.urllib.request, "urlopen", fake_urlopen)
    status, data = auth_pve.verify_pve_credentials(
        "root@pam", "pw", otp="123456", tfa_token="challenge123")
    assert status == auth_pve.OK
    assert "otp=123456" in captured["body"]
    assert captured["has_tfa_header"] is True


def test_tfa_on_200_with_null_data_and_header(monkeypatch):
    # PVE 需 TFA 时也可能回 200 + data:null + 挑战头
    headers = {"PVE-TFA-Token": "challenge-200"}
    resp = _FakeResp(200, headers, json.dumps({"data": None}).encode())
    _patch(monkeypatch, resp)
    status, data = auth_pve.verify_pve_credentials("root@pam", "pw")
    assert status == auth_pve.TFA
    assert data == "challenge-200"


def test_fail_on_401_without_header(monkeypatch):
    resp = _make_httperror(401, {}, b"{}")
    _patch(monkeypatch, resp)
    status, data = auth_pve.verify_pve_credentials("root@pam", "bad")
    assert status == auth_pve.FAIL
    assert data is None


def test_fail_on_empty_credentials():
    status, _ = auth_pve.verify_pve_credentials("", "pw")
    assert status == auth_pve.FAIL
    status, _ = auth_pve.verify_pve_credentials("root@pam", "")
    assert status == auth_pve.FAIL


def test_error_on_unreachable(monkeypatch):
    monkeypatch.setattr(
        auth_pve.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("conn refused")),
    )
    status, data = auth_pve.verify_pve_credentials("root@pam", "pw")
    assert status == auth_pve.ERROR
    assert "conn refused" in data


def test_change_password_success(monkeypatch):
    calls = []

    def fake_urlopen(req, *a, **k):
        calls.append(req)
        if req.get_method() == "POST":
            return _FakeResp(200, {}, json.dumps(
                {"data": {"ticket": "T", "CSRFPreventionToken": "C"}}).encode())
        return _FakeResp(200, {}, b'{"data": null}')

    monkeypatch.setattr(auth_pve.urllib.request, "urlopen", fake_urlopen)
    status, data = auth_pve.change_pve_password("pvewebtools@pve", "old", "NewPass123")
    assert status == auth_pve.OK
    assert data == "pvewebtools@pve"
    put = calls[-1]
    assert put.get_method() == "PUT"
    assert "access/password" in put.full_url
    low = {k.lower(): v for k, v in dict(put.header_items()).items()}
    assert low.get("cookie", "").startswith("PVEAuthCookie=")
    assert low.get("csrfpreventiontoken") == "C"
    body = put.data.decode()
    assert "password=NewPass123" in body
    assert "userid=pvewebtools%40pve" in body
    assert "confirmation-password=old" in body


def test_change_password_wrong_old(monkeypatch):
    def fake_urlopen(req, *a, **k):
        if req.get_method() == "POST":
            raise _make_httperror(401, {}, b"{}")
        return _FakeResp(200, {}, b'{"data": null}')

    monkeypatch.setattr(auth_pve.urllib.request, "urlopen", fake_urlopen)
    status, data = auth_pve.change_pve_password("u@pve", "bad", "NewPass123")
    assert status == auth_pve.FAIL


def test_change_password_rejected_by_pve(monkeypatch):
    def fake_urlopen(req, *a, **k):
        if req.get_method() == "POST":
            return _FakeResp(200, {}, json.dumps(
                {"data": {"ticket": "T", "CSRFPreventionToken": "C"}}).encode())
        raise _make_httperror(400, {}, json.dumps({"errors": {"password": "too weak"}}).encode())

    monkeypatch.setattr(auth_pve.urllib.request, "urlopen", fake_urlopen)
    status, data = auth_pve.change_pve_password("u@pve", "old", "x")
    assert status == auth_pve.PWD_FAIL
    assert "too weak" in data
