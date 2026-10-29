
CUR=$(uname -r)
export DEBIAN_FRONTEND=noninteractive
# 镜像包判定：一次性反查「哪个包拥有 /boot/vmlinuz-* 文件」（dpkg -S）。
# 不用逐包 dpkg -L | grep -q：内核包文件清单几千行，grep -q 命中即退出会让
# dpkg -L 吃 SIGPIPE，pipefail 下整条管道判失败，所有包会被误标成元包、清理永远没动作
KOWNERS=$({ dpkg -S '/boot/vmlinuz-*' 2>/dev/null || true; } | awk -F': ' '{n=split($1,a,", "); for(i=1;i<=n;i++) print a[i]}')
if [ "$PT_ACTION" = "current" ]; then
 echo "当前内核: $CUR  架构: $(uname -m)"
 echo "默认内核元包: $(dpkg-query -W -f='${Version}' proxmox-default-kernel 2>/dev/null || echo 未安装)"
 echo; echo "---- 已安装内核包 ----"
 # 镜像=真正含内核文件的包（占空间大头），元包=依赖指针，卸载会连带上层元包
 for p in $(dpkg -l | { grep -E '^ii[[:space:]]+(proxmox|pve)-kernel-[0-9]' || true; } | awk '{print $2}' | sort -V); do
   if printf '%s\n' "$KOWNERS" | grep -qx -- "$p"; then TAG="镜像"; else TAG="元包"; fi
   MARK=""
   case "$p" in *"${CUR%-pve}"*) MARK=" ← 运行中";; esac
   echo "$p [$TAG]$MARK"
 done
elif [ "$PT_ACTION" = "available" ]; then
 echo "当前内核: $CUR"
 echo "[1/2] 刷新软件包列表 ..."
 wait_apt_lock || { echo "[错误] apt 锁持续被后台任务占用（如 unattended-upgrades），请稍后重试"; exit 1; }
 run "apt update 失败，常见原因是源配置有无效/重复条目，可先执行'更换软件源'修复" apt update
 echo "[2/2] 仓库中可用的内核包（已过滤 template 构建包；[元包]=系列指针，装了会拉它指向的镜像）:"
 # 仓库保留全部历史版本，直接列会刷几百行噪音；去掉 -template（打包辅助物，不是内核），
 # 系列元包（如 proxmox-kernel-7.0）单独打标，其余即具体镜像包
 for n in $(apt-cache search --names-only '^(proxmox|pve)-kernel-[0-9]' | awk '{print $1}' | { grep -vE -- '-template$' || true; } | sort -V); do
   if [[ "$n" =~ ^(proxmox|pve)-kernel-[0-9]+\.[0-9]+$ ]]; then echo "$n [元包]"; else echo "$n"; fi
 done
 echo "[提示] 这里列的是仓库全部历史版本；'安装新内核'的下拉会按当前系统 signed/未签名形态过滤并默认选中最新版"
