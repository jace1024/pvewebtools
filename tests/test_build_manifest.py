"""M2/T2.8 构建清单一致性。

编译产物由两段式构成：Cython 把清单内的 ``core/**/*.py`` 编成 ``.so`` 后删源码，
Nuitka 再把 ``.so`` + ``__init__.py`` + ``static/`` 打成一个二进制。**新增模块若漏登记**，
Cython 不编译它 → 产物里根本没有 → 运行时 ``ModuleNotFoundError``（且会被入口的中性
文案 E2 盖住，很难查）。本测试把这条纪律钉死：清单与磁盘实际模块必须一致。

``console/``（编译工具侧）不在产品仓库时自动跳过。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
SETUP_PY = ROOT / "console" / "build-scripts" / "setup.py"
BUILD_SH = ROOT / "console" / "build-scripts" / "build.sh"

# 不参与 Cython 编译的模块（保持明文 / 以数据文件形式打包）
EXCLUDED = {
    "core/entry/main.py",          # Nuitka 入口，明文极简
    "core/web/collect.py",         # 作为数据文件单独打包给远端节点执行
}

pytestmark = pytest.mark.skipif(
    not (SETUP_PY.exists() and BUILD_SH.exists()), reason="console/ 不在场，跳过构建清单校验"
)


def _actual_modules():
    mods = set()
    for p in CORE.rglob("*.py"):
        if p.name == "__init__.py":
            continue
        mods.add(p.relative_to(ROOT).as_posix())
    return mods


def _parse_setup():
    """返回 (base 清单, 含 pro 的全部清单)。"""
    src = SETUP_PY.read_text(encoding="utf-8")
    m = re.search(r"EXT_MODULES\s*=\s*\[(.*?)\]", src, re.S)
    base = set(re.findall(r'"([^"]+\.py)"', m.group(1))) if m else set()
    extra = set(re.findall(r'\.append\(\s*"([^"]+\.py)"', src))
    return base, base | extra


def _parse_build_sh():
    m = re.search(r"BASE_MODULES=\((.*?)\)", BUILD_SH.read_text(encoding="utf-8"), re.S)
    if not m:
        return set()
    return {"core/%s.py" % tok for tok in m.group(1).split()}


def test_setup_and_build_manifests_match():
    setup_base, _ = _parse_setup()
    build = _parse_build_sh()
    assert setup_base == build, (
        "setup.py EXT_MODULES 与 build.sh BASE_MODULES 不一致：\n"
        "  仅 setup 有: %s\n  仅 build.sh 有: %s" % (sorted(setup_base - build), sorted(build - setup_base))
    )


def test_every_compiled_module_is_registered():
    setup_base, _ = _parse_setup()
    actual = _actual_modules() - EXCLUDED - PRO_ONLY
    missing = actual - setup_base
    assert not missing, "以下模块未登记到编译清单（编译产物会 ModuleNotFoundError）：%s" % sorted(missing)
    stale = setup_base - actual
    assert not stale, "编译清单登记了磁盘上不存在的模块：%s" % sorted(stale)


def test_pro_module_registered_conditionally():
    _base, allmods = _parse_setup()
    assert PRO_ONLY <= allmods, "pro 版模块未在 setup.py 条件登记：%s" % sorted(PRO_ONLY - allmods)
