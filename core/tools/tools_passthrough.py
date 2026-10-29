# 工具分类模块：passthrough（拆自原 toolbox.py TOOLS_BASE，按 category 归类）
from core.tools.toolbox_common import COMMON_HELPERS, KARGS_PRESETS

TOOLS = [
{
       "id": "passthrough_check", "category": "passthrough", "risk": "info", "order": 1,
       "name": "直通前置检查",
       "desc": "只读检查直通前提：CPU 虚拟化、IOMMU 状态、分组隔离、显卡驱动占用等。",
       "params": [],
       "script_file": "scripts/passthrough_check.sh",
       "recommended": True,
       "tag": "前置检查",
   },

{
       "id": "passthrough_overview", "category": "passthrough", "risk": "info", "order": 2,
       "name": "直通设备总览",
       "desc": "只读列出各虚拟机的直通设备：PCIe、USB 与 RDM 磁盘归属。",
       "params": [],
       "script_file": "scripts/passthrough_overview.sh",
       "tag": "状态总览",
   },

{
       "id": "iommu_manage", "category": "passthrough", "risk": "high", "order": 3,
       "name": "开启硬件直通",
       "desc": "一键开启/关闭直通：配置 IOMMU、vfio 模块、可选驱动屏蔽并更新引导（重启生效）。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "enable", "label": "开启硬件直通（全部前提一次配齐）"},
               {"value": "disable", "label": "一键关闭并还原全部直通配置"},
           ]},
           {"name": "blacklist_dgpu", "label": "屏蔽独显驱动",
            "type": "select", "required": False, "default": "yes",
            "tooltip": "屏蔽 nouveau/nvidia/nvidiafb/amdgpu/radeon。注意：AMD 独显与 AMD APU 核显共用 amdgpu/radeon 驱动，勾选后会一并解绑（Intel 核显不受影响）。",
            "options": [
               {"value": "no", "label": "否（宿主机继续使用独显，直通时可能冲突）"},
               {"value": "yes", "label": "是（宿主机不再占用独显，让给 VM 直通；选是后宿主机将失去独显显示）"},
           ]},
           {"name": "blacklist_igpu", "label": "屏蔽核显/声卡驱动",
            "type": "select", "required": False, "default": "no",
            "tooltip": "屏蔽 i915（Intel 核显）+ snd_hda 声卡；AMD 平台的 APU 核显驱动同为 amdgpu/radeon，也会一并屏蔽（与 AMD 独显共用驱动）。",
            "options": [
               {"value": "no", "label": "否（宿主机继续使用核显；ARM 无核显选此项）"},
               {"value": "yes", "label": "是（宿主机不再占用核显，让给 VM 直通；Intel i915 + AMD APU amdgpu 一并屏蔽）"},
           ]},
       ],
       "script_file": "scripts/iommu_manage.sh",
       "recommended": True,
       "tag": "必做",
   },

{
       "id": "disk_manage", "category": "passthrough", "risk": "high", "order": 7,
       "name": "磁盘直通",
       "desc": "为虚拟机添加/移除磁盘直通（RDM 块映射或 PCIe 直通），设备全部下拉选择。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "add", "label": "添加磁盘直通"},
               {"value": "del", "label": "移除磁盘直通"},
           ]},
           {"name": "mode", "label": "直通方式", "type": "select", "options": [
               {"value": "block", "label": "块设备映射（RDM，把整块盘当普通硬盘给 VM，兼容性最好，推荐）"},
               {"value": "pcie", "label": "PCIe 设备直通（把整个硬盘控制器给 VM，可读原生 SMART，但需 IOMMU 且部分盘不兼容）"},
           ]},
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "pattern": r"^\d+$"},
           {"name": "device", "label": "磁盘（by-id）", "type": "select", "options_from": "device",
            "pattern": r"^[\w.:-]+$"},
           {"name": "bus", "label": "总线类型", "type": "select", "required": False, "options": [
               {"value": "scsi", "label": "SCSI（推荐）"},
               {"value": "sata", "label": "SATA"},
               {"value": "virtio", "label": "VirtIO Block"},
           ]},
       ],
       "script_file": "scripts/disk_manage.sh",
       "recommended": True,
   },

