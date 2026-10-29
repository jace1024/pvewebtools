# 工具分类模块：storage（拆自原 toolbox.py TOOLS_BASE，按 category 归类）
from core.tools.toolbox_common import COMMON_HELPERS

TOOLS = [
{
       "id": "storage_status", "category": "storage", "risk": "info", "order": 1,
       "name": "存储状态总览",
       "desc": "快速总览（存储池/LVM/SMART）或深度体检（SMART 指标/通电时长/ZFS/水位预警）。",
       "action_risk": {"overview": "info", "deep": "info"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "overview", "label": "快速总览"},
               {"value": "deep", "label": "深度体检"},
           ]},
       ],
       "script_file": "scripts/storage_status.sh",
       "recommended": True,
   },

{
       "id": "disk_prep", "category": "storage", "risk": "high", "order": 2,
       "name": "磁盘分区与格式化",
       "desc": "清除旧盘（擦签名/全盘覆写/SSD 安全擦除，三选一）或分区格式化（GPT 1~4 区 + EXT4/XFS），均不可逆。",
       "action_risk": {"wipe": "high", "format": "high"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "wipe", "label": "清除旧盘"},
               {"value": "format", "label": "分区并格式化"},
           ]},
           {"name": "disk", "label": "目标磁盘", "type": "select", "options_from": "disks",
            "pattern": r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$"},
           {"name": "wipe_mode", "label": "擦除方式", "type": "select", "required": False, "options": [
               {"value": "quick", "label": "快速清除（擦签名，秒完成，数据可恢复）"},
               {"value": "zero", "label": "彻底覆写（全盘写 0，不可恢复，较慢）"},
               {"value": "discard", "label": "SSD 安全擦除（blkdiscard，仅 SSD/NVMe，不可恢复）"},
           ]},
           {"name": "parts", "label": "分区数", "type": "select", "required": False, "options": [
               {"value": "1", "label": "1 个分区（整盘）"},
               {"value": "2", "label": "2 个等分分区"},
               {"value": "3", "label": "3 个等分分区"},
               {"value": "4", "label": "4 个等分分区"},
           ]},
           {"name": "fstype", "label": "文件系统", "type": "select", "required": False, "options": [
               {"value": "ext4", "label": "EXT4（兼容性好）"},
               {"value": "xfs", "label": "XFS（大文件性能好）"},
               {"value": "btrfs", "label": "Btrfs（支持快照与压缩，适合 NAS）"},
               {"value": "exfat", "label": "exFAT（跨平台，Windows/Mac 通用）"},
           ]},
       ],
       "script_file": "scripts/disk_prep.sh",
   },

{
       "id": "disk_convert", "category": "storage", "risk": "high", "order": 3,
       "name": "虚机磁盘格式转换",
       "desc": "磁盘在 raw/qcow2/vmdk 间互转（含数据盘与 EFI 盘，自动清理残留旧盘）；raw 不支持快照、vmdk 便于迁 VMware/ESXi。转换前需关机。",
       "params": [
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "no_template": True, "pattern": r"^\d+$"},
           {"name": "target", "label": "目标格式", "type": "select", "options": [
               {"value": "qcow2", "label": "QCOW2（支持快照/稀疏，推荐）"},
               {"value": "raw", "label": "RAW（裸格式，性能略高但占满空间、不支持快照）"},
               {"value": "vmdk", "label": "VMDK（VMware/ESXi 格式，便于迁移到 VMware）"},
           ]},
           {"name": "keep_src", "label": "保留原盘", "type": "select", "required": False, "options": [
               {"value": "no", "label": "否（转换成功并验证后删除原盘，推荐）"},
               {"value": "yes", "label": "是（保留原盘，手动确认后再删）"},
           ]},
       ],
       "script_file": "scripts/disk_convert.sh",
   },

