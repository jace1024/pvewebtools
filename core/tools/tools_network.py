# 工具分类模块：network（拆自原 toolbox.py TOOLS_BASE，按 category 归类）
from core.tools.toolbox_common import COMMON_HELPERS

TOOLS = [
{
       "id": "net_diagnose", "category": "network", "risk": "info", "order": 2,
       "name": "一键网络诊断",
       "desc": "汇总接口、连通性、DNS、IPv6 状态，快速定位网络异常。",
       "params": [],
       "script_file": "scripts/net_diagnose.sh",
       "recommended": True,
   },

{
       "id": "net_info", "category": "network", "risk": "info", "order": 1,
       "name": "网络信息",
       "desc": "只读查看网络运行状态（接口/网桥/路由/端口）或配置文件。",
       "action_risk": {"status": "info", "config": "info"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "status", "label": "查看运行状态"},
               {"value": "config", "label": "查看配置文件（/etc/network/interfaces）"},
           ]},
       ],
       "script_file": "scripts/net_info.sh",
   },

{
       "id": "net_ping", "category": "network", "risk": "info", "order": 3,
       "name": "连通性测试",
       "desc": "Ping + DNS 解析测试（需目标地址），或 IPv6 就绪度检查。",
       "action_risk": {"ping": "info", "ipv6": "info"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "ping", "label": "Ping / DNS 解析测试"},
               {"value": "ipv6", "label": "IPv6 就绪度检查"},
           ]},
           {"name": "target", "label": "目标地址", "type": "text",
            "required": False, "pattern": r"^[\w.:-]+$",
            "placeholder": "如 223.5.5.5 或 www.baidu.com"},
       ],
       "script_file": "scripts/net_ping.sh",
   },

{
       "id": "iface_manage", "category": "network", "risk": "high", "order": 4,
       "name": "网络配置管理",
       # 风险按操作区分：查看/列出只读免确认词，备份低风险，恢复与应用走高危确认词
       "action_risk": {"view": "info", "backup": "low", "list": "info",
                       "restore": "high", "apply": "high"},
       "desc": "查看、备份、恢复或重新加载网络配置（/etc/network/interfaces）；改完在此点「应用配置」。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "view", "label": "查看当前配置"},
               {"value": "backup", "label": "备份当前配置"},
               {"value": "list", "label": "列出现有备份"},
               {"value": "restore", "label": "从备份恢复（高危）"},
               {"value": "apply", "label": "应用配置使其生效（高危）"},
           ]},
           {"name": "note", "label": "备份备注", "type": "text", "pattern": r"^[\w\u4e00-\u9fa5-]{0,30}$",
            "placeholder": "如：改网桥前（仅备份时用）", "required": False},
           {"name": "backup_name", "label": "备份文件", "type": "select",
            "required": False, "options_from": "net_backups",
            "pattern": r"^[\w\u4e00-\u9fa5.-]+\.bak$"},
       ],
       "script_file": "scripts/iface_manage.sh",
   },

{
       "id": "bridge_manage", "category": "network", "risk": "high", "order": 5,
       "name": "网桥与 VLAN",
       "desc": "创建/删除 vmbr 网桥、添加/移除 VLAN 接口（自动备份）；改完到「网络配置管理」应用生效。",
       "action_risk": {"add_bridge": "high", "del_bridge": "high",
                       "add_vlan": "high", "del_vlan": "high"},
       "recommend": [{"tool": "iface_manage",
                      "reason": "网桥变更可能导致断网，建议先备份网络配置以便出问题时快速恢复"}],
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "add_bridge", "label": "创建网桥（自动分配下一个 vmbr 号）"},
               {"value": "del_bridge", "label": "删除网桥（高危）"},
               {"value": "add_vlan", "label": "添加 VLAN"},
               {"value": "del_vlan", "label": "删除 VLAN（高危）"},
           ]},
           {"name": "bridge", "label": "网桥", "type": "select",
            "required": False, "options_from": "bridges",
            "pattern": r"^vmbr\d{1,3}$"},
           {"name": "port", "label": "物理网口", "type": "select",
            "required": False, "options_from": "phys_nics",
            "pattern": r"^[A-Za-z0-9_.@-]{1,32}$"},
           {"name": "cidr", "label": "网桥 IP", "type": "text",
            "required": False, "pattern": r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$",
            "placeholder": "如 192.168.10.1/24"},
           {"name": "tag", "label": "VLAN 标签", "type": "text",
            "required": False, "pattern": r"^[1-9][0-9]{0,3}$", "placeholder": "1-4094，如 100"},
           {"name": "vlan", "label": "VLAN 接口", "type": "select",
            "required": False, "options_from": "vlans",
            "pattern": r"^[A-Za-z0-9_-]{1,16}\.[0-9]{1,4}$"},
           {"name": "vcidr", "label": "VLAN 接口 IP", "type": "text",
            "required": False, "pattern": r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$",
            "placeholder": "如 192.168.20.1/24"},
           {"name": "apply_now", "label": "改完立即生效", "type": "checkbox", "checked": True,
            "required": False, "value": "1",
            "placeholder": "勾选后自动应用配置（取消则需到「网络配置管理」手动应用）"},
       ],
       "script_file": "scripts/bridge_manage.sh",
   }
]
