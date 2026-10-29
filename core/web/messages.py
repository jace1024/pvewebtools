"""中文用户可见文案集中管理（重构 T4.4，先中文单语言）。

把散落在各模块、最终返回给前端展示的「文案」集中到这里，便于未来多语言化与统一校对。
当前仅中文单语言：:func:`t` 直接返回中文；key 缺失时回退为 key 本身（便于发现漏翻而不致崩溃）。

前端对应文件见 ``core/static/zh.js``（保持同步）。
"""

from typing import Dict

MESSAGES: Dict[str, str] = {
    # ---- 授权域（core/web/routers/license.py）----
    "lic_added": "已添加授权：%s（%s）",
    "lic_added_no_match": (
        "已添加授权（指纹 %s），但当前集群里没有指纹与之相符的机器 —— "
        "熔断不会因此解除。请确认发给授权方的指纹就是该机器的指纹，"
        "并确认该机器当前可达。"
    ),
    "lic_need_ip_pwd": "请填写机器 IP 与该机器 root 密码",
    "lic_host_format": "机器地址格式非法（仅允许字母数字及 .-:[] ）",
    "lic_need_ip": "请填写机器 IP（不支持主机名，避免与本机或现有节点重名）",
    "lic_no_ssh": "本机缺少 ssh 与 setsid，无法用密码连接新机器",
    "lic_fp_fail": "无法采集该机器硬件指纹（虚机节点不支持、或不可达）",
    # ---- 错误映射（core/web/errors.py）----
    "err_internal": "服务器内部错误",
}


def t(key: str) -> str:
    """查中文文案；缺失时回退为 key（避免崩溃，并暴露漏翻）。

    文案里的占位符（``%s`` 等）保持原样，由调用方按既有方式格式化，例如
    ``t("lic_added") % (node, fp)``，行为与抽离前完全一致。
    """
    return MESSAGES.get(key, key)


__all__ = ["MESSAGES", "t"]
