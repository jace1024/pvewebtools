"""传输中心域路由（结构重构 M2/T2.6 第六批）。

覆盖备份（``backup/*``）、模板缓存（``template-cache/*``）、直通物料上传与同步（``vbios/*``、
``vgpu/*``）、分片上传基础设施（``upload/*``）以及两个只读下拉源（``community-scripts/list``、
``docker-versions``）。

只声明数据，**不 import** ``core.web.server``（避免循环导入）。本域全部是无路径参数的精确
路由，一律用方法名字符串登记。

``vgpu/gpus`` 与 ``vgpu/types`` 在旧 if 链里由一条 ``parts in (...)`` 分支同时处理并共用
节点校验；迁入表后拆成两条，各自重复该校验，行为等价（这是文件里唯一的「一条件多路径」形态）。
"""

HANDLERS = {
    "get": {
        # 分片上传基础设施（断点续传 / 秒传）
        "upload/status": "_handle_upload_status",
        "upload/verify": "_handle_upload_verify",
        # 备份
        "backup/storages": "_handle_backup_storages",
        "backup/files": "_handle_backup_files",
        "backup/download": "_handle_backup_download",
        # 模板缓存（「缓存加速」面板）
        "template-cache/nodes": "_handle_template_cache_nodes",
        "template-cache": "_handle_template_cache",
        # 直通物料（vBIOS ROM / vGPU 宿主机物料）
        "vbios/roms": "_handle_vbios_roms",
        "vgpu/materials": "_handle_vgpu_materials",
        "vgpu/gpus": "_handle_vgpu_gpus",
        "vgpu/types": "_handle_vgpu_types",
        # 其它只读下拉源
        "community-scripts/list": "_handle_community_scripts",
        "docker-versions": "_handle_docker_versions",
    },
    "post": {
        "backup/upload": "_handle_backup_upload",
        "vbios/upload": "_handle_vbios_upload",
        "vbios/sync": "_handle_vbios_sync",
        "vgpu/upload": "_handle_vgpu_upload",
        "upload/register": "_handle_upload_register",
        "template-cache/warmup": "_handle_template_cache_warmup",
        "template-cache/expire": "_handle_template_cache_expire",
    },
    "delete": {
        "template-cache": "_handle_template_cache_delete",
        "backup": "_handle_backup_delete",
        "vbios": "_handle_vbios_remove",
    },
}
