# 工具分类模块：kernel（拆自原 toolbox.py TOOLS_BASE，按 category 归类）
from core.tools.toolbox_common import COMMON_HELPERS

TOOLS = [
{
       "id": "boot_check", "category": "kernel", "risk": "info", "order": 1,
       "name": "启动状态检查",
       "desc": "内核版本、已装内核、GRUB 参数与直通状态、EFI 注册状态、GRUB 备份，一次看全。",
       "params": [],
       "script_file": "scripts/boot_check.sh",
   },

{
       "id": "grub_mgr", "category": "kernel", "risk": "high", "order": 2,
       "name": "GRUB 配置",
       # 风险按操作区分：查看/列出只读免确认词，备份低风险，改参数与恢复走高危确认词
       "action_risk": {"view": "info", "backup": "low", "list": "info",
                       "add": "high", "remove": "high", "restore": "high"},
       "desc": "查看/备份/恢复 GRUB 配置，添加/移除内核启动参数；写操作自动备份并更新引导，改坏可回退。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "view", "label": "查看当前配置"},
               {"value": "backup", "label": "备份当前配置"},
               {"value": "list", "label": "列出现有备份"},
               {"value": "add", "label": "添加启动参数（高危）"},
               {"value": "remove", "label": "移除启动参数（高危）"},
               {"value": "restore", "label": "从备份恢复（高危）"},
           ]},
           {"name": "param", "label": "启动参数", "type": "text",
            "required": False, "pattern": r"^[A-Za-z0-9_.=-]{1,64}$",
            "placeholder": "如 quiet 或 mitigations=off；移除时填参数名即可"},
           {"name": "note", "label": "备份备注", "type": "text", "pattern": r"^[\w\u4e00-\u9fa5-]{0,30}$",
            "placeholder": "如：开直通前（仅备份时用）", "required": False},
           {"name": "backup_name", "label": "备份文件", "type": "select",
            "required": False, "options_from": "grub_backups",
            "pattern": r"^[\w\u4e00-\u9fa5.-]+\.bak$"},
       ],
       "script_file": "scripts/grub_mgr.sh",
   },

{
       "id": "kernel_mgr", "category": "kernel", "risk": "high", "order": 3,
       "name": "内核配置",
       # 风险按操作区分：两项查看只读免确认词，安装/设默认/清理/重启走高危确认词
       "action_risk": {"current": "info", "available": "info",
                       "install": "high", "default": "high", "cleanup": "high", "reboot": "high"},
       "recommend": [{"tool": "grub_mgr",
                      "reason": "内核变更有风险，建议先备份或查看 GRUB 配置以便出问题时恢复"}],
       "desc": "显示/安装/切换/清理内核，并设置默认启动项、重启以生效。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "current", "label": "显示当前内核"},
               {"value": "available", "label": "查看可用内核"},
               {"value": "install", "label": "安装新内核"},
               {"value": "default", "label": "设置默认启动内核"},
               {"value": "cleanup", "label": "清理旧内核"},
               {"value": "reboot", "label": "重启系统应用新内核"},
           ]},
           {"name": "version", "label": "要安装的内核", "type": "select",
            "required": False, "options_from": "kernels_available",
            "pattern": r"^(?:[0-9][0-9A-Za-z.+-]{0,63}|(?:proxmox|pve)-kernel-[0-9][0-9A-Za-z.+-]{0,63})$"},
           {"name": "boot_ver", "label": "设为默认启动的内核", "type": "select",
            "required": False, "options_from": "kernels",
            "pattern": r"^[0-9][0-9A-Za-z.+-]{0,63}$"},
       ],
       "script_file": "scripts/kernel_mgr.sh",
   },

{
       "id": "efi_boot", "category": "kernel", "risk": "low", "order": 4,
       "name": "EFI 引导维护",
       "desc": "查看/同步 EFI 引导项（proxmox-boot-tool），修复 ESP 注册丢失；BIOS 引导自动跳过。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "status", "label": "查看 EFI 引导状态"},
               {"value": "sync", "label": "同步内核到 EFI 分区（refresh）"},
               {"value": "fix", "label": "修复 ESP 注册（uuids 文件丢失时用）"},
           ]},
       ],
       "script_file": "scripts/efi_boot.sh",
   }
]
