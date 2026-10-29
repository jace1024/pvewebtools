"""工具箱注册表：PVE 常用运维操作合集。

每个工具是一条数据记录：
 id / category / name / desc      基本信息
 risk    info=只读  low=普通操作(一次确认)  high=高危(需输入确认词)
 params  参数定义，服务端按 type/pattern/options 校验后经 PT_ 环境变量注入
 script  固定 bash 片段（用户输入不拼接进脚本，只走环境变量，避免注入）

写配置前均先备份，块状配置用 "PVEWEBTOOLS BEGIN/END 标记" 幂等管理。
"""

import re

import json

import shlex

import os

import importlib.util
import importlib.resources as ires

from pathlib import Path

_env_ed = os.environ.get("PVE_WEBTOOLS_EDITION")

if _env_ed:
   EDITION = _env_ed.lower()
else:
   EDITION = "pro" if importlib.util.find_spec("core.tools.toolbox_pro") else "lite"

try:
   from edition import EDITION as _BUILD_EDITION
except ImportError:
   _BUILD_EDITION = ""

from core.tools.toolbox_common import COMMON_HELPERS, MIRRORS, GENTOO_MIRRORS

CATEGORIES_BASE = [
   {"id": "optimize", "name": "日常优化", "icon": "⚡"},
   {"id": "kernel", "name": "启动与内核", "icon": "🧬"},
   {"id": "passthrough", "name": "硬件直通", "icon": "🎮"},
   {"id": "vmops", "name": "虚机/容器运维", "icon": "🖥"},
   {"id": "network", "name": "网络工具", "icon": "🌐"},
   {"id": "storage", "name": "存储与磁盘", "icon": "💾"},
   {"id": "rescue", "name": "诊断与救砖", "icon": "🚑"},
]

CATEGORY_PRO = {"id": "advanced", "name": "高级玩法", "icon": "🚀"}

CATEGORIES = CATEGORIES_BASE + ([CATEGORY_PRO] if EDITION == "pro" else [])

def _resolve_script(tool):
    """外置脚本解析：声明了 script_file 的工具，从 core/tools/scripts/{id}.sh 读取正文
    覆盖 script（源码模式读物理文件；Nuitka 数据文件经 importlib.resources 读取）。
    内联 script 作为回退，兼容尚未外置的工具。"""
    sf = tool.get("script_file")
    if sf:
        phys = Path(__file__).with_suffix("").parent / sf
        if phys.is_file():
            return phys.read_text(encoding="utf-8")
        try:
            return (ires.files("core.tools.scripts") / Path(sf).name).read_text(encoding="utf-8")
        except Exception:
            pass
    return tool.get("script")


# 基础分类模块（顺序 = 前端分类顺序）。新增分类文件（tools_xxx.py，声明 TOOLS）
# 只需在此追加一个模块名即可并入——无需再写 N 行 import + 1 行拼合，落实
# 「新增工具只需写脚本 + 注册一条」：在对应文件 TOOLS 追加一条；新建分类则新增文件 + 此处加一名。
_BASE_MODULES = [
    "tools_optimize", "tools_kernel", "tools_passthrough",
    "tools_vmops", "tools_network", "tools_storage", "tools_rescue",
]

TOOLS_BASE = []
for _mod_name in _BASE_MODULES:
    _mod = importlib.import_module("core.tools." + _mod_name)
    TOOLS_BASE.extend(getattr(_mod, "TOOLS", []))

try:
   from core.tools.toolbox_pro import TOOLS_PRO
except ImportError:
   TOOLS_PRO = []

try:
   from core.tools.toolbox_dev import TOOLS_DEV
except ImportError:
   TOOLS_DEV = []

CATEGORY_DEV = {"id": "dev", "name": "开发版", "icon": "🧪"}

if TOOLS_DEV:
   CATEGORIES = CATEGORIES + [CATEGORY_DEV]

TOOLS = TOOLS_BASE + (TOOLS_PRO if EDITION == "pro" else []) + TOOLS_DEV

