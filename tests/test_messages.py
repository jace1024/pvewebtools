"""T4.4 文案集中管理回归测试。

- t(key) 返回正确中文；未知 key 回退为 key 本身。
- 所有后端 t("key") 调用点的 key 都必须存在于 MESSAGES（防止抽离后漏翻/拼错）。
- 前端 core/static/zh.js 的 key 集合必须与后端 MESSAGES 完全一致（前后端文案同步）。
"""

import re

import core.web.messages as messages


def test_t_known_keys():
    assert messages.t("lic_added") == "已添加授权：%s（%s）"
    assert messages.t("err_internal") == "服务器内部错误"
    # % 占位符保持原样，调用方自行格式化
    assert messages.t("lic_added") % ("node1", "fp-x") == "已添加授权：node1（fp-x）"


def test_t_unknown_key_falls_back():
    assert messages.t("__not_a_real_key__") == "__not_a_real_key__"


def _backend_call_site_keys():
    import pathlib

    keys = set()
    pat = re.compile(r"""\bt\(\s*["']([^"']+)["']\s*\)""")
    for p in pathlib.Path("core").rglob("*.py"):
        for line in p.read_text(encoding="utf-8").splitlines():
            for m in pat.findall(line):
                keys.add(m)
    return keys


def test_all_backend_call_site_keys_exist():
    missing = sorted(k for k in _backend_call_site_keys() if k not in messages.MESSAGES)
    assert not missing, f"以下 t() key 在 MESSAGES 中缺失：{missing}"


def test_frontend_keys_match_backend():
    zh = (pathlib := __import__("pathlib")).Path("core/static/zh.js").read_text(encoding="utf-8")
    frontend_keys = set(re.findall(r'^\s*"([^"]+)":', zh, re.M))
    backend_keys = set(messages.MESSAGES.keys())
    assert frontend_keys == backend_keys, (
        f"前后端文案 key 不一致：仅前端有 {sorted(frontend_keys - backend_keys)}，"
        f"仅后端有 {sorted(backend_keys - frontend_keys)}"
    )
