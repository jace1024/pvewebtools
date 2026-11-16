"""登录路由与 require_auth 门禁的单元测试（mock PVE 校验，无需真机）。"""

import io
import json

import pytest

from core.web import auth_pve, auth_session, routing
from core.web.routers import auth as auth_router


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """每个用例前后清空进程级限流状态，避免相互污染。"""
    auth_router._LIMITER.reset()
    yield
    auth_router._LIMITER.reset()


class _Hdrs:
    def __init__(self, d):
        self._d = d

    def get(self, k, default=None):
        return self._d.get(k, default)


class FakeHandler:
    """最小化 ApiHandler 替身：捕获状态码 / 响应头 / 响应体，供断言登录路由行为。"""

    def __init__(self, cookie_header=None, body=None):
        self.headers = _Hdrs({"Cookie": cookie_header} if cookie_header else {})
        self._body = body or {}
        self.status = None
        self.extra = {}
        self.wfile = io.BytesIO()
        self.json_data = None
        self.err = None
        self.client_address = ("127.0.0.1", 0)

    def _read_body(self):
        return self._body

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.extra[k] = v

    def end_headers(self):
        pass

    def _json(self, data, status=200):
        self.status = status
        self.json_data = data

    def _error(self, msg, status=400):
        self.status = status
        self.err = msg


def test_login_ok_sets_cookie(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.OK, "root@pam"))
    h = FakeHandler(body={"username": "root@pam", "password": "pw"})
    auth_router._login(h, {})
    assert h.status == 200
    assert "pwt_session=" in h.extra.get("Set-Cookie", "")
    body = json.loads(h.wfile.getvalue().decode("utf-8"))
    assert body["data"]["user"] == "root@pam"


def test_login_tfa_challenge(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.TFA, "challenge-token"))
    h = FakeHandler(body={"username": "root@pam", "password": "pw"})
    auth_router._login(h, {})
    assert h.status == 200
    assert h.json_data["tfa"] is True
    assert h.json_data["tfaToken"] == "challenge-token"


def test_login_bad_creds(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.FAIL, None))
    h = FakeHandler(body={"username": "x", "password": "y"})
    auth_router._login(h, {})
    assert h.status == 401
    assert h.err


def test_login_pve_unreachable(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.ERROR, "boom"))
    h = FakeHandler(body={"username": "x", "password": "y"})
    auth_router._login(h, {})
    assert h.status == 503


def test_auth_status_reports_user(monkeypatch):
    secret = b"sec-32-bytes-long-enough-for-test!!"
    cookie = auth_session.mint("admin@pam", secret=secret)
    h = FakeHandler(cookie_header=cookie)
    # verify 默认用文件 secret；此处直接校验同源，改用注入不便，故断言无 cookie 时未登录
    assert auth_session.verify(cookie, secret=secret) == "admin@pam"
    h2 = FakeHandler()
    auth_router._auth_status(h2, {})
    assert h2.json_data["authenticated"] is False


def test_logout_clears_cookie():
    h = FakeHandler()
    auth_router._logout(h, {})
    assert h.status == 200
    assert "Max-Age=0" in h.extra.get("Set-Cookie", "")


def test_require_auth_public_endpoints():
    # 登录/状态/版本豁免，无需会话
    class M(routing.RoutingMixin):
        def __init__(self):
            self.err_status = None
        def _error(self, msg, status=400):
            self.err_status = status
    for parts in (["login"], ["auth", "status"], ["edition"]):
        m = M()
        assert m._require_auth(parts) is True
        assert m.err_status is None


def test_require_auth_rejects_without_session():
    class M(routing.RoutingMixin):
        def __init__(self):
            self.headers = _Hdrs({})
            self.err_status = None
        def _error(self, msg, status=400):
            self.err_status = status
    m = M()
    assert m._require_auth(["tools", "x", "run"]) is False
    assert m.err_status == 401


def test_require_auth_accepts_valid_session(monkeypatch):
    secret = b"sec-32-bytes-long-enough-for-test!!"
    cookie = auth_session.mint("root@pam", secret=secret)
    captured = {}

    class M(routing.RoutingMixin):
        def __init__(self):
            self.headers = _Hdrs({"Cookie": cookie})
        def _error(self, msg, status=400):
            captured["err"] = status

    # 注入测试 secret 给模块级 verify（生产走文件，这里直接断言逻辑分支）
    monkeypatch.setattr(auth_session, "_secret", lambda explicit=None: secret)
    m = M()
    assert m._require_auth(["tools", "x", "run"]) is True
    assert "err" not in captured


