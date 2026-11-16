"""密码 SSH 辅助（免 sshpass 依赖）。

加机流程经 SSH 连接尚未建立互信的新节点（pvecm add / 远端脚本）。
优先 sshpass；缺失时退化为 OpenSSH 自带的 SSH_ASKPASS + setsid（不需任何第三方包）。

供「加入节点」等需要密码 SSH 的功能使用。
"""

import os
import shutil
import subprocess
import tempfile

_SSH_OPTS = (
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "NumberOfPasswordPrompts=1",
    "-o", "PreferredAuthentications=password,keyboard-interactive",
)


def password_ssh_available() -> bool:
    """是否具备「用密码 SSH 登录新节点」的能力。"""
    if shutil.which("sshpass"):
        return True
    return bool(shutil.which("ssh") and shutil.which("setsid"))


def _askpass_script() -> str:
    """写一个临时 askpass 脚本（从环境变量读密码并打印），返回路径；调用方负责删除。"""
    fd, path = tempfile.mkstemp(prefix="pvewebtools-askpass-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nprintf '%s\\n' \"$PVE_WEBTOOLS_SSH_PW\"\n")
    os.chmod(path, 0o700)
    return path


def password_ssh_run(host, remote_args, password, input_text=None, timeout=300):
    """以密码 SSH 登录 host 执行远端命令，返回 subprocess.CompletedProcess。

    - 有 sshpass：`sshpass -e ssh ...`（密码经 SSHPASS 环境变量，不出现在命令行）；
    - 无 sshpass：`setsid ssh ...` + `SSH_ASKPASS`（OpenSSH >= 8.4 的 SSH_ASKPASS_REQUIRE=force）。

    remote_args 为远端命令及其参数（list）；input_text 非空时作为远端 stdin。
    """
    env = dict(os.environ)
    if shutil.which("sshpass"):
        cmd = ["sshpass", "-e", "ssh"] + list(_SSH_OPTS) + ["root@" + host] + list(remote_args)
        env["SSHPASS"] = password
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, input=input_text)

    askpass = _askpass_script()
    try:
        cmd = ["setsid", "-w", "ssh"] + list(_SSH_OPTS) + ["root@" + host] + list(remote_args)
        env["SSH_ASKPASS"] = askpass
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = "pvewebtools:0"
        env["PVE_WEBTOOLS_SSH_PW"] = password
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, input=input_text)
    finally:
        try:
            os.unlink(askpass)
        except OSError:
            pass
