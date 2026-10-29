"""网络与硬件直通域路由（结构重构 M2/T2.6 第五批）。

覆盖网络（``network/*``）、PCIe 直通（``pci`` 与 ``pci/*``）、USB 直通（``usb/devices``）
以及节点级硬件清单（``kernels*``、``grub/backups``）。全部是只读下拉数据源，带 ``?node=``
时按节点采集。

只声明数据，**不 import** ``core.web.server``（避免循环导入）；这些路由都没有路径参数，
一律用方法名字符串登记，处理入口见 ``ApiHandler._handle_network_*`` / ``_handle_pci_*`` 等。
"""

HANDLERS = {
    "get": {
        # 网络（网桥 / VLAN / 物理网口）
        "network/bridges": "_handle_network_bridges",
        "network/vlans": "_handle_network_vlans",
        "network/interfaces": "_handle_network_interfaces",
        # 网络配置备份（接口配置管理-从备份恢复下拉数据源）
        "network/backups": "_handle_network_backups",
        # PCIe 直通
        "pci": "_handle_pci_storage",
        "pci/nics": "_handle_pci_nics",
        "pci/gpus": "_handle_pci_gpus",
        "pci/generic": "_handle_pci_generic",
        "pci/vfio": "_handle_pci_vfio",
        # USB 直通
        "usb/devices": "_handle_usb_devices",
        # 节点级硬件清单
        "kernels": "_handle_kernels",
        "kernels/available": "_handle_kernels_available",
        "grub/backups": "_handle_grub_backups",
    },
}
