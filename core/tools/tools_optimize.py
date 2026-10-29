# 工具分类模块：optimize（拆自原 toolbox.py TOOLS_BASE，按 category 归类）
from core.tools.toolbox_common import COMMON_HELPERS

TOOLS = [
{
       "id": "quick_setup", "category": "optimize", "risk": "low", "order": 1,
       "name": "一键优化",
       "desc": "组合执行：换源 + 系统更新 + 删除订阅弹窗 + 时间同步。",
       "params": [],
       "script": None,  # 运行时由 server 组装（复用换源/更新/弹窗/时间同步四段脚本）
       # 顺序约束（apt upgrade 会覆盖以下两类由包提供的文件，故相关步骤必须排在系统更新之后）：
       #  1) 删订阅弹窗：apt upgrade 更新 proxmox-widget-toolkit 会把 proxmoxlib.js 覆盖回原始版本；
       #  2) 换 LXC(CT) 源：apt upgrade 更新 libpve-common-perl 会把 APLInfo.pm 覆盖回官方 download.proxmox.com。
       # 故 change_sources 必须放在 update_system 之后，否则刚改的 CT 源会被升级冲掉。
       # 为兼顾「系统更新走镜像加速」，升级前也先写一次源（apt 源文件非包所有、升级不覆盖，且仅 APLInfo.pm 会被还原）。
       "compose": ["change_sources:mirror=ustc,security=mirror",
                   "update_system:action=update",
                   "change_sources:mirror=ustc,security=mirror",
                   "remove_popup",
                   "time_sync:action=sync"],
       "recommended": True,
   },

{
       "id": "update_system", "category": "optimize", "risk": "low", "order": 2,
       "action_risk": {"update": "low", "block": "low", "unblock": "low"},
       "name": "系统更新与禁止",
       "desc": "立即更新系统软件包；或禁止/解除系统自动更新。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "update", "label": "立即更新系统"},
               {"value": "block", "label": "禁止系统更新（锁定）"},
               {"value": "unblock", "label": "解除禁止（恢复更新）"}]},
       ],
       "script_file": "scripts/update_system.sh",
   },

{
       "id": "remove_popup", "category": "optimize", "risk": "low", "order": 4,
       "name": "删除订阅弹窗",
       "desc": "移除 WebUI 订阅弹窗，并安装 apt hook 使升级后仍保持屏蔽。",
       "params": [],
       "script_file": "scripts/remove_popup.sh",
   },

{
       "id": "ssh_speed", "category": "optimize", "risk": "low", "order": 5,
       "name": "优化 SSH 连接速度",
       "desc": "关闭 sshd 的 DNS 反查与 GSSAPI 认证，加快 SSH 登录与响应。",
       "params": [],
       "script_file": "scripts/ssh_speed.sh",
   },

{
       "id": "change_sources", "category": "optimize", "risk": "low", "order": 3,
       "name": "更换软件源和 LXC 源",
       "desc": "切换 Debian / PVE / Ceph 与 LXC 模板源，加速下载、修复更新失败。",
       "params": [
           {"name": "mirror", "label": "镜像源", "type": "select", "options": [
               {"value": "ustc", "label": "中科大 USTC"},
               {"value": "tuna", "label": "清华 TUNA"},
               {"value": "official", "label": "官方源"},
               {"value": "auto", "label": "自动测速选最快（推荐）"},
           ]},
           {"name": "security", "label": "安全更新源", "type": "select", "default": "mirror",
            "hint": "安全更新源即 Debian 系统漏洞补丁的来源。暴露在公网、要求第一时间打补丁 → 选官方源；只在意国内下载速度 → 选镜像站源。",
            "options": [
               {"value": "official", "label": "官方源（补丁最及时，国内可能慢）"},
               {"value": "mirror", "label": "镜像站源（下载快，补丁可能晚几小时）"},
           ]},
       ],
       "script_file": "scripts/change_sources.sh",
   },

{
       "id": "disk_clean", "category": "optimize", "risk": "low", "order": 8,
       "name": "一键释放系统空间",
       "desc": "清理 apt 缓存与冗余包 + 系统日志瘦身 + 轮转旧日志。",
       "params": [
           {"name": "journal", "label": "日志保留上限", "type": "select", "options": [
               {"value": "100M", "label": "100 MB（最省空间）"},
               {"value": "200M", "label": "200 MB（推荐）"},
               {"value": "500M", "label": "500 MB（保留更多排查线索）"},
               {"value": "1G", "label": "1 GB"},
           ]},
       ],
       "script_file": "scripts/disk_clean.sh",
   },