# 兜底：工具声明了、但分类表里没有的分类要补上，否则这些工具在前端无处可去会静默消失
# （前端严格按 categories 分组渲染）。典型场景：开发版工具挂到「高级玩法」下，而当前以
# lite 形态运行（EDITION != pro 时 CATEGORY_PRO 不进表）。
for _cat in (CATEGORY_PRO, CATEGORY_DEV):
   if _cat not in CATEGORIES and any(t["category"] == _cat["id"] for t in TOOLS):
      CATEGORIES = CATEGORIES + [_cat]

# 外置脚本：声明了 script_file 的工具，用 core/tools/scripts/{id}.sh 正文覆盖 script
for _t in TOOLS:
    if _t.get("script_file"):
        _t["script"] = _resolve_script(_t)

# 统一工具注册表：所有工具声明汇入此处（按 id 索引）。各 tools_*.py 只负责声明数据，
# 由本处汇总成 REGISTRY；get_tool / list_tools / build_* 均以 REGISTRY 为唯一来源。
REGISTRY = {t["id"]: t for t in TOOLS}
_TOOL_INDEX = REGISTRY


# ---- 参数「节点相关性」分类表（单一来源；前端只读 list_tools() 注入的标记）----
# 弹窗里的「执行节点」决定任务在哪台机器上跑，各参数的可选值与节点的关系只有四类：
#
#   node        值来自**执行节点**（硬件/网络/存储/内核/备份…）：请求要带 ?node=，切换节点要重拉
#   guest       值全局唯一（VMID/CTID），但操作它的 qm/pct 只能在**该虚机所属节点**上跑
#               → 选项按执行节点过滤 + 服务端复核归属（否则"看着成功、其实没做"）
#   guest_node  值跟着**所选虚机所在节点**（虚机磁盘、mdev 规格），不是执行节点
#   global      全局/共享资源（云镜像、模板、物料 ROM）：任何节点等价 → 不联动
#
# 为什么要有这张表：这些规则原先散落在「前端各分支手写 ?node=」和「工具名白名单」里，
# 改一处忘一处就会漏（真实踩坑：guests_all 忘了按节点过滤 → 跨节点操作静默失败；
# lxc_storages 忘了带节点 → 在别的节点上选出不存在的存储）。现在统一在此登记、
# 由 list_tools() 打标下发、前端按标记处理；新增数据源若忘了登记，
# scripts/lint_tool_sources.py 会直接报出来，而不是静默退化成"不联动"。
NODE_SCOPED_SOURCES = {
    "backup_storages", "backups", "bridges", "device", "disk_storages", "disks", "gpu",
    "grub_backups", "image_storages", "kernels", "kernels_available", "lxc_storages",
    "net_backups", "nic", "pci_generic", "phys_nics", "usb", "vfio_devices", "vlans",
}
GUEST_OWNED_SOURCES = {"guests", "guests_all"}
GUEST_NODE_SOURCES = {"vm_disks", "vgpu_gpus", "vgpu_types"}
NODE_AGNOSTIC_SOURCES = {
    "cloud_images", "deploy_sources", "disk_upload", "lxc_templates", "vbios_roms",
    "vgpu_drivers", "vgpu_patches", "vgpu_unlock_libs",
    # community_scripts：社区脚本(community-scripts)可安装应用清单，读本机仓库内容
    # （默认 <pvewebtools 家目录>/community-scripts），与执行节点无关；
    # 数据源见 server.py /api/community-scripts/list
    "community_scripts",
    # nodes：目标节点下拉（迁移用）。集群成员表与「谁在执行」无关，任何节点看到的都一致，
    # 因此不带 ?node=、也不按执行节点过滤；数据源见 server.py /api/nodes
    "nodes",
    # docker_versions：Docker 引擎稳定版清单（「虚机内装 Docker」版本下拉）。抓 docker-ce
    # 仓库 static/stable 目录（服务端缓存 6h），与执行节点/虚机发行版无关；
    # 数据源见 server.py /api/docker-versions
    "docker_versions",
}

_SOURCE_SCOPE = {
    **{s: "node" for s in NODE_SCOPED_SOURCES},
    **{s: "guest" for s in GUEST_OWNED_SOURCES},
    **{s: "guest_node" for s in GUEST_NODE_SOURCES},
    **{s: "global" for s in NODE_AGNOSTIC_SOURCES},
}

