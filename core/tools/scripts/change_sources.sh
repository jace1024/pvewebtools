
# 自动测速选源：实测各候选镜像 IPv4/IPv6 真实下载速度取最快（脚本里「连通性勾」不可信）
if [ "${PT_M_AUTO:-0}" = "1" ] && [ -n "${PT_M_TABLE:-}" ]; then
  echo "[自动测速] 正在实测各镜像源（IPv4/IPv6 取最快）..."
  eval "$(pw_pick_fastest_mirror "${PT_M_TABLE}" debian /dists/stable/Release 2>/dev/null)" || true
  DEB="$debian"; SEC="${security:-$debian}"; PVE="$pve"; CEPH="$ceph"; CT="$ct"
  echo "[自动测速] 选定最快源：${KEY:-ustc}"
fi
DEB="${DEB:-$PT_M_DEBIAN}"; SEC="${SEC:-$PT_M_SECURITY}"; PVE="${PVE:-$PT_M_PVE}"; CEPH="${CEPH:-$PT_M_CEPH}"; CT="${CT:-$PT_M_CT}"
# 自动检测 Debian 代号：PVE 8 = bookworm，PVE 9 = trixie
DISTRO_CODENAME=$(cat /etc/os-release 2>/dev/null | grep '^VERSION_CODENAME=' | cut -d= -f2 | tr -d '"' | head -1)
[ -n "$DISTRO_CODENAME" ] || DISTRO_CODENAME=$(dpkg -l base-files 2>/dev/null | awk '/^ii/{print $3}' | head -1 | grep -oE '[a-z]+$' || echo trixie)
echo "[0/8] 检测到 Debian 代号: $DISTRO_CODENAME"
echo "[1/8] 清理 /etc/apt/sources.list 旧条目（防重复与无效源）"
# 该文件若残留手写源（拼错的组件、失效的仓库、与 sources.list.d 重复的条目），
# apt update 会直接失败或刷大量警告；统一注释掉，源全部由 sources.list.d 管理
SL=/etc/apt/sources.list
if [ -s "$SL" ] && grep -qE '^[[:space:]]*deb([[:space:]]|\[)' "$SL"; then
 backup_file "$SL"
 sed -i -E 's/^([^#])/# \1/' "$SL"
 echo "[清理] sources.list 旧条目已全部注释（已备份），如下面验证仍报错请检查 sources.list.d 下的 .list 文件"
else
 echo "[清理] sources.list 无生效条目，跳过"
fi

# 确保 Proxmox GPG 密钥环存在且最新，避免 NO_PUBKEY 错误
echo "[密钥] 检查 Proxmox GPG 密钥环 ..."
if ! dpkg -l proxmox-archive-keyring 2>/dev/null | grep -q '^ii'; then
 echo "  安装 proxmox-archive-keyring ..."
 apt install -y proxmox-archive-keyring 2>/dev/null || true
fi
KEYRING=/usr/share/keyrings/proxmox-archive-keyring.gpg
if [ ! -f "$KEYRING" ]; then
 echo "  密钥环文件不存在，尝试从官方下载 ..."
 mkdir -p /usr/share/keyrings
 # PVE 8 (bookworm) 和 PVE 9 (trixie) 的 release key
 for KEYURL in \
   https://download.proxmox.com/debian/proxmox-release-bookworm.gpg \
   https://download.proxmox.com/debian/proxmox-release-trixie.gpg; do
   wget -qO "$KEYRING" "$KEYURL" 2>/dev/null && break
 done
fi
# 针对 NO_PUBKEY 1140AF8F639E0C39 尝试从 keyserver 导入（apt-key 已废弃，用 gpg 直接操作）
if [ -f "$KEYRING" ] && ! gpg --no-default-keyring --keyring "$KEYRING" --list-keys 1140AF8F639E0C39 2>/dev/null | grep -q 1140AF8F639E0C39; then
 echo "  尝试从 keyserver 导入缺失密钥 1140AF8F639E0C39 ..."
 gpg --no-default-keyring --keyring "$KEYRING" --keyserver keyserver.ubuntu.com --recv-keys 1140AF8F639E0C39 2>/dev/null || true
fi
# 同步到 trusted.gpg.d 作为兜底
TRUSTED=/etc/apt/trusted.gpg.d/proxmox-archive-keyring.gpg
if [ -f "$KEYRING" ] && [ ! -f "$TRUSTED" ]; then
 cp "$KEYRING" "$TRUSTED" 2>/dev/null || true
fi
echo "[2/8] 写入 Debian 源 ($DISTRO_CODENAME)"
backup_file /etc/apt/sources.list.d/debian.sources
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: $DEB
Suites: $DISTRO_CODENAME $DISTRO_CODENAME-updates $DISTRO_CODENAME-backports
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: $SEC
Suites: $DISTRO_CODENAME-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
echo "[3/8] 注释企业订阅源"
# 注释所有未注释行：同时兼容 deb822 与传统单行格式，避免残留非法 stanza 导致 apt 报 Malformed
for ENT in /etc/apt/sources.list.d/pve-enterprise.sources \
          /etc/apt/sources.list.d/pve-enterprise.list \
          /etc/apt/sources.list.d/ceph-enterprise.sources; do
 if [ -f "$ENT" ]; then
   backup_file "$ENT"
   sed -i -E 's/^([^#])/# \1/' "$ENT"
 fi
