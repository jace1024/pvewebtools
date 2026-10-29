"""启动引导（lite 开源版：无授权校验）。

包内模块按包路径导入；源码运行时直接装载 HTTP 服务并启动。
"""

from core.web import server  # noqa: F401  # 装载 ApiHandler / 路由表
from core.web.boot import main


def run():
    main()
