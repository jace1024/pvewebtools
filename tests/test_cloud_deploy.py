"""执行云镜像部署的真实网络配置片段，只记录命令参数，不调用 qm。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("ip,gateway,expected", [
    ("192.0.2.20/24", "192.0.2.1", "ip=192.0.2.20/24,gw=192.0.2.1"),
    ("192.0.2.20/24", "", "ip=192.0.2.20/24"),
    ("", "", "ip=dhcp"),
])
def test_cloud_network_command(ip, gateway, expected):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to run the deployment shell fragment")
    script_path = Path(__file__).resolve().parents[1] / "core/tools/scripts/cloud_deploy.sh"
    script = script_path.read_text(encoding="utf-8")
    network = script.split("# 6. 网络：", 1)[1].split('if [ -n "$PT_DNS" ]; then', 1)[0]
    network = network[network.index('if [ -n "$PT_IP" ]; then'):]
    harness = (
        "set -eu\n"
        'PT_IP=$1\nPT_GW=$2\nNEWID=123\n'
        'run() { shift; printf "%s\\0" "$@"; }\n'
    )

    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", harness + network, "cloud-network-test", ip, gateway],
        capture_output=True, timeout=10, check=True,
    )

    assert result.stdout.decode("utf-8").split("\0") == [
        "qm", "set", "123", "--ipconfig0", expected, "",
    ]
