"""登录 / 会话状态 / 注销路由（方案 A：凭证校验交给本机 PVE，会话 Cookie 自行签发）。

端点：
  POST /api/login         {username, password, otp?, tfaToken?}
                         -> 200 {user}（已签发会话 Cookie）
                         -> 200 {tfa:true, tfaToken}（需二次因子，前端收码后重提）
                         -> 200 {mustChangePassword:true, username}（默认密码首次登录，须先改密）
                         -> 401 用户名或密码错误 / 429 限流 / 503 认证服务不可用
  POST /api/auth/change-password {username, oldPassword, newPassword, otp?, tfaToken?}
                         -> 200 {user}（改密成功并签发会话 Cookie）
  GET  /api/auth/status  -> {authenticated, user}（前端首屏判断是否需要登录）
  POST /api/logout       -> 清除会话 Cookie

凭证校验全部委托 auth_pve.verify_pve_credentials；本模块只负责把「校验结果」翻译成
会话 Cookie / 挑战令牌，不接触任何 PVE 密码。
"""

import json
import os
import threading
import time

from core.web import auth_pve, auth_session


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class _LoginLimiter:
    """按来源 IP 的登录失败限流：窗口内失败达阈值即短时锁定，防在线爆破。

    第 ``max_fails`` 次失败触发锁定，锁定期间所有尝试直接 429；成功登录清空该 IP 记录。
    纯内存、进程级；重启即清零。阈值/窗口/锁定秒数可用环境变量覆盖（调试期可放宽）：
      PVE_WEBTOOLS_LOGIN_MAX_FAILS / PVE_WEBTOOLS_LOGIN_WINDOW / PVE_WEBTOOLS_LOGIN_LOCKOUT
    """

    def __init__(self, max_fails=None, window=None, lockout=None):
        self.max_fails = max_fails if max_fails is not None else _env_int("PVE_WEBTOOLS_LOGIN_MAX_FAILS", 5)
        self.window = window if window is not None else float(_env_int("PVE_WEBTOOLS_LOGIN_WINDOW", 60))
        self.lockout = lockout if lockout is not None else float(_env_int("PVE_WEBTOOLS_LOGIN_LOCKOUT", 60))
        self._lock = threading.Lock()
        self._rec = {}  # ip -> {"count": int, "start": float, "until": float}

    def blocked(self, ip):
        """返回剩余锁定秒数（>0 表示被锁）。"""
        now = time.monotonic()
        with self._lock:
            rec = self._rec.get(ip)
            if rec and rec["until"] > now:
                return rec["until"] - now
            return 0.0

    def fail(self, ip):
        """记一次失败；返回触发锁定后的剩余秒数（0 表示未锁定）。"""
        now = time.monotonic()
        with self._lock:
            rec = self._rec.get(ip)
            if not rec or now - rec["start"] > self.window:
                rec = {"count": 0, "start": now, "until": 0.0}
                self._rec[ip] = rec
            rec["count"] += 1
            if rec["count"] >= self.max_fails:
                rec["until"] = now + self.lockout
                rec["count"] = 0
                rec["start"] = now
                return self.lockout
            return 0.0

    def attempts(self, ip):
        """返回当前窗口内已累计的失败次数（未锁定分支用，供提示剩余次数）。"""
        with self._lock:
            rec = self._rec.get(ip)
            return rec["count"] if rec else 0

    def success(self, ip):
        with self._lock:
            self._rec.pop(ip, None)

    def reset(self):
        with self._lock:
            self._rec.clear()


_LIMITER = _LoginLimiter()


def _client_ip(handler):
    try:
        return handler.client_address[0]
    except Exception:  # noqa: BLE001  # 测试替身可能无该属性
        return "?"


# ---- 专用账号 / 首次强制改密 / 允许名单 ----
_FORCE_PW_FILE = "/etc/pvewebtools/force_pw_change"
_DEFAULT_PW = "pvewebtools"      # 安装脚本设定的初始密码；首次登录必须改掉
_PW_MIN_LEN = 8


