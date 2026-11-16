"""PVE WebTools（开源版）产品源码包。

目录划分：
- entry/  程序入口（main 明文入口 + loader 启动引导）
- web/    HTTP 服务与异步任务（server / jobs）
- pve/    PVE 平台交互（pvesh 封装 / 存储供给 / 密码 SSH）
- tools/  工具箱（注册表 + 各分类工具集 + scripts/ 下的 bash 实现）
- static/ 前端资源；config/ 可编辑配置
- version.py  版本号与构建串展示

本版本不含授权（licensing）、系统升级（upgrade）、高级玩法（Pro）与开发版（Dev）模块。
"""
