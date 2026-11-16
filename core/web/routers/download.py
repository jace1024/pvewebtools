"""镜像下载域路由。

覆盖云镜像（``cloud-image/*``）与 CT 模板（``lxc-template/*``）两部分：后台下载任务的进度
查询与取消、可选下载源/目标存储下拉、预设源维护。

只声明数据，**不 import** ``core.web.server``（避免循环导入）。本域全部是无路径参数的精确
路由，一律用方法名字符串登记。

注意 ``cloud-image/presets`` 在 GET 与 POST 下语义不同（GET 读预设源、POST 覆盖写入），
表里按方法分开登记，处理入口也不同（``_handle_cloud_image_presets_get`` /
``_handle_cloud_image_presets_set``）。
"""

HANDLERS = {
    "get": {
        # 云镜像：进度 / 任务列表 / 预设源 / 下载源 / 目标存储
        "cloud-image/status": "_handle_cloud_image_status",
        "cloud-image/list": "_handle_cloud_image_list",
        "cloud-image/presets": "_handle_cloud_image_presets_get",
        "cloud-image/mirrors": "_handle_cloud_image_mirrors",
        "cloud-image/storages": "_handle_cloud_image_storages",
        # CT 模板：可下载列表 / 已下载 / 目标存储 / 集群源一致性 / 进度 / 任务列表
        "lxc-template/available": "_handle_lxc_available",
        "lxc-template/downloaded": "_handle_lxc_downloaded",
        "lxc-template/storages": "_handle_lxc_storages",
        "lxc-template/cluster-sources": "_handle_lxc_cluster_sources",
        "lxc-template/status": "_handle_lxc_status",
        "lxc-template/list": "_handle_lxc_list",
    },
    "post": {
        # 云镜像：下载 / 预设源写入 / 预设源探活 / 预设源重置 / 取消
        "cloud-image/download": "_handle_cloud_image_download",
        "cloud-image/presets": "_handle_cloud_image_presets_set",
        "cloud-image/presets/check": "_handle_cloud_image_presets_check",
        "cloud-image/presets/reset": "_handle_cloud_image_presets_reset",
        "cloud-image/cancel": "_handle_cloud_image_cancel",
        # CT 模板：分片上传 / 下载 / 批量换源 / 取消
        "lxc-template/upload": "_handle_vztmpl_upload",
        "lxc-template/download": "_handle_lxc_download",
        "lxc-template/batch-source-swap": "_handle_lxc_batch_swap",
        "lxc-template/cancel": "_handle_lxc_cancel",
    },
}
