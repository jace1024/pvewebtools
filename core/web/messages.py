"""中文用户可见文案集中管理（先中文单语言）。
把散落在各模块、最终返回给前端展示的「文案」集中到这里，便于未来多语言化与统一校对。
当前仅中文单语言：:func:`t` 直接返回中文；key 缺失时回退为 key 本身（便于发现漏翻而不致崩溃）。

前端对应文件见 ``core/static/zh.js``（保持同步）。
"""

from typing import Dict

MESSAGES: Dict[str, str] = {
    # ---- 错误映射（core/web/errors.py）----
    "err_internal": "服务器内部错误",
}


def t(key: str) -> str:
    """查中文文案；缺失时回退为 key（避免崩溃，并暴露漏翻）。"""
    return MESSAGES.get(key, key)


__all__ = ["MESSAGES", "t"]
