"""节点（node）域路由（结构重构 M2/T2.6 第二批）。

只声明数据，**不 import** ``core.web.server``（避免循环导入）：无参处理用方法名字符串，
带参处理用模块级可调用对象 ``fn(handler, params)``——``<node>`` 由模板捕获，省掉旧分支里
``parts[1]`` 那样的位置约定。

授权判定不在这层：写闸口仍在 ``do_POST`` 里统一执行（``node/join-init`` 在闸口内单独
豁免，因为加机流程另有 ``gate_add_node`` 闸口），本表只负责「路径 -> 处理」的映射。
"""


def _reboot(handler, params):
    """POST /api/nodes/<node>/reboot  重启节点（直通配置等需重启生效的场景）。"""
    return handler._reboot_node(params["node"])


def _storage_fix(handler, params):
    """POST /api/nodes/<node>/storage-fix  对已加入集群的节点重跑共享存储接入。

    客户端侧动作：挂 NFS + 铺 vBIOS 软链；若目标节点即导出端则自动改跑服务端修复，
    避免自己挂自己（判定在 ``_handle_node_storage_fix`` 内）。
    """
    return handler._handle_node_storage_fix(params["node"])


def _nfs_server_fix(handler, params):
    """POST /api/nodes/<node>/nfs-server-fix  在 NFS 导出端重跑服务端导出。

    装服务 + 写 /etc/exports + exportfs。
    """
    return handler._handle_node_nfs_server_fix(params["node"])


HANDLERS = {
    "get": {
        # GET /api/nodes  节点列表（节点管理页 / 执行节点下拉），详见 _handle_nodes
        "nodes": "_handle_nodes",
    },
    "post": {
        # POST /api/node/join-init  新节点加入集群初始化：挂 NFS + 铺 vBIOS 软链
        "node/join-init": "_handle_node_join_init",
        "nodes/<node>/reboot": _reboot,
        "nodes/<node>/storage-fix": _storage_fix,
        "nodes/<node>/nfs-server-fix": _nfs_server_fix,
    },
}
