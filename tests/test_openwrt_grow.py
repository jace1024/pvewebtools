"""OpenWrt 模板建机的系统盘自动扩根：squashfs+overlay / ext4 两种镜像都要覆盖。"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "core/tools/toolbox_common.py"


def _expand_body() -> str:
    src = COMMON.read_text(encoding="utf-8")
    m = re.search(r"cat <<'PWEXPAND'\n(.*?)\nPWEXPAND", src, re.S)
    assert m, "未找到 pw_root_expand_body 的 PWEXPAND heredoc"
    return m.group(1)


def test_expand_body_bash_syntax():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to run the shell syntax check")
    body = _expand_body()
    # 清掉 BASH_ENV/ENV：Cygwin 等环境会用它们注入启动脚本，干扰 bash -n
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("BASH_ENV", "ENV")}
    proc = subprocess.run([bash, "--noprofile", "--norc", "-n"],
                          input=body.encode(), capture_output=True, timeout=10, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("needle", [
    "openwrt_release",   # OpenWrt 识别
    "/overlay",          # squashfs 版真正要扩的挂载点
    "resizepart",        # parted 扩分区（opkg 环境无 growpart 时）
    "resize.f2fs",       # f2fs overlay 文件系统
    "resize2fs",         # ext4 根 / ext4 overlay
    "partx -u",          # 分区表重读
])
def test_openwrt_branch_present(needle):
    assert needle in _expand_body(), f"OpenWrt 扩根分支缺少 {needle}"


def test_openwrt_branch_precedes_standard_path():
    body = _expand_body()
    # OpenWrt 分支必须在通用 findmnt 逻辑之前 exit：OpenWrt(busybox) 通常没有 findmnt
    assert body.index("openwrt_release") < body.index("ROOT_SRC=$(findmnt")
