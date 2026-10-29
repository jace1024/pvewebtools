#!/usr/bin/env python3
"""从 Python 源抽取内嵌 bash 片段 + 直接检查 .sh 文件，逐个跑 shellcheck。

对应对标项目 community-scripts/ProxmoxVE 的工程纪律：
「所有 bash 都要过 shellcheck，CI 不通过则拦下」。

用法（在仓库根执行）：
    python scripts/lint_shell.py    # 检查全部内嵌片段与 console/*.sh
依赖：
    shellcheck（https://github.com/koalaman/shellcheck），未安装会提示安装。
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile

# 仓库根（本脚本位于 <仓库根>/scripts/），所有相对路径都以它为基准
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 可能含内嵌 bash 的源文件（包内相对路径；无片段的文件会被安全跳过）
SOURCES = [
    "core/pve/pve_api.py",
    "core/pve/storage_provision.py",
    "core/tools/toolbox_common.py",
    "core/tools/tools_kernel.py",
]

# 内嵌 bash 片段识别特征（满足其一即视为 shell）
_SHELL_MARKERS = (
    "function ", "set -", "echo ", "local ", "export ",
    "if [", "then", "fi\n", "done", ";;", "$(", "${",
    "esac", "case ", "fi ", "`",
)


def _looks_like_shell(text):
    if "\n" not in text:
        return False
    return any(m in text for m in _SHELL_MARKERS)


def extract_embedded(targets):
    """用 ast 解析每个源文件，收集所有「字符串常量赋值」中像 bash 的片段。"""
    found = []
    for rel in targets:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as e:
            print(f"[跳过] {rel} 语法错误: {e}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if (isinstance(t, ast.Name)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and _looks_like_shell(node.value.value)):
                    found.append((rel, t.id, node.value.value))
    return found


def scan_sh_files():
    """直接收集 console 下的独立 .sh 文件。"""
    out = []
    sh_dir = os.path.join(ROOT, "console")
    if os.path.isdir(sh_dir):
        for name in sorted(os.listdir(sh_dir)):
            if name.endswith(".sh"):
                out.append(os.path.join(sh_dir, name))
    return out


def main():
    snippets = extract_embedded(SOURCES)
    sh_files = scan_sh_files()
    total = len(snippets) + len(sh_files)
    if total == 0:
        print("未发现内嵌 bash 片段或 .sh 文件。")
        return 0

    if shutil.which("shellcheck") is None:
        print("错误：未找到 shellcheck。请先安装：https://github.com/koalaman/shellcheck",
              file=sys.stderr)
        print(f"已提取 {len(snippets)} 个内嵌片段 + {len(sh_files)} 个 .sh 文件"
              f"（未执行检查，仅验证提取）。", file=sys.stderr)
        return 1

    rcfile = os.path.join(ROOT, ".shellcheckrc")
    tmp = tempfile.mkdtemp(prefix="pvewebtools-shellcheck-")
    rc = 0

    # 内嵌片段：写出临时 .sh 再检查
    for rel, var, val in snippets:
        safe = f"{os.path.splitext(rel)[0].replace(os.sep, '_')}_{var}.sh"
        fpath = os.path.join(tmp, safe)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(val)
        print(f"==> 检查 {rel}:{var}")
        cmd = ["shellcheck", "-S", "warning"]
        if os.path.exists(rcfile):
            cmd += ["--rcfile", rcfile]
        cmd.append(fpath)
        if subprocess.run(cmd).returncode != 0:
            rc = 1

    # 独立 .sh：直接检查
    for fpath in sh_files:
        print(f"==> 检查 {os.path.relpath(fpath, ROOT)}")
        cmd = ["shellcheck", "-S", "warning"]
        if os.path.exists(rcfile):
            cmd += ["--rcfile", rcfile]
        cmd.append(fpath)
        if subprocess.run(cmd).returncode != 0:
            rc = 1

    if rc == 0:
        print(f"\n✓ 全部 {total} 项通过 shellcheck。")
    else:
        print(f"\n✗ 存在问题，请按上面提示修复（共 {total} 项）。", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
