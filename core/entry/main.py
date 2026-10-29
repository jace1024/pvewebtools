"""程序启动入口（明文极简，仅声明依赖并启动主逻辑）。

下方「标准库声明」必须保留：打包工具只静态分析本入口，无法分析编译后
模块内部的 import，在此显式 import 标准库才能把依赖一并打包。

包内模块（pvewebtools.*）编译为 .so 随包分发、源码在编译后被删除，因此
这里用动态导入而非静态 import：Nuitka 不追踪包结构，运行时由 sys.path
上的 .so 提供（编译参数见 console/build-scripts/build.sh）。
"""
import os  # noqa: F401
import sys  # noqa: F401

# ---- 标准库声明（供 Nuitka 收集依赖，勿删） ----
# 包内模块会被 Cython 编成 .so（二进制），Nuitka 无法静态分析其内部 import，
# 所以「包内任何模块用到的标准库顶层模块」都必须在这里显式出现，否则编译产物运行到
# 那一步就是 ModuleNotFoundError（且被中性化文案盖住，很难查）。
import base64  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import re  # noqa: F401
import shlex  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import subprocess  # noqa: F401
import tarfile  # noqa: F401  # pve/upgrade.py 解包升级包用（.so 内部 import 无法被 Nuitka 静态分析）
import threading  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
import signal  # noqa: F401
import ipaddress  # noqa: F401
from pathlib import Path  # noqa: F401
from concurrent.futures import ThreadPoolExecutor  # noqa: F401
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401
from urllib.parse import parse_qs, urlparse  # noqa: F401
# 下面几个不一定能由上面的 import 传递引入（平台/版本相关），显式声明最稳：
import copy  # noqa: F401      # pve/pve_api.py
import gzip  # noqa: F401      # web/server.py（gzip 响应）
import lzma  # noqa: F401      # web/server.py（xz 解压）
import queue  # noqa: F401     # web/jobs.py（异步任务队列）
import tempfile  # noqa: F401  # 备用
# 子模块要单独声明：`from urllib.parse import ...` 只引入 urllib.parse，
# 不带上 urllib.request / urllib.error（它们是兄弟模块，谁也不会顺带 import）；
# importlib 同理，`import importlib` 不会把 importlib.util 一起装进来。
# 漏了就等运行到那一步 ModuleNotFoundError（见 BUILD.md 第 11 条 E2）。
import urllib.error  # noqa: F401    # web/server.py（HTTP 异常类型）
import urllib.request  # noqa: F401  # web/server.py（云镜像 / 驱动下载）
import importlib.util  # noqa: F401  # tools/toolbox.py（find_spec 探测 pro 模块）
import importlib.abc  # noqa: F401      # 下方 _CoreExtFinder 继承 MetaPathFinder
import importlib.machinery  # noqa: F401  # ExtensionFileLoader 加载 core.* 的 .so

# ---- 编译产物：封掉调试出口，报错保持中性 ----
# 判据是 Nuitka 注入的 __compiled__（源码运行时不存在的全局名；本文件上方
# _detect_pkg_root() 也用它区分形态）。编译产物既不认 --debug 参数，也不认
# PVE_WEBTOOLS_DEBUG 环境变量——否则客户能用它拿到含 .py 路径与模块结构的堆栈。
# 同时给下游（core.web.server）打标记，让它不必再提示「加 --debug」。
# 源码运行（python3 -m core.web.server）不设该标记，调试出口保持可用。
_BUILT = globals().get("__compiled__") is not None
if _BUILT:
    os.environ.pop("PVE_WEBTOOLS_DEBUG", None)
    sys.argv = [a for a in sys.argv if a != "--debug"]
    os.environ["PVE_WEBTOOLS_BUILT"] = "1"

# 包的父目录（含 core/ 包与编译出的 .so）需置于 sys.path 首位。各形态的落点：
# - --onefile：内容解压到临时目录，本模块 __file__ 即指向该目录
#   （要求编译参数 --file-reference-choice=runtime；取 frozen 会变成
#    "<frozen __main__>" 伪路径，取 original 指向编译机路径，两者都无法定位）
# - --standalone：资源与可执行文件同目录的 <exe>.dist/ 下
# - 源码运行：本文件位于 <仓库根>/core/entry/main.py，需向上找到含包的目录
# 注意：Nuitka 不提供 PyInstaller 的 sys._MEIPASS（仅作兼容候选，正常取不到）。
def _detect_pkg_root():
    """返回含 core/__init__.py 的目录；候选全不命中时退回可执行文件所在目录。"""
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    cands = []
    for extra in (getattr(sys, "_MEIPASS", None),          # 兼容 PyInstaller 产物（Nuitka 无此项）
                  getattr(globals().get("__compiled__"),
                          "containing_dir", None)):        # Nuitka 产物所在目录（standalone/.app）
        if isinstance(extra, str) and extra:
            cands.append(extra)
    cands.append(exe_dir)
    cands.append(os.path.join(exe_dir, os.path.basename(sys.argv[0]) + ".dist"))
    src = globals().get("__file__")
    if isinstance(src, str) and "<" not in src:            # onefile：<解压目录>/core/entry/main.py
        _d = os.path.dirname(os.path.abspath(src))
        for _ in range(5):
            cands.append(_d)
            _parent = os.path.dirname(_d)
            if _parent == _d:
                break
            _d = _parent
    for cand in cands:
        if os.path.isfile(os.path.join(cand, "core", "__init__.py")):
            return cand
    return exe_dir