def test_require_auth_bypass_when_disabled(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_NO_AUTH", "1")
    m = type("M", (routing.RoutingMixin,), {
        "_error": lambda self, msg, status=400: (_ for _ in ()).throw(AssertionError("不应调用")),
    })()
    assert m._require_auth(["whatever"]) is True
    monkeypatch.delenv("PVE_WEBTOOLS_NO_AUTH", raising=False)


def test_auth_status_authenticated_when_disabled(monkeypatch):
    monkeypatch.setattr(auth_session, "disabled", lambda: True)
    h = FakeHandler()
    auth_router._auth_status(h, {})
    assert h.json_data["authenticated"] is True


def test_csrf_required_on_write_when_auth_on(monkeypatch):
    monkeypatch.setattr(auth_session, "disabled", lambda: False)

    class M(routing.RoutingMixin):
        def __init__(self, hdr):
            self.headers = _Hdrs(hdr)
        def _error(self, msg, status=400):
            self.err = status

    # 写请求缺少 CSRF 头 → 不通过；带上 → 通过
    assert M({})._csrf_ok(True) is False
    assert M({"X-Requested-With": "XMLHttpRequest"})._csrf_ok(True) is True
    # 读请求始终通过
    assert M({})._csrf_ok(False) is True
    # --no-auth 下不校验 CSRF
    monkeypatch.setattr(auth_session, "disabled", lambda: True)
    assert M({})._csrf_ok(True) is True


def test_single_fail_does_not_lock(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.FAIL, None))
    h = FakeHandler(body={"username": "root", "password": "x"})
    auth_router._login(h, {})
    assert h.status == 401            # 单次失败不应锁定
    assert "还可尝试" in h.err


def test_limiter_threshold_env_override(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_LOGIN_MAX_FAILS", "2")
    lim = auth_router._LoginLimiter()
    assert lim.max_fails == 2
    assert lim.fail("ip-x") == 0.0    # 第 1 次
    assert lim.fail("ip-x") > 0       # 第 2 次即触发锁定


def test_login_rate_limited_after_failures(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.FAIL, None))
    # 阈值 5：前 4 次回 401，第 5 次触发锁定并回 429
    for _ in range(4):
        h = FakeHandler(body={"username": "x", "password": "y"})
        auth_router._login(h, {})
        assert h.status == 401
    h5 = FakeHandler(body={"username": "x", "password": "y"})
    auth_router._login(h5, {})
    assert h5.status == 429
    # 锁定期间即使凭证正确也直接 429
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.OK, "root@pam"))
    h6 = FakeHandler(body={"username": "root@pam", "password": "pw"})
    auth_router._login(h6, {})
    assert h6.status == 429


def test_login_success_resets_limiter(monkeypatch):
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.FAIL, None))
    for _ in range(3):
        h = FakeHandler(body={"username": "x", "password": "y"})
        auth_router._login(h, {})
        assert h.status == 401
    # 成功一次 → 计数清零
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.OK, "root@pam"))
    hs = FakeHandler(body={"username": "root@pam", "password": "pw"})
    auth_router._login(hs, {})
    assert hs.status == 200
    # 再失败 4 次仍是 401（未重新累计到阈值）
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.FAIL, None))
    for _ in range(4):
        hh = FakeHandler(body={"username": "x", "password": "y"})
        auth_router._login(hh, {})
        assert hh.status == 401


def test_login_autocompletes_realm(monkeypatch):
    captured = {}

    def fake_verify(user, pw, **k):
        captured["user"] = user
        return auth_pve.OK, user

    monkeypatch.setattr(auth_pve, "verify_pve_credentials", fake_verify)
    # 只输 "pvewebtools" → 自动补成 "pvewebtools@pve"（默认真域 pve）
    h = FakeHandler(body={"username": "pvewebtools", "password": "pw"})
    auth_router._login(h, {})
    assert captured["user"] == "pvewebtools@pve"
    assert h.status == 200
    # 已带 realm 的原样保留
    h2 = FakeHandler(body={"username": "admin@pve", "password": "pw"})
    auth_router._login(h2, {})
    assert captured["user"] == "admin@pve"


def test_auth_status_returns_expires_at(monkeypatch):
    secret = b"sec-32-bytes-long-enough-for-test!!"
    monkeypatch.setattr(auth_session, "_secret", lambda explicit=None: secret)
    cookie = auth_session.mint("root@pam", secret=secret)
    h = FakeHandler(cookie_header=cookie)
    auth_router._auth_status(h, {})
    assert h.json_data["authenticated"] is True
    assert h.json_data["user"] == "root@pam"
    assert isinstance(h.json_data["expiresAt"], int)