{
       "id": "cpu_freq", "category": "optimize", "risk": "low", "order": 7,
       "name": "CPU 调频与节能设置",
       "desc": "切换 CPU 调频策略（性能优先 / 均衡 / 省电），可选满载锁频；或切换为「查看实时频率」，只读监控每核真实频率。",
       "action_risk": {"view": "info", "set": "low"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "set", "label": "切换调频策略（性能/均衡/省电，可锁频）"},
               {"value": "view", "label": "查看实时频率（只读监控，每秒刷新）"},
           ]},
           {"name": "governor", "label": "调频模式", "type": "select",
            # 「查看实时频率」时本行被 show_if 隐藏、不提交值，须声明非必填，否则后端报「缺少参数」
            "required": False,
            "show_if": {"param": "action", "value": "set"},
            "options": [
               {"value": "performance", "label": "性能优先（performance，允许睿频至最高频率）"},
               {"value": "schedutil", "label": "均衡（schedutil，由内核按负载动态调频）"},
               {"value": "powersave", "label": "省电（powersave，频率保持最低，降低功耗与温度）"},
               {"value": "conservative", "label": "平滑省电（conservative，按负载缓慢升降频）"},
           ]},
           {"name": "lock_freq", "label": "满载锁频", "type": "checkbox", "value": "1",
            "required": False,
            "show_if": {"param": "action", "value": "set"},
            "placeholder": "锁定所有核心在最高频率（仅对性能优先模式生效，耗电与发热最高）"},
           {"name": "secs", "label": "监控秒数", "type": "number", "min": 0, "max": 120,
            "required": False,
            "show_if": {"param": "action", "value": "view"},
            "placeholder": "连续刷新秒数（默认 15）；填 0 只打印一次当前快照"},
       ],
       "script_file": "scripts/cpu_freq.sh",
   },

{
       "id": "time_sync", "category": "optimize", "risk": "low", "order": 6,
       "name": "时间同步",
       "desc": "查看时间与同步状态；NTP 校时（可选国内源）、主板时钟（RTC）校准、修改时区。",
       "action_risk": {"status": "info", "sync": "low", "rtc": "low", "tz": "low"},
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "status", "label": "查看时间与同步状态"},
               {"value": "sync", "label": "开启 NTP 并立即同步"},
               {"value": "rtc", "label": "主板时钟（RTC）校准"},
               {"value": "tz", "label": "修改时区"}]},
           {"name": "ntp_server", "label": "NTP 服务器", "type": "select", "required": False,
            "options": [
               {"value": "ntp.aliyun.com", "label": "阿里云（国内推荐）"},
               {"value": "ntp.tencent.com", "label": "腾讯云"},
               {"value": "ntp.ntsc.ac.cn", "label": "国家授时中心"},
               {"value": "pool.ntp.org", "label": "国际公共池"}]},
           {"name": "tz", "label": "时区", "type": "select", "required": False,
            "pattern": r"^UTC$|^[A-Za-z_]+/[A-Za-z_+-]+$",
            "options": [
               {"value": "Asia/Shanghai", "label": "Asia/Shanghai（中国标准时间 UTC+8）"},
               {"value": "Asia/Hong_Kong", "label": "Asia/Hong_Kong（UTC+8）"},
               {"value": "Asia/Tokyo", "label": "Asia/Tokyo（UTC+9）"},
               {"value": "UTC", "label": "UTC（协调世界时）"}]},
       ],
       "script_file": "scripts/time_sync.sh",
   },

{
       "id": "reset_password", "category": "optimize", "risk": "high", "order": 9,
       "name": "重置 PVE 密码",
       "desc": "重置 PVE WebUI 登录密码（root 用户，支持 PAM 与内置认证）。",
       "params": [
           {"name": "password", "label": "新密码", "type": "password",
            "placeholder": "请输入新密码（至少 6 位）"},
       ],
       "script_file": "scripts/reset_password.sh",
   },

{
       "id": "pve8to9", "category": "optimize", "risk": "high", "order": 10,
       "name": "PVE 8.x 升级到 9.x",
       "recommend": [{"tool": "grub_mgr",
                      "reason": "重大版本升级风险高，建议先备份 GRUB 配置；升级前请自行确认关键数据已备份"}],
       "desc": "将 PVE 8.x 升级到 9.x（自动完成检测、备份、换源、dist-upgrade、验证与重启），升级前请确认备份可用。",
       "params": [],
       "script_file": "scripts/pve8to9.sh",
   },

{
       "id": "root_resize", "category": "optimize", "risk": "high", "order": 11,
       "name": "PVE 根分区扩容",
       "desc": "删除 swap 扩容根分区，或删除 local-lvm 并把空间并入 local（均不可逆）。",
       "params": [
           {"name": "action", "label": "操作", "type": "select", "options": [
               {"value": "del_swap", "label": "删除 Swap 并扩容根分区（不可逆）"},
               {"value": "merge_local", "label": "删除 local-lvm 并入 local（不可逆）"},
           ]},
       ],
       "script_file": "scripts/root_resize.sh",
   }
]