_pkg_root = _detect_pkg_root()
# 是否真的定位到包（含 core/__init__.py）；下面失败时的错误码据此区分阶段
_PKG_ROOT_OK = os.path.isfile(os.path.join(_pkg_root, "core", "__init__.py"))
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)
# 包内模块（core.paths）据此定位随包资源（static/、config/ 等），避免各处重复探测
os.environ["PVE_WEBTOOLS_ROOT"] = _pkg_root


# ---- 让 Cython 预编译的 core.* .so 在 Nuitka 产物里可被 import ----
# 包内模块由 Cython 编成 .so 后随包分发，Nuitka 只把它们当作普通数据文件打进产物，
# 不会登记成可导入模块；其自带的导入器也认不出这些 .so（运行时 import 会报
# ModuleNotFoundError: No module named 'core.entry.loader'）。这里在 meta_path 最前面
# 挂一个查找器：凡 core.* 的 import，一律直接定位到解压目录下对应的 __init__.py（包）
# 或 <leaf>.cpython-*.so（扩展模块），用标准 importlib 的 FileLoader / ExtensionFileLoader
# 加载——不依赖 Nuitka 的导入器，也不干扰它对标准库 / edition 等模块的正常解析。
class _CoreExtFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = root

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "core" and not fullname.startswith("core."):
            return None
        rel = fullname.replace(".", os.sep)
        base = os.path.join(self.root, rel)
        # 包（core / core.entry / core.web ...）：靠 __init__.py 判定
        init_py = os.path.join(base, "__init__.py")
        if os.path.isfile(init_py):
            return importlib.util.spec_from_file_location(
                fullname, init_py, submodule_search_locations=[base])
        # 扩展模块（core.entry.loader ...）：匹配 <leaf>.cpython-*.so / <leaf>.so
        parent = os.path.dirname(base)
        leaf = os.path.basename(base)
        if os.path.isdir(parent):
            for fn in os.listdir(parent):
                if fn.startswith(leaf + ".") and fn.endswith(".so"):
                    so_path = os.path.join(parent, fn)
                    loader = importlib.machinery.ExtensionFileLoader(fullname, so_path)
                    return importlib.util.spec_from_file_location(
                        fullname, so_path, loader=loader)
        return None


sys.meta_path.insert(0, _CoreExtFinder(_pkg_root))


def _dump_startup_traceback():
    """把启动失败的完整堆栈追加到「主程序所在目录/startup-error.log」。

    对外保持中性（客户只看到一句错误码），细节留给支持侧排查——与 server.py 把业务报错
    原文写服务日志（`[detail]` 前缀）是同一个口径：客户不必看，root 可查。
    路径取可执行文件所在目录（部署后即 /etc/pvewebtools），升级替换二进制不影响该文件。
    """
    try:
        import traceback
        d = os.path.dirname(os.path.abspath(sys.argv[0])) or "."
        with open(os.path.join(d, "startup-error.log"), "a", encoding="utf-8") as f:
            f.write("==== %s | argv: %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(sys.argv[1:]) or "(无参数)"))
            traceback.print_exc(file=f)
    except Exception:  # noqa: BLE001
        pass


import importlib  # noqa: E402

try:
    # 动态导入：包内模块以 .so 形式随包分发，源码在编译产物中已被删除
    importlib.import_module("core.entry.loader").run()
except (SystemExit, KeyboardInterrupt):
    raise
except Exception:
    # 编译产物：不打堆栈、不提示调试开关（避免暴露语言/模块结构等实现细节），只给中性文案 +
    # 一个稳定错误码——它对客户无信息量，但能让我们一眼定位失败阶段（映射表见 BUILD.md）：
    #   E1 = 没找到包根（core/__init__.py 不在 sys.path 上）→ 产物拼接 / 入口探测问题
    #   E2 = 包根就位，但导入或启动过程抛异常 → 多为 .so 缺标准库声明、ABI 不符等
    if _BUILT:
        # 细节只进 startup-error.log，不往客户屏幕上打；对外就一句最短的中性提示（含阶段码）
        _dump_startup_traceback()
        sys.stderr.write("启动失败（%s）。\n" % ("E2" if _PKG_ROOT_OK else "E1"))
    else:
        sys.stderr.write("启动失败，请检查运行环境或加 --debug 重试。\n")
        if os.environ.get("PVE_WEBTOOLS_DEBUG") or "--debug" in sys.argv:
            import traceback
            traceback.print_exc()
    sys.exit(1)
