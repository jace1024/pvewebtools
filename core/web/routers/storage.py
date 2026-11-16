"""存储域路由。

覆盖存储列表与内容（``storages`` / ``storage/*``）、数据中心级存储配置（``storage-cfg``）
以及磁盘镜像管理（``disks/*``）。只声明数据，**不 import** ``core.web.server``（避免循环
导入）：无路径参数的处理用方法名字符串，带参处理用 ``fn(handler, params)``，参数由
``<storage>`` 模板捕获。

路径形态说明：``storage/<storage>/content`` 是 3 段带参模板，与同为 ``storage/*`` 的
``storage/download`` / ``storage/move-targets``（2 段全字面）段数不同，天然互斥；
``storage-cfg``（1 段）与 ``storage-cfg/<storage>``（2 段）同理。
"""


def _storage_content(handler, params):
    """GET /api/storage/<storage>/content[?node=]  列出指定存储的已有内容。"""
    return handler._handle_storage_content(params["storage"])


def _storage_cfg_get(handler, params):
    """GET /api/storage-cfg/<storage>  获取单个存储的完整配置（编辑表单回填用）。"""
    return handler._handle_storage_cfg_get(params["storage"])


def _storage_cfg_update(handler, params):
    """PUT /api/storage-cfg/<storage>  更新存储配置（body 为要修改的字段）。"""
    return handler._handle_storage_cfg_update(params["storage"])


def _storage_cfg_delete(handler, params):
    """DELETE /api/storage-cfg/<storage>  移除存储（仅删除配置，不影响实际数据）。"""
    return handler._handle_storage_cfg_delete(params["storage"])


HANDLERS = {
    "get": {
        # 存储列表（「存储内容」选择器 / 新增磁盘选存储）
        "storages": "_handle_storages",
        "storages/images": "_handle_image_storages",
        "storages/rootdir": "_handle_storages_rootdir",
        # 单个存储的内容与下载
        "storage/move-targets": "_handle_storage_move_targets",
        "storage/<storage>/content": _storage_content,
        "storage/download": "_handle_storage_download",
        # 数据中心级存储配置
        "storage-cfg": "_handle_storage_cfg_list",
        "storage-cfg/<storage>": _storage_cfg_get,
        # 磁盘镜像
        "disks": "_handle_disks",
        "disks/byid": "_handle_disks_byid",
        "disks/storages": "_handle_disk_storages",
        "disks/vm": "_handle_disks_vm",
        "disks/imported": "_handle_disks_imported",
        "disks/exported": "_handle_disk_list",
        "disks/download": "_handle_disk_download",
    },
    "post": {
        "storage/move-content": "_handle_storage_move_content",
        "storage/move": "_handle_storage_move",
        "storage-cfg": "_handle_storage_cfg_create",
        "disks/upload": "_handle_disk_upload",
        "disks/delete": "_handle_disk_delete",
    },
    "put": {
        "storage-cfg/<storage>": _storage_cfg_update,
    },
    "delete": {
        "storage": "_handle_storage_content_delete",
        "storage-cfg/<storage>": _storage_cfg_delete,
    },
}