def _allow_users():
    """允许登录的 PVE 账号集合（环境变量 PVE_WEBTOOLS_ALLOW_USERS，逗号分隔）；空表示不限制。"""
    raw = os.environ.get("PVE_WEBTOOLS_ALLOW_USERS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _is_allowed(user):
    allow = _allow_users()
    return (not allow) or (user in allow)


def _default_realm():
    """用户名未写 realm 时的默认域（PVE_WEBTOOLS_DEFAULT_REALM，缺省 pve）。

    默认 pve：让用户只输 `pvewebtools` 即可登录（等价 pvewebtools@pve）。
    """
    r = (os.environ.get("PVE_WEBTOOLS_DEFAULT_REALM") or "pve").strip().lstrip("@")
    return r or "pve"


def _force_user():
    """读取「首次登录强制改密」标记；返回待改密的用户名，无标记返回空串。"""
    try:
        with open(_FORCE_PW_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _clear_force_user(user):
    """清除强制改密标记（仅当标记属于该用户）。"""
    try:
        if _force_user() == user:
            os.remove(_FORCE_PW_FILE)
    except OSError:
        pass


def _send_json_cookie(handler, body, cookie, status=200):
    """手写响应：在 _send_json 之外注入 Set-Cookie（标准出口无此钩子）。"""
    out = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Set-Cookie", cookie)
    handler.send_header("Content-Length", str(len(out)))
    handler.end_headers()
    handler.wfile.write(out)


def _fail(handler, ip, user, reason):
    """统一处理一次登录失败：计数、打日志、按是否触发锁定回不同状态码。"""
    locked = _LIMITER.fail(ip)
    if locked:
        print(f"[login] 失败并锁定({reason}) ip={ip} user={user!r} 锁={int(locked)}s")
        handler._error(f"登录失败次数过多，请 {int(locked) + 1} 秒后再试", 429)
        return
    left = max(0, _LIMITER.max_fails - _LIMITER.attempts(ip))
    print(f"[login] 失败({reason}) ip={ip} user={user!r} 剩余尝试={left}")
    handler._error(f"用户名或密码错误（还可尝试 {left} 次）", 401)


def _login(handler, _params):
    ip = _client_ip(handler)
    remain = _LIMITER.blocked(ip)
    if remain > 0:
        print(f"[login] 已锁定 ip={ip} 剩余={int(remain)}s")
        handler._error(f"登录尝试过于频繁，请 {int(remain) + 1} 秒后重试", 429)
        return
    body = handler._read_body() or {}
    user = (body.get("username") or "").strip()
    # PVE 的 /access/ticket 只认 "用户名@realm"；未写 realm 时按默认 pve 域补齐，
    # 让用户只输 "pvewebtools" 也能登录（等价 pvewebtools@pve）。
    if user and "@" not in user:
        user += "@" + _default_realm()
    if user and not _is_allowed(user):
        _fail(handler, ip, user, "不在允许名单")
        return
    pw = str(body.get("password") or "")
    otp = body.get("otp") or None
    tfa_token = body.get("tfaToken") or None
    if not user or not pw:
        _fail(handler, ip, user, "凭证为空")
        return
    status, data = auth_pve.verify_pve_credentials(user, pw, otp=otp, tfa_token=tfa_token)
    if status == auth_pve.OK:
        _LIMITER.success(ip)
        forced = _force_user()
        if forced == user and pw == _DEFAULT_PW:
            print(f"[login] 需要强制改密 ip={ip} user={user!r}")
            handler._json({"mustChangePassword": True, "username": user}, status=200)
            return
        if forced == user:
            _clear_force_user(user)   # 已在别处改过密码，清掉标记
        print(f"[login] 成功 ip={ip} user={user!r}")
        cookie = auth_session.mint(data)
        info = auth_session.info(cookie) or {}
        _send_json_cookie(
            handler,
            {"data": {"user": data, "expiresAt": info.get("exp")}},
            cookie,
        )
        return
    if status == auth_pve.TFA:
        # TFA 挑战意味着密码已通过，不计入失败次数
        print(f"[login] 需要 TFA ip={ip} user={user!r}")
        handler._json({"tfa": True, "tfaToken": data}, status=200)
        return
    if status == auth_pve.ERROR:
        print(f"[login] PVE 不可达 ip={ip} user={user!r}")
        handler._error("认证服务不可用，请确认 PVE 服务已启动", 503)
        return
    _fail(handler, ip, user, "凭证错误")


def _auth_status(handler, _params):
    # --no-auth 破窗通道：无登录门禁时直接视为已登录，前端据此跳过登录浮层
    if auth_session.disabled():
        handler._json({"authenticated": True, "user": "", "enabled": False})
        return
    info = auth_session.info(handler.headers.get("Cookie"))
    if info:
        handler._json({
            "authenticated": True,
            "user": info.get("user") or "",
            "enabled": True,
            "expiresAt": info.get("exp"),
        })
    else:
        handler._json({"authenticated": False, "user": "", "enabled": True})


def _logout(handler, _params):
    _send_json_cookie(handler, {"data": {"ok": True}}, auth_session.logout_cookie())


def _change_password(handler, _params):
    """强制改密 / 主动改密：校验旧密码 → 以 root 调 pveum 改密 → 签发会话。

    请求体：{username, oldPassword, newPassword, otp?, tfaToken?}
    """
    ip = _client_ip(handler)
    remain = _LIMITER.blocked(ip)
    if remain > 0:
        handler._error(f"操作过于频繁，请 {int(remain) + 1} 秒后重试", 429)
        return
    body = handler._read_body() or {}
    user = (body.get("username") or "").strip()
    if user and "@" not in user:
        user += "@" + _default_realm()
    old_pw = str(body.get("oldPassword") or "")
    new_pw = str(body.get("newPassword") or "")
    otp = body.get("otp") or None
    tfa_token = body.get("tfaToken") or None
    if not user or not old_pw or not new_pw:
        handler._error("参数不完整", 400)
        return
    if not _is_allowed(user):
        _fail(handler, ip, user, "不在允许名单")
        return
    if not user.endswith("@pve"):
        handler._error("仅支持修改 @pve 用户密码（PAM 账号请用系统命令 passwd）", 400)
        return
    if new_pw == _DEFAULT_PW:
        handler._error("新密码不能与初始密码相同", 400)
        return
    if len(new_pw) < _PW_MIN_LEN:
        handler._error(f"新密码至少 {_PW_MIN_LEN} 位", 400)
        return
    status, data = auth_pve.change_pve_password(user, old_pw, new_pw, otp=otp, tfa_token=tfa_token)
    if status == auth_pve.TFA:
        handler._json({"tfa": True, "tfaToken": data}, status=200)
        return
    if status == auth_pve.ERROR:
        handler._error("认证服务不可用，请确认 PVE 服务已启动", 503)
        return
    if status == auth_pve.PWD_FAIL:
        handler._error(("修改密码失败：" + str(data)) if data else "修改密码失败", 400)
        return
    if status != auth_pve.OK:
        _fail(handler, ip, user, "旧密码错误")
        return
    _LIMITER.success(ip)
    _clear_force_user(user)
    print(f"[login] 改密成功 ip={ip} user={user!r}")
    cookie = auth_session.mint(data or user)
    info = auth_session.info(cookie) or {}
    _send_json_cookie(
        handler,
        {"data": {"user": data or user, "expiresAt": info.get("exp")}},
        cookie,
    )


HANDLERS = {
    "post": {
        "login": _login,
        "logout": _logout,
        "auth/change-password": _change_password,
    },
    "get": {
        "auth/status": _auth_status,
    },
}
