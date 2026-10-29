#!/usr/bin/env python3
"""检查工具箱「下拉数据源 ↔ 节点相关性」的登记情况。

工程纪律：任何 `options_from` 数据源都必须在 core/tools/toolbox.py 的
NODE_SCOPED_SOURCES / GUEST_OWNED_SOURCES / GUEST_NODE_SOURCES / NODE_AGNOSTIC_SOURCES
四张表里登记。漏登记不会报错，只会让该下拉"不联动"——而这类问题在界面上表现为
「切了执行节点但列表没变」「选到的对象在目标节点上不存在」「操作显示成功其实没做」，
排查成本很高（历史上已经踩过两次：guests_all 未按节点过滤、lxc_storages 未带节点）。

检查项：
  1. 有工具在用、但没登记分类的数据源        → 报错（exit 1）
  2. 登记了、但没有任何工具使用              → 提示（分类表里的死条目）
  3. 登记为 node_scoped、但服务端 _resolve_option_values 未覆盖
     → 提示（服务端复核会 fail-open，提交值不做校验）

用法（在仓库根执行）：
    python scripts/lint_tool_sources.py
"""
import ast
import os
import sys

# 仓库根（本脚本位于 <仓库根>/scripts/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "core", "web", "server.py")


def collect_used_sources():
    """导入工具注册表，返回 {数据源: [工具 id, ...]} 与工具箱模块。"""
    sys.path.insert(0, ROOT)
    from core.tools import toolbox
    used = {}
    for t in toolbox.TOOLS:
        for p in t.get("params") or []:
            src = p.get("options_from")
            if src:
                used.setdefault(src, []).append(t["id"])
    return used, toolbox


def collect_server_covered():
    """解析 server.py 的 _resolve_option_values，收集它明确处理过的 options_from 取值。"""
    try:
        tree = ast.parse(open(SERVER_PY, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return set()
    covered = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_resolve_option_values"):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Compare):
                continue
            left = sub.left
            if not (isinstance(left, ast.Name) and left.id == "options_from"):
                continue
            for comp in sub.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    covered.add(comp.value)
                elif isinstance(comp, (ast.Tuple, ast.Set, ast.List)):
                    covered |= {e.value for e in comp.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return covered


def main():
    used, toolbox = collect_used_sources()
    registered = (set(toolbox.NODE_SCOPED_SOURCES) | set(toolbox.GUEST_OWNED_SOURCES)
                  | set(toolbox.GUEST_NODE_SOURCES) | set(toolbox.NODE_AGNOSTIC_SOURCES))

    unknown = sorted(s for s in used if not toolbox.param_node_scope({"options_from": s}))
    dead = sorted(registered - set(used))
    covered = collect_server_covered()
    unverified = sorted(s for s in toolbox.NODE_SCOPED_SOURCES if s not in covered)

    print("== 数据源节点相关性检查 ==")
    print("使用中的数据源 %d 个；已登记 %d 个" % (len(used), len(registered)))

    if unknown:
        print("\n[错误] 以下数据源被工具使用、但没有登记分类（会导致该下拉不联动）：")
        for s in unknown:
            print("  %-18s ← %s" % (s, "、".join(sorted(set(used[s])))))
        print("  请补进 core/tools/toolbox.py 的分类表（node_scoped / belongs_to_node /"
              " guest_node / node_agnostic 四选一）")
    if dead:
        print("\n[提示] 以下数据源已登记、但没有任何工具使用（可能是历史残留）：")
        for s in dead:
            print("  %s" % s)
    if unverified:
        print("\n[提示] 以下 node_scoped 数据源未被服务端 _resolve_option_values 覆盖：")
        for s in unverified:
            print("  %s（前端按节点采集，但提交值不做复核，属于 fail-open）" % s)

    if not (unknown or dead or unverified):
        print("\n全部通过：数据源分类登记完整，且服务端复核覆盖全部节点级数据源。")
    return 1 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())
