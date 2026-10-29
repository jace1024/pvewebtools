"""登录会话（方案 A 的本地会话层）。

PVE 校验通过后，由本模块签发一个**无状态 HMAC 签名 Cookie** 给浏览器；
后续请求靠它识别登录态，工具自身不保存任何会话（重启也无需回收）。

安全要点：
  - 密钥持久化到 /etc/pvewebtools/session.secret（权限 600）；文件缺失时临时生成
    并尽力落盘，落盘失败则仅进程内有效（重启即全部掉登录，属可接受的降级）。
  - Cookie 属性：HttpOnly; Path=/api; SameSite=Strict；若前面挂了 HTTPS 反代，
    设 PVE_WEBTOOLS_SECURE_COOKIE=1 追加 Secure。
  - 签名用 HMAC-SHA256，比对走 hmac.compare_digest 防时序攻击；含 exp 过期时间。
  - 仅承载「user」与「exp」，绝不携带 PVE 密码或 PVE ticket。

本模块只依赖标准库，无 PVE/站点依赖，可单测（inject secret 即可）。
"""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

COOKIE_NAME = "pwt_session"
DEFAULT_TTL = 12 * 3600  # 12 小时
_SECRET_PATH = Path("/etc/pvewebtools/session.secret")

_secret_cache = None  # 进程内缓存，避免每次请求读盘


def disabled():
    """是否关闭登录门禁（--no-auth 破窗通道）。返回 True 时 require_auth 直接放行。"""
    return os.environ.get("PVE_WEBTOOLS_NO_AUTH") == "1"


def _secret(explicit=None):
    global _secret_cache
    if explicit is not None:
        return explicit
    if _secret_cache is not None:
        return _secret_cache
    data = None
    try:
        if _SECRET_PATH.exists():
            data = _SECRET_PATH.read_bytes()
    except OSError:
        data = None
    if not data:
        data = os.urandom(32)
        try:
            _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError:
            # 落盘失败：仅进程内有效，重启后全部掉登录；不影响本次运行
            pass
    _secret_cache = data
    return data


def _b64(b):
    return base64.urlsafe_b64encode(b).decode("ascii")


def _attrs():
    attrs = "HttpOnly; Path=/api; SameSite=Strict"
    if os.environ.get("PVE_WEBTOOLS_SECURE_COOKIE") == "1":
        attrs += "; Secure"
    return attrs


def mint(user, ttl=DEFAULT_TTL, secret=None):
    """为已通过 PVE 校验的 user 签发会话 Cookie 字符串。"""
    payload = json.dumps(
        {"user": user, "exp": int(time.time()) + ttl},
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(_secret(secret), payload, hashlib.sha256).digest()
    return f"{COOKIE_NAME}={_b64(payload)}.{_b64(sig)}; {_attrs()}"


def logout_cookie():
    """注销用 Cookie：同名空值 + Max-Age=0，让浏览器立即丢弃。"""
    return f"{COOKIE_NAME}=; {_attrs()}; Max-Age=0"


def _cookie_value(cookie_header):
    """从 Cookie 头里取出本会话的原值（不含属性）；缺失返回 None。"""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(COOKIE_NAME + "="):
            return part[len(COOKIE_NAME) + 1:]
    return None


def _parse_token(value, secret=None):
    """校验签名与有效期，返回 payload dict；任何异常/篡改/过期均 None。"""
    if not value or "." not in value:
        return None
    p, s = value.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(p)
        sig = base64.urlsafe_b64decode(s)
    except Exception:  # noqa: BLE001
        return None
    expected = hmac.new(_secret(secret), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data


def _verify_token(value, secret=None):
    data = _parse_token(value, secret)
    return data.get("user") if data else None


def verify(cookie_header, secret=None):
    """从请求 Cookie 头解析并校验会话，返回 user 或 None（缺失/篡改/过期均 None）。"""
    return _verify_token(_cookie_value(cookie_header), secret)


def info(cookie_header, secret=None):
    """返回会话信息 ``{"user": str, "exp": int}``（exp 为 epoch 秒）；无效/缺失返回 None。

    供 ``/api/auth/status`` 向前端展示「当前登录用户 / 会话到期时间」。
    """
    data = _parse_token(_cookie_value(cookie_header), secret)
    if not data:
        return None
    return {"user": data.get("user"), "exp": data.get("exp")}
