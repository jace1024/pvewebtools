"""当前运行版本的展示信息。

以源码直跑交付，版本号只读随包的 ``VERSION`` 文件；文件缺失或为空时回退为「未知」，
此时前端版本胶囊只显示「开源版」不带版本号。
"""

from core import paths


def _source_version() -> str:
    """随包 VERSION 文件里的版本号。"""
    try:
        text = (paths.RESOURCE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.splitlines()[0].strip() if text else ""


def version_info() -> dict:
    """当前运行版本的展示信息（版本号 / 来源）。"""
    return {"version": _source_version() or "未知", "source": "source"}


__all__ = ["version_info"]
