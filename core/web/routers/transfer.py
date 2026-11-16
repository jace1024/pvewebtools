"""传输中心域路由。

覆盖备份（``backup/*``）、模板缓存（``template-cache/*``）、vBIOS ROM 上传与同步（``vbios/*``）、
分片上传基础设施（``upload/*``）以及只读下拉源 ``docker-versions``。

只声明数据，**不 import** ``core.web.server``（避免循环导入）。本域全部是无路径参数的精确
路由，一律用方法名字符串登记。
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
        # 直通物料（vBIOS ROM）
        "vbios/roms": "_handle_vbios_roms",
        # 其它只读下拉源
        "docker-versions": "_handle_docker_versions",
    },
    "post": {
        "backup/upload": "_handle_backup_upload",
        "vbios/upload": "_handle_vbios_upload",
        "vbios/sync": "_handle_vbios_sync",
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
