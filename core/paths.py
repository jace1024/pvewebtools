"""资源与运行期数据目录的统一解析。

两类运行形态下资源落点不同，集中在此判断，避免各模块重复写分支：
- 源码运行：__file__ = <仓库根>/core/<子包>/<模块>.py，资源在 <仓库根>/core/ 下；
- 打包运行：资源被放到程序目录旁的资源根，入口探测出该目录后经环境变量
  PVE_WEBTOOLS_ROOT 传进来（此时资源目录就是它本身）。

用法：
- core.paths.RESOURCE_DIR  只读资源（static/、config/、VERSION）
- core.paths.PROJECT_ROOT  运行期可写数据的落点（logs/ 等）
"""

import os
import sys
from pathlib import Path

_root = os.environ.get("PVE_WEBTOOLS_ROOT", "").strip()

# 只认「确实带 static/ 的根」：源码以 main.py 方式启动时该变量指向仓库根
# （static/ 在 core/ 下），此时应走下面的源码分支。
if _root and (Path(_root) / "static").is_dir():
    RESOURCE_DIR = Path(_root)            # 打包：随包资源（static/ 等）在解压目录根下
    PROJECT_ROOT = RESOURCE_DIR           # 打包：无源码根，回退同一目录
else:
    RESOURCE_DIR = Path(__file__).resolve().parent      # 源码：包目录 core/
    PROJECT_ROOT = RESOURCE_DIR.parent                  # 源码：仓库根

# 可执行文件所在目录：二进制 onefile/standalone 下即安装目录（部署后即 /etc/pvewebtools），持久可写；
# 用作「覆盖配置目录」的锚点，使运维改动跨重启/升级保留——随包资源目录在 onefile 下是临时解压目录，不可持久。
# 用 realpath 解析软链（如经 /usr/local/bin 软链调用时仍能落回真实安装目录）。
EXE_DIR = Path(os.path.dirname(os.path.realpath(sys.argv[0] or ".")) or ".").resolve()

# 持久覆盖配置目录（cloud-image-presets.json 可放此处覆盖随包默认值）
CONFIG_OVERRIDE_DIR = EXE_DIR / "config"


def preset_override_dir():
    """换源/预设配置的可写目录：

    - 二进制（PVE_WEBTOOLS_BUILT=1）：用 EXE_DIR/config（安装目录，持久），保证 onefile 形态下改动不随临时解压目录丢失；
    - 源码运行：直接写随包资源目录 core/config，与旧行为一致。
    """
    if os.environ.get("PVE_WEBTOOLS_BUILT") == "1":
        return CONFIG_OVERRIDE_DIR
    return RESOURCE_DIR / "config"
