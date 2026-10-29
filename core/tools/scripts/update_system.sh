
case "$PT_ACTION" in
 update)
   export DEBIAN_FRONTEND=noninteractive
   wait_apt_lock || { echo "[错误] apt 锁持续被后台任务占用（如 unattended-upgrades），请稍后重试"; exit 1; }
   # 上次 APT 被中断留下的半配置包会让 apt upgrade 直接失败（dpkg was interrupted）：先自愈
   fix_dpkg_interrupted || exit 1
   echo "[1/3] 更新软件包列表 ..."
   run "apt update 失败，常见原因是源配置有无效/重复条目，可先执行'更换软件源和 LXC 源'修复" apt update
   echo "[2/3] 升级系统软件包 ..."
   run "apt upgrade 失败" apt upgrade -y
   echo "[3/3] 清理无用软件包 ..."
   run "apt autoremove 失败" apt autoremove -y
   echo "[完成] 系统更新完成"
   ;;
 block)
   echo "[1/4] 停用并屏蔽 unattended-upgrades 自动更新服务 ..."
   systemctl stop unattended-upgrades 2>/dev/null || true
   systemctl disable --now unattended-upgrades 2>/dev/null || true
   systemctl mask unattended-upgrades 2>/dev/null || true
   # 防止被 apt 触发拉起
   REBOOT_CFG=/etc/apt/apt.conf.d/50unattended-upgrades
   if [ -f "$REBOOT_CFG" ]; then
     backup_file "$REBOOT_CFG"
     sed -i 's/^\(\s*"distro_upgrade"\)/\t\/\/\1/' "$REBOOT_CFG" 2>/dev/null || true
   fi
   echo "[2/4] 禁用 apt 自动更新配置 ..."
   APT_PERIODIC=/etc/apt/apt.conf.d/10periodic
   backup_file "$APT_PERIODIC" 2>/dev/null || true
   cat > "$APT_PERIODIC" <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
   APT_AUTO=/etc/apt/apt.conf.d/20auto-upgrades
   if [ -f "$APT_AUTO" ]; then
     backup_file "$APT_AUTO"
     sed -i 's/"1"/"0"/g' "$APT_AUTO"
   fi
   echo "[3/4] 锁定 PVE 核心软件包（防止误升级导致不兼容） ..."
   PKGS="pve-kernel-$(uname -r) proxmox-ve pve-manager pve-qemu-kvm pve-container pve-docs"
   for pkg in $PKGS; do
     if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
       apt-mark hold "$pkg" 2>/dev/null && echo "  已锁定: $pkg" || true
     fi
   done
   echo "[4/4] 停用 apt-daily 相关定时器 ..."
   systemctl disable --now apt-daily.timer 2>/dev/null || true
   systemctl disable --now apt-daily-upgrade.timer 2>/dev/null || true
   systemctl mask apt-daily.timer 2>/dev/null || true
   systemctl mask apt-daily-upgrade.timer 2>/dev/null || true
   echo "[完成] 系统更新已禁止，PVE 核心包已锁定"
   echo "[提示] 如需手动更新，请使用本工具的「立即更新系统」操作"
   ;;
 unblock)
   echo "[1/5] 解除 apt-daily 定时器屏蔽 ..."
   systemctl unmask apt-daily.timer 2>/dev/null || true
   systemctl unmask apt-daily-upgrade.timer 2>/dev/null || true
   systemctl enable --now apt-daily.timer 2>/dev/null || true
   systemctl enable --now apt-daily-upgrade.timer 2>/dev/null || true
   echo "[2/5] 恢复 unattended-upgrades 自动更新服务 ..."
   systemctl unmask unattended-upgrades 2>/dev/null || true
   systemctl enable --now unattended-upgrades 2>/dev/null || true
   echo "[3/5] 恢复 apt 自动更新配置 ..."
   APT_AUTO=/etc/apt/apt.conf.d/20auto-upgrades
   if [ -f "$APT_AUTO" ]; then
     sed -i 's/"0"/"1"/g' "$APT_AUTO"
   fi
   APT_PERIODIC=/etc/apt/apt.conf.d/10periodic
   if [ -f "$APT_PERIODIC" ]; then
     sed -i 's/"0"/"1"/g' "$APT_PERIODIC"
   fi
   echo "[4/5] 解除 PVE 核心软件包锁定 ..."
   PKGS="pve-kernel-$(uname -r) proxmox-ve pve-manager pve-qemu-kvm pve-container pve-docs"
   for pkg in $PKGS; do
     apt-mark unhold "$pkg" 2>/dev/null && echo "  已解锁: $pkg" || true
   done
   echo "[5/5] 刷新软件包列表 ..."
   export DEBIAN_FRONTEND=noninteractive
   apt update 2>/dev/null || true
   echo "[完成] 系统更新禁止已解除，可正常更新"
   ;;
 *)
   echo "[错误] 未知操作: $PT_ACTION"; exit 1 ;;
esac
