# 工具分类模块：rescue（拆自原 toolbox.py TOOLS_BASE，按 category 归类）
from core.tools.toolbox_common import COMMON_HELPERS

TOOLS = [
{
       "id": "svc_diag", "category": "rescue", "risk": "low", "order": 1,
       "name": "PVE 服务诊断",
       "desc": "查看 PVE 核心服务状态与 8006 端口，或按依赖顺序重启全部服务。",
       "action_risk": {"status": "info", "restart": "low"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "status", "label": "查看服务状态"},
               {"value": "restart", "label": "重启全部服务"},
           ]},
       ],
       "script_file": "scripts/svc_diag.sh",
   },

{
       "id": "log_check", "category": "rescue", "risk": "info", "order": 2,
       "name": "错误日志速查",
       "desc": "只读聚合系统错误日志（journal）、内核错误（dmesg）与失败单元。",
       "params": [],
       "script_file": "scripts/log_check.sh",
   },

{
       "id": "comp_restore", "category": "rescue", "risk": "low", "order": 3,
       "name": "官方组件恢复",
       "desc": "重装官方 WebUI 组件（还原 JS 修改）或官方 pve-qemu-kvm。",
       "action_risk": {"webui": "low", "qemu": "low"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "webui", "label": "恢复官方 WebUI 文件"},
               {"value": "qemu", "label": "恢复官方 pve-qemu-kvm"},
           ]},
       ],
       "script_file": "scripts/comp_restore.sh",
   },
]