elif [ "$PT_ACTION" = "install" ]; then
 echo "当前内核: $CUR"
 wait_apt_lock || { echo "[错误] apt 锁持续被后台任务占用（如 unattended-upgrades），请稍后重试"; exit 1; }
 # 上次 APT 被中断留下的半配置包会让下面的 apt install 直接失败（dpkg was interrupted）：先自愈
 fix_dpkg_interrupted || exit 1
 run "apt update 失败，常见原因是源配置有无效/重复条目，可先执行'更换软件源'修复" apt update
 # 待装包来自 Proxmox 官方源时提前提醒：官方源在境外，大内核包下载可能很慢，
 # 建议先切国内镜像（不阻断安装，着急的用户可自行终止换源后重来）
 if apt-cache policy 2>/dev/null | grep -q 'download\.proxmox\.com'; then
   echo "[提示] 当前使用 Proxmox 官方源（download.proxmox.com），内核包较大下载可能很慢；"
   echo "       可先终止本任务，执行'更换软件源'选国内镜像（如 USTC/清华）后重新安装"
 fi
 if [ -n "$PT_VERSION" ]; then
   VER="$PT_VERSION"
   # 双保险：只允许合法字符，防止异常输入拼进包名
   case "$VER" in *[!0-9A-Za-z.+-]*) echo "[错误] 内核版本格式非法: $VER"; exit 1;; esac
   if apt-cache show "$VER" >/dev/null 2>&1; then
     # 兼容直接粘贴完整包名（如 proxmox-kernel-7.0.14-9-pve-signed）：仓库里有就直接装
     PKG="$VER"
   else
     # 按版本号处理：PVE 内核优先装 signed 包，仓库没有再退回非 signed
     PKG="proxmox-kernel-$VER-signed"
     apt-cache show "$PKG" >/dev/null 2>&1 || PKG="proxmox-kernel-$VER"
   fi
   apt-cache show "$PKG" >/dev/null 2>&1 || { echo "[错误] 未找到 $VER 对应的内核包，请先'查看可用内核'确认"; exit 1; }
   echo "安装指定内核: $PKG ..."
   run "内核安装失败" apt install -y "$PKG"
 else
   # 留空=装最新：PVE 9 用 proxmox-default-kernel 元包；老版本退回已装内核系列元包
   META=""
   apt-cache show proxmox-default-kernel >/dev/null 2>&1 && META=proxmox-default-kernel
   if [ -z "$META" ]; then
     META=$(dpkg -l | { grep '^ii' || true; } | { grep -oE '(proxmox|pve)-kernel-[0-9]+\.[0-9]+' || true; } | sort -uV | tail -1)
   fi
   [ -n "$META" ] || { echo "[错误] 找不到内核元包，无法安装最新内核"; exit 1; }
   echo "安装最新默认内核（元包）: $META ..."
   run "内核安装失败" apt install -y "$META"
 fi
 echo "安装后已装内核:"
 dpkg -l | grep '^ii' | { grep -E '(proxmox-kernel|pve-kernel)-[0-9]' || true; } | awk '{print $2, $3}' | sort -V
 echo "[完成] 内核安装结束，新内核需重启后启用（可用本工具'重启系统应用新内核'）"
 if [ -d /sys/firmware/efi ]; then
   echo "[提示] EFI 引导的主机建议随后执行「EFI 引导维护 → 同步」，确保 EFI 分区有新内核引导项"
 fi
elif [ "$PT_ACTION" = "default" ]; then
 VER="$PT_BOOT_VER"
 [ -n "$VER" ] || { echo "[错误] 请填写要设为默认启动的内核版本（如 6.17.2-1-pve，可先用'显示当前内核'查看）"; exit 1; }
 case "$VER" in *[!0-9A-Za-z.+-]*) echo "[错误] 内核版本格式非法: $VER"; exit 1;; esac
 # 目标内核必须已安装，否则 GRUB 会生成无效菜单项
 if [ ! -f "/boot/vmlinuz-$VER" ]; then
   echo "[错误] 内核 $VER 未安装，当前 /boot 中的内核文件:"
   ls /boot | { grep '^vmlinuz' || true; }
   exit 1
 fi
 if [ -d /sys/firmware/efi ]; then
   echo "[提示] 本机为 EFI 引导：启动列表是 EFI 分区里同步的内核，默认启动最新已同步内核，不走 GRUB 菜单"
   echo "      如需切换 EFI 默认启动内核，请用「EFI 引导维护」同步目标内核"
   if command -v proxmox-boot-tool >/dev/null 2>&1; then
     echo; echo "---- EFI 分区当前内核 ----"
     proxmox-boot-tool kernel list || true
   fi
   exit 0
 fi
 GRUB=/etc/default/grub
 backup_file "$GRUB"
 # GRUB 子菜单路径格式：高级选项>具体内核条目（update-grub 生成的标准标题）
 TITLE="Advanced options for Proxmox VE GNU/Linux>Proxmox VE GNU/Linux, with Linux $VER"
 TMP=$(pw_job_mktemp_file)
 awk -v t="$TITLE" '/^GRUB_DEFAULT=/{print "GRUB_DEFAULT=\"" t "\""; next}{print}' "$GRUB" > "$TMP"
 cp "$TMP" "$GRUB"; rm -f "$TMP"
 grep -qF "GRUB_DEFAULT=\"$TITLE\"" "$GRUB" || { echo "[错误] GRUB_DEFAULT 写入未生效"; exit 1; }
 run "update-grub 执行失败" update-grub
 # 写后校验：grub.cfg 里确实生成了目标内核的菜单项
 grep -qF "with Linux $VER" /boot/grub/grub.cfg || { echo "[错误] grub.cfg 未生成 $VER 的菜单项，请用「GRUB 配置」还原"; exit 1; }
 echo "[完成] 默认启动内核已设为 $VER，重启后生效"
