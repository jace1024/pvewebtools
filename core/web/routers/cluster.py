"""集群管理域路由：状态查询（``cluster/info|quorum``）与建集群 / 加入 / 仲裁调整
（``cluster/create|join|quorum/degrade|quorum/restore``）。

只声明数据、不 import ``core.web.server``（避免循环导入）。``cluster/quorum``（2 段）与
``cluster/quorum/{degrade,restore}``（3 段）段数不同，天然互斥。四个写操作均不在授权熔断的
豁免名单里（熔断时不可改集群拓扑）。
"""

HANDLERS = {
    "get": {
        "cluster/info": "_handle_cluster_info",
        "cluster/quorum": "_handle_cluster_quorum",
    },
    "post": {
        "cluster/create": "_create_cluster",
        "cluster/join": "_join_cluster",
        "cluster/quorum/degrade": "_degrade_quorum",
        "cluster/quorum/restore": "_restore_quorum",
    },
}
