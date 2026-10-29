"""PVE WebTools 产品源码包。

目录划分：
- entry/  程序入口（main 明文入口 + loader 启动引导）
- web/    HTTP 服务与异步任务（server / jobs）
- pve/    PVE 平台交互（pvesh 封装 / 存储供给 / 系统升级）
- tools/  工具箱（注册表 + 各分类工具集）
- static/ 前端资源；config/ 可编辑配置

编译（Cython + Nuitka）按包路径逐个模块编译为 .so，源码在编译后删除；
打包参数见 console/build-scripts/{setup.py,build.sh}。
"""
