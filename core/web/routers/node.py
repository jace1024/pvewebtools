"""节点（node）域路由。

只声明数据，**不 import** ``core.web.server``（避免循环导入）：无参处理用方法名字符串，
带参处理用模块级可调用对象 ``fn(handler, params)``——``<node>`` 由模板捕获，省掉旧分支里
``parts[1]`` 那样的位置约定。

开源版不含授权校验，加机 / 节点操作不设任何版本门禁，本表只负责「路径 -> 处理」的映射。
"""


def _reboot(handler, params):
    """POST /api/nodes/<node>/reboot  重启节点（直通配置等需重启生效的场景）。"""
    return handler._reboot_node(params["node"])


def _shutdown(handler, params):
    """POST /api/nodes/<node>/shutdown  关机节点（需确认词「关机节点」，无远程开机能力）。"""
    return handler._shutdown_node(params["node"])


def _storage_fix(handler, params):
    """POST /api/nodes/<node>/storage-fix  单节点重跑共享存储接入（保留供旧客户端 / 脚本）。

    按目标节点角色自动分流（客户端挂 NFS + 铺 vBIOS 软链；导出端改跑 NFS 服务端
    导出修复，避免自己挂自己）。判定在 ``ops._fix_node_storage`` 内；页面上的
    一键修复走 ``storage-fix-all``，逐台复用同一逻辑。
    """
    return handler._handle_node_storage_fix(params["node"])


def _nfs_server_fix(handler, params):
    """POST /api/nodes/<node>/nfs-server-fix  在 NFS 导出端重跑服务端导出。

    装服务 + 写 /etc/exports + exportfs。前端已统一走 storage-fix（按角色自动分流），
    此入口保留供旧客户端 / 运维脚本直接调用。
    """
    return handler._handle_node_nfs_server_fix(params["node"])


def _storage_fix_all(handler, params):
    """POST /api/nodes/storage-fix-all  一键修复所有节点：导出端优先，逐台返回修复结果。"""
    return handler._handle_node_storage_fix_all()


HANDLERS = {
    "get": {
        # GET /api/nodes  节点列表（节点管理页 / 执行节点下拉），详见 _handle_nodes
        "nodes": "_handle_nodes",
    },
    "post": {
        # POST /api/node/join-init  新节点加入集群初始化：挂 NFS + 铺 vBIOS 软链
        "node/join-init": "_handle_node_join_init",
        # 注意：两段静态路径要排在 nodes/<node>/... 之前无歧义（段数不同，find 按整段匹配）
        "nodes/storage-fix-all": _storage_fix_all,
        "nodes/<node>/reboot": _reboot,
        "nodes/<node>/shutdown": _shutdown,
        "nodes/<node>/storage-fix": _storage_fix,
        "nodes/<node>/nfs-server-fix": _nfs_server_fix,
    },
}