{
       "id": "nic_manage", "category": "passthrough", "risk": "high", "order": 4,
       "name": "网卡直通",
       "desc": "为虚拟机添加/移除 PCIe 网卡直通（需先开启 IOMMU），管理网口自动拦截防失联。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "add", "label": "添加网卡直通"},
               {"value": "del", "label": "移除网卡直通"},
           ]},
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "pattern": r"^\d+$"},
           {"name": "device", "label": "PCIe 网卡", "type": "select", "options_from": "nic",
            "pattern": r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$"},
       ],
       "script_file": "scripts/nic_manage.sh",
   },

{
       "id": "gpu_manage", "category": "passthrough", "risk": "high", "order": 5,
       "name": "显卡直通",
       "desc": "为虚拟机添加/移除 PCIe 显卡直通（需开启 IOMMU）：整块卡给 1 台 VM 独占，性能接近原生；区分独显/核显，可选挂 vBIOS。核显显示输出仅 Intel 支持 legacy-igd，AMD APU 核显走纯直通。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "add", "label": "添加显卡直通"},
               {"value": "del", "label": "移除显卡直通"},
           ]},
           {"name": "gtype", "label": "显卡类型", "type": "select", "options": [
               {"value": "dgpu", "label": "独显（NVIDIA / AMD / Intel Arc 独立显卡）"},
               {"value": "igpu", "label": "核显（Intel UHD/Iris/Arc 集成显卡、AMD APU Radeon/Vega/8xxM 等 CPU 集成显卡）"},
           ]},
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "pattern": r"^\d+$"},
           {"name": "device", "label": "PCIe 显卡", "type": "select", "options_from": "gpu",
            "pattern": r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$"},
           {"name": "igpu_use", "label": "核显用途", "type": "select",
            "tooltip": "仅核显有效。「仅计算」= 纯直通（不带任何子项，保留模拟显卡、VNC 控制台可用，Intel/AMD 都适用）；「需要显示输出」会自动把 VM 显示设为 none，再附加 legacy-igd（PVE 硬性要求，VNC 将看不到画面）——该项仅 Intel 核显支持，AMD APU 核显会退回纯直通",
            # 独显模式下该下拉会被置灰（不提交值），必须声明非必填，否则后端报「缺少参数」
            "required": False,
            "options": [
                {"value": "compute", "label": "仅计算（QSV/OpenCL 转码，推荐；Intel/AMD 通用）"},
                {"value": "display", "label": "需要显示输出（仅 Intel：vga=none + legacy-igd）"},
            ]},
           {"name": "xvga", "label": "独显用途", "type": "select",
            "tooltip": "两项在 VM 内都能用独显（计算 / 渲染不受影响），区别只在画面从哪输出：选「远程管理」不加 x-vga（VNC 可看画面），选「显示器直连」会加 x-vga=1（独显成为 VM 主显卡，画面从独显接口输出，VNC 黑屏）",
            # 核显模式下该下拉会被置灰（不提交值），故必须声明非必填，否则后端报「缺少参数」
            "required": False,
            "options": [
                {"value": "no", "label": "远程管理 / 计算（独显作副卡，画面走网页 VNC 控制台，推荐）"},
                {"value": "yes", "label": "显示器直连（独显作主显卡，画面从独显显示器接口输出，需接显示器；VNC 黑屏）"},
            ]},
           {"name": "romfile", "label": "vBIOS ROM 文件", "type": "select",
            "tooltip": "仅独显直通后黑屏、或虚拟机认不到显卡时才需要；ROM 上传后存放在 /usr/share/kvm/",
            "required": False, "options_from": "vbios_roms",
            "pattern": r"^[\w.-]{1,100}$"},
           {"name": "guest_fix", "label": "为虚机适配内核参数", "type": "checkbox",
            "required": False, "value": "1", "checked": True,
            "placeholder": "Linux 虚机自动写入，防直通后黑屏卡死（会自动开关机）；Windows 跳过"},
           {"name": "guest_preset", "label": "常用参数（快捷）", "type": "select",
            "required": False, "pattern": r"^[A-Za-z0-9_.=, +-]{0,120}$",
            # 清单与「虚机/容器运维 → 调整虚机内核参数」共用（toolbox_common.KARGS_PRESETS）
            "options": [{"value": "", "label": "不选（用默认值）"}] + KARGS_PRESETS},
           {"name": "guest_kargs", "label": "自定义参数（可选）", "type": "text",
            "required": False, "pattern": r"^[A-Za-z0-9_.=, +-]{0,120}$",
            "placeholder": "与默认值、快捷项叠加，如 modprobe.blacklist=pcspkr"},
       ],
       "script_file": "scripts/gpu_manage.sh",
   },