# 分类 -> 注入到参数上的标记名（前端据此决定是否带 ?node= / 是否按节点过滤虚机）
_SCOPE_MARKERS = {
    "node": "node_scoped",
    "guest": "belongs_to_node",
    "guest_node": "guest_node",
    "global": "node_agnostic",
}


def param_node_scope(param):
    """参数的数据源分类：node / guest / guest_node / global；未登记返回 ""（未知）。"""
    return _SOURCE_SCOPE.get((param or {}).get("options_from") or "", "")


def unknown_option_sources(tools=None):
    """列出「用到了但没登记分类」的 options_from（供 scripts/lint_tool_sources.py 与启动自检）。"""
    seen = set()
    for t in (tools if tools is not None else TOOLS):
        for p in t.get("params") or []:
            src = p.get("options_from")
            if src and not param_node_scope(p):
                seen.add(src)
    return sorted(seen)


def _decorate_params(params):
    """给参数补上节点相关性标记；返回浅拷贝，避免污染注册表原始定义。"""
    out = []
    for p in params or []:
        scope = param_node_scope(p)
        item = dict(p)
        if scope:
            item[_SCOPE_MARKERS[scope]] = True
        out.append(item)
    return out


_unknown_src_warned = False


def warn_unknown_option_sources():
    """启动自检：有未登记的数据源就打一行警告（只打一次），提示去补分类表。

    不抛异常：生产环境不能因为少登记一个数据源就起不来；但必须留下痕迹，
    否则它会静默退化成"不联动"，重新变成难查的界面问题。"""
    global _unknown_src_warned
    if _unknown_src_warned:
        return
    _unknown_src_warned = True
    unknown = unknown_option_sources()
    if unknown:
        import sys
        print("[警告] 以下下拉数据源未登记节点相关性分类（将按「不联动」处理，"
              "请补进 core/tools/toolbox.py 的分类表）: " + ", ".join(unknown),
              file=sys.stderr)


class ToolError(Exception):
   """参数校验或工具查找失败，消息直接返回前端"""

def get_tool(tool_id):
   tool = _TOOL_INDEX.get(tool_id)
   if not tool:
       raise ToolError(f"工具不存在: {tool_id}")
   return tool

def list_tools():
   """返回前端所需的工具清单（不含脚本内容）。

   参数会补上节点相关性标记（node_scoped / belongs_to_node / guest_node / node_agnostic），
   前端据此统一决定"是否带 ?node=、是否按执行节点过滤虚机、切换节点时是否重拉"——
   规则集中在分类表里，不在前端各分支手写。"""
   warn_unknown_option_sources()
   out = []
   for t in TOOLS:
       item = {k: t[k] for k in ("id", "category", "name", "desc", "risk", "params", "order")}
       item["params"] = _decorate_params(t.get("params") or [])
       if t.get("action_risk"):
           item["action_risk"] = t["action_risk"]  # 风险按操作区分（如只读操作免确认词）
       if t.get("recommend"):
           item["recommend"] = t["recommend"]  # 前置工具建议（前端提示用）
       if t.get("recommended"):
           item["recommended"] = True  # 分类推荐入口（前端打「推荐」标签）
       if t.get("tag"):
           item["tag"] = t["tag"]  # 自定义标签文案（如「必做」，替代默认「推荐」）
       if t.get("hidden"):
           item["hidden"] = t["hidden"]  # 隐藏底层工具（如云镜像的 Linux/Windows 子工具），仅合成入口可见
       if t.get("disabled"):
           item["disabled"] = True  # 占位卡片（如「高级玩法 → 即将开放」）：前端置灰、点击只提示，不可执行
       out.append(item)
   return {"categories": CATEGORIES, "tools": out, "edition": _BUILD_EDITION}