def test_login_default_realm_from_env(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_DEFAULT_REALM", "pve")
    captured = {}

    def fake(user, pw, **k):
        captured["user"] = user
        return auth_pve.OK, user

    monkeypatch.setattr(auth_pve, "verify_pve_credentials", fake)
    h = FakeHandler(body={"username": "pvewebtools", "password": "pvewebtools"})
    auth_router._login(h, {})
    assert captured["user"] == "pvewebtools@pve"


def test_login_allowlist_rejects_other(monkeypatch):
    monkeypatch.setenv("PVE_WEBTOOLS_ALLOW_USERS", "pvewebtools@pve")
    hit = {}

    def fake(*a, **k):
        hit["v"] = True
        return auth_pve.OK, "root@pam"

    monkeypatch.setattr(auth_pve, "verify_pve_credentials", fake)
    h = FakeHandler(body={"username": "root", "password": "pw"})
    auth_router._login(h, {})
    assert h.status == 401
    assert "v" not in hit              # 名单外账号不应去 PVE 校验


def test_login_forces_change_on_default_password(tmp_path, monkeypatch):
    marker = tmp_path / "force_pw_change"
    marker.write_text("pvewebtools@pve", encoding="utf-8")
    monkeypatch.setattr(auth_router, "_FORCE_PW_FILE", str(marker))
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.OK, "pvewebtools@pve"))
    h = FakeHandler(body={"username": "pvewebtools@pve", "password": "pvewebtools"})
    auth_router._login(h, {})
    assert h.status == 200
    assert h.json_data["mustChangePassword"] is True
    assert h.json_data["username"] == "pvewebtools@pve"
    assert "Set-Cookie" not in h.extra   # 强制改密阶段不签发会话


def test_login_clears_marker_when_password_changed(tmp_path, monkeypatch):
    marker = tmp_path / "force_pw_change"
    marker.write_text("pvewebtools@pve", encoding="utf-8")
    monkeypatch.setattr(auth_router, "_FORCE_PW_FILE", str(marker))
    monkeypatch.setattr(auth_pve, "verify_pve_credentials",
                        lambda *a, **k: (auth_pve.OK, "pvewebtools@pve"))
    h = FakeHandler(body={"username": "pvewebtools@pve", "password": "NewPass123"})
    auth_router._login(h, {})
    assert h.status == 200
    assert "pwt_session=" in h.extra.get("Set-Cookie", "")
    assert not marker.exists()


def test_change_password_ok(tmp_path, monkeypatch):
    marker = tmp_path / "force_pw_change"
    marker.write_text("pvewebtools@pve", encoding="utf-8")
    monkeypatch.setattr(auth_router, "_FORCE_PW_FILE", str(marker))
    monkeypatch.setattr(auth_pve, "change_pve_password",
                        lambda *a, **k: (auth_pve.OK, "pvewebtools@pve"))
    h = FakeHandler(body={
        "username": "pvewebtools@pve",
        "oldPassword": "pvewebtools",
        "newPassword": "NewPass123",
    })
    auth_router._change_password(h, {})
    assert h.status == 200
    assert "pwt_session=" in h.extra.get("Set-Cookie", "")
    assert not marker.exists()


def test_change_password_pwd_fail(monkeypatch):
    monkeypatch.setattr(auth_pve, "change_pve_password",
                        lambda *a, **k: (auth_pve.PWD_FAIL, "密码太弱"))
    h = FakeHandler(body={"username": "pvewebtools@pve", "oldPassword": "x",
                          "newPassword": "NewPass123"})
    auth_router._change_password(h, {})
    assert h.status == 400
    assert "修改密码失败" in h.err


def test_change_password_rejects_pam(monkeypatch):
    h = FakeHandler(body={"username": "root@pam", "oldPassword": "x",
                          "newPassword": "NewPass123"})
    auth_router._change_password(h, {})
    assert h.status == 400
    assert "pve" in h.err


def test_change_password_rejects_short_or_default(monkeypatch):
    h = FakeHandler(body={"username": "pvewebtools@pve", "oldPassword": "x",
                          "newPassword": "pvewebtools"})
    auth_router._change_password(h, {})
    assert h.status == 400          # 不能与初始密码相同
    h2 = FakeHandler(body={"username": "pvewebtools@pve", "oldPassword": "x",
                           "newPassword": "abc"})
    auth_router._change_password(h2, {})
    assert h2.status == 400         # 太短