{
       "id": "disk_export", "category": "storage", "risk": "high", "order": 4,
       "name": "虚机磁盘导出",
       "desc": "将磁盘（或所有磁盘）转为指定格式导出到存储目录，便于迁 VMware/Hyper-V/VirtualBox/公有云；vmdk 可被 VMware 识别，vhdx/vdi 仅可下载不能挂回。导出前需关机。",
       "params": [
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "no_template": True, "pattern": r"^\d+$"},
           {"name": "disk", "label": "导出磁盘", "type": "select", "required": False, "options_from": "vm_disks"},
           {"name": "target", "label": "目标格式", "type": "select", "options": [
               {"value": "qcow2", "label": "QCOW2（KVM/OpenStack/PVE，推荐）"},
               {"value": "raw", "label": "RAW（裸格式，公有云常用）"},
               {"value": "vmdk", "label": "VMDK（VMware/ESXi）"},
               {"value": "vhdx", "label": "VHDX（Hyper-V，仅导出下载）"},
               {"value": "vdi", "label": "VDI（VirtualBox，仅导出下载）"},
           ]},
           {"name": "storage", "label": "导出到存储", "type": "select", "options_from": "disk_storages"},
       ],
       "script_file": "scripts/disk_export.sh",
   },

{
       "id": "disk_import", "category": "storage", "risk": "high", "order": 5,
       "name": "虚机磁盘导入",
       "desc": "将已上传的磁盘镜像（vmdk/qcow2/raw/vhdx/vdi）导入为指定虚拟机的磁盘并挂到所选总线。",
       "params": [
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "no_template": True, "pattern": r"^\d+$"},
           {"name": "diskfile", "label": "磁盘镜像", "type": "select", "options_from": "disk_upload"},
           {"name": "storage", "label": "导入到存储", "type": "select", "options_from": "disk_storages"},
           {"name": "bus", "label": "挂载总线", "type": "select", "options": [
               {"value": "auto", "label": "自动（下一个空闲槽位）"},
               {"value": "scsi0", "label": "scsi0（推荐，virtio-scsi）"},
               {"value": "sata0", "label": "sata0"},
               {"value": "virtio0", "label": "virtio0（virtio-blk）"},
               {"value": "ide0", "label": "ide0（兼容性最好）"},
           ]},
       ],
       "script_file": "scripts/disk_import.sh",
   },

{
       "id": "vm_disk", "category": "storage", "risk": "high", "order": 6,
       "name": "虚机磁盘新增与移除",
       "desc": "为虚拟机新增磁盘（选总线/存储/容量，自动填下一个空闲槽位）或移除磁盘；移除时可选彻底删除或仅卸载保留文件，且需先关机、数据不可恢复。",
       "action_risk": {"add": "high", "remove": "high"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "add", "label": "新增磁盘"},
               {"value": "remove", "label": "移除磁盘"},
           ]},
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "no_template": True, "pattern": r"^\d+$"},
           {"name": "bus", "label": "磁盘总线", "type": "select", "required": False, "options": [
               {"value": "scsi", "label": "SCSI（virtio-scsi，推荐）"},
               {"value": "virtio", "label": "VirtIO Block"},
               {"value": "sata", "label": "SATA"},
               {"value": "ide", "label": "IDE（兼容性最好）"},
           ]},
           {"name": "storage", "label": "目标存储", "type": "select", "required": False,
            "options_from": "image_storages"},
           {"name": "size", "label": "磁盘容量", "type": "text", "required": False,
            "pattern": r"^\d+[GTKMgtkm]?$", "placeholder": "如 32G / 500G / 2T",
            "hint": "数字，可带单位 G/T/M/K"},
           {"name": "disk", "label": "要移除的磁盘", "type": "select", "required": False,
            "options_from": "vm_disks"},
           {"name": "keep_disk", "label": "仅卸载保留磁盘", "type": "checkbox",
            "value": "1", "required": False,
            "placeholder": "勾选则仅从虚机卸载、保留磁盘（可重新挂载）；不勾选则彻底删除"},
       ],
       "script_file": "scripts/vm_disk.sh",
   }
]