elif [ "$PT_ACTION" = "cleanup" ]; then
 echo "当前内核: $CUR"
 echo "清理策略: 只卸实际内核镜像包（提供 /boot/vmlinuz），强制保留运行中与最新 2 个；系列/默认元包一律不碰"
 # 元包（如 proxmox-kernel-7.0）卸载会连带 proxmox-default-kernel → proxmox-ve，绝不能当内核包清
 # 注意：grep 无匹配时退出码 1 会触发 ERR trap，而 trap 的 echo 会被 $() 捕获污染变量，
 # 所以无匹配也合法的管道必须 || true 兜底
 IMAGES=""
 for p in $(dpkg -l | { grep -E '^ii[[:space:]]+(proxmox|pve)-kernel-[0-9]' || true; } | awk '{print $2}'); do
   # 镜像包 = 拥有 /boot/vmlinuz-* 文件的包（脚本头部 KOWNERS 反查得到）；
   # grep -qx 整行精确匹配，避免 proxmox-kernel-7.0 被 7.0.14-x 的子串误命中
   printf '%s\n' "$KOWNERS" | grep -qx -- "$p" && IMAGES="$IMAGES $p" || true
 done
 IMAGES=$(echo "$IMAGES" | tr ' ' '\n' | { grep -v '^$' || true; } | sort -V)
 [ -n "$IMAGES" ] || { echo "[完成] 未发现可清理的内核镜像包"; exit 0; }
 KEEP=$(echo "$IMAGES" | tail -2)
 for pkg in $IMAGES; do
   # 双保险：包名必须是合法内核包格式，防止变量被异常内容污染后拿去做 apt 参数
   case "$pkg" in
     proxmox-kernel-*|pve-kernel-*) ;;
     *) echo "[跳过] 非法包名: $pkg"; continue;;
   esac
   echo "$KEEP" | grep -q "^$pkg$" && { echo "[保留] $pkg (最新)"; continue; } || true
   case "$pkg" in *"${CUR%-pve}"*) echo "[保留] $pkg (运行中)"; continue;; esac
   # 双保险：模拟卸载若会牵连 proxmox-ve/默认内核元包则放弃该包
   if apt-get -s remove --purge "$pkg" 2>/dev/null | grep '^Remv ' | grep -qE 'proxmox-ve|proxmox-default-kernel'; then
     echo "[跳过] $pkg（卸载会牵连 proxmox-ve/默认内核元包，不安全）"
     continue
   fi
   echo "[卸载] $pkg"
   run "内核包卸载失败" apt-get remove -y --purge "$pkg"
 done
 run "update-grub 执行失败" update-grub
 echo "[完成] 旧内核清理完成（系列元包仍保留，如需彻底移除请手动处理）"
elif [ "$PT_ACTION" = "reboot" ]; then
 echo "当前运行内核: $CUR"
 LATEST=$(ls /boot | { grep '^vmlinuz-' || true; } | sed 's/^vmlinuz-//' | sort -V | tail -1)
 if [ -n "$LATEST" ]; then
   echo "最新已装内核: $LATEST"
   [ "$CUR" = "$LATEST" ] && echo "[确认] 运行中即最新内核，重启后仍将引导它"
 fi
 echo "[警告] 10 秒后重启宿主机：本节点上的虚拟机/容器会随主机一起重启！"
 for i in 10 9 8 7 6 5 4 3 2 1; do echo "倒计时 ${i}s ..."; sleep 1; done
 echo "[重启] 执行 reboot ..."
 reboot
fi
