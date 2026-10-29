"""工具箱共享常量：所有工具脚本共用的 bash 辅助函数与镜像源表。

拆自原 toolbox.py，供 tools_*.py 各分类模块 import。
"""
import json
from pathlib import Path

from core import paths

# 「调整虚机内核参数」工具与「显卡直通 → 为虚机适配内核参数」共用的常用参数清单。
# 单一来源：两处下拉都由此生成，避免同一条目改一处漏一处。
KARGS_PRESETS = [
    {"value": "nomodeset", "label": "nomodeset：不初始化显示（直通 / 黑屏通用）"},
    {"value": "nomodeset i915.enable_guc=0", "label": "Intel 核显直通：防卡死（QSV 仍可用）"},
    {"value": "amdgpu.runpm=0", "label": "AMD 显卡/核显直通：防掉卡"},
    {"value": "pci=noaer pcie_aspm=off", "label": "直通设备报错 / 不稳时用"},
    {"value": "loglevel=7 initcall_debug", "label": "排查开机问题：打印详细日志"},
    {"value": "console=ttyS0,115200 earlyprintk=serial,ttyS0,115200", "label": "黑屏排查：日志输出到串口（需串口线）"},
    {"value": "mitigations=off", "label": "性能：关 CPU 漏洞缓解（安全性下降）"},
    {"value": "net.ifnames=0", "label": "网卡名固定为 eth0（老脚本需要）"},
]

# 换源可用性表内置默认（镜像×发行版）；与 config/cloud-image-presets.json 的 mirror_repo_support 字段、
# web/server.py 的 DEFAULT_MIRROR_REPO_SUPPORT 三处保持一致。腾讯云镜像暂未确认收录，保守排除。
DEFAULT_MIRROR_REPO_SUPPORT = {
    "anolis": ["aliyun", "huawei", "ustc", "tsinghua"],
    "openeuler": ["aliyun", "huawei", "ustc", "tsinghua"],
}


def load_mirror_repo_support():
    """读取换源可用性表（镜像×发行版）；合并持久覆盖目录后的结果，缺省回退内置默认。

    与 web/server.py 的 _merged_preset_config 一致：随包资源目录为基，覆盖目录（见 core.paths.preset_override_dir）
    的同名文件逐键叠加。运维改 JSON 即可调整，无需动脚本。
    """
    base = dict(DEFAULT_MIRROR_REPO_SUPPORT)
    try:
        candidates = [Path(paths.RESOURCE_DIR) / "config" / "cloud-image-presets.json",
                      paths.preset_override_dir() / "cloud-image-presets.json"]
        for p in candidates:
            if not p.is_file():
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(d, dict) and isinstance(d.get("mirror_repo_support"), dict):
                base = d["mirror_repo_support"]
            elif isinstance(d, list):  # 旧版纯数组文件：无可用性表，回退默认
                base = dict(DEFAULT_MIRROR_REPO_SUPPORT)
    except Exception:  # noqa: BLE001 路径解析等异常一律回退默认，不致命
        pass
    return base

