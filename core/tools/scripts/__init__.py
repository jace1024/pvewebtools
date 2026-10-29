# 工具箱外置 bash 脚本目录（T4.2）。
# 各 tools_*.py 的工具可声明 "script_file": "scripts/<id>.sh"，
# 由 core/tools/toolbox.py 的 _resolve_script 在加载期读取正文覆盖 script。
# 源码模式读物理文件；Nuitka 数据文件（--include-data-dir）经 importlib.resources 读取。
