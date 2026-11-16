"""写操作闸口。

``POST`` / ``PUT`` / ``DELETE`` 的公共前置校验集中在这里：路由分发前统一过一次，
以后要加跨接口的写保护（审计、限流、权限）只改本模块，不必逐个接口散着写。
"""


class GateMixin:
    """写接口统一闸口。"""

    def _gate_write(self, parts):
        """写接口统一闸口：返回 True 表示放行（当前无额外限制）。"""
        return True