{
       "id": "pcie_manage", "category": "passthrough", "risk": "high", "order": 6,
       "name": "PCIe/USB 直通",
       "desc": "为虚拟机添加/移除 PCIe 设备（采集卡/声卡/USB 控制器等）与 USB 外设（支持热插）；存储/网卡/显卡用专用工具。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "pcie_add", "label": "添加 PCIe 设备直通"},
               {"value": "pcie_del", "label": "移除 PCIe 设备直通"},
               {"value": "usb_add", "label": "添加 USB 直通（支持热插）"},
               {"value": "usb_del", "label": "移除 USB 直通"},
           ]},
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "pattern": r"^\d+$"},
           {"name": "device", "label": "PCIe 设备", "type": "select",
            "options_from": "pci_generic", "required": False,
            "pattern": r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$"},
           {"name": "usb_device", "label": "USB 设备（vid:pid）", "type": "select",
            "options_from": "usb", "required": False,
            "pattern": r"^[0-9a-f]{4}:[0-9a-f]{4}$"},
           {"name": "usb3", "label": "USB 3.0 模式", "type": "select", "required": False, "options": [
               {"value": "no", "label": "否（默认，按 USB 2.0 挂载，兼容性最好）"},
               {"value": "yes", "label": "是（按 USB 3.0 挂载，速度更快，但需 USB3 控制器且兼容性略差）"}]},
       ],
       "script_file": "scripts/pcie_manage.sh",
   },

{
       "id": "passthrough_clear", "category": "passthrough", "risk": "high", "order": 8,
       "name": "移除 VM 全部直通",
       "desc": "清空指定 VM 的全部直通（PCIe/USB/RDM 盘，普通盘不动）并归还驱动；运行中先关机、移完开回。删除 VM 后设备仍被 vfio 占用的残留，请改用「回收直通设备」。",
       "params": [
           {"name": "vmid", "label": "虚机（VMID）", "type": "select", "options_from": "guests",
            "pattern": r"^\d+$"},
       ],
       "script_file": "scripts/passthrough_clear.sh",
   },

{
       "id": "vfio_release", "category": "passthrough", "risk": "high", "order": 9,
       "name": "回收直通设备",
       # 只读查看免确认；真正的释放走高危确认；仅解除屏蔽属配置层、风险低
       "action_risk": {"list": "info", "release": "high", "unblacklist": "low"},
       "desc": "解绑已无 VM 引用、仍被 vfio 占用的设备并装回原驱动（立即生效），同时清掉持久化黑名单。停用某台 VM 的直通请用「移除 VM 全部直通」。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "list", "label": "列出候选设备（只看不改）"},
               {"value": "release", "label": "释放设备给宿主机用（现在就能用）"},
               {"value": "unblacklist", "label": "只清驱动黑名单（不碰设备，重启后生效）"},
           ]},
           {"name": "devices", "label": "要释放的设备", "type": "checkbox", "multiple": True,
            "required": False, "options_from": "vfio_devices",
            "pattern": r"^([0-9a-fA-F:.]+)(,[0-9a-fA-F:.]+)*$",
            "errmsg": "设备地址格式不正确（形如 0000:03:00.0）",
            "hint": "列表只含「没人用、宿主也用不了」的设备：被 vfio 占着的，或上次解绑后还没装回驱动的；正在直通的不会出现在这里，手工填了也会被脚本拒绝"},
           {"name": "clear_override", "label": "清掉 driver_override", "type": "checkbox",
            "required": False, "value": "1", "checked": True,
            "placeholder": "PVE 直通时会给设备写 driver_override=vfio-pci；不清掉，原驱动绑不回去"},
       ],
       "tag": "直通清理",
       "script_file": "scripts/vfio_release.sh",
   }
]