COMMON_HELPERS = r"""
# 统一使用 C locale，确保 df / lspci / dmidecode / sensors 等文本解析不被系统语言影响
# （非英文系统下数字千分位、小数点等会导致解析失败）。放在所有助手函数之前。
export LC_ALL=C LANG=C LANGUAGE=C
set -E -o pipefail
# 兜底报错：任何未被 run 包裹的命令失败时，也记录退出码/行号/完整命令行，便于排查
# 注意用 $LINENO：trap 内 $BASH_LINENO 会多出 trap 自身栈帧而显示 0
_err_trap() {
  # 先存退出码：local 自身会重置 $?，放在后面取值会恒为 0（日志里的退出码就没意义了）
  local rc=$? c="$BASH_COMMAND" fn="${FUNCNAME[1]:-main}" call_line="${BASH_LINENO[0]:-?}"
  # 行号说明：trap 里的 $LINENO 展开的是 trap 自己那一行——任何失败都报同一行，没有信息量
  # （现场日志里就是每条都显示「行 17」）。真正有用的是出错命令文本 + 所在函数 + 调用处行。
  # set -E(errtrace) 会让 ERR trap 渗入命令替换内部：形如 $(xxx | true) 或 $(xxx || true)
  # 的良性、已被接住的失败会被误报为 true/false 等空操作，从而空打“命令执行失败”。
  # 这里跳过永不可能失败的空操作与显式 || true 接住的命令，避免出现误导性错误。
  case "$c" in
    true|false|:|""|*"|| true"*|*"|| :"*) return 0 ;;
  esac
  # 打到 stderr：jobs.py 用 stderr=STDOUT 合并捕获，日志照常显示；
  # 关键是 stdout 必须纯净，否则会被 $( ) 命令替换当成返回值污染（如迁移测速）
  echo "[错误] 命令执行失败 (退出码 $rc, 函数 $fn, 调用处行 $call_line): $c" >&2
}
trap '_err_trap' ERR
BACKUP_DIR=/etc/pvewebtools/backup
# 旧版目录一次性迁移（改名前备份不丢失，已迁移则跳过）
if [ -d /etc/pvewebtools9 ]; then
 mkdir -p /etc/pvewebtools
 mv /etc/pvewebtools9/* /etc/pvewebtools/ 2>/dev/null || true
 rmdir /etc/pvewebtools9 2>/dev/null || true
fi
backup_file() {
 [ -f "$1" ] || return 0
 mkdir -p "$BACKUP_DIR"
 cp -a "$1" "$BACKUP_DIR/$(basename "$1").$(date +%Y%m%d_%H%M%S).bak"
 echo "[备份] $1 -> $BACKUP_DIR"
}
remove_block() {  # $1=file $2=marker；文件不存在时视为无事可做，返回 0 避免误触发 ERR trap
 [ -f "$1" ] || return 0
 sed -i "/# PVEWEBTOOLS BEGIN $2/,/# PVEWEBTOOLS END $2/d" "$1"
}
apply_block() {   # $1=file $2=marker $3=content
 backup_file "$1"; remove_block "$1" "$2"
 { echo "# PVEWEBTOOLS BEGIN $2"; echo "$3"; echo "# PVEWEBTOOLS END $2"; } >> "$1"
 echo "[写入] $1 [$2]"
}
# ── 任务级临时目录（强杀/崩溃后由 jobs.py 按 PT_JOB_ID 统一清理）─────────────────────
# 所有工具脚本的临时文件/目录都应落在这个根下：执行日志页「强制结束」或服务重启后，
# Python 侧按 PT_JOB_ID 精确删除该任务遗留的临时文件，不留孤儿。
# 未带 PT_JOB_ID（如手工跑脚本调试）时统一退化为 ...-manual 目录。
pw_job_tmp_root() {
  printf '%s' "/var/tmp/pvewebtools-job-${PT_JOB_ID:-manual}"
}
# 在任务临时根下建目录，stdout 输出其路径；失败返回非零（调用处自行判断并报错）
pw_job_mktemp_dir() {
  local root
  root=$(pw_job_tmp_root) || return 1
  mkdir -p "$root" 2>/dev/null || { echo "[错误] 创建任务临时目录失败: $root" >&2; return 1; }
  mktemp -d "$root/XXXXXXXX" 2>/dev/null || { echo "[错误] mktemp 失败" >&2; return 1; }
}
# 在任务临时根下建临时文件，stdout 输出其路径；失败返回非零
pw_job_mktemp_file() {
  local root
  root=$(pw_job_tmp_root) || return 1
  mkdir -p "$root" 2>/dev/null || { echo "[错误] 创建任务临时目录失败: $root" >&2; return 1; }
  mktemp "$root/XXXXXXXX" 2>/dev/null || { echo "[错误] mktemp 失败" >&2; return 1; }
}
# ── 内核启动参数读写（GRUB / systemd-boot 自适应，供 IOMMU/直通/vGPU 等共用）─────────
# 关键：PVE 8/9 用 ZFS 安装时默认 systemd-boot，参数在 /etc/kernel/cmdline + proxmox-boot-tool
# refresh；此时改 /etc/default/grub 完全无效——这是 IOMMU/直通/vGPU 最常见的失败原因。
# 凡是要改宿主机内核启动参数的工具，都应复用这组函数，不要各自只写 GRUB。

# 读配置文件里的内核启动参数（不是 /proc/cmdline 的运行时参数，供幂等判断用）
boot_param_read() {
  if [ -f /etc/kernel/cmdline ]; then
    tr '\n' ' ' < /etc/kernel/cmdline 2>/dev/null || true
  else
    { grep '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub 2>/dev/null | head -1 | sed 's/^[^=]*=//; s/"//g'; } || true
  fi
  return 0
}

# 当前生效的引导方式名（日志/提示用）
boot_param_mode() {
  if [ -f /etc/kernel/cmdline ]; then printf '%s' "systemd-boot（/etc/kernel/cmdline）"
  else printf '%s' "GRUB（/etc/default/grub）"; fi
}

# 把完整参数串落盘并刷新引导：systemd-boot 直接写文件 + refresh；GRUB 改写该行 + update-grub
boot_param_write() {   # $1=完整参数串
  local new="$1" f
  if [ -f /etc/kernel/cmdline ]; then
    backup_file /etc/kernel/cmdline
    printf '%s\n' "$new" > /etc/kernel/cmdline
    if command -v proxmox-boot-tool >/dev/null 2>&1; then
      run "刷新 EFI 引导项失败（proxmox-boot-tool refresh）" proxmox-boot-tool refresh
    fi
  else
    f=/etc/default/grub
    [ -f "$f" ] || { echo "[错误] 找不到 $f（且无 /etc/kernel/cmdline），无法修改内核启动参数"; return 1; }
    backup_file "$f"
    # 只替换引号内的值，保留行尾注释；无引号写法或整行缺失时兜底处理
    if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT="' "$f"; then
      sed -i "s|^\(GRUB_CMDLINE_LINUX_DEFAULT=\)\"[^\"]*\"|\1\"$new\"|" "$f"
    elif grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$f"; then
      sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"$new\"|" "$f"
    else
      printf 'GRUB_CMDLINE_LINUX_DEFAULT="%s"\n' "$new" >> "$f"
    fi
    run "更新 GRUB 失败（update-grub）" update-grub
  fi
}

# 幂等追加内核启动参数（已存在的项自动跳过）
boot_param_add() {   # $1=要追加的参数（空格分隔）
  local want="$1" cur add a
  [ -n "$want" ] || return 0
  cur=$(boot_param_read)
  add=""
  for a in $want; do case " $cur " in *" $a "*) ;; *) add="$add $a";; esac; done
  if [ -z "$add" ]; then echo "[跳过] $(boot_param_mode) 已含: $want"; return 0; fi
  echo "[写入] $(boot_param_mode) 追加:$add"
  boot_param_write "$(printf '%s%s' "$(printf '%s' "$cur" | sed 's/[[:space:]]*$//')" "$add")"
}

# 幂等移除内核启动参数：按「键名」删（iommu=pt 由键 iommu 命中；iommu.passthrough=1 用键 iommu.passthrough）
boot_param_del() {   # $1..=要删除的键名
  local keys=" $* " cur new t k
  [ "$#" -gt 0 ] || return 0
  cur=$(boot_param_read)
  new=""
  for t in $cur; do
    k="${t%%=*}"
    case "$keys" in
      *" $k "*) ;;                    # 命中要删的键 → 丢弃该项
      *) new="$new $t" ;;
    esac
  done
  new=$(printf '%s' "$new" | sed 's/^ *//; s/ *$//; s/  */ /g')
  if [ "$new" = "$(printf '%s' "$cur" | sed 's/^ *//; s/ *$//; s/  */ /g')" ]; then
    echo "[跳过] $(boot_param_mode) 不含: $*"; return 0
  fi
  echo "[写入] $(boot_param_mode) 移除:$*"
  boot_param_write "$new"
}
run() {  # 执行命令并透输出；失败时打印退出码与具体命令行后退出。用法: run "失败提示" 命令 [参数...]
 local hint="$1"; shift
 local cmd_str="" a rc
 for a in "$@"; do cmd_str="${cmd_str:+$cmd_str }$(printf '%q' "$a")"; done
 if "$@"; then :; else
  rc=$?
  echo "[错误] 命令执行失败 (退出码 $rc): $cmd_str"
  [ -n "$hint" ] && echo "[提示] $hint"
  # 失败后再归因：若 /etc/pve 当前不可写（多为集群失去仲裁），补一句指引，
  # 避免用户把仲裁问题误判成镜像/参数/存储问题（仅只读时打印，无关失败静默返回）
  pw_quorum_hint_if_readonly || true
  exit 1
  fi
  }
# ── DNS 列表规范化（pct/qm --nameserver 专用）─────────────────────────────
# 用户输入允许逗号/分号/空格混写（参数 pattern 是 ^[\d.,\s]+$），这里统一成「空格分隔」的地址串。
# 同时剔掉控制字符（\r、\n 等）：PVE 9.2 起对值里的换行/回车更严格（pve-common 9.2.2
# 「schema: improve handling of (trailing) newlines in values」、pve-container 6.1.14
# 「reject line feeds and carriage returns in every rendered config value」），带 CR 的地址
# 会被判成非法 IP 直接 400。
# 注意不能简单 `tr -d ' '`：那会把空格分隔的多个地址粘成一个（8.8.8.8 1.1.1.1 → 8.8.8.81.1.1.1），
# 正确做法是「先删控制字符，再逗号→空格，最后压缩空白并去首尾」。
# 用法： DNS=$(pw_dns_list "$PT_DNS")
pw_dns_list() {
 printf '%s' "${1:-}" | tr -d '\000-\037\177' | tr ',;' ' ' | tr -s '[:space:]' ' ' | sed 's/^ *//; s/ *$//'
}

# 取规范化后的首个地址（用于 API 兜底写入：单个 IP 总是合法值）
pw_dns_first() {
 pw_dns_list "${1:-}" | awk '{print $1}'
}

# 给定 IPv4 CIDR（如 192.168.1.10/24）与偏移量，返回末段递增后的 CIDR（仅增最后一段）。
# 用于批量建机静态 IP 自动分配：第 i 台 = 基址末段 + (i-1)。非 IPv4/CIDR、或递增后超出
# 1-254 范围均返回错误（调用方应中断），避免生成 .0/.255 或跨段的非法地址。
pw_inc_ip() {
 local cidr="$1" off="${2:-0}" ip mask prefix last newlast
 [ -n "$cidr" ] || { echo "[错误] pw_inc_ip: 空地址" >&2; return 1; }
 case "$cidr" in
   *:*) echo "[错误] pw_inc_ip: 暂仅支持 IPv4（${cidr}）" >&2; return 1 ;;
 esac
 ip="${cidr%/*}"; mask="${cidr#*/}"
 case "$ip" in
   [0-9]*.[0-9]*.[0-9]*.[0-9]) ;;
   *) echo "[错误] pw_inc_ip: 非法 IPv4 地址（${cidr}）" >&2; return 1 ;;
 esac
 last="${ip##*.}"
 case "$last" in
   *[!0-9]*) echo "[错误] pw_inc_ip: 非法末段（${last}）" >&2; return 1 ;;
 esac
 prefix="${ip%.*}"
 newlast=$((last + off))
 if [ "$newlast" -lt 1 ] || [ "$newlast" -gt 254 ]; then
   echo "[错误] 批量静态 IP 递增后超出 1-254 范围（${prefix}.${newlast}）" >&2; return 1
 fi
 echo "${prefix}.${newlast}/${mask}"
}

# 给已存在的 guest 设置 DNS：逐级兜底，最终失败只警告不中断（DNS 不该让建机/部署失败）。
# 为什么不能只靠 pct/qm set --nameserver：实测 PVE 9.2.20 上连「单个合法 IPv4」都会被参数校验拒绝
#   400 nameserver: invalid format - value does not look like a valid IP address
# （与取值无关，CLI 侧 list 参数的传参方式有问题），因此依次退：
#   1) pct/qm set --nameserver <完整列表>
#   2) pct/qm set --nameserver <首个地址>
#   3) pvesh set /nodes/<node>/{lxc|qemu}/<id>/config —— 走 REST API（GUI 同一条路径），绕开 CLI 参数解析
#   4) 直接改配置文件 /etc/pve/{lxc,qemu-server}/<id>.conf —— PVE 启动时按它渲染 guest 的 resolv.conf
# 用法： pw_set_dns qm|pct <vmid> "<DNS 串>"
pw_set_dns() {
 local kind="$1" id="$2" dns first out node api conf tmp
 dns=$(pw_dns_list "${3:-}")
 [ -n "$dns" ] || return 0
 first=$(pw_dns_first "$dns")
 # 直写配置/API 时只用首个地址：qm 的 nameserver 是单值选项，多地址会写坏配置
 out="$dns"; [ "$kind" = "qm" ] && out="$first"
 if "$kind" set "$id" --nameserver "$dns" >/dev/null 2>&1; then
   echo "[DNS] 已设置: $dns"
   return 0
 fi
 if [ "$first" != "$dns" ] && "$kind" set "$id" --nameserver "$first" >/dev/null 2>&1; then
   echo "[DNS] 该版本不接受多地址，已设置首个: $first（如需全部，请在 guest 内维护 /etc/resolv.conf）"
   return 0
 fi
 node=$(hostname -s 2>/dev/null || hostname 2>/dev/null)
 api=lxc; [ "$kind" = "qm" ] && api=qemu
 if [ -n "$node" ] && command -v pvesh >/dev/null 2>&1 &&
    pvesh set "/nodes/$node/$api/$id/config" --nameserver "$out" >/dev/null 2>&1; then
   echo "[DNS] CLI 拒绝该参数，已改用 API（pvesh）设置: $out"
   return 0
 fi
 conf=/etc/pve/lxc/$id.conf; [ "$kind" = "qm" ] && conf=/etc/pve/qemu-server/$id.conf
 if [ -f "$conf" ]; then
   # || true / || tmp="" 是硬要求：COMMON_HELPERS 顶部有 ERR trap，未接住的失败会打印误导性的「命令执行失败」
   tmp=$(mktemp 2>/dev/null) || tmp=""
   # 就地重写（不用 sed -i：pmxcfs 上 rename 不可靠）；新值放首行，避免落进 [PENDING] 段
   if [ -n "$tmp" ] && grep -v '^nameserver:' "$conf" > "$tmp" 2>/dev/null; then
     if { echo "nameserver: $out"; cat "$tmp"; } > "$conf" 2>/dev/null; then
       rm -f "$tmp" 2>/dev/null || true
       echo "[DNS] 已直接写入配置文件: nameserver: $out"
       return 0
     fi
   fi
   rm -f "$tmp" 2>/dev/null || true
 fi
 echo "[警告] DNS 设置失败（$dns）；不影响建机，可在 guest 内手动配置 /etc/resolv.conf"
 return 1
}

# 给已存在的 LXC 容器设置 root 密码：逐级兜底，最终失败才警告（不留后手会让用户
# 拿到一个「root 无密码」的容器，SSH/控制台都进不去）。为什么不能只靠 pct set --password：
# 部分 PVE 版本/模板组合下该命令会静默失败（stdin 读密码的姿势、未启动容器的
# /etc/shadow 状态等），所以依次退：
#   1) pct set <id> --password            —— 标准做法（离线写 /etc/shadow）
#   2) pvesh set .../config --password     —— 走 REST API（GUI 同一条路径），绕开 CLI 解析
#   3) 容器内 chpasswd（需运行）—— pct exec chpasswd 直写，最可靠；
#      容器未运行时先 pct start 临时拉起，设密成功后还原到启动前的停止态（设密是一次性
#      写入，停掉后密码仍保留，不影响用户原意图）
# 用法： pw_set_password <CTID> "<密码>"
pw_set_password() {
 local id="$1" pw="$2" node was_running=0
 [ -n "$id" ] || return 1
 pw="${pw:-123456}"
 # 1) 标准 CLI（离线写 /etc/shadow）
 if printf '%s' "$pw" | pct set "$id" --password >/dev/null 2>&1; then
   echo "[密码] 已设置 root 密码（pct set）"
   return 0
 fi
 # 2) REST API 兜底（绕开 CLI 参数解析的已知坑）
 node=$(hostname -s 2>/dev/null || hostname 2>/dev/null)
 if [ -n "$node" ] && command -v pvesh >/dev/null 2>&1 &&
    pvesh set "/nodes/$node/lxc/$id/config" --password "$pw" >/dev/null 2>&1; then
   echo "[密码] 已设置 root 密码（pvesh API）"
   return 0
 fi
 # 3) 容器内 chpasswd：需容器运行。未运行则临时拉起，设密后还原停止态
 pct status "$id" 2>/dev/null | grep -q running && was_running=1
 if [ "$was_running" != "1" ] && pct start "$id" >/dev/null 2>&1; then was_running=2; fi
 if pct status "$id" 2>/dev/null | grep -q running; then
   if printf 'root:%s' "$pw" | pct exec "$id" -- chpasswd >/dev/null 2>&1; then
     [ "$was_running" = "2" ] && pct stop "$id" >/dev/null 2>&1 || true
     echo "[密码] 已设置 root 密码（容器内 chpasswd）"
     return 0
   fi
   [ "$was_running" = "2" ] && pct stop "$id" >/dev/null 2>&1 || true
 fi
 echo "[警告] 设置 root 密码失败（请改用 pct enter $id 进容器后 passwd 手动设密）"
 return 1
}

# 删除本工具为某台虚机生成的 Cloud-Init 片段（/var/lib/vz/snippets/cloud-<vmid>-user.yaml）。
# 为什么要连带删：qm destroy / --purge 只管虚机自身（磁盘、备份任务、复制、HA），
# 这些片段是存储上的普通文件，不在 PVE 的管理范围内，删了虚机它仍留在盘上：
#   1) 同 VMID 之后被别的途径（WebUI / 手工 qm create）复用时，若配置里恰好仍带
#      cicustom: user=local:snippets/cloud-<vmid>-user.yaml，会把上一台机器的主机名/SSH 配置
#      注入新机器（身份串台，排查起来极绕）；
#   2) 反过来，配置里留着 cicustom 而片段被清掉了，虚机启动会因
#      "volume 'local:snippets/...' does not exist" 失败 —— 所以「删虚机」与「删片段」要一起做。
# 只删本工具自己生成的那个固定文件名，用户自建的片段一律不碰。
# 全程尽力而为：不存在/删不掉都只提示，绝不影响删除虚机这个主流程。
# 注：路径与建机时的写入路径一致（/var/lib/vz/snippets）；跨节点场景下片段在创建它的那台
#     机器上，本节点找不到就静默跳过（不误报、也不影响删除结果）。
pw_snippet_rm() {  # $1=VMID
 local f="/var/lib/vz/snippets/cloud-$1-user.yaml"
 [ -f "$f" ] || return 0
 if rm -f "$f" 2>/dev/null; then
   echo "[清理] 已删除 Cloud-Init 片段: $f"
 else
   echo "[提示] Cloud-Init 片段删除失败（不影响虚机删除）: $f"
 fi
 return 0
}

# ── 集群仲裁（quorum）：/etc/pve 可写性判定 + 统一指引 ──────────────────────
# 为什么要它：PVE 集群失去仲裁时 /etc/pve 变为只读，qm/pct/qmrestore 的写配置动作一律被拒
# （`cluster not ready - no quorum?`，退出码 255；或更原始的
#  `unable to open file '/etc/pve/nodes/x/qemu-server/103.conf.tmp.18546' - Permission denied`）
# ——报错晦涩，批量时还会每台白跑一遍，用户容易误判成镜像/参数/存储问题。
#
# 判定方式：真去写一下。pmxcfs 失去仲裁时不是把挂载点改成只读，而是按条目返回 EACCES
# （非 quorate 时目录 0555 / 文件 0444；PVE 9 起由 dcdb 在写路径上先查 cfs_is_quorate()
# 直接返回 -EACCES），所以「touch 一下再删掉」与实际写配置走的是同一道门禁。
# 探测点必须取真正要写的目录（qm/qmrestore 写的是
# /etc/pve/nodes/<本节点>/qemu-server/<id>.conf），只看 /etc/pve 根目录覆盖不到这条路径。
# 返回 0 = 只读（不可写），1 = 可写。
pw_pve_readonly() {
 local p
 [ -f /etc/pve/corosync.conf ] || return 1   # 无 corosync.conf = 独立单机，不存在仲裁问题
 for p in "/etc/pve/nodes/$(hostname -s 2>/dev/null)/qemu-server" \
          "/etc/pve/nodes/$(hostname 2>/dev/null)/qemu-server" /etc/pve; do
   [ -d "$p" ] || continue
   if ! touch "$p/.pw-wtest" 2>/dev/null; then return 0; fi
   rm -f "$p/.pw-wtest" 2>/dev/null || true
 done
 return 1
}
# 失去仲裁的统一文案：前置检测与「跑到一半才失去仲裁」两处共用，避免各写一份而口径走偏
# （改一处漏一处 → 同类故障只在一半工具里给出可操作指引）
pw_quorum_guidance() {
 export PATH="$PATH:/usr/sbin:/sbin"
 local st
 st=$( { pvecm status 2>/dev/null || true; } | sed -n 's/^Quorate:[[:space:]]*//p' | head -1 )
 echo "        当前 Quorate: ${st:-未知}；这是集群层面的问题，与镜像/参数/存储无关。"
 echo "[原因] 集群失去仲裁（多数节点不在线、或心跳网络不通）时 /etc/pve 为只读："
 echo "       PVE 会拒绝任何写配置的动作——所以不是模板/参数/存储的问题，重试也不会成功。"
 echo "       集群健康时这条检测不会触发；一旦触发，说明本机当前确实写不了集群配置。"
 echo "[解决] 任选其一后重跑本任务："
 echo "        1) 到「节点管理」页刷新，离线节点行会出现「临时取得仲裁」按钮，点它并输入确认词，"
 echo "           即可立刻恢复写操作（等价 pvecm expected <在线节点数>；只改运行时票数，"
 echo "           不改集群配置、不删节点、不影响任何虚机）；"
 echo "        2) 把失联节点恢复上线，或修复心跳网络，仲裁会自动回来；"
 echo "        3) 长期方案：给集群补第三票——部署 QDevice 或加一台节点；两台集群也可启用"
 echo "           auto_tie_breaker，让存活方自动保有仲裁。"
}
# 前置检测：动手前先探一次，命中就给出可操作指引并终止
pw_quorum_precheck() {
 pw_pve_readonly || return 0
 echo "[错误] 本机所在 PVE 集群已失去仲裁（quorum），/etc/pve 为只读："
 echo "        建机等写操作会被 PVE 直接拒绝（报 cluster not ready - no quorum? 退出码 255，"
 echo "        或 unable to open file '/etc/pve/.../xxx.conf.tmp.xxxxx' - Permission denied）；这里提前拦下，避免白跑。"
 pw_quorum_guidance
 exit 1
}
# 失败后的即时归因：任何写操作（qm/qmrestore/qm set 等）失败后调用一次——
# 前置检测只探「任务启动那一刻」，而模板缓存拉取可能要几分钟（共享存储慢时更久），
# 期间集群可能失去仲裁；此时日志里只剩 PVE 的原始报错，用户会误判成镜像/参数/存储问题。
# 返回 0 = 确实是只读导致的失败（已打印指引），1 = 与只读无关（调用方照原样报错）。
pw_quorum_hint_if_readonly() {
 pw_pve_readonly || return 1
 echo "[提示] 检测到本机 /etc/pve 当前不可写（最常见是集群失去仲裁）：本次失败是集群层面的原因，"
 echo "       与模板/镜像/参数/存储无关——PVE 直接拒绝了「写虚机配置」这一步。"
 pw_quorum_guidance
 return 0
}
# ---- 写后校验 / 回滚统一封装 ----
# 各工具「改 VM 配置后回读 qm config 确认」的站点（直通/磁盘/网卡/USB 等约 16 处）原本各自手写
# grep 校验，且失败时仅 exit 1、不回滚（留下半修改状态）。这里统一封装，并补上「未生效自动回滚到
# 修改前备份」——各调用点已在 qm set 之前执行 backup_file，故回滚取到的是修改前那份备份。
verify_qm_change() {  # $1=vmid $2=slot $3=期望回读子串(neg 模式表示「不应再出现」) $4=neg(可选)
 local vmid="$1" slot="$2" expect="$3" neg="$4"
 local conf="/etc/pve/qemu-server/${vmid}.conf"
 local bak; bak=$(ls -t "${BACKUP_DIR}"/"$(basename "$conf")".*.bak 2>/dev/null | head -1)
 if [ "$neg" = "neg" ]; then
   if qm config "$vmid" 2>/dev/null | grep -q "$expect"; then
     echo "[错误] 移除未生效（VM $vmid 仍含 '$expect'），已回滚到修改前"
     [ -n "$bak" ] && cp "$bak" "$conf"
     exit 1
   fi
   echo "[验证] 已移除 $slot"
 elif [ -n "$expect" ]; then
   qm config "$vmid" 2>/dev/null | grep -q "^${slot}:.*${expect}" || {
     echo "[错误] 配置未生效（VM $vmid 的 $slot 未找到 '$expect'），已回滚到修改前"
     [ -n "$bak" ] && cp "$bak" "$conf"
     exit 1; }
   echo "[验证] ${slot} 已生效"
 else
   qm config "$vmid" 2>/dev/null | grep -q "^${slot}:" || {
     echo "[错误] 配置未生效（槽位 $slot 未出现），已回滚到修改前"
     [ -n "$bak" ] && cp "$bak" "$conf"
     exit 1; }
   echo "[验证] ${slot} 已生效"
 fi
}
# 把目标文件恢复到指定备份（恢复前先备份目标自身）。用于显式「从备份恢复」动作；
# grub_mgr restore 与 iface_manage restore 共用，避免两处各写一份 cp+cmp。
restore_from_backup() {  # $1=目标文件 $2=备份文件
 local dst="$1" src="$2"
 [ -n "$src" ] || { echo "[错误] 请选择备份文件；下拉无选项说明该节点还没有备份，请先执行'备份当前配置'"; exit 1; }
 [ -f "$src" ] || { echo "[错误] 备份不存在: $src"; echo "可用备份:"; ls "$(dirname "$src")" 2>/dev/null; exit 1; }
 backup_file "$dst"
 run "备份文件复制失败" cp "$src" "$dst"
 run "恢复未生效，文件内容与备份不一致" cmp -s "$src" "$dst"
}
# 内核参数串规范化：去重（保持顺序）、单空格分隔。
# 预设项与自定义参数叠加后常有重复，直接写进虚机会得到同名参数多份。
kargs_dedupe() {
 printf '%s\n' $1 | awk '!seen[$0]++' | tr '\n' ' ' | sed 's/ *$//'
}
# CPU 机型候选链：所选机型优先，其次通用基线 x86-64-v2-AES → kvm64 → host（去重保序）。
# ARM 架构只支持 host。用法: cpu_model_chain "$PT_CPU"
cpu_model_chain() {
 case "$(uname -m)" in aarch64|arm64) printf '%s' "host"; return 0 ;; esac
 local chain="x86-64-v2-AES kvm64 host"
 [ -n "$1" ] && chain=$(printf '%s\n' $1 $chain | awk '!seen[$0]++' | tr '\n' ' ')
 printf '%s' "$chain"
}
# 设置虚机 CPU 机型（不启动）：所选优先，取候选链中第一个本机支持的。用法: vm_set_cpu <vmid> [所选机型]
vm_set_cpu() {
 local vmid="$1" chain c
 chain=$(cpu_model_chain "$2")
 for c in $chain; do
   qm set "$vmid" --cpu "$c" >/dev/null 2>&1 && { echo "[信息] CPU 机型：$c"; return 0; }
 done
 echo "[警告] 未找到本机支持的 CPU 机型（已尝试：$chain）"; return 1
}
# 设置虚机 CPU 机型并启动：所选优先，启动失败自动降级重试。用法: vm_start_cpu <vmid> [所选机型]
vm_start_cpu() {
 local vmid="$1" chain c err
 chain=$(cpu_model_chain "$2")
 for c in $chain; do
   qm set "$vmid" --cpu "$c" >/dev/null 2>&1 || { echo "[跳过] CPU 机型 $c 本机不支持"; continue; }
   if err=$(qm start "$vmid" 2>&1); then echo "[信息] CPU 机型：$c（启动成功）"; return 0; fi
   echo "[警告] CPU 机型 $c 启动失败：$(printf '%s' "$err" | tail -n1)"
 done
 echo "[错误] 启动失败：已尝试 $chain"; return 1
}
# 内存气球最小内存（MB）：$1=模式（空=不启用 / 50 / 70 / custom）$2=自定义 MB $3=基准内存 MB。
# 输出实际最小 MB（不启用或参数无效时输出空），调用方据此决定是否 qm set --balloon。
balloon_min_mb() {
 local mode="$1" custom="$2" base="$3" bal=""
 case "$mode" in
   "") return 0 ;;
   50|70)
     case "$base" in ''|*[!0-9]*) return 0 ;; esac
     bal=$((base * mode / 100)) ;;
   custom)
     case "$custom" in ''|*[!0-9]*) return 0 ;; esac
     bal="$custom" ;;
 esac
 printf '%s' "$bal"
}
   # 通过 QEMU 代理在虚机内读写内核启动参数（GRUB 系统）——「调整虚机内核参数」工具与
   # 「显卡直通 → 为虚机适配内核参数」共用这一份实现。
   # 用法: guest_kargs_apply <vmid> <list|add|remove> "<kargs>" [reboot]
   #   成功、或跳过（Windows 虚机 / 非 GRUB 引导）返回 0；代理不可用或执行失败返回 1（并打印原因）。
   #   reboot=1 时由虚机自行延迟重启（调用方无需再关机开机）。
   # 说明：虚机侧输出故意用英文，避免 QEMU 代理把 UTF-8 字节按 latin-1 解码导致中文乱码。
   guest_kargs_apply() {
   local vmid="$1" mode="$2" kargs="$3" reboot="${4:-}" sh b64 out
   case "$mode" in list|add|remove) ;; *) echo "[虚机] 未知操作: $mode"; return 1;; esac
   [ -n "$vmid" ] || { echo "[虚机] 未指定虚机"; return 1; }
   if [ "$mode" != "list" ] && [ -z "$kargs" ]; then echo "[虚机] 未提供内核参数"; return 1; fi
   command -v qm >/dev/null 2>&1 || { echo "[虚机] 宿主机缺少 qm 命令"; return 1; }
   qm agent "$vmid" ping >/dev/null 2>&1 || { echo "[虚机] QEMU 代理不可用（未装 qemu-guest-agent / 虚机未开机 / VM 选项未开 agent=1）"; return 1; }
   sh=$(cat <<'EOSH'
set -u
GRUB=/etc/default/grub
MARK="$GRUB.webtools-kargs"   # 记录「由本工具写入」的参数：查看时着色、移除时可清理
KARGS="@KARGS@"
MODE="@MODE@"
[ -f "$GRUB" ] || { echo "[guest] skipped: $GRUB not found (Windows guest or non-GRUB boot)"; exit 0; }
CUR=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" | head -1 || true)
OLD=$(tr '\n' ' ' < "$MARK" 2>/dev/null || true)
# 首次运行（还没有标记文件）时，从当前配置里认出本工具的预设项，避免「上次改的参数显示不出颜色」
KNOWN="nomodeset i915.enable_guc=0 amdgpu.runpm=0 pci=noaer pcie_aspm=off mitigations=off net.ifnames=0 loglevel=7 initcall_debug console=ttyS0,115200 earlyprintk=serial,ttyS0,115200"
if [ -z "$OLD" ]; then
 for t in $CUR; do case " $KNOWN " in *" $t "*) OLD="$OLD $t";; esac; done
 # 「查看」保持真正只读：只在内存里识别，不落盘
 if [ -n "$OLD" ] && [ "$MODE" != "list" ]; then printf '%s\n' "${OLD# }" > "$MARK" 2>/dev/null || true; fi
fi
# 把「本工具负责的参数」包上 [[ ]] 标记（宿主侧再换成 «» 供前端上橙色），与原本就有的参数区分开
# 注意：这里只能用 ASCII 标记——QEMU 代理按 latin-1 回传字节，直接输出 «» 会变成 Â«Â» 乱码
mark() {
 local t o=""
 for t in $1; do
   case " $OLD $KARGS " in *" $t "*) o="$o [[$t]]" ;; *) o="$o $t" ;; esac
 done
 printf '%s' "${o# }"
}
echo "[guest] config: $(mark "${CUR:-GRUB_CMDLINE_LINUX_DEFAULT not found}")"
echo "[guest] running: $(mark "$(cat /proc/cmdline)")"
if [ -n "$OLD" ]; then echo "[guest] tool-managed:$(printf ' [[%s]]' $OLD)"; fi
if [ "$MODE" = "list" ]; then
 echo "[guest] read-only, nothing changed"
 exit 0
fi
BAK="$GRUB.bak.$(date +%F_%H%M%S)"
cp -a "$GRUB" "$BAK"
echo "[guest] backup: $BAK"
if [ "$MODE" = "add" ]; then
 # 已存在的参数不再重复追加：先去掉行首键名与两端引号再比对
 # （CUR 末尾是收尾的引号，用 " $CUR " 比对会让「最后一个参数」永远匹配不上 → 反复追加重复项）
 CURV=$(printf '%s' "$CUR" | sed 's/^GRUB_CMDLINE_LINUX_DEFAULT=//; s/"//g')
 ADD=""
 for a in $KARGS; do case " $CURV " in *" $a "*) ;; *) ADD="$ADD $a";; esac; done
 if [ -z "$ADD" ]; then
   echo "[guest] already present: $KARGS"
 else
   sed -i "s|^\(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*\)\"|\1$ADD\"|" "$GRUB"
   echo "[guest] appended:$(printf ' [[%s]]' $ADD)"
 fi
 NEW="$OLD $KARGS"
else
 for a in $KARGS; do
   sed -i "s| $a ||g; s|\"$a |\"|g" "$GRUB"
 done
 echo "[guest] removed: $KARGS"
 NEW=""
 for t in $OLD; do case " $KARGS " in *" $t "*) ;; *) NEW="$NEW $t";; esac; done
fi
# 更新标记文件（去重、保持顺序）：记录当前由本工具负责的参数
UNIQ=""
for t in $NEW; do case " $UNIQ " in *" $t "*) ;; *) UNIQ="$UNIQ $t";; esac; done
printf '%s\n' "${UNIQ# }" > "$MARK" 2>/dev/null || true
if command -v update-grub >/dev/null 2>&1; then update-grub >/dev/null 2>&1; else grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1; fi
echo "[guest] after: $(mark "$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" | head -1)")"
# 用 if 而非 && ：若本工具的参数被清空，末行返回 0 才不会让整段脚本以非 0 退出
if [ -n "$UNIQ" ]; then echo "[guest] tool-managed:$(printf ' [[%s]]' $UNIQ)"; fi
if [ "@REBOOT@" = "1" ]; then
 echo "[guest] rebooting in 3s ..."
 nohup sh -c 'sleep 3; systemctl reboot' >/dev/null 2>&1 &
fi
EOSH
)
   sh=$(printf '%s' "$sh" | sed "s|@KARGS@|$kargs|g; s|@MODE@|$mode|g; s|@REBOOT@|$reboot|g")
   b64=$(printf '%s' "$sh" | base64 -w0)
   if out=$(qm guest exec "$vmid" -- bash -lc "echo $b64 | base64 -d | bash" 2>&1); then
     body=$(printf '%s' "$out" | python3 -c 'import json,sys
raw=sys.stdin.read()
try:
    d=json.loads(raw)
except Exception:
    sys.stdout.write(raw); raise SystemExit(0)
sys.stdout.write(d.get("out-data",""))
ec=d.get("exitcode",0)
if ec not in (0,None):
    sys.stdout.write("[guest exit code] %s\n" % ec)
' 2>/dev/null) || body="$out"
     # 虚机侧标记是 ASCII 的 [[x]]（见上），在这里换成本工具日志通用的 «x»，交给前端上橙色
     printf '%s' "$body" | sed 's/\[\[\([^]]*\)\]\]/«\1»/g'
     return 0
   fi
   echo "[虚机] 代理执行失败：$out"
   return 1
   }
   # 解析 PVE 存储根路径（与服务端 pve_api.storage_path 一致，单一来源）。
   # 重要：pvesh 默认输出是带边框的表格，行形如「│ path │ ... │」，用 sed 匹配 '^path' 永远失败；
   # 必须 --output-format json 解析。sid=pvewebtools 且为 nfs 时优先取 export（绕开回环挂载 Stale），否则取 path。
   # 返回 0 并回显根路径；无法解析时回显空（调用方再走本地回退目录）。
   pw_storage_root() {
   local sid="$1" raw val
   [ -n "$sid" ] || return 1
   raw=$(pvesh get "/storage/$sid" --output-format json 2>/dev/null) || return 1
   [ -n "$raw" ] || return 1
   if command -v python3 >/dev/null 2>&1; then
     val=$(printf '%s' "$raw" | python3 -c "import sys,json; d=json.load(sys.stdin); v=(d.get('export') if str(d.get('type','')).lower()=='nfs' else None) or d.get('path') or ''; sys.stdout.write(v)" 2>/dev/null)
   fi
   if [ -z "$val" ]; then
     val=$(printf '%s' "$raw" | sed -n 's/.*"export"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
     [ -z "$val" ] && val=$(printf '%s' "$raw" | sed -n 's/.*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
   fi
   [ -n "$val" ] && printf '%s' "$val"
   }
  # 宿主机时区（探测失败回退 Asia/Shanghai）。新建容器/虚机统一时区时用作默认值。
  pw_host_tz() {
  local t
  t=$(cat /etc/timezone 2>/dev/null || true)
  [ -n "$t" ] || t=$(timedatectl show -p Timezone --value 2>/dev/null || true)
  [ -n "$t" ] || t=Asia/Shanghai
  printf '%s' "$t"
  }
  # 统一容器时区：$1=CTID，$2=时区（留空或 host 表示跟随宿主机）。
  # 优先用 PVE 原生 `pct set --timezone`（持久化到 /etc/pve/lxc/<id>.conf，重启后仍生效）；
  # 老版本 PVE 没有该选项时，回退到容器内写 /etc/localtime + /etc/timezone（需容器已运行）。
  # 注意：容器与宿主机共享内核时钟，时刻天然一致，这里统一的是「时区」。
  pw_container_tz() {
  local ct="$1" tz="$2" real
  [ -n "$ct" ] || return 0
  [ -n "$tz" ] || tz=host
  if pct set "$ct" --timezone "$tz" >/dev/null 2>&1; then
    echo "[时区] CT $ct -> $tz（PVE 原生设置，持久生效）"
    return 0
  fi
  real="$tz"
  if [ "$real" = "host" ]; then real=$(pw_host_tz); fi
  if ! pct status "$ct" 2>/dev/null | grep -q running; then
    echo "[提示] 本机 PVE 不支持 --timezone 且 CT $ct 未运行，时区未设置"
    return 0
  fi
  if ! pct exec "$ct" -- test -e "/usr/share/zoneinfo/$real" >/dev/null 2>&1; then
    echo "[提示] CT $ct 内缺少 $real 的时区数据（tzdata），时区未设置"
    return 0
  fi
  if pct exec "$ct" -- sh -c "ln -sf /usr/share/zoneinfo/$real /etc/localtime; echo $real > /etc/timezone" >/dev/null 2>&1; then
    echo "[时区] CT $ct -> $real（容器内写入）"
  else
    echo "[提示] CT $ct 时区写入失败"
  fi
  return 0
  }
   wait_apt_lock() {  # 后台任务（unattended-upgrades/apt.daily 等）可能占用 apt 锁；轮询等待最多 90 秒
 local i
 for i in $(seq 1 30); do
   fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || return 0
   echo "[等待] apt 锁被后台任务占用，等待释放 ($i/30)..."
   sleep 3
 done
 return 1
}
# dpkg 状态自愈：上一次 apt/dpkg 操作被中途打断（掉电、OOM、任务被「强制结束」、进程被 kill）
# 会留下「半配置 / 半安装」的包，此后**所有** apt-get install 都以
#   E: dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem.
# 直接失败（vGPU 阶段一装构建依赖时就是这么撞上的，且报错指向的是本次命令，容易被误判成源或包名问题）。
# 这里在装包前先自动补一次 dpkg --configure -a，省掉用户登节点手工修复。
# 判定用 dpkg --audit：有输出即代表存在未完成的包；无输出则完全不动，避免平白拖慢装包。
fix_dpkg_interrupted() {
 local audit out
 audit=$(dpkg --audit 2>&1 || true)
 [ -n "$audit" ] || return 0
 echo "[修复] 检测到 dpkg 有未完成的配置（上次 APT 操作被中断），自动执行 dpkg --configure -a ..."
 printf '%s\n' "$audit" | head -n 10 | sed 's/^/         /'
 # 结尾 || true：失败由下面的 --audit 复核判定，这里不让 ERR trap 抢着打一条误导性的「命令执行失败」
 out=$(DEBIAN_FRONTEND=noninteractive dpkg --configure -a 2>&1 || true)
 printf '%s\n' "$out" | tail -n 20 | sed 's/^/         /'
 # 复核：--audit 已清空才算修好（dpkg --configure -a 对个别包可能返回非零但已无待处理项）
 if [ -z "$(dpkg --audit 2>&1 || true)" ]; then
   echo "[修复] dpkg 状态已恢复正常，继续安装"
   return 0
 fi
 echo "[错误] dpkg --configure -a 未能修复，请登录节点手工排查后重试："
 echo "         dpkg --audit; dpkg --configure -a; apt-get -f install"
 return 1
}
# ── 宿主占用盘 / 承载它们的存储控制器（直通类工具的拦截共用一处，避免口径漂移）──────
# 只有「PVE 真正在用」的盘才算占用：其它系统（如飞牛）的存储池在 PVE 眼里同样是 LVM/ZFS
# 成员，但 PVE 并未使用，按元数据类型拦截会把用户本来可用的盘锁死。
# 输出：盘名（空格分隔，含前导空格），供 `for D in $(pw_host_used_disks)` 分词使用。
pw_host_used_disks() {
 local pve_vgs=" pve" d vg p mounted
 # PVE 在用卷组 = 默认卷组 pve + 含有已挂载逻辑卷的卷组
 # 查询类命令必须 || true 兜底：脚本开了 pipefail + ERR trap，无匹配也返回非零会触发 trap
 mounted=$(lvs --noheadings -o vg_name,lv_path 2>/dev/null | while read -r vg p; do
   [ -n "$p" ] || continue
   # 用 if 而非 &&：grep 无匹配时 && 短路会让整行返回非零并触发 ERR trap
   if grep -q "^$p " /proc/mounts 2>/dev/null; then echo " $vg"; fi
 done || true)
 pve_vgs="$pve_vgs$mounted "
 for d in $(lsblk -dno NAME 2>/dev/null || true); do
   # 1. 有挂载点（含子孙分区/子卷）——PVE 已挂载即在使用
   if lsblk -lno MOUNTPOINTS "/dev/$d" 2>/dev/null | grep -q '[^[:space:]]'; then
     echo " $d"; continue
   fi
   # 2. ZFS 成员：zpool status 只列 PVE 已导入的池，未导入的其他系统池不算占用
   if lsblk -lno FSTYPE "/dev/$d" 2>/dev/null | grep -q 'zfs_member'; then
     if command -v zpool >/dev/null 2>&1 && zpool status 2>/dev/null | grep -qE "(^|[[:space:]/])${d}([0-9p]*[[:space:]]|$)"; then
       echo " $d"
     fi
     continue
   fi
   # 3. RAID 成员：仅当属于当前已组装的 md 阵列才算占用
   if lsblk -lno FSTYPE "/dev/$d" 2>/dev/null | grep -q 'linux_raid_member'; then
     if grep -qE "${d}\[[0-9]+\]" /proc/mdstat 2>/dev/null; then echo " $d"; fi
     continue
   fi
   # 4. LVM 成员：仅当属于 PVE 在用卷组才算占用（飞牛等系统的存储池不属于此列）
   if lsblk -lno FSTYPE "/dev/$d" 2>/dev/null | grep -q 'lvm2_member'; then
     vg=$(pvs --noheadings -o pv_name,vg_name 2>/dev/null | awk -v d="/dev/$d" '$1==d {print $2; exit}' || true)
     if [ -n "$vg" ]; then
       case "$pve_vgs" in *" $vg "*) echo " $d";; esac
     fi
   fi
 done
 return 0
}
# 承载宿主在用盘的存储控制器 PCI 地址（完整 BDF，小写；每行一个）。
# 直通这些控制器（或整组连带它们）会让宿主机掉盘：AMD APU 平台上核显与 FCH SATA
# 控制器常在同一 IOMMU 组，整组直通会把宿主盘一起交出去，VM 自身的 qcow2 也会读不到。
# 注意取的是路径里**最后一个** BDF（叶子设备）：/sys/block/sda/device 解析出来是
#   /sys/devices/pci0000:00/0000:00:08.1/0000:05:00.0/ata1/host0/target0:0:0/0:0:0:0
# 第一个匹配到的是上游桥 0000:00:08.1；若取 head -1，得到的会是桥而不是 SATA 控制器，
# 整组拦截就会比对不上（NVMe 走在根端口后面时同理，也会取到根端口）。
pw_host_used_storage_pci() {
 local d p pci
 for d in $(pw_host_used_disks); do
   p=$(readlink -f "/sys/block/$d/device" 2>/dev/null || true)
   pci=$(echo "$p" | grep -oE '0000:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]' | tail -1 || true)
   if [ -n "$pci" ]; then echo "${pci,,}"; fi
 done
 return 0
}
# 指定的「缩短 bus:device / 完整 BDF」→ 第一个 function 的完整 BDF（取不到输出空）。
# 组内设备列表用的是缩短语法（0000:05:00），读 class/driver 前需要补上 function。
pw_pci_first_bdf() {
 local hit
 hit=$(ls -d "/sys/bus/pci/devices/${1}."* 2>/dev/null | head -1 || true)
 [ -n "$hit" ] && basename "$hit"
 return 0
}
# 指定设备的 PCI class（如 0x010601）；直通拦截里用来识别存储控制器（class 前两位 01）。
pw_pci_class() {
 local bdf
 bdf=$(pw_pci_first_bdf "$1")
 [ -n "$bdf" ] || return 0
 cat "/sys/bus/pci/devices/$bdf/class" 2>/dev/null || true
 return 0
}
# 指定设备当前绑定的驱动名；无驱动/设备不存在输出空。
pw_pci_driver() {
 local bdf link
 bdf=$(pw_pci_first_bdf "$1")
 [ -n "$bdf" ] || return 0
 link="/sys/bus/pci/devices/$bdf/driver"
 if [ -e "$link" ]; then basename "$(readlink "$link" 2>/dev/null || true)"; fi
 return 0
}
# ── 直通安全闸门：涉及存储控制器的直通/整组连带，风险不明即拒绝执行 ──────────────
# 用法：pw_guard_storage_ctrl <设备(缩短 bus:device 或完整 BDF)> <场景说明> → 风险时返回 1
#   调用方：pw_guard_storage_ctrl "$dev" "整组直通" || exit 1
# 判定（非存储控制器直接放行，与本闸门无关）：
#   1) 该控制器承载宿主在用盘（系统盘/PVE 存储/已挂载盘）→ 拒绝（直通必掉盘）
#   2) 该控制器当前被 vfio-pci 占用 → 拒绝：说明有虚机正用它（或上次直通没清干净），
#      此刻宿主根本看不到它上面的盘，无法证明不是宿主在用盘 —— 不放行、不赌运气
#   3) 其余（原生驱动持有）→ 放行，并把判定依据打出来，便于用户核对
pw_guard_storage_ctrl() {
 local dev="$1" scene="$2" bdf cls drv hit
 bdf=$(pw_pci_first_bdf "$dev")
 [ -n "$bdf" ] || return 0
 cls=$(pw_pci_class "$dev")
 case "$cls" in
   0x01*) ;;
   *) return 0 ;;   # 非存储控制器（显卡/网卡/音频/USB…）：不涉及掉盘，放行
 esac
 for hit in $(pw_host_used_storage_pci); do
   if [ "$bdf" = "$hit" ]; then
     echo "[错误] 「$scene」会涉及存储控制器 $bdf，而它承载着宿主的在用盘 —— 直通后宿主机将掉盘，已拒绝执行（未做任何改动）"
     echo "[说明] 隔离组内设备共享 IOMMU 上下文，PVE/vfio 要求整组一起直通，无法只跳过存储控制器；"
     echo "       AMD APU 等平台的核显/网卡常与 FCH SATA 控制器同组，正是这种情形"
     echo "[建议] 先把 /var/lib/vz 等宿主在用盘迁到别的控制器（如 NVMe 上的存储），或改用不涉及该控制器的方案"
     return 1
   fi
 done
 drv=$(pw_pci_driver "$bdf")
 if [ "$drv" = "vfio-pci" ]; then
   echo "[错误] 「$scene」会涉及存储控制器 $bdf，而它当前正被 vfio-pci 占用：宿主机上该控制器（及其上的盘）已不可见，"
   echo "       无法确认它不承载宿主盘 —— 为免宿主机掉盘，已拒绝执行（未做任何改动）"
   echo "[建议] 先关机/移除相关直通，并用「回收直通设备」把它还给宿主驱动（lspci -k 确认 driver=ahci/nvme 后再重试）"
   return 1
 fi
 echo "[检查] 存储控制器 $bdf 由 ${drv:-无} 驱动持有、其上无宿主在用盘：本次直通会把它交给虚机（宿主将失去它）"
 return 0
}
# 取指定 PCI 设备所在 IOMMU 隔离组内「可直通的设备列表」（换行分隔）。
# 规则：
#   - 排除 bridge 类设备（host/PCI/ISA bridge，class 前两位 06）——桥不应直通给 VM，
#     直通根端口反而会连带影响其下所有设备且 PVE 会拒绝；
#   - 同 bus:device 的多个 function 合并为缩短语法（01:00.0+01:00.1 -> 01:00），
#     与 PVE 官方「All Functions」等价，避免给音频控制器单独写 function；
# 同一隔离组内直通必须整组一起过（共享 IOMMU 上下文），否则 VM 拿到不完整功能组。
# 返回 0 并回显设备列表；设备无隔离组（IOMMU 未开/不受管理）时返回 1。
iommu_group_devices() {
 local dev="$1" g gpath
 [ -e "/sys/bus/pci/devices/$dev" ] || return 1
 gpath=$(readlink -f "/sys/bus/pci/devices/$dev/iommu_group" 2>/dev/null) || return 1
 g=$(basename "$gpath")
 [ -n "$g" ] || return 1
 # 遍历组内设备，过滤 bridge（class 0x06xxxx），按 bus:device 合并 function。
 # bdf 形如 0000:01:00.0（含 domain 前缀），去 .function 后保留 domain（0000:01:00），
 # 与前端下拉 PT_DEVICE 的 domain 前缀一致，方便直接比对与写后验证。
 local d cls bdf bd seen
 seen=""
 for d in /sys/kernel/iommu_groups/$g/devices/*; do
   [ -e "$d" ] || continue
   bdf=$(basename "$d")
   cls=$(cat "/sys/bus/pci/devices/$bdf/class" 2>/dev/null || true)
   case "$cls" in
     0x06*) continue ;;   # host/PCI/ISA bridge 跳过，不直通
   esac
   bd="${bdf%.*}"         # 去掉 .function 后缀，得到 domain:bus:device
   case " $seen " in
     *" $bd "*) continue ;;   # 同 bus:device 已记录（多 function 合并）
   esac
   seen="${seen:+$seen }$bd"
   echo "$bd"
 done
}
# 把被 vfio-pci 占用的 PCIe 设备归还给原生内核驱动（如 nvme）。
# 移除 PCIe 直通后设备不会自动归还：仍绑在 vfio-pci 上时宿主机「掉盘」（/dev/nvmeXnY 消失）。
# 这里先清 driver_override、解绑 vfio-pci，再按候选顺序把原驱动装回并绑定。
# 成功返回 0；设备已被原生驱动持有、或找不出可用驱动时返回 1（不视为错误，调用方自行提示）。
release_vfio_device() {
 local bdf="${1:-}" drv_link drv cands m ov cur hi lo
 if [ -z "$bdf" ] || [ ! -e "/sys/bus/pci/devices/$bdf" ]; then return 1; fi
 drv_link="/sys/bus/pci/devices/$bdf/driver"
 drv=""
 if [ -e "$drv_link" ]; then
   drv=$(basename "$(readlink -f "$drv_link" 2>/dev/null)" 2>/dev/null || true)
 fi
 # 原生驱动已持有 → 无需处理；vfio 占用、或上次释放后没装回驱动（无驱动）都要尝试归还
 if [ -n "$drv" ] && [ "$drv" != "vfio-pci" ]; then return 1; fi
 # 原驱动候选：lspci 认出的模块去掉 vfio-pci；framebuffer/legacy 类排到最后再试
 # （NVIDIA 卡常列出 "nvidiafb, nouveau, nvidia"，而 nvidiafb 绑不上独显 → 必须逐个试）
 cands=$(lspci -Dknn -s "$bdf" 2>/dev/null | sed -n 's/.*Kernel modules: //p' | tr ',' '\n' \
   | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^vfio-pci$' | grep -v '^$' || true)
 if [ -z "$cands" ]; then return 1; fi
 for m in $cands; do
   case " nvidiafb nouveau efifb vesafb simpledrm vga16fb uvesafb " in
     *" $m "*) lo="$lo $m" ;; *) hi="$hi $m" ;;
   esac
 done
 cands="$hi $lo"
 # driver_override：PVE 直通时写过 vfio-pci，不清掉则解绑后内核会立刻重新绑回 vfio。
 # 未设置时内核可能读回 "(null)"，要当空处理
 ov="/sys/bus/pci/devices/$bdf/driver_override"
 if [ -e "$ov" ]; then
   cur=$(cat "$ov" 2>/dev/null || true)
   if [ "$cur" = "(null)" ]; then cur=""; fi
   if [ -n "$cur" ]; then
     printf '' > "$ov" 2>/dev/null || true
   fi
 fi
 if [ "$drv" = "vfio-pci" ]; then
   printf '%s' "$bdf" > /sys/bus/pci/drivers/vfio-pci/unbind 2>/dev/null || true
 fi
 # 按候选顺序逐个装载并绑定，第一个绑上的即为结果
 # 注意：写 bind 是同步接口，要等驱动 probe 完成才返回——AHCI/NVMe 这类会逐端口复位并识别硬件，
 # 慢时可达 1~2 分钟（链路异常/掉盘过更久），上层看起来就像「卡死」。
 # 这里用 timeout 兜底，并在超时后回查实际驱动：探测仍在后台跑、但设备已绑上时同样算成功。
 for m in $cands; do
   modprobe "$m" 2>/dev/null || true
   if [ -d "/sys/bus/pci/drivers/$m" ] \
      && timeout 180 sh -c "printf '%s' '$bdf' > '/sys/bus/pci/drivers/$m/bind'" 2>/dev/null; then
     return 0
   fi
   cur=$(basename "$(readlink "/sys/bus/pci/devices/$bdf/driver" 2>/dev/null || true)" 2>/dev/null || true)
   if [ "$cur" = "$m" ]; then return 0; fi
 done
 return 1
 }
# ── 唯一标识（硬件指纹）重置：克隆/还原后避免被业务（PCDN/网心云等）判定为同一台 ──
# 覆盖配置层可改的 4 个身份维度：网卡 MAC、SMBIOS UUID+序列号、磁盘序列号。
# 系统层（machine-id / SSH host key / 网心云 /etc 下设备指纹等）无法在配置层覆盖，需进系统清理。
# 调用前提：虚机处于停机状态（修改 MAC/序列号要求停机，否则 qm set 会失败）。
# 用法： sanitize_identity <vmid>   （仅虚拟机 qm；容器 pct 走原生 --unique 重置 MAC）
sanitize_identity() {
 local vmid="$1" NETKEY NETLINE MODEL REST NEWMAC
 local NEWUUID NEWSMBIOS_SERIAL DK DLINE NEWDISK_SERIAL NEWDLINE
 [ -n "$vmid" ] || { echo "[警告] sanitize_identity: 缺少 VMID，跳过"; return 1; }

 # 1) 网卡 MAC：重新生成全部 netN 的 MAC（替换 model=MAC 段，保留 bridge/firewall 等其余参数）
 for NETKEY in $(qm config "$vmid" 2>/dev/null | grep -E '^net[0-9]+:' | cut -d: -f1); do
   NETLINE=$(qm config "$vmid" 2>/dev/null | grep "^${NETKEY}:" | sed 's/^[^:]*:[[:space:]]*//')
   MODEL=$(echo "$NETLINE" | cut -d= -f1)
   REST=$(echo "$NETLINE" | sed "s/^${MODEL}=[0-9A-Fa-f:]*//; s/^,//; s/,${MODEL}=[0-9A-Fa-f:]*//")
   NEWMAC="BC:24:11:$(printf '%02X' $((RANDOM % 256))):$(printf '%02X' $((RANDOM % 256))):$(printf '%02X' $((RANDOM % 256)))"
   run "重新生成 MAC 失败（$NETKEY）" qm set "$vmid" "--$NETKEY" "${MODEL}=${NEWMAC}${REST:+,$REST}"
 done
 echo "  [唯一] 已重新生成网卡 MAC 地址"

 # 2) SMBIOS UUID + 序列号：用全新 smbios1 覆盖，丢弃克隆体继承的旧标记
 NEWUUID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen 2>/dev/null)
 NEWSMBIOS_SERIAL=$(head -c 128 /dev/urandom | tr -dc 'A-Z0-9'); NEWSMBIOS_SERIAL=${NEWSMBIOS_SERIAL:0:12}
 if [ -n "$NEWUUID" ]; then
   qm set "$vmid" --smbios1 "uuid=$NEWUUID,serial=$NEWSMBIOS_SERIAL" >/dev/null 2>&1 \
     && echo "  [唯一] 已重置 SMBIOS UUID 与序列号" \
     || echo "  [警告] SMBIOS 重置失败，请手动检查"
 else
   echo "  [警告] 无法生成 UUID，跳过 SMBIOS 重置"
 fi

 # 3) 磁盘序列号（scsi/sata/virtio/ide 的 serial=），避免磁盘级指纹重复
 # 跳过 cloud-init 盘：它是 media=cdrom 的配置 ISO，PVE 不允许给它加 serial，且本就不向 guest
 # 暴露磁盘序列号（没有可重置的指纹）——不跳过只会每次刷一条无意义的「重置 ide2 序列号失败」。
 # 末尾 || true：配置里没有可用磁盘（如只剩 cloudinit）时 grep 落空返回 1，未接住会被顶部
 # ERR trap 误报成「命令执行失败」。
 for DK in $(qm config "$vmid" 2>/dev/null | grep -E '^(scsi|sata|virtio|ide)[0-9]+:' | grep -v 'cloudinit' | cut -d: -f1 || true); do
   DLINE=$(qm config "$vmid" 2>/dev/null | grep "^${DK}:" | sed 's/^[^:]*:[[:space:]]*//')
   NEWDISK_SERIAL=$(head -c 128 /dev/urandom | tr -dc 'A-Z0-9'); NEWDISK_SERIAL=${NEWDISK_SERIAL:0:12}
   if echo "$DLINE" | grep -q 'serial='; then
     NEWDLINE=$(echo "$DLINE" | sed "s/serial=[^,]*/serial=$NEWDISK_SERIAL/")
   else
     NEWDLINE="${DLINE},serial=$NEWDISK_SERIAL"
   fi
   qm set "$vmid" "--$DK" "$NEWDLINE" >/dev/null 2>&1 \
     && echo "  [唯一] 已重置 $DK 序列号" \
     || echo "  [警告] 重置 $DK 序列号失败，请手动检查"
   done
   }
   # ── 系统盘扩容（建机部署期调用，VM 停机）：系统盘不足所选容量时自动扩容，仅放大不缩小 ──
   # 用法：resize_system_disk <vmid> <目标GB>。目标留空/非法自动跳过；
   # 当前容量 ≥ 目标时跳过（qm resize 只允许放大）；容量读取失败时尝试直接扩容并容错。
   resize_system_disk() {
   local vmid="$1" want="$2" dev line cur cur_g
   [ -n "$vmid" ] || return 1
   want="${want%%[Gg]}"
   case "$want" in ''|*[!0-9]*) return 0;; esac
   [ "$want" -gt 0 ] 2>/dev/null || return 0
   # 系统盘 = 首个非 cloudinit 的数据盘（scsi/virtio/sata/ide）
   dev=$(qm config "$vmid" 2>/dev/null | grep -E '^(scsi|virtio|sata|ide)[0-9]+:' | grep -v 'cloudinit' | head -1 | cut -d: -f1 || true)
   [ -n "$dev" ] || { echo "[警告] 未找到系统盘，跳过扩容"; return 0; }
   line=$(qm config "$vmid" 2>/dev/null | grep "^${dev}:" | head -1 || true)
   # 末尾 || true：磁盘行没有 size= 时（部分卷不回报大小）grep 落空返回 1，未接住会被 ERR trap
   # 误报成「命令执行失败」——那是正常落空，应由下面的「无法可靠解析」分支去处理。
   cur=$(echo "$line" | grep -oE 'size=[0-9]+(\.[0-9]+)?[KkMmGgTtBb]?' | head -1 | cut -d= -f2 || true)
   # size 写法不止「整数+G」一种：PVE 会按容量大小写成 2022.3G / 2022300K / 2022300M / ...B。
   # 原先的正则只截整数部分、且只认 G/M，遇到 K/B 会把数字原样当成 GB、遇到小数会截断；
   # 一旦算出来的值偏大，就会误判「当前 ≥ 目标」而把用户选的容量静默跳过（日志里出现
   # 「系统盘当前 2022300G ≥ 目标 50G，无需扩容」这种明显不合理数字就是此因）。
   # 这里统一折算成整数 GB；折算不出来、或数值明显不合理（≥1PB=解析残留）时按「未知」处理，
   # 交给下面的「尝试扩容」分支——宁可让 qm resize 报一句无关痛痒的失败，也不要漏扩容。
   cur_g=$(printf '%s' "$cur" | awk '
     {
       s = $0; u = "G"; n = length(s)
       if (n > 0 && substr(s, n, 1) ~ /[A-Za-z]/) { u = toupper(substr(s, n, 1)); s = substr(s, 1, n - 1) }
       if (s !~ /^[0-9]+(\.[0-9]+)?$/) { print ""; exit }
       v = s + 0
       if (u == "K") v = v / 1024 / 1024
       else if (u == "M") v = v / 1024
       else if (u == "B") v = v / 1024 / 1024 / 1024
       else if (u == "T") v = v * 1024
       else if (u != "G") { print ""; exit }
       if (v < 0 || v >= 1000000) { print ""; exit }
       printf "%.0f", v
     }')
   RESIZE_GREW=""
   if [ -z "$cur_g" ]; then
    echo "[提示] 系统盘 $dev 当前大小无法可靠解析（size=${cur:-缺省}），直接按目标 ${want}G 尝试扩容 ..."
    if qm resize "$vmid" "$dev" "${want}G" >/dev/null 2>&1; then
      RESIZE_GREW=1
      echo "[扩容] 系统盘已扩容到 ${want}G"
    else
      echo "[警告] 系统盘扩容失败（可能已大于等于 ${want}G 或存储空间不足），保持现状"
    fi
    return 0
   fi
   if [ "$cur_g" -ge "$want" ] 2>/dev/null; then
    echo "[提示] 系统盘当前 ${cur_g}G ≥ 目标 ${want}G，无需扩容"
   else
    if qm resize "$vmid" "$dev" "${want}G" >/dev/null 2>&1; then
      RESIZE_GREW=1
      echo "[扩容] 系统盘已从 ${cur_g}G 扩容到 ${want}G"
    else
      echo "[警告] 系统盘扩容到 ${want}G 失败（存储空间不足？），保持 ${cur_g}G"
    fi
   fi
   }
# ── 建机后「扩根」第二步：qm resize 只改虚拟盘，虚机内的分区 / LVM / 文件系统必须自己扩 ──
# openEuler / RHEL 系云镜像的根多在 LVM 上，cloud-init 自带的 growpart + resizefs 只扩到物理
# 分区，LV 和文件系统不跟着长；部分镜像（openEuler 22.03 实测）连 growpart 都没装，cloud-init
# 从头到尾没扩过。表现为「PVE 面板 size=100G，登进系统 df -h 还是 40G」。
# 三个入口：
#   pw_root_expand_body        —— 扩根脚本正文（虚机内执行，幂等，已扩满时全程空跑）
#   pw_emit_root_expand_runcmd —— 以 Cloud-Init runcmd 列表项输出（"  - |" + 4 空格缩进）
#   pw_guest_grow_root <vmid>  —— 经 QEMU 代理在已开机虚机内执行（模板克隆等注入不了 cloud-init）
pw_root_expand_body() {
cat <<'PWEXPAND'
ROOT_SRC=$(findmnt -n -o SOURCE / | sed 's/\[.*//')
BEFORE=$(findmnt -n -o SIZE /)
case "$ROOT_SRC" in
  /dev/*) ;;
  *) echo "[disk] skip: unrecognized root device ($ROOT_SRC)"; exit 0 ;;
esac
PART=$(lsblk -nso NAME,TYPE "$ROOT_SRC" 2>/dev/null | awk '$2=="part"{print $1; exit}')
if [ -n "$PART" ]; then
  DISK=$(lsblk -nso PKNAME "/dev/$PART" 2>/dev/null | tail -1)
  NUM=$(cat "/sys/class/block/$PART/partition" 2>/dev/null)
  if [ -n "$DISK" ] && [ -n "$NUM" ]; then
    if ! command -v growpart >/dev/null 2>&1; then
      # 包名按发行版区分：Debian/Ubuntu 在 cloud-guest-utils 里，RHEL 系是 cloud-utils-growpart
      if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y cloud-guest-utils >/dev/null 2>&1 || true
      elif command -v dnf >/dev/null 2>&1; then
        dnf install -y cloud-utils-growpart >/dev/null 2>&1 || true
      elif command -v yum >/dev/null 2>&1; then
        yum install -y cloud-utils-growpart >/dev/null 2>&1 || true
      fi
    fi
    if command -v growpart >/dev/null 2>&1; then
      growpart "/dev/$DISK" "$NUM" >/dev/null 2>&1 || true
    elif command -v parted >/dev/null 2>&1; then
      parted -s "/dev/$DISK" resizepart "$NUM" 100% >/dev/null 2>&1 || true
      partx -u "/dev/$DISK" >/dev/null 2>&1 || true
    fi
  fi
fi
if command -v pvs >/dev/null 2>&1; then
  for _pv in $(pvs --noheadings -o pv_name 2>/dev/null); do
    pvresize "$_pv" >/dev/null 2>&1 || true
  done
  case "$ROOT_SRC" in
    /dev/mapper/*) lvextend -l +100%FREE "$ROOT_SRC" >/dev/null 2>&1 || true ;;
  esac
fi
case "$(findmnt -n -o FSTYPE /)" in
  xfs) xfs_growfs / >/dev/null 2>&1 || true ;;
  ext2|ext3|ext4) resize2fs "$ROOT_SRC" >/dev/null 2>&1 || true ;;
esac
AFTER=$(findmnt -n -o SIZE /)
[ "$BEFORE" = "$AFTER" ] || echo "[disk] root expanded: $BEFORE -> $AFTER"
PWEXPAND
}
pw_emit_root_expand_runcmd() {
echo "  - |"
pw_root_expand_body | sed 's/^/    /'
}
# 经 QEMU 代理执行扩根：仅在有代理且 guest-exec 未被禁用的虚机上可用。
# 全程 best-effort：代理不就绪/被禁用都只提示，绝不让建机流程失败（恒返回 0）。
pw_guest_grow_root() {
local vmid="$1" i err b64 raw
[ -n "$vmid" ] || return 0
for i in $(seq 1 10); do
  if err=$(timeout 5 qm guest exec "$vmid" -- true 2>&1 >/dev/null); then
    err=""
    break
  fi
  if echo "$err" | grep -qi 'guest-exec has been disabled'; then
    echo "[扩根] 虚机内 guest-exec 被禁用，跳过自动扩分区；可进系统执行 growpart + resize2fs"
    return 0
  fi
  sleep 3
done
if [ -n "$err" ]; then
  echo "[扩根] QEMU 代理未就绪（30s），跳过自动扩分区；可进系统执行 growpart + resize2fs"
  return 0
fi
echo "[扩根] 经 QEMU 代理在虚机内扩展分区/文件系统 ..."
b64=$(pw_root_expand_body | base64 -w0)
raw=$(qm guest exec "$vmid" --timeout 300 -- bash -c "echo $b64 | base64 -d | bash" 2>&1 || true)
# 不同 PVE 版本的 out-data 可能是 base64 原文，也可能是已解码文本：按内容自适应
printf '%s' "$raw" | python3 -c '
import base64,json,re,sys
raw=sys.stdin.read()
try:
    d=json.loads(raw) or {}
except Exception:
    print(raw.strip()); raise SystemExit(0)
def t(s):
    if not s: return ""
    x="".join(s.split())
    if len(x)>=8 and len(x)%4==0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}",x):
        try:
            y=base64.b64decode(x,validate=True).decode("utf-8")
            if y and all(c in "\r\n\t" or 32<=ord(c)<127 for c in y): return y
        except Exception: pass
    return s
o=t(d.get("out-data") or ""); e=t(d.get("err-data") or "")
if o: print(o)
if e: print(e)
' 2>/dev/null || true
return 0
}

# ── 集群级 VMID/CTID 占用判定（VMID 在 PVE 中集群唯一，必须用集群视图，不能只看本机 qm/pct status）──
# 依据与 qm/pct create 一致：集群资源清单（含 .vmlist）。单机/集群通用。
# 用法： vmid_state_cluster <id>  → 占用则打印 running/stopped（或 local）并返回 0；未占用返回 1（无输出）。
# 缓存集群全部已占 ID 到 _PVE_VMIDS（"id:status" 空格分隔），避免批量时反复查 API。
_PVE_VMIDS=""
_pve_vmids_load() {
  # 用 if 而非 [ ] && return 0：首次调用（_PVE_VMIDS 为空，常态）时 [ ] 为 false → 返回 1 触发顶部 ERR trap 误报
  if [ -n "$_PVE_VMIDS" ]; then return 0; fi
  local raw
  raw=$(pvesh get /cluster/resources --output-format json 2>/dev/null) || raw=""
  if [ -n "$raw" ] && command -v python3 >/dev/null 2>&1; then
    _PVE_VMIDS=$(printf '%s' "$raw" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
s=set()
for x in d:
    v=x.get('vmid')
    if v is not None:
        s.add('%s:%s' % (v, x.get('status','')))
print(' '.join(sorted(s, key=lambda e:int(str(e).split(':')[0]))))
" 2>/dev/null)
  fi
  if [ -z "$_PVE_VMIDS" ]; then
    # 兜底：pvesh 不可用（如集群异常）时退回本机视图，保证单机场景仍可用
    _PVE_VMIDS=$( { qm list 2>/dev/null | awk 'NR>1{print $1":local"}'; pct list 2>/dev/null | awk 'NR>1{print $1":local"}'; } | tr '\n' ' ')
  fi
  return 0
}
vmid_state_cluster() {
  local id="$1" entry
  [ -n "$id" ] || return 1
  _pve_vmids_load || return 1
  entry=$(printf '%s\n' $_PVE_VMIDS | tr ' ' '\n' | grep -E "^${id}:")
  # 用 if 而非 [ ] && return 1：vmid 被占用（常态、本就该返回占用状态）时 [ ] 为 false → 返回 1 触发 ERR trap 误报
  if [ -z "$entry" ]; then return 1; fi
  printf '%s' "${entry#*:}"
  return 0
}
# ── 网络存储诊断（模板/镜像/备份源在 NFS/CIFS/Ceph 等网络存储时，
#    打印挂载参数、到服务端的链路速率与实测吞吐，并直接给出「瓶颈在哪」的结论）──
# 仅当源文件确在网络存储上才输出，本地存储直接 return。用法: nfs_mount_diag "$源文件路径"

# 到指定服务端地址的出口网卡及速率（回显 "<网卡> <Mb/s>"；取不到回显空）。
# 虚拟口/无线口常报 0 或 -1，此时视为未知（不强行下结论）。
pw_link_speed() {
 local addr="${1:-}" dev spd
 [ -n "$addr" ] || return 0
 dev=$(ip route get "$addr" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}' || true)
 [ -n "$dev" ] || return 0
 spd=$(cat "/sys/class/net/$dev/speed" 2>/dev/null || true)
 case "$spd" in ''|*[!0-9]*) return 0 ;; 0) return 0 ;; esac
 printf '%s %s' "$dev" "$spd"
 return 0
}
# 「实测吞吐 + 链路速率」→ 瓶颈结论。$1=实测 MB/s（可空） $2=链路 Mb/s（可空） $3=挂载选项
# 结论口径：贴着链路上限 → 只能换链路/换本地源；链路只有 100M → 就是网口协商问题；
# 链路够快却跑不满 → 服务端盘/NFS 单连接（给 nconnect 的可执行做法）。
pw_speed_verdict() {
 local m="${1:-}" link="${2:-}" opts="${3:-}" cap
 if [ -z "$link" ]; then
   echo "  [提示] 链路速率未取到（虚拟口/无线常见）：可用 iperf3 复测，或把源放本机盘对比后再判断"
   return 0
 fi
 cap=$(( link / 8 ))
 if [ -z "$m" ]; then
   echo "  链路能力: 约 ${cap} MB/s（${link}Mb/s）；本次未采到吞吐，无法比对"
   return 0
 fi
 awk -v m="$m" -v cap="$cap" -v link="$link" -v opts="$opts" 'BEGIN{
   ratio = (cap > 0) ? m / cap : 0
   printf "  链路能力: 约 %d MB/s（%sMb/s），实测约占 %.0f%%\n", cap, link, ratio*100
   if (link <= 100) {
     # 100M 网口本身就是问题：实测已占 xx% 说明读侧不是瓶颈，是链路被协商成百兆了
     printf "  [结论] 瓶颈是网口速率（%sMb/s ≈ %dMB/s，实测已占其 %.0f%%）：基本是网线/交换机口协商成了 100M\n", link, cap, ratio*100
     printf "  [建议] 换千兆/2.5G 网线与交换机口后约提速 %d 倍；12MB/s 上下就是 100M 的物理极限，调 NFS 参数或换存储架构都救不了\n", int(1000/link)
   } else if (ratio >= 0.6) {
     printf "  [结论] 读侧已接近链路上限：想更快只能换更快链路（2.5G/10G）或改用本机盘上的源\n"
   } else {
     printf "  [结论] 链路够快（%sMb/s）却只跑出其 %.0f%%：瓶颈在读侧其它环节——服务端导出盘的速度/负载，或 NFS 单 TCP 连接\n", link, ratio*100
     if (opts !~ /nconnect=/) {
       printf "  [建议] 本机 NFS 客户端未启用多连接（单流常被单 TCP 连接限制，多连接实测可显著提速）：\n"
       printf "         工具在「新挂载 / 修复共享存储」时会自动带上 nconnect=8（失败自动回退，不会挂不上）；\n"
       printf "         但已挂载的存储不能用 remount 改，需在维护窗口先停用该挂载点上的虚机/任务后重挂：\n"
       printf "           umount <挂载点> && mount -o nconnect=8 <server>:<export> <挂载点>\n"
       printf "         持久化：把 /etc/fstab 该行选项里加上 nconnect=8（需内核 ≥5.3 且 NFSv4.1+，服务端 nfsd ≥4.1）\n"
     } else {
       printf "  [建议] 已启用 nconnect 仍跑不满：在服务端用 dd bs=4M 复测该文件的纯盘读速，确认不是导出盘本身慢（HDD/被占用）\n"
     }
   }
 }'
 return 0
}
nfs_mount_diag() {
  local f="$1"
  [ -f "$f" ] || return 0
  local fstype=$(stat -f -c '%T' "$f" 2>/dev/null || echo unknown)
  case "$fstype" in
    nfs|nfs4|cifs|fuse|ceph|*gluster*) ;;
    *) return 0 ;;
  esac
  echo "[诊断] 数据源位于网络存储（类型: $fstype），后续读取将从网络进行，可能成为瓶颈 ..."
  local mpinfo=$(findmnt -n -o TARGET,OPTIONS -T "$f" 2>/dev/null | head -1)
  local opts="" srv="" linkspd="" _mb=""
  if [ -n "$mpinfo" ]; then
    local mnt=$(echo "$mpinfo" | awk '{print $1}')
    opts=$(echo "$mpinfo" | cut -d' ' -f2-)
    echo "  挂载点: $mnt  选项: $opts"
    local rsz=$(echo "$opts" | grep -oE 'rsize=[0-9]+' | cut -d= -f2)
    local wsz=$(echo "$opts" | grep -oE 'wsize=[0-9]+' | cut -d= -f2)
    local proto=$(echo "$opts" | grep -oE 'proto=[a-z]+' | cut -d= -f2)
    local syncfl=$(echo "$opts" | grep -oE '(^|,)sync(,|$)' || true)
    [ -n "$rsz" ] && [ "$rsz" -lt 1048576 ] 2>/dev/null && echo "  [建议] rsize=$rsz 偏小，建议挂载加 rsize=1048576"
    [ -n "$wsz" ] && [ "$wsz" -lt 1048576 ] 2>/dev/null && echo "  [建议] wsize=$wsz 偏小，建议挂载加 wsize=1048576"
    [ "$proto" = "udp" ] && echo "  [建议] proto=udp 吞吐低，建议改为 proto=tcp"
    [ -n "$syncfl" ] && echo "  [建议] 挂载含 sync（同步写），非必须建议改为 async"
  fi
  # 到服务端的出口链路速率：把「实测吞吐」换算成「占链路百分比」，直接定位瓶颈在哪一侧
  srv=$(echo "$opts" | grep -oE 'addr=[0-9.]+' | cut -d= -f2 || true)
  if [ -z "$srv" ]; then
    srv=$(findmnt -n -o SOURCE -T "$f" 2>/dev/null | head -1 | cut -d: -f1 | sed 's|^//||; s|/.*||' || true)
  fi
  if [ -n "$srv" ]; then
    local linkinfo=$(pw_link_speed "$srv")
    local linkdev=$(echo "$linkinfo" | awk '{print $1}')
    linkspd=$(echo "$linkinfo" | awk '{print $2}')
    [ -n "$linkdev" ] && echo "  服务端: $srv  出口网卡: $linkdev"
  fi
  echo "  [采样] 测量网络读取吞吐（直读 64M，绕开页缓存，超时 30s）..."
  local _t0=$(date +%s)
  local _sp="" _buffered=0
  # 必须 iflag=direct：不带它时 dd 读的是页缓存而不是网络，而本诊断读的正是文件头 64M，
  # 自己上一轮跑过的数据就躺在缓存里，于是回回报出十几 GB/s 的假值 —— 一旦链路速率能读到，
  # pw_speed_verdict 会据此判成「读侧已接近链路上限」，把用户引向换网卡，真瓶颈（服务端盘/单连接）被掩盖。
  _sp=$(timeout 30 dd if="$f" of=/dev/null bs=1M count=64 iflag=direct 2>&1 \
        | { grep -oE '[0-9.]+ (MB|GB)/s' || true; } | tail -1)
  if [ -z "$_sp" ]; then
    # 少数文件系统不支持 O_DIRECT（dd 直接报错）：退回缓冲读，但结果可能含页缓存，输出里标注清楚
    _sp=$(timeout 30 dd if="$f" of=/dev/null bs=1M count=64 2>&1 \
          | { grep -oE '[0-9.]+ (MB|GB)/s' || true; } | tail -1)
    [ -n "$_sp" ] && _buffered=1
  fi
  local _t1=$(date +%s)
  if [ -n "$_sp" ]; then
    if [ "$_buffered" = "1" ]; then
      echo "  实测读取吞吐: $_sp（该文件系统不支持直读，退为缓冲读，可能受页缓存影响，仅供参考）"
    else
      echo "  实测读取吞吐: $_sp（直读：已绕开页缓存，未含预读，略低于顺序大块读的实际值属正常）"
    fi
    case "$_sp" in
      *" GB/s") _mb=$(awk -v v="${_sp% GB/s}" 'BEGIN{printf "%.1f", v*1024}' || true) ;;
      *" MB/s") _mb="${_sp% MB/s}" ;;
    esac
    pw_speed_verdict "$_mb" "$linkspd" "$opts"
  else
    echo "  [提示] 吞吐采样未完成（耗时约 $((_t1-_t0))s/64M），可检查 NFS 挂载参数"
    pw_speed_verdict "" "$linkspd" "$opts"
  fi
  echo "  [补充] 若读侧已达链路能力、整体仍慢：瓶颈在目标存储（本机盘）写入，可用 dd bs=4M 实测写速"
}

# ===== 模板 / 镜像 / 备份的本机缓存 =====
# 背景：模板与备份常放在共享存储（NFS）上，建机/还原每次都要把整个文件从网络读一遍。
# 千兆以下的链路里，10G 级模板单台就要十几分钟，批量更是逐台重复读同一份数据。
# 这里在【节点本机盘】留一份缓存：某节点首次用到该文件时拉取一次，之后本节点所有任务
# 直读本地，网络只付一次代价（单台同样受益，不再只在批量时才预拉取）。
#
# 关键约束：缓存根必须落在本机盘。旧实现把批量「暂存目录」建在共享存储的 dump/ 下，
# 实际是 NFS 读 + NFS 写，没有任何本地化效果（还多赔一次网络写），此处显式规避。
#
# 可覆盖的环境变量：
#   PT_TEMPLATE_CACHE            指定缓存根目录（默认 <PVE local 存储根>/pvewebtools-cache）
#   PT_TEMPLATE_CACHE_MAX_RATIO  缓存占用比例上限（0~1，默认 0.2）：缓存不得超过所在盘总容量的该比例
#                                如 0.2 = 不超过本机盘 20%；这是主上限，随盘大小自适应
#   PT_TEMPLATE_CACHE_MAX_AGE_DAYS 保留天数（天，默认 30）：超过 N 天未使用（meta 的 mtime）的条目直接过期，
#                                与大小无关；按时间收口，避免缓存无限膨胀
#   PT_TEMPLATE_CACHE_MAX_MB     固定 MB 上限（默认 0 = 不设）：仅作为比例上限之上的硬天花板（取小），
#                                一般不改；设为 >0 可再额外卡一道绝对容量
#   PT_TEMPLATE_CACHE_FORCE=1    忽略已有缓存，强制重新拉取
# 淘汰策略：先按「保留天数」过期清理，再按「占用比例」做 LRU；二者任一触发即淘汰最久未用的条目。
# GC 在每次新拉取前触发（见 pw_cache_ensure），界面「清理过期」按钮可主动触发仅年龄淘汰。

# 路径所在文件系统类型；路径不存在时向上找最近的已存在父目录
pw_fstype_of() {
  local p="${1:-}"
  while [ -n "$p" ] && [ "$p" != "/" ] && [ ! -e "$p" ]; do
    p=$(dirname "$p")
  done
  stat -f -c '%T' "$p" 2>/dev/null || echo unknown
}
# 是否位于网络存储（判定口径与 nfs_mount_diag 一致）
pw_is_remote_path() {
  case "$(pw_fstype_of "$1")" in
    nfs|nfs4|cifs|fuse|ceph|*gluster*) return 0 ;;
  esac
  return 1
}
# 字节数 → 人类可读
pw_human_size() {
  awk -v b="${1:-0}" 'BEGIN{
    if (b >= 1073741824) printf "%.2f GB", b/1073741824;
    else if (b >= 1048576) printf "%.1f MB", b/1048576;
    else printf "%d B", b;
  }'
}
# PVE local（dir 类型）存储的根目录：优先读 /etc/pve/storage.cfg 的 dir: local 块；
# 该块通常不显式写 path（走内置默认），故缺省回退 /var/lib/vz
pw_local_store_root() {
  local p=""
  p=$(awk '
    /^[a-z]+:[[:space:]]/ { inblk = ($0 ~ /^dir:[[:space:]]*local[[:space:]]*$/); next }
    inblk && /^[[:space:]]*path[[:space:]]/ { sub(/^[[:space:]]*path[[:space:]]+/, ""); print; exit }
  ' /etc/pve/storage.cfg 2>/dev/null || true)
  if [ -n "$p" ] && [ -d "$p" ]; then printf '%s\n' "$p"; return 0; fi
  if [ -d /var/lib/vz ]; then printf '/var/lib/vz\n'; return 0; fi
  return 1
}
# 本机缓存根目录（stdout）；无可用位置时返回非零
pw_cache_root() {
  if [ -n "${PT_TEMPLATE_CACHE:-}" ]; then printf '%s\n' "$PT_TEMPLATE_CACHE"; return 0; fi
  local r=""
  r=$(pw_local_store_root) || r=""
  if [ -n "$r" ] && [ -d "$r" ] && [ -w "$r" ] && ! pw_is_remote_path "$r"; then
    printf '%s\n' "$r/pvewebtools-cache"; return 0
  fi
  # 兜底 /var/tmp（PVE 上位于本机盘；不能用 /tmp，可能是 tmpfs 吃内存）
  if [ -d /var/tmp ] && [ -w /var/tmp ] && ! pw_is_remote_path /var/tmp; then
    printf '/var/tmp/pvewebtools-cache\n'; return 0
  fi
  return 1
}
# 缓存总量上限（KB）——保留作内部参考；实际生效上限见 pw_cache_effective_max_kb
pw_cache_max_kb() { printf '%s\n' $(( ${PT_TEMPLATE_CACHE_MAX_MB:-0} * 1024 )); }
# 保留天数（天），默认 30：超过 N 天未使用（meta 的 mtime）的条目直接过期，与大小无关。
pw_cache_max_age_days() { printf '%s\n' "${PT_TEMPLATE_CACHE_MAX_AGE_DAYS:-30}"; }
# 磁盘占用比例上限（0~1），默认 0.2：缓存不得超过所在盘总容量的该比例（0.2 = 不超过 20%）。
pw_cache_max_ratio() { printf '%s\n' "${PT_TEMPLATE_CACHE_MAX_RATIO:-0.2}"; }
# 所在盘总容量（KB）。直接取块数×块大小，不走 df 文本解析（同 pw_cache_free_kb 的理由）。
pw_cache_disk_total_kb() {
  local blocks="" bsize=""
  blocks=$(stat -f -c '%b' "$1" 2>/dev/null) || return 1
  bsize=$(stat -f -c '%S' "$1" 2>/dev/null) || return 1
  [ -n "$blocks" ] && [ -n "$bsize" ] || return 1
  printf '%s\n' $(( blocks * bsize / 1024 ))
}
# 有效容量上限（KB）：占用比例上限优先（ratio × 盘总容量），再被固定 MB 上限兜住（取小）。
#   PT_TEMPLATE_CACHE_MAX_MB=0（默认）表示「不设固定上限」，仅用比例；>0 时作为硬天花板。
#   比例/盘容量都拿不到可信值时，退化为「不按容量淘汰」（返回极大值），宁可留着缓存也绝不误删。
pw_cache_effective_max_kb() {
  local maxmb="" ratio="" dkbtot="" ratiokb=""
  maxmb=${PT_TEMPLATE_CACHE_MAX_MB:-0}
  case "$maxmb" in *[!0-9]*) maxmb=0 ;; esac
  ratio=$(pw_cache_max_ratio)
  case "$ratio" in *[!0-9.]*) ratio=0 ;; esac
  if [ "$ratio" = "0" ] || [ -z "$ratio" ]; then
    [ "$maxmb" -gt 0 ] 2>/dev/null && { printf '%s\n' $(( maxmb * 1024 )); return 0; }
    printf '%s\n' 9223372036854775807; return 0
  fi
  dkbtot=$(pw_cache_disk_total_kb "$1")
  if [ -z "$dkbtot" ] || [ "$dkbtot" -le 0 ] 2>/dev/null; then
    [ "$maxmb" -gt 0 ] 2>/dev/null && { printf '%s\n' $(( maxmb * 1024 )); return 0; }
    printf '%s\n' 9223372036854775807; return 0
  fi
  ratiokb=$(awk -v tot="$dkbtot" -v r="$ratio" 'BEGIN{ printf "%d", tot*r }')
  if [ "$maxmb" -gt 0 ] 2>/dev/null; then
    awk -v a="$ratiokb" -v b=$(( maxmb * 1024 )) 'BEGIN{ if (a<b) print a; else print b }'
  else
    printf '%s\n' "$ratiokb"
  fi
}
# 保留天数过期清理：meta 最后使用时间早于「现在 - N 天」的条目直接删除，与大小无关。
# 被并发任务占用的条目跳过，绝不删别人正在读的文件。$1=缓存根；age<=0 时关闭（默认 30，即开启）。
pw_cache_purge_expired() {
  local root="$1" age="" now="" cutoff=0 m="" vname="" vfile="" n=0
  age=$(pw_cache_max_age_days)
  [ "$age" -gt 0 ] 2>/dev/null || return 0
  now=$(date +%s); cutoff=$(( now - age * 86400 ))
  [ -d "$root/.meta" ] || return 0
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    [ -f "$m" ] || continue
    [ "$(stat -c '%Y' "$m" 2>/dev/null || echo 0)" -lt "$cutoff" ] 2>/dev/null || continue
    vname=$(basename "$m"); vname="${vname%.meta}"; vfile="$root/$vname"
    pw_cache_busy "$root/.locks/$vname.lock" && continue
    rm -f "$vfile" "$m" 2>/dev/null || true
    if [ ! -e "$vfile" ] && [ ! -e "$m" ]; then
      n=$(( n + 1 ))
      echo "[缓存] 保留天数到期（${age} 天未使用），清理：$vname" >&2
    fi
  done <<< "$(find "$root/.meta" -maxdepth 1 -type f -name '*.meta' 2>/dev/null || true)"
  # 用 if 而非 [ .. ] && echo：n=0（无过期条目，常态）时 [ ] 返回 1 会触发脚本顶部的 ERR
  # trap，被误报成「命令执行失败」（实测表现就是建机日志里出现这条 ✕ 错误，实际清理流程照常进行）。
  if [ "$n" -gt 0 ]; then
    echo "[缓存] 已按保留天数清理 $n 个过期条目" >&2
  fi
}
# 目录所在盘的可用空间（KB）。直接取块数×块大小，不走 df 的文本解析：
# df 的列位会受文件系统名长度等影响，解析落空时拿到空值，一旦被当成 0 就会误判
# 「空间不足」而把缓存逐条删光——现象就是「缓存里永远只留得下最后一次拉的那个」。
pw_cache_free_kb() {
  local blocks="" bsize=""
  blocks=$(stat -f -c '%a' "$1" 2>/dev/null) || return 1
  bsize=$(stat -f -c '%S' "$1" 2>/dev/null) || return 1
  [ -n "$blocks" ] && [ -n "$bsize" ] || return 1
  printf '%s\n' $(( blocks * bsize / 1024 ))
}
# 缓存条目是否正被并发任务占用（拉取锁拿不到即视为占用）；$1=锁文件
pw_cache_busy() {
  command -v flock >/dev/null 2>&1 || return 1
  [ -e "$1" ] || return 1
  ! flock -n "$1" true 2>/dev/null
}
# LRU 清理：按最后使用时间由旧到新删除条目，直到「总量不超上限」且「可用空间满足 need_kb」。
# 正被并发任务占用的条目跳过，避免删掉别人正在读取的缓存文件。
# $1=缓存根 $2=需要的可用空间（KB）
pw_cache_gc() {
  local root="$1" need="${2:-0}"
  local max="" ent="" vname="" vfile="" f="" fsz=0 total=0 av=0 n=0 list="" line=""
  local _maxround=200 _round=0
  # 有效容量上限：占用比例（默认盘 20%）优先，再被固定 MB 上限兜住（取小）
  max=$(pw_cache_effective_max_kb "$root")
  # 先按「保留天数」过期清理（与大小无关），再做容量 LRU
  pw_cache_purge_expired "$root"
  # 崩溃残留的拉取中间文件：超过 1 小时仍未完成，直接清掉
  find "$root" -maxdepth 1 -type f -name '*.part.*' -mmin +60 -delete 2>/dev/null || true
  while :; do
    # 以 .meta 为索引列出缓存条目（按最后使用时间升序）。没有 meta 的文件不归本缓存管理
    # （如同目录下的解压中间产物），一律不碰，避免误删正在使用的东西。
    list=$(find "$root/.meta" -maxdepth 1 -type f -name '*.meta' -printf '%T@ %f\n' 2>/dev/null | sort -n || true)
    [ -n "$list" ] || return 0
    total=0; n=0
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      f="$root/${line#* }"; f="${f%.meta}"
      [ -f "$f" ] || continue
      fsz=$(stat -c '%s' "$f" 2>/dev/null || echo 0)
      total=$(( total + fsz )); n=$(( n + 1 ))
    done <<< "$list"
    # 用 if 而非 [ ] && return 0：n!=0（有缓存条目，常态）时 [ ] 返回 1 会触发顶部 ERR trap 误报
    if [ "$n" -eq 0 ]; then return 0; fi
    av=$(pw_cache_free_kb "$root")
    if [ -z "$av" ]; then
      # 读不到可用空间就不动缓存：宁可少清理，也绝不能把「读不到」当成「空间不足」，
      # 那会把缓存一条条删光（曾经就是这样，表现为缓存里只留得下最后一个）
      echo "[缓存] 无法获取可用空间，跳过缓存清理" >&2
      return 0
    fi
    # 用 if 而非 [ ] && [ ] && return 0：需要清理时（total>max 或 av<need，恰是本次场景）
    # 整条链为 false → 返回 1，会触发顶部 ERR trap 误报成「命令执行失败」。
    if [ "$total" -le "$max" ] && [ "$av" -ge "$need" ]; then return 0; fi
    # 挑最久未使用的一条（meta 的 mtime 在每次命中时同步刷新）；被并发任务占用则剔除后重挑，
    # 全部被占用就放弃清理
    ent=$(printf '%s\n' "$list" | head -1); ent="${ent#* }"
    vname="${ent%.meta}"; vfile="$root/$vname"
    if pw_cache_busy "$root/.locks/$vname.lock"; then
      list=$(printf '%s\n' "$list" | awk -v v="$ent" '{p=$0; sub(/^[^ ]* /, "", p); if (p != v) print}' || true)
      [ -n "$list" ] || return 0
      ent=$(printf '%s\n' "$list" | head -1); ent="${ent#* }"
      vname="${ent%.meta}"; vfile="$root/$vname"
      # 用 if 而非 && return 0：条目未被占用（常态、本就要清理）时 && 链为 false → 返回 1 触发 ERR trap 误报
      if pw_cache_busy "$root/.locks/$vname.lock"; then return 0; fi
    fi
    # 说清到底是哪个条件触发，别一律叫「空间不足」——两种原因的处理方式完全不同
    if [ "$total" -gt "$max" ]; then
      echo "[缓存] 缓存总量 $(pw_human_size $(( total * 1024 ))) 超过上限 $(pw_human_size $(( max * 1024 )))，清理最久未使用的：$vname" >&2
    else
      echo "[缓存] 可用空间 $(pw_human_size $(( av * 1024 ))) 不足（本次需要 $(pw_human_size $(( need * 1024 )))），清理最久未使用的：$vname" >&2
    fi
    rm -f "$vfile" "$root/.meta/$ent" 2>/dev/null || true
    # 删除失败（权限 / 只读挂载）时若继续循环会反复选中同一条而死循环，这里直接收手
    if [ -e "$vfile" ] || [ -e "$root/.meta/$ent" ]; then
      echo "[警告] 缓存文件删除失败，跳过缓存清理：$vname" >&2
      return 0
    fi
    _round=$(( _round + 1 ))
    # 用 if 而非 [ ] && return 0：round<max（每轮常态）时 [ ] 返回 1 会触发顶部 ERR trap 误报
    if [ "$_round" -ge "$_maxround" ]; then return 0; fi
  done
}
# 命中判定：缓存文件存在，且 meta 记录的 size/mtime 与源一致（源被替换会自动失效）。
# 命中时 touch 缓存文件以刷新 LRU 使用时间。$1=源 $2=缓存文件 $3=meta
pw_cache_hit() {
  local src="$1" cf="$2" meta="$3" s="" m=""
  [ -f "$cf" ] && [ -f "$meta" ] || return 1
  s=$(stat -c '%s' "$src" 2>/dev/null) || return 1
  m=$(stat -c '%Y' "$src" 2>/dev/null) || return 1
  grep -qx "size=$s" "$meta" 2>/dev/null || return 1
  grep -qx "mtime=$m" "$meta" 2>/dev/null || return 1
  # meta 的 mtime 即「最后使用时间」，供 LRU 判断；两个文件一起刷新
  touch "$cf" "$meta" 2>/dev/null || true
  return 0
}
# 确保文件在本机本地可用（必要时拉取一次并缓存）。
# 约定：stdout 固定两行 —— 第 1 行状态、第 2 行可用文件路径；人类可读提示一律走 stderr，
# 避免污染调用方的 $( ) 解析。状态：
#   local    源本来就在本机非网络存储上，无需缓存（路径=源路径）
#   hit      命中本机缓存
#   fetched  本次拉取完成
#   fallback 拉取失败（空间不足 / 源不可读等），调用方应回退为直读源
#   disabled 没有可用的本机缓存位置
# 用法：_res=$(pw_cache_ensure "$F"); _st=$(printf '%s\n' "$_res" | sed -n 1p); _cf=$(printf '%s\n' "$_res" | sed -n 2p)
# 一般不必直接调用：建机/云镜像/还原/预热四条路径统一走 pw_cache_use —— 它在这一层之上
# 补齐了提示文案、回退口径与「只在缓存不可用时才做链路诊断」，避免四处各写一份。
pw_cache_ensure() {
  local src="${1:-}"
  [ -n "$src" ] && [ -f "$src" ] || { printf 'fallback\n\n'; return 0; }
  if ! pw_is_remote_path "$src"; then printf 'local\n%s\n' "$src"; return 0; fi

  local root=""
  root=$(pw_cache_root) || root=""
  [ -n "$root" ] || { printf 'disabled\n\n'; return 0; }
  mkdir -p "$root/.meta" "$root/.locks" 2>/dev/null || { printf 'disabled\n\n'; return 0; }

  local name="" cf="" meta="" lk="" ssz=0 need=0 part="" ok=0 t0=0
  name=$(basename "$src"); cf="$root/$name"; meta="$root/.meta/$name.meta"; lk="$root/.locks/$name.lock"
  [ "${PT_TEMPLATE_CACHE_FORCE:-}" = "1" ] && { rm -f "$cf" "$meta" 2>/dev/null || true; }
  if pw_cache_hit "$src" "$cf" "$meta"; then printf 'hit\n%s\n' "$cf"; return 0; fi

  # 未命中 → 加锁后拉取（同名模板并发时只有一个任务真正拉取，其余等锁后直接命中）。
  # 等锁上限 30 分钟：百兆链路下拉 10G 级模板本就要十几分钟，等别人拉完远比两个任务
  # 各拉一遍划算；超时则回退直读源，不让任务白等。
  if command -v flock >/dev/null 2>&1; then
    : > "$lk" 2>/dev/null || true
    # 注意：这里不能写 exec 8>"$lk" 2>/dev/null —— 不带命令的 exec 会把重定向**持久**作用到
    # 当前 shell，fd 2 从此指向 /dev/null，本函数之后所有 >&2 的提示（含拉取进度）会被静默丢弃，
    # 现象就是日志里只剩头尾、中间的进度全没了。用花括号块把 stderr 的屏蔽限制在块内，
    # 同时 fd 8 的重定向照常持久生效（锁仍持有到脚本结束）。
    [ -f "$lk" ] && { exec 8>"$lk"; } 2>/dev/null && flock -w 1800 8 2>/dev/null || true
  fi
  # 等锁期间别的任务可能已拉好：二次判定
  if pw_cache_hit "$src" "$cf" "$meta"; then printf 'hit\n%s\n' "$cf"; return 0; fi

  ssz=$(stat -c '%s' "$src" 2>/dev/null || echo 0)
  [ "$ssz" -gt 0 ] 2>/dev/null || { printf 'fallback\n\n'; return 0; }
  # 需求空间 = 源大小 ×1.15 + 1GB 余量（KB）
  need=$(( ssz / 1024 * 115 / 100 + 1048576 ))
  pw_cache_gc "$root" "$need"

  echo "[缓存] 首次在本节点使用该文件，正在拉取到本机本地（$(pw_human_size "$ssz")）：本节点后续建机/还原将直读本地，不再走网络 ..." >&2
  t0=$(date +%s)
  part="$cf.part.$$"
  # 拉取放后台跑，前台按「整数百分比变化」打点报进度。
  # 不用 rsync --info=progress2：它靠 \r 原地刷新，而任务日志是按行收集的，整段进度会
  # 挤成一行、直到进程结束才吐出，等于没有进度（十几个 G 的模板会让人以为卡死）。
  # --inplace 也是为此：rsync 默认「写临时文件 + 改名」，那样 $part 在完成前根本不存在，
  # 前台采不到大小；原子性由本函数自己的 .part + mv 保证，不需要 rsync 再兜一层。
  # </dev/null：脚本本体是经 stdin 喂给 bash -s 的，后台进程绝不能去碰这个 stdin。
  _pw_cache_fetch() {
    if command -v rsync >/dev/null 2>&1 && rsync -a --inplace "$1" "$2" >/dev/null 2>&1; then
      return 0
    fi
    rm -f "$2" 2>/dev/null || true
    cp -f "$1" "$2" >/dev/null 2>&1
  }
  _pw_cache_fetch "$src" "$part" </dev/null &
  _fpid=$!
  ok=0
  _last=-1
  _lastout=$(date +%s)
  while kill -0 "$_fpid" 2>/dev/null; do
    sleep 2
    _cur=$(stat -c '%s' "$part" 2>/dev/null || echo 0)
    [ "$_cur" -gt 0 ] 2>/dev/null || continue
    _pct=$(( _cur * 100 / ssz ))
    [ "$_pct" -gt 100 ] && _pct=100
    if [ "$_pct" != "$_last" ]; then
      _now=$(date +%s)
      # 打点间隔 ≥3 秒：小模板几秒就传完，否则每个整数百分比一行会瞬间刷满整屏；100% 必打
      if [ $(( _now - _lastout )) -ge 3 ] || [ "$_pct" -ge 100 ]; then
        _el=$(( _now - t0 )); [ "$_el" -gt 0 ] || _el=1
        _rate=$(( _cur / _el ))
        _eta=""
        if [ "$_rate" -gt 0 ]; then
          _left=$(( (ssz - _cur) / _rate ))
          _eta="，剩余约 $(( _left / 60 )) 分 $(( _left % 60 )) 秒"
        fi
        echo "[缓存] 拉取中 ${_pct}%（$(pw_human_size "$_cur") / $(pw_human_size "$ssz")，均速 $(( _rate / 1048576 )) MB/s${_eta}）" >&2
        _last=$_pct
        _lastout=$_now
      fi
    fi
    # 大小已达预期说明写盘完成，直接收尾，不必再等 kill -0 的时机
    [ "$_cur" -ge "$ssz" ] && break
  done
  wait "$_fpid" 2>/dev/null && ok=1
  unset -f _pw_cache_fetch 2>/dev/null || true
  if [ "$ok" = "1" ]; then
    [ "$(stat -c '%s' "$part" 2>/dev/null || echo 0)" = "$ssz" ] || ok=0
  fi
  if [ "$ok" != "1" ]; then
    rm -f "$part" 2>/dev/null || true
    echo "[警告] 拉取到本机失败（本机磁盘空间不足或源不可读），回退为直接读取远端文件" >&2
    printf 'fallback\n\n'; return 0
  fi
  if ! mv -f "$part" "$cf" 2>/dev/null; then
    rm -f "$part" 2>/dev/null || true
    printf 'fallback\n\n'; return 0
  fi
  {
    echo "src=$src"
    echo "size=$ssz"
    echo "mtime=$(stat -c '%Y' "$src" 2>/dev/null || echo 0)"
    echo "fetched=$(date +%s)"
  } > "$meta" 2>/dev/null || true
  echo "[缓存] 已缓存到本机：$cf（耗时 $(( $(date +%s) - t0 )) 秒，后续直读本地）" >&2
  printf 'fetched\n%s\n' "$cf"; return 0
}
# 统一的「取用本机缓存」入口：在 pw_cache_ensure 外面收口四条调用路径（模板建机 / 云镜像建机 /
# 备份还原 / 预热模板缓存），把原先四处复制粘贴的口径合成一处：
#   · 提示文案与回退口径（拿不到本机缓存就回退读源文件）只写一遍；
#   · 「只有真的要读网络时才做链路诊断」只写一遍 —— nfs_mount_diag 那次 64M 采样是实打实的
#     网络读取，缓存命中时再跑纯属白白占带宽（旧实现里预热就是无条件先采样再拉取的）；
#   · 「不可用时该说什么、该回退读哪个文件」只写一遍。
# $1=源文件路径 $2=给用户看的名称（模板/镜像/备份，默认「文件」）
# stderr: 人类可读提示（含诊断输出；诊断必须留在 stderr，否则会污染下面的 stdout）
# stdout: 固定两行 —— 第 1 行状态（local|hit|fetched|fallback|disabled），第 2 行实际可用的
#         文件路径（缓存不可用时回退为源路径，永不为空）
# 返回码恒为 0，调用方按第 1 行状态判断。这里绝不能 return 非 0：COMMON_HELPERS 顶部是
# set -E + ERR trap，而 trap 的「[错误] 命令执行失败」是打到 stdout 的，set -E 会把它渗进
# 调用方的 $( ) 里，于是「路径」被污染成两行（实测过，预热脚本会直接把脏值当路径用）。
pw_cache_use() {
  local src="${1:-}" label="${2:-文件}" res="" st="" cf=""
  if [ -z "$src" ] || [ ! -f "$src" ]; then
    echo "[提示] ${label}不存在或不可读，无法缓存：$src" >&2
    printf 'fallback\n%s\n' "$src"
    return 0
  fi
  res=$(pw_cache_ensure "$src")
  st=$(printf '%s\n' "$res" | sed -n 1p)
  cf=$(printf '%s\n' "$res" | sed -n 2p)
  case "$st" in
    local)   echo "[缓存] 源文件本就在本机盘上，无需缓存：$cf" >&2 ;;
    hit)     echo "[缓存] 命中本机${label}缓存，本次不再读网络：$cf" >&2 ;;
    fetched) echo "[缓存] 已拉取${label}到本机，本节点后续任务将直读本地，不再走网络：$cf" >&2 ;;
    *)       echo "[提示] 本机${label}缓存不可用，本次直接读取远端${label}（较慢；可检查本机磁盘空间）" >&2
             # 只有缓存不可用（真的要读网络）才做链路诊断；重定向到 stderr 是硬要求，见函数头说明
             nfs_mount_diag "$src" >&2 ;;
  esac
  [ -n "$cf" ] && [ -f "$cf" ] || cf="$src"
  printf '%s\n%s\n' "$st" "$cf"
  return 0
}

# ── 装应用前自动测速选源（通用能力：所有社区脚本统一走）──
# 思路：脚本里那套「连通性勾」不可信（A 主机通≠到下载镜像快），故直接按真实下载速度挑最快。
# 对 Debian/Ubuntu 等 apt 应用挑最快 apt 源；对 gentoo 等源码编译型系统挑最快 distfiles/rsync。
# 仅实测 IPv4（用户环境 IPv6 常不通/抽风，双栈探测会显著拖慢选源）。

# 给定镜像表 JSON、基准字段、探测路径，并发实测各镜像 IPv4 下载速度取最快，
# 输出 KEY=<最优key> 及各字段=值（可直接 eval 取值）。网络不可达的镜像自动跳过；都不通回退首个。
pw_pick_fastest_mirror() {
  local table="$1" field="$2" probe="$3"
  [ -n "$table" ] || { echo "KEY="; return 0; }
  python3 - "$table" "$field" "$probe" <<'PY'
import json, subprocess, sys, concurrent.futures, shlex
tbl = json.loads(sys.argv[1]); field = sys.argv[2]; probe = sys.argv[3]
def speed(u):
    # 仅实测 IPv4 下载速度（用户环境 IPv6 常不通/抽风，双栈探测会显著拖慢测速）
    try:
        r = subprocess.run(
            ["curl", "-4", "-s", "-o", "/dev/null", "-w", "%{speed_download}",
             "--range", "0-5242880", "--connect-timeout", "4", "--max-time", "6",
             u.rstrip("/") + probe],
            capture_output=True, text=True, timeout=10)
        try:
            return float(r.stdout.strip() or 0)
        except Exception:
            return 0.0
    except Exception:
        return 0.0
best = None; bv = -1.0
urls = {k: v.get(field) for k, v in tbl.items() if v.get(field)}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    fut = {k: ex.submit(speed, u) for k, u in urls.items()}
    for k, f in fut.items():
        try:
            sp = f.result()
        except Exception:
            sp = 0.0
        if sp > bv:
            bv = sp; best = k
if best is None:
    best = next(iter(tbl), "")
v = tbl.get(best, {})
# 用 shlex.quote 包裹输出值：调用方 eval 时值不会被子 shell 解释，杜绝镜像表被改造成含 shell 元字符时的注入面
print("KEY=%s" % shlex.quote(best))
for f, val in v.items():
    print("%s=%s" % (f, shlex.quote(val)))
PY
}

# 写入通用镜像注入脚本（第三方社区脚本 source 后，在首次包管理同步前改写容器内源）。
# 值由安装启动器经环境变量 PW_* 传入；无对应变量时注入脚本为空操作，绝不破坏安装。
os_mirror_inject_write() {
  mkdir -p /usr/local/community-scripts
  cat > /usr/local/community-scripts/os-mirror-inject.sh <<'INJ'
#!/bin/bash
# PVEWEBTOOLS 装前自动测速选源注入（值由启动器经环境变量 PW_* 传入）
_ct="${CTID:-${var_ctid:-}}"
[ -n "$_ct" ] || exit 0

# Debian/Ubuntu：apt 源指向测速最快镜像，覆盖模板自带官方源
if [ -n "${PW_APT_DEBIAN:-}" ]; then
  _codename=$(pct exec "$_ct" -- sh -c 'grep "^VERSION_CODENAME=" /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d "\"" | head -1' 2>/dev/null)
  [ -n "$_codename" ] || _codename=bookworm
  pct exec "$_ct" -- sh -c "mkdir -p /etc/apt/sources.list.d && printf '%s\n' 'deb ${PW_APT_DEBIAN} ${_codename} main contrib non-free non-free-firmware' 'deb ${PW_APT_SECURITY} ${_codename}-security main contrib non-free non-free-firmware' > /etc/apt/sources.list.d/pvewebtools.list && rm -f /etc/apt/sources.list && { [ -n '${PW_PREFER_IPV4:-}' ] && echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf; }" 2>/dev/null \
    || echo "[提示] apt 镜像注入未生效（容器未就绪或变量名不符）"
fi

# Gentoo：GENTOO_MIRRORS + sync-uri + 优先 IPv4
if [ -n "${PW_GENTOO_MIRRORS:-}" ]; then
  pct exec "$_ct" -- sh -c 'mkdir -p /etc/portage/repos.conf; m="'"${PW_GENTOO_MIRRORS}"'"; s="'"${PW_GENTOO_SYNC}"'"; if grep -q "^GENTOO_MIRRORS" /etc/portage/make.conf 2>/dev/null; then sed -i "s#^GENTOO_MIRRORS.*#GENTOO_MIRRORS=\"$m\"#" /etc/portage/make.conf; else echo "GENTOO_MIRRORS=\"$m\"" >> /etc/portage/make.conf; fi; [ -n "$s" ] && sed -i "s#rsync://[^ ]*/gentoo-portage#$s#" /etc/portage/repos.conf/gentoo.conf 2>/dev/null; [ -n "'"${PW_PREFER_IPV4}"'" ] && echo "precedence ::ffff:0:0/96 100" >> /etc/gai.conf' 2>/dev/null \
    || echo "[提示] Gentoo 镜像注入未生效（容器未就绪或变量名不符）"
fi
INJ
}

# 给第三方社区脚本注入 source 钩子（幂等）：在首个 apt/emerge/apk 同步行前插入，
# 使其安装前改写容器内源。找不到锚点则插到文件末尾前一行（仍不破坏脚本运行）。
os_mirror_patch() {
  local f="$1"
  [ -f "$f" ] || return 0
  grep -q "os-mirror-inject.sh" "$f" 2>/dev/null && return 0
  python3 - "$f" <<'PY'
import sys, re
f = sys.argv[1]
s = open(f, encoding="utf-8", errors="replace").read()
if "os-mirror-inject.sh" in s:
    sys.exit(0)
marker = 'source /usr/local/community-scripts/os-mirror-inject.sh  # PVEWEBTOOLS auto-mirror\n'
lines = s.split("\n")
ins = -1
pat = re.compile(r'\b(apt-get\s+update|apt\s+update|apt-get\s+install|apt\s+install|emerge\b[^\n]*--sync|\bemaint\b|apk\s+update|apk\s+add)\b')
for i, l in enumerate(lines):
    if pat.search(l):
        ins = i
        break
if ins < 0:
    ins = max(0, len(lines) - 1)
lines.insert(ins, marker)
open(f, "w", encoding="utf-8").write("\n".join(lines))
PY
}

   """


