"""M1.3 纯逻辑：core.paths 目录解析（源码形态，不依赖打包环境）。"""

from pathlib import Path


def test_resource_dir_is_core_package():
    from core import paths

    assert paths.RESOURCE_DIR.name == "core"
    assert (paths.RESOURCE_DIR / "static").is_dir()


def test_project_root_is_repo_root():
    from core import paths

    assert paths.PROJECT_ROOT == paths.RESOURCE_DIR.parent
    assert (paths.PROJECT_ROOT / "core").is_dir()


def test_exe_dir_is_absolute_path():
    from core import paths

    assert isinstance(paths.EXE_DIR, Path)
    assert paths.EXE_DIR.is_absolute()


def test_preset_override_dir_source_mode(monkeypatch):
    """源码运行（非 PVE_WEBTOOLS_BUILT=1）时，预设覆盖配置写在随包 config 目录。"""
    from core import paths

    monkeypatch.delenv("PVE_WEBTOOLS_BUILT", raising=False)
    assert paths.preset_override_dir() == paths.RESOURCE_DIR / "config"


def test_preset_override_dir_built_mode(monkeypatch):
    """二进制运行（PVE_WEBTOOLS_BUILT=1）时，预设覆盖配置写在安装目录（持久）。"""
    from core import paths

    monkeypatch.setenv("PVE_WEBTOOLS_BUILT", "1")
    assert paths.preset_override_dir() == paths.CONFIG_OVERRIDE_DIR