done
echo "[4/8] 清理 sources.list.d 下的残留源文件（本工具不管理的）"
# 手写的 xxx.list/xxx.sources（如把 PVE 源路径错写成 proxmox/debian）同样会弄坏 apt update；
# 统一备份后注释，源全部由本工具写入的文件集中管理；若残留文件含 ceph 源则记录，后面补建
HAD_CEPH=0
for F in /etc/apt/sources.list.d/*; do
 case "$F" in
   */debian.sources|*/pve-no-subscription.sources|*/ceph.sources|\
   */pve-enterprise.sources|*/pve-enterprise.list|*/ceph-enterprise.sources) continue;;
   *.list|*.sources) ;;
   *) continue;;
 esac
 if grep -qE '^[[:space:]]*(deb([[:space:]]|\[)|Types:)' "$F"; then
   grep -qi ceph "$F" && HAD_CEPH=1
   backup_file "$F"
   sed -i -E 's/^([^#])/# \1/' "$F"
   echo "[清理] 残留源文件已注释: $F"
 fi
done
echo "[5/8] 写入 PVE no-subscription 源"
# PVE 仓库并非所有镜像都承载；镜像未提供 PVE 源时降级官方源，避免写空 URIs 破坏 apt
if [ -z "$PVE" ]; then
 PVE="http://download.proxmox.com/debian/pve"
 echo "[提示] 当前镜像不提供 PVE 源，PVE 仓库降级为官方源"
fi
backup_file /etc/apt/sources.list.d/pve-no-subscription.sources
cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<EOF
Types: deb
URIs: $PVE
Suites: $DISTRO_CODENAME
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
echo "[6/8] 写入 Ceph 源"
# Ceph 仓库同理：镜像未提供时降级官方源（下面两个分支共用此降级值）
if [ -z "$CEPH" ]; then
 CEPH="http://download.proxmox.com/debian/ceph-squid"
 echo "[提示] 当前镜像不提供 Ceph 源，Ceph 仓库降级为官方源"
fi
if [ -f /etc/apt/sources.list.d/ceph.sources ]; then
 backup_file /etc/apt/sources.list.d/ceph.sources
 cat > /etc/apt/sources.list.d/ceph.sources <<EOF
Types: deb
URIs: $CEPH
Suites: $DISTRO_CODENAME
Components: no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
elif [ "$HAD_CEPH" = "1" ]; then
 # 原 ceph 源在被注释的残留文件里，补建标准 ceph.sources 避免丢源
 cat > /etc/apt/sources.list.d/ceph.sources <<EOF
Types: deb
URIs: $CEPH
Suites: $DISTRO_CODENAME
Components: no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
 echo "[写入] 新建 ceph.sources（原 ceph 源在上面被注释的残留文件里）"
fi
echo "[7/8] 替换 LXC(CT) 模板源（修改 APLInfo.pm 并重启 pvedaemon 生效）"
APL=/usr/share/perl5/PVE/APLInfo.pm
if [ -f "$APL" ]; then
 backup_file "$APL"
 # 先将 APLInfo.pm 中可能残留的任意镜像站/官方 CT 域名统一还原为官方地址，
 # 再写入本次所选镜像站的 CT 源，避免切换源时旧域名残留导致网页端仍走旧源
 sed -i -E "s#https?://[a-zA-Z0-9._-]+/proxmox#http://download.proxmox.com#g" "$APL"
 if [ -n "$CT" ] && [ "$CT" != "http://download.proxmox.com" ]; then
   sed -i "s|http://download.proxmox.com|$CT|g" "$APL"
   echo "[LXC源] 已切换为: $CT"
 else
   echo "[LXC源] 当前镜像不提供 CT 模板源，保持官方源（http://download.proxmox.com）"
 fi
 systemctl restart pvedaemon
else
 echo "[提示] 未找到 $APL（非 PVE 环境？），跳过 LXC 模板源替换"
fi
# 写后验证：源文件内容确实是新镜像地址
if ! grep -q "$DEB" /etc/apt/sources.list.d/debian.sources; then
 echo "[错误] Debian 源写入未生效"; exit 1
fi
if ! grep -q "$PVE" /etc/apt/sources.list.d/pve-no-subscription.sources; then
 echo "[错误] PVE 源写入未生效"; exit 1
fi
echo "[8/8] 验证新源（apt update）"
# 验证与锁失败是两回事：源已写入成功，若后台 apt 任务占锁则跳过验证并如实告知
if wait_apt_lock; then
 run "apt update 仍失败，请把上方报错中的源文件名反馈给我" apt update
 echo "[完成] 换源完成，建议随后执行'系统更新'"
else
 echo "[提示] 源已写入成功，但 apt 锁仍被后台任务占用，本次跳过验证；稍后手动执行 apt update 确认即可"
fi
