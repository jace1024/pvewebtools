"""PVE 账号体系校验（登录方案 A）。

设计前提：工具**不存储任何 PVE 密码**。登录时把凭证转发到本机 pveproxy 的
``/api2/json/access/ticket`` 校验，凭据有效即返回归一化后的用户名；校验通过后
由调用方（登录路由）自行签发工具自身的 HMAC 会话 Cookie。这样白嫖 PVE 的账号
锁定与 TFA，且本地不落盘任何密码哈希。

兼容性（PVE 8 / 9 通用）：``/access/ticket`` 接口与 ``PVE-TFA-Token`` 挑战机制
在两代大版本上长期稳定。本模块只认两件事，不解析多余字段以避免版本耦合：

  - HTTP 200 且响应体含 ``ticket``            => 成功（返回用户名）
  - HTTP 401 且响应头含 ``PVE-TFA-Token``      => 需要二次因子（返回挑战令牌）

其余情形一律视为失败（含密码错、网络/TLS 错误、PVE 未启动）。
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

# 本机 pveproxy；仅走 127.0.0.1，避免任何出网。
_PVE_TICKET_URL = "https://127.0.0.1:8006/api2/json/access/ticket"
# PVE 自签根 CA：用它校验 localhost 证书，既验证对端又避免裸关校验。
_PVE_CA_PATH = "/etc/pve/pve-root-ca.pem"
# TFA 挑战令牌的响应头名（PVE 7+ 一致；8/9 通用）。
_PVE_TFA_HEADER = "PVE-TFA-Token"

# 校验结果状态常量
OK = "ok"
TFA = "tfa"
FAIL = "fail"
ERROR = "error"        # PVE 不可达 / TLS 错误等基础设施故障，区别于“密码错”
PWD_FAIL = "pwd_fail"  # 改密被 PVE 拒绝（密码策略 / 参数）


def _ssl_context(cafile):
    """构建 TLS 校验上下文：优先用 PVE 根 CA，缺失时回退系统 CA。绝不关闭校验。"""
    try:
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
        return ssl.create_default_context()
    except Exception:  # noqa: BLE001  # 任何异常都不退化为“不校验”，而是让上层报 ERROR
        return ssl.create_default_context()


def _read_json(payload):
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _unwrap(body):
    """PVE 标准 REST 响应形如 ``{"data": {...}}``；兼容直接平铺的旧/测试格式。"""
    if isinstance(body, dict):
        d = body.get("data")
        if isinstance(d, dict):
            return d
    return body if isinstance(body, dict) else {}


def _post_ticket(url, username, password, otp, tfa_token, cafile, timeout):
    """打一次 /access/ticket，返回 (status, headers, body_dict)。

    status 为 None 表示底层网络/TLS 故障（URLError / SSL 异常）。
    """
    body = {"username": username, "password": password}
    if otp:
        body["otp"] = otp
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if tfa_token:
        req.add_header(_PVE_TFA_HEADER, tfa_token)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(cafile)) as resp:
            return resp.status, resp.headers, _read_json(resp.read())
    except urllib.error.HTTPError as e:  # 401 等：仍需读响应头/体判断 TFA
        return e.code, e.headers, _read_json(e.read())
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        return None, None, {"__error__": str(e)}


def _post_form(url, fields, cookie=None, csrf=None, cafile=None, timeout=10, method="POST"):
    """发送表单请求，返回 (status, body_dict)；网络/TLS 故障 status=None。"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if cookie:
        req.add_header("Cookie", cookie)
    if csrf:
        req.add_header("CSRFPreventionToken", csrf)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(cafile)) as resp:
            return resp.status, _read_json(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _read_json(e.read())
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        return None, {"__error__": str(e)}


def change_pve_password(username, old_password, new_password, otp=None, tfa_token=None,
                        url=None, cafile=None, timeout=10):
    """用旧密码换 ticket，再调 PVE ``/access/password`` 改密。

    返回 (status, data)：
      (OK, username)     改密成功
      (TFA, token)       需要二次因子（旧密码正确但缺 OTP）
      (FAIL, None)       旧密码错误
      (PWD_FAIL, 原因)   PVE 拒绝改密（密码策略等）
      (ERROR, 原因)      PVE 不可达 / TLS 故障
    """
    ticket_url = url or _PVE_TICKET_URL
    ca = cafile if cafile is not None else _PVE_CA_PATH
    status, headers, body = _post_ticket(ticket_url, username, old_password, otp, tfa_token, ca, timeout)
    if status is None:
        return ERROR, body.get("__error__")
    payload = _unwrap(body)
    if status != 200 or not payload.get("ticket"):
        if headers and headers.get(_PVE_TFA_HEADER):
            return TFA, headers.get(_PVE_TFA_HEADER)
        return FAIL, None
    ticket = payload.get("ticket")
    csrf = payload.get("CSRFPreventionToken") or ""
    base = ticket_url.rsplit("/access/ticket", 1)[0]
    # PVE 参数：userid=目标用户，password=新密码，confirmation-password=执行者当前(旧)密码
    st2, body2 = _post_form(
        base + "/access/password",
        {
            "userid": username,
            "password": new_password,
            "confirmation-password": old_password,
        },
        cookie="PVEAuthCookie=" + ticket, csrf=csrf, cafile=ca, timeout=timeout, method="PUT")
    if st2 is None:
        return ERROR, body2.get("__error__")
    if st2 == 200:
        return OK, username
    reason = ""
    if isinstance(body2, dict):
        errs = body2.get("errors")
        if isinstance(errs, dict) and errs:
            reason = "; ".join(f"{k}: {v}" for k, v in errs.items())
        reason = reason or body2.get("message") or ""
    return PWD_FAIL, (reason or f"HTTP {st2}")


def verify_pve_credentials(username, password, otp=None, tfa_token=None,
                           url=None, cafile=None, timeout=10):
    """校验一组 PVE 凭证。

    参数：
      username    形如 ``user@realm``（如 ``root@pam``）；调用方负责补全 realm。
      password    明文密码，仅在本函数内用于转发，不落盘、不回显。
      otp         二次因子（TOTP 等）；首次提交为空，TFA 挑战后由前端带回。
      tfa_token   上次挑战拿到的 ``PVE-TFA-Token``；非 None 时作为请求头回传。
      url/cafile/timeout  仅单测注入用，生产走默认（本机 8006 + PVE 根 CA）。

    返回 ``(status, data)``：
      (OK,   "<归一化用户名>")   校验通过
      (TFA,  "<挑战令牌>")       需要二次因子，前端收码后带 otp+tfa_token 重试
      (FAIL, None)               凭证错误
      (ERROR,"<原因>")           PVE 不可达 / TLS 错误（应提示“认证服务不可用”）
    """
    if not username or not password:
        return FAIL, None
    status, headers, body = _post_ticket(
        url or _PVE_TICKET_URL, username, password, otp, tfa_token,
        cafile if cafile is not None else _PVE_CA_PATH, timeout)
    if status is None:
        return ERROR, body.get("__error__")
    # PVE 把结果包在 data 里：{"data": {"ticket": ..., "username": ...}}
    payload = _unwrap(body)
    # 成功：data.ticket 存在即视为通过；用户名以 PVE 归一的为准。
    if status == 200 and payload.get("ticket"):
        return OK, str(payload.get("username") or username)
    # TFA 挑战：PVE 在响应头回 PVE-TFA-Token（可能 401，也可能 200 + data:null）。
    if headers and headers.get(_PVE_TFA_HEADER):
        return TFA, headers.get(_PVE_TFA_HEADER)
    return FAIL, None