def validate_params(tool, raw_params):
   """逐项校验用户参数，全部通过才返回干净的参数字典（早返回抛错）"""
   clean = {}
   raw_params = raw_params or {}
   for p in tool["params"]:
      val = str(raw_params.get(p["name"], "") or "").strip()
      if not val:
         if p.get("required", True):
            raise ToolError(f"缺少参数: {p['label']}")
         clean[p["name"]] = ""
         continue
      if p["type"] == "select":
         if p.get("options_from"):
            # 动态下拉（虚机/磁盘列表由接口提供）：选项集运行时才知，改用 pattern 校验
            pattern = p.get("pattern")
            if pattern and not re.fullmatch(pattern, val):
               raise ToolError(f"参数 {p['label']} 格式不正确：{p.get('hint', '请检查输入')}")
         else:
            allowed = {o["value"] for o in p["options"]}
            if val not in allowed:
               raise ToolError(f"参数 {p['label']} 取值非法")
      elif p["type"] in ("text", "password", "autotext", "textarea"):
         # autotext：下拉默认「自动值」时提交空串（required=False 即放行），
         # 切到「自定义」后是普通文本框，按普通 text 校验 pattern；
         # textarea 同 text：有 pattern 即校验，防止自由文本拼进脚本被执行
         pattern = p.get("pattern")
         if pattern and not re.fullmatch(pattern, val):
            raise ToolError(f"参数 {p['label']} 格式不正确：{p.get('hint', '请检查输入')}")
      elif p["type"] == "time":
         # 时间选择器提交 HH:MM（24 小时制）
         if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", val):
            raise ToolError(f"参数 {p['label']} 格式不正确：需为 HH:MM 24 小时制")
      elif p["type"] == "checkbox":
         if p.get("multiple"):
            # 多选 checkbox-group：前端把勾选项合并为逗号分隔字符串提交；
            # 有 pattern 时按整体格式校验，无 pattern 时直接放行（动态选项无法逐项校验）
            if val:
               pattern = p.get("pattern")
               if pattern and not re.fullmatch(pattern, val):
                  raise ToolError(f"参数 {p['label']} 格式不正确：{p.get('hint', '请检查输入')}")
         else:
            # 单选开关：勾选时提交 value，未勾选提交空
            if val not in ("", p.get("value", "1")):
               raise ToolError(f"参数 {p['label']} 取值非法")
      elif p["type"] == "number":
         # 整数类型，支持 min/max 范围约束（缺省不限制）
         try:
            n = int(val)
         except ValueError:
            raise ToolError(f"参数 {p['label']} 必须为整数")
         lo = p.get("min")
         hi = p.get("max")
         if lo is not None:
            lo = int(lo)
            if n < lo:
               raise ToolError(f"参数 {p['label']} 不能小于 {lo}")
         if hi is not None:
            hi = int(hi)
            if n > hi:
               raise ToolError(f"参数 {p['label']} 不能大于 {hi}")
      elif p["type"] == "port":
         # 端口号：1-65535，强制整数范围
         if not re.fullmatch(r"\d+", val):
            raise ToolError(f"参数 {p['label']} 必须为端口号")
         n = int(val)
         if not (1 <= n <= 65535):
            raise ToolError(f"参数 {p['label']} 端口号需在 1-65535 之间")
      clean[p["name"]] = val
   return clean

