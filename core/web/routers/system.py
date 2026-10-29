"""系统 / 元信息域路由（结构重构 M2/T2.6 第二、九批）。

- ``options``：批量只读下拉端点，把工具弹窗的多个 GET 请求合并成 1 个。它内部复用
  ``ApiHandler._handle_get``，所以子请求同样走本路由表——白名单见 ``_OPTION_SOURCES``，
  未列入的路径一律拒绝（避免被当成任意转发代理）。
- ``edition``：编译期版本标记 + 运行期工具档位 + 版本号，供首屏胶囊秒显（不触发
  toolbox/jobs 重型路径）。
- ``schedules``：定时开关机任务列表（?node= 缺省查本机）。

只声明数据，**不 import** ``core.web.server``（避免循环导入）。
"""

HANDLERS = {
    "get": {
        "options": "_handle_options",
        "edition": "_handle_edition",
        "schedules": "_handle_schedules",
    },
}