MIRRORS = {
   "ustc": {
       "debian": "https://mirrors.ustc.edu.cn/debian",
       "security": "https://mirrors.ustc.edu.cn/debian-security",
       "pve": "https://mirrors.ustc.edu.cn/proxmox/debian/pve",
       "ceph": "https://mirrors.ustc.edu.cn/proxmox/debian/ceph-squid",
       "ct": "https://mirrors.ustc.edu.cn/proxmox",
   },
   "tuna": {
       "debian": "https://mirrors.tuna.tsinghua.edu.cn/debian",
       "security": "https://mirrors.tuna.tsinghua.edu.cn/debian-security",
       "pve": "https://mirrors.tuna.tsinghua.edu.cn/proxmox/debian/pve",
       "ceph": "https://mirrors.tuna.tsinghua.edu.cn/proxmox/debian/ceph-squid",
       "ct": "https://mirrors.tuna.tsinghua.edu.cn/proxmox",
   },
   "official": {
       "debian": "https://deb.debian.org/debian",
       "security": "https://security.debian.org/debian-security",
       "pve": "http://download.proxmox.com/debian/pve",
       "ceph": "http://download.proxmox.com/debian/ceph-squid",
       "ct": "http://download.proxmox.com",
   },
   "aliyun": {
       "debian": "https://mirrors.aliyun.com/debian",
       "security": "https://mirrors.aliyun.com/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "tencent": {
       "debian": "https://mirrors.cloud.tencent.com/debian",
       "security": "https://mirrors.cloud.tencent.com/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "huaweicloud": {
       "debian": "https://mirrors.huaweicloud.com/debian",
       "security": "https://mirrors.huaweicloud.com/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "netease163": {
       "debian": "http://mirrors.163.com/debian",
       "security": "http://mirrors.163.com/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "bfsu": {
       "debian": "https://mirrors.bfsu.edu.cn/debian",
       "security": "https://mirrors.bfsu.edu.cn/debian-security",
       "pve": "https://mirrors.bfsu.edu.cn/proxmox/debian/pve",
       "ceph": "https://mirrors.bfsu.edu.cn/proxmox/debian/ceph-squid",
       "ct": "",
   },
   "nju": {
       "debian": "https://mirrors.nju.edu.cn/debian",
       "security": "https://mirrors.nju.edu.cn/debian-security",
       "pve": "https://mirrors.nju.edu.cn/proxmox/debian/pve",
       "ceph": "https://mirrors.nju.edu.cn/proxmox/debian/ceph-squid",
       "ct": "",
   },
   "pku": {
       "debian": "https://mirrors.pku.edu.cn/debian",
       "security": "https://mirrors.pku.edu.cn/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "sjtug-zhiyuan": {
       "debian": "https://mirrors.sjtug.sjtu.edu.cn/debian",
       "security": "https://mirrors.sjtug.sjtu.edu.cn/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "sjtug-siyuan": {
       "debian": "https://mirror.sjtu.edu.cn/debian",
       "security": "https://mirror.sjtu.edu.cn/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "hust": {
       "debian": "https://mirrors.hust.edu.cn/debian",
       "security": "https://mirrors.hust.edu.cn/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
   "xjtu": {
       "debian": "https://mirrors.xjtu.edu.cn/debian",
       "security": "https://mirrors.xjtu.edu.cn/debian-security",
       "pve": "",
       "ceph": "",
       "ct": "",
   },
}

# Gentoo 源码编译型系统的镜像候选表（供「装应用前自动测速选源」使用）。
# distfiles = 源码/软件包下载基址（写入容器 /etc/portage/make.conf 的 GENTOO_MIRRORS）；
# rsync    = Portage 树同步地址（写入 /etc/portage/repos.conf/gentoo.conf 的 sync-uri）。
# 仅收录「实测有 Gentoo 官方镜像」的站点；不保证有 gentoo 的（aliyun/tencent 等）一律不列，
# 避免把 GENTOO_MIRRORS 配成空/失效地址。测速时按 distfiles 基址实测 IPv4 速度取最快。
GENTOO_MIRRORS = {
    "tuna": {
        "label": "清华 TUNA",
        "distfiles": "https://mirrors.tuna.tsinghua.edu.cn/gentoo",
        "rsync": "rsync://mirrors.tuna.tsinghua.edu.cn/gentoo-portage",
    },
    "ustc": {
        "label": "中科大 USTC",
        "distfiles": "https://mirrors.ustc.edu.cn/gentoo",
        "rsync": "rsync://mirrors.ustc.edu.cn/gentoo-portage",
    },
    "bfsu": {
        "label": "北外 BFSU",
        "distfiles": "https://mirrors.bfsu.edu.cn/gentoo",
        "rsync": "rsync://mirrors.bfsu.edu.cn/gentoo-portage",
    },
    "official": {
        "label": "官方源",
        "distfiles": "https://distfiles.gentoo.org",
        "rsync": "rsync://rsync.gentoo.org/gentoo-portage",
    },
}