def build_env(tool, clean_params):
   """组装注入脚本的环境变量；换源工具额外展开镜像地址"""
   env = dict(clean_params)
   if tool["id"] in ("change_sources", "quick_setup"):
       # quick_setup 自身无参数，镜像/安全源写在 compose 的 change_sources 条目里。
       # 必须从 compose 解析出这些声明，否则此处读不到，
       # 会导致 compose 声明与实际展开不一致（声明 official 却用了镜像源）。
       if tool["id"] == "quick_setup":
           for entry in tool.get("compose") or []:
               tid, _, kv = entry.partition(":")
               if tid != "change_sources" or not kv:
                   continue
               declared = dict(p.split("=", 1) for p in kv.split(",") if "=" in p)
               for key in ("mirror", "security"):
                   if key in declared:
                       env[key] = declared[key]
       # auto：不在服务端写死镜像，而是把镜像表传给脚本，由各节点实测挑最快
       if env.get("mirror") == "auto":
           env["m_auto"] = "1"
           env["m_table"] = json.dumps(MIRRORS, ensure_ascii=False)
           # 兜底：测速失败时退回中科大（debian/security 可用）；PVE/Ceph/LXC 仓回退官方源，
           # 与「手动选源」语义一致（手动模式也是 mirror[xxx] or official[xxx]），避免写出空地址
           fallback = MIRRORS["ustc"]; official = MIRRORS["official"]
           env["m_debian"] = fallback["debian"]
           env["m_pve"] = official["pve"]
           env["m_ceph"] = official["ceph"]
           env["m_ct"] = official["ct"]
           env["m_security"] = MIRRORS["official"]["security"] if env.get("security") == "official" else fallback["security"]
       else:
           mirror = MIRRORS[env.get("mirror", "ustc")]
           official = MIRRORS["official"]
           env["m_debian"] = mirror["debian"]
           # 镜像站未承载 Proxmox 仓库时降级官方源，避免写入空地址
           env["m_pve"] = mirror["pve"] or official["pve"]
           env["m_ceph"] = mirror["ceph"] or official["ceph"]
           env["m_ct"] = mirror["ct"] or official["ct"]
           # 安全源可独立选择官方
           if env.get("security") == "official":
               env["m_security"] = MIRRORS["official"]["security"]
           else:
               env["m_security"] = mirror["security"]
   # 社区脚本安装：把镜像候选表传给脚本，由安装流程在目标节点实测挑最快再注入容器。
   # apt_table 供 Debian/Ubuntu 等应用挑最快 apt 源；gentoo_table 供 gentoo 等源码编译型系统。
   if tool["id"] == "community_scripts":
       env["apt_table"] = json.dumps(MIRRORS, ensure_ascii=False)
       env["gentoo_table"] = json.dumps(GENTOO_MIRRORS, ensure_ascii=False)
   return env

def build_script(tool):
   """取工具脚本；compose 类工具按序拼接子工具脚本。

   compose 条目格式 "tool_id" 或 "tool_id:k=v,k2=v2"（内嵌子工具参数，
   经 shlex.quote 后以 export PT_ 变量注入，组合脚本同进程执行）。
   所有脚本统一注入 COMMON_HELPERS（备份/块管理/run 失败报错），已含则不重复。
   """
   def _with_helpers(script):
       return script if script.startswith(COMMON_HELPERS) else COMMON_HELPERS + script

   def _strip_helpers(script):
       """剥掉子脚本自带的 COMMON_HELPERS，改由外层统一前置一份。

       helpers 约 63KB，若不剥离会随子步骤数量重复内嵌（一键优化 5 步 → 脚本
       从约 70KB 膨胀到 400KB+），既拖慢 bash 解析，也更容易撑爆管道缓冲。
       helpers 里只有函数定义和少量全局设置，父 shell 定义后子 shell 天然继承
       （compose 的每个子步骤都在 ( ) 子 shell 中执行），语义不变。
       """
       return script[len(COMMON_HELPERS):] if script.startswith(COMMON_HELPERS) else script

   if tool.get("script") is not None:
       return _with_helpers(tool["script"])
   if tool.get("compose"):
       seq = []
       for entry in tool["compose"]:
           tid, _, kv = entry.partition(":")
           child = get_tool(tid)
           params = dict(p.split("=", 1) for p in kv.split(",") if "=" in p) if kv else {}
           seq.append((child["name"], _strip_helpers(child["script"]), params))
       parts = []
       for i, (name, script, params) in enumerate(seq, 1):
           parts.append(f'echo; echo "===== [{i}/{len(seq)}] {name} ====="')
           if params:
               exports = " ".join("PT_%s=%s" % (k.upper(), shlex.quote(v))
                                  for k, v in params.items())
               parts.append("export " + exports)
           # 子脚本在子 shell 中执行，exit 只退出子 shell，不影响后续步骤
           parts.append(f"( {script} ) || echo \"[警告] {name} 执行失败，继续下一步\"")
       return _with_helpers("\n".join(parts))
   raise ToolError(f"工具 {tool['id']} 缺少脚本定义")
