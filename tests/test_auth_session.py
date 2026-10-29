"""auth_session 的单元测试：签发/校验/过期/篡改/注销/破窗通道。"""

import base64
import os
import time

from core.web import auth_session

SECRET = b"test-secret-32-bytes-long-enough!!"


def test_mint_then_verify():
    cookie = auth_session.mint("root@pam", secret=SECRET)
    assert cookie.startswith(auth_session.COOKIE_NAME + "=")
    user = auth_session.verify("junk; " + cookie, secret=SECRET)
    assert user == "root@pam"


def test_tampered_signature_rejected():
    cookie = auth_session.mint("root@pam", secret=SECRET)
    value = cookie.split("=", 1)[1]            # payload.sig; HttpOnly; ...
    payload, rest = value.split(".", 1)
    sig_b64 = rest.split(";", 1)[0]            # 仅取签名段
    sb = bytearray(base64.urlsafe_b64decode(sig_b64))
    sb[-1] ^= 0xFF                             # 翻转签名最后一个字节
    bad_sig = base64.urlsafe_b64encode(bytes(sb)).decode()
    bad = f"{auth_session.COOKIE_NAME}={payload}.{bad_sig}; HttpOnly; Path=/api; SameSite=Strict"
    assert auth_session.verify(bad, secret=SECRET) is None


def test_expired_rejected():
    cookie = auth_session.mint("root@pam", ttl=-1, secret=SECRET)  # 已过期
    assert auth_session.verify(cookie, secret=SECRET) is None


def test_no_cookie_rejected():
    assert auth_session.verify(None, secret=SECRET) is None
    assert auth_session.verify("", secret=SECRET) is None


def test_logout_cookie_clears():
    cookie = auth_session.logout_cookie()
    assert cookie.startswith(auth_session.COOKIE_NAME + "=;")
    assert "Max-Age=0" in cookie


def test_disabled_bypass(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_NO_AUTH", "1")
    assert auth_session.disabled() is True
    monkeypatch.delenv("PVE_WEBTOOLS_NO_AUTH", raising=False)
    assert auth_session.disabled() is False


def test_secure_flag_via_env(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_SECURE_COOKIE", "1")
    assert "Secure" in auth_session.mint("u", secret=SECRET)
    monkeypatch.delenv("PVE_WEBTOOLS_SECURE_COOKIE", raising=False)
    assert "Secure" not in auth_session.mint("u", secret=SECRET)


def test_info_exposes_expiry():
    cookie = auth_session.mint("root@pam", secret=SECRET)
    info = auth_session.info(cookie, secret=SECRET)
    assert info["user"] == "root@pam"
    assert isinstance(info["exp"], int)
    assert info["exp"] > time.time() - 5
    # 无效/缺失 → None
    assert auth_session.info("", secret=SECRET) is None
    assert auth_session.info("pwt_session=junk", secret=SECRET) is None
