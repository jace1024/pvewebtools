"""建机磁盘容量单位：新建磁盘必须显式带 G，否则 qm set 会把裸数字按默认单位解析（20 → 20M）。"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "core/tools/scripts"


@pytest.mark.parametrize("name", ["vm_deploy.sh", "cloud_deploy.sh"])
def test_deploy_data_disk_uses_gb_suffix(name):
    script = (SCRIPTS / name).read_text(encoding="utf-8")
    assert '"${DISK_STORE}:${DATA_DISK}G"' in script, f"{name} 数据盘创建缺 GB 单位"
    assert '"${DISK_STORE}:${DATA_DISK}"' not in script, f"{name} 仍有裸数字容量"


@pytest.mark.parametrize("name", ["vm_deploy.sh", "cloud_deploy.sh"])
def test_deploy_data_disk_qm_set_arg(name):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to run the shell fragment")
    script = (SCRIPTS / name).read_text(encoding="utf-8")
    line = next(l for l in script.splitlines() if 'qm set "$NEWID" --"${SLOT}"' in l)
    cmd = line.strip().removeprefix("if ").split(" 2>&1 |")[0]
    harness = (
        "set -eu\n"
        'NEWID=123\nSLOT=scsi1\nDATA_DISK=20\nDISK_STORE=local-lvm\n'
        'qm() { printf "%s\\0" "$@"; }\n'
        f"{cmd}\n"
    )
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", harness],
                            capture_output=True, timeout=10, check=True)
    args = [a for a in result.stdout.decode().split("\0") if a]
    assert args == ["set", "123", "--scsi1", "local-lvm:20G"]


def test_vm_disk_bare_number_defaults_to_gb():
    script = (SCRIPTS / "vm_disk.sh").read_text(encoding="utf-8")
    assert 'SIZE="${SIZE}G"' in script, "新增磁盘的纯数字容量应默认按 GB"
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to run the shell fragment")
    fragment = '\n'.join(l for l in script.splitlines()
                         if 'grep -qE \'^[0-9]+$\'' in l or 'tr \'a-z\' \'A-Z\'' in l)
    harness = f"set -eu\nSIZE=$1\n{fragment}\nprintf '%s' \"$SIZE\"\n"
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", harness, "size-test", "20"],
                            capture_output=True, timeout=10, check=True)
    assert result.stdout.decode().strip() == "20G"
