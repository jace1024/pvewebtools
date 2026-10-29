
echo "====== 硬件直通前置检查 ======"
FAIL=0

# 0. 虚拟化环境检测：虚拟机内无法做 PCIe 直通，后续检查无意义
# 注：systemd-detect-virt 在部分机器上会分层输出多行（如两行 none），统一过滤 none 行取首个有效值
VIRT=$(systemd-detect-virt 2>/dev/null || echo none)
VIRT=$(printf '%s\n' "$VIRT" | grep -v '^none$' | head -n1 || true)
if [ -n "$VIRT" ]; then
 echo "[信息] 当前运行在虚拟机中（$VIRT），PCIe 直通需要物理宿主机支持"
 echo "[跳过] CPU 虚拟化 / IOMMU / 隔离组等检查在虚拟机内无意义，已跳过"
 echo "[信息] 磁盘块映射 (RDM)：不依赖 IOMMU，即使在虚拟机中也可使用"
 echo "[结果] 虚拟机环境：PCIe 直通不可用，磁盘 RDM 直通仍可用"
 exit 0
fi

# 1. CPU 虚拟化扩展（VT-x/AMD-V）
if grep -qE 'vmx|svm' /proc/cpuinfo; then
 echo "[通过] CPU 虚拟化扩展 (VT-x/AMD-V)：已支持"
else
 echo "[失败] CPU 虚拟化扩展 (VT-x/AMD-V)：未检测到，请先在 BIOS 开启"; FAIL=$((FAIL+1))
fi

# 2. IOMMU 启动参数：区分「未配置 / 已写入未重启 / 已生效」三种状态
# 配置文件按引导方式读（GRUB → /etc/default/grub；systemd-boot → /etc/kernel/cmdline）；
# 只认 GRUB 会把 ZFS 安装的 PVE 8/9（默认 systemd-boot）上已配好的机器误报成「未配置」
CMDLINE=$(cat /proc/cmdline)
CFG_IOMMU=""; RUN_IOMMU=""
printf '%s' "$(boot_param_read)" | grep -qE '(intel|amd)_iommu=on' && CFG_IOMMU=yes
echo "$CMDLINE" | grep -qE '(intel|amd)_iommu=on' && RUN_IOMMU=yes
if [ -n "$RUN_IOMMU" ]; then
 echo "[通过] IOMMU 启动参数：已开启"
elif [ -n "$CFG_IOMMU" ]; then
 echo "[失败] IOMMU 启动参数：已写入 $(boot_param_mode) 但当前内核未加载"; FAIL=$((FAIL+1))
 echo "  → 配置已就绪但需重启生效：请点弹窗内「重启节点生效配置」按钮或执行 reboot，重启后重跑本检查"
else
 echo "[失败] IOMMU 启动参数：未配置"; FAIL=$((FAIL+1))
 echo "  → 请先执行「开启硬件直通」（自动写入 intel/amd_iommu=on + iommu=pt + vfio 模块），再重启宿主机生效"
fi

# 3. IOMMU 运行状态（隔离组非空才算真正可用）
# 注：不能用 GROUPS 作变量名——它是 bash 只读特殊数组（当前用户组 ID 列表），
# 对其赋值会静默失败，导致 $GROUPS 恒为 root 的 gid(0)，永远误判「隔离组为空」
IOMMU_GROUPS=$(ls /sys/kernel/iommu_groups 2>/dev/null | wc -l) || IOMMU_GROUPS=0
if [ "$IOMMU_GROUPS" -gt 0 ]; then
 echo "[通过] IOMMU 运行状态：已生效（共 $IOMMU_GROUPS 个隔离组）"
elif [ -z "$RUN_IOMMU" ] && [ -n "$CFG_IOMMU" ]; then
 echo "[失败] IOMMU 运行状态：未生效（隔离组为空）"; FAIL=$((FAIL+1))
 echo "  → 启动参数已写入 $(boot_param_mode) 但当前内核尚未加载：请重启宿主机后再跑本检查"
elif [ -z "$RUN_IOMMU" ]; then
 echo "[失败] IOMMU 运行状态：未生效（隔离组为空）"; FAIL=$((FAIL+1))
 echo "  → 尚未配置 IOMMU 启动参数：请先执行「开启硬件直通」并重启宿主机"
else
 # 内核参数已加载但仍无隔离组 → 用 dmesg 的 DMAR/IOMMU 日志定位硬件层真实原因，
 # 而不是武断断言「BIOS 未开启」（用户可能已开启但 DMAR 表异常/CSM 干扰/BIOS 设置未真正保存）
 echo "[失败] IOMMU 运行状态：未生效（隔离组为空）"; FAIL=$((FAIL+1))
 DMESG=$(dmesg 2>/dev/null | grep -i -E 'dmar|iommu' | head -20 || true)
 if echo "$DMESG" | grep -qi 'disabled by BIOS'; then
   echo "  → dmesg 显示「DMAR: IOMMU disabled by BIOS」：BIOS 层面 VT-d 实际未生效"
   if grep -qi intel /proc/cpuinfo; then
     echo "  → 请进 BIOS 确认 VT-d 已启用并保存：Intel 主板常见在 Advanced → System Agent (North Bridge) → VT-d"
   else
     echo "  → 请进 BIOS 确认 AMD IOMMU/SVM 已启用并保存：常见在 Advanced → AMD CBS → NBIO → IOMMU"
   fi
   echo "  → 提示：部分主板需同时开启 VT-x/AMD-V；若 BIOS 已开启仍无效，可尝试清空 CMOS 后重新设置"
 elif echo "$DMESG" | grep -qi 'IOMMU enabled'; then
   echo "  → 意外：dmesg 显示 IOMMU 已启用，但 /sys/kernel/iommu_groups 为空"
   echo "  → 请执行 update-initramfs -u -k all 并重启；仍无效时检查 BIOS 的 CSM/兼容模式与 DMAR 相关选项"
 elif [ -n "$DMESG" ]; then
   echo "  → dmesg DMAR/IOMMU 相关输出（供排查）："
   echo "$DMESG" | sed 's/^/    /'
 else
   echo "  → dmesg 未捕获 DMAR/IOMMU 日志（dmesg 受限或无输出）"
   echo "  → 请手动执行 dmesg | grep -i dmar 查看；日志为空但 BIOS 已开 VT-d 时，多为 BIOS 的 DMAR 表未正常生成"
 fi
 echo "  → 修改后保存退出并重启宿主机，重新执行本检查"
fi

# BDF → 设备名称（lspci 全量取一次），供隔离组/显卡/网口等处展示友好名称
LSPCI_ALL=$(lspci -D 2>/dev/null || true)
pci_desc() {  # $1=完整 BDF；输出设备描述（超长截断），未匹配则无输出
 printf '%s\n' "$LSPCI_ALL" | awk -v a="$1" '$1==a { $1=""; sub(/^[ \t]+/,""); print substr($0,1,72); exit }'
}
pci_tag() {  # $1=设备描述；按关键字给类别标签，便于判断整组直通会连带什么
 case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
   *vga*|*"3d controller"*|*display*)                     echo "【显卡】" ;;
   *ethernet*|*network*)                                  echo "【网卡】" ;;
   *sata*|*nvme*|*raid*|*"serial ata"*|*"ide interface"*)  echo "【存储】" ;;
   *audio*)                                               echo "【声卡】" ;;
   *"usb controller"*)                                     echo "【USB控制器】" ;;
   *bridge*)                                              echo "【桥接】" ;;
   *)                                                     echo "" ;;
 esac
}

# 4. 隔离组分离度：同组多设备时直通需整组一起过
if [ "$IOMMU_GROUPS" -gt 0 ]; then
 MIXED=""
 for g in /sys/kernel/iommu_groups/*; do
   N=$(ls "$g/devices" 2>/dev/null | wc -l || true)
   if [ "$N" -gt 1 ]; then
     MIXED="$MIXED
     组 $(basename "$g")（$N 个设备）："
     for d in $(ls "$g/devices" 2>/dev/null || true); do
       DESC=$(pci_desc "$d")
       TAG=$(pci_tag "$DESC")
       LINE="- $d"
       [ -n "$TAG" ] && LINE="$LINE  $TAG"
       [ -n "$DESC" ] && LINE="$LINE  $DESC"
       MIXED="$MIXED
       $LINE"
     done
   fi
 done
 if [ -n "$MIXED" ]; then
   echo "[警告] 隔离组分离度：以下组含多个设备（直通时需整组一起过，或启用 ACS 分离）：$MIXED"
 else
   echo "[通过] 隔离组分离度：良好（每个设备独立成组）"
 fi
fi

# 5. 显卡驱动占用：宿主驱动绑着显卡时 VM 可能拿不到设备
GPUS=$(printf '%s\n' "$LSPCI_ALL" | grep -Ei 'vga|3d|display' | awk '{print $1}' || true)
if [ -z "$GPUS" ]; then
 echo "[信息] 显卡：未检测到显示设备"
else
 for A in $GPUS; do
   GDESC=$(pci_desc "$A")
   DRV=""
   [ -e "/sys/bus/pci/devices/$A/driver" ] && DRV=$(basename "$(readlink -f "/sys/bus/pci/devices/$A/driver")")
   if [ -z "$DRV" ]; then
     echo "[通过] 显卡 $A${GDESC:+ · $GDESC}：宿主未绑定驱动（直通友好）"
   else
     case "$DRV" in
       nouveau|nvidia|amdgpu|radeon|i915|xe)
         echo "[警告] 显卡 $A${GDESC:+ · $GDESC}：宿主 $DRV 驱动占用中，直通后 VM 若拿不到设备，需屏蔽该驱动并重启（「开启硬件直通」内置驱动屏蔽选项）" ;;
       *)
         echo "[通过] 显卡 $A${GDESC:+ · $GDESC}：绑定驱动 $DRV" ;;
     esac
   fi
 done
fi

# 6. 宿主管理网口提示（网卡直通不可选它，工具内已自动拦截）
DEF_IF=$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')
if [ -n "$DEF_IF" ]; then
 DEF_PCI=""
 [ -e "/sys/class/net/$DEF_IF/device" ] && DEF_PCI=$(basename "$(readlink -f "/sys/class/net/$DEF_IF/device")")
 DEF_DESC=""
 [ -n "$DEF_PCI" ] && DEF_DESC=$(pci_desc "$DEF_PCI")
 echo "[信息] 宿主管理网口：$DEF_IF${DEF_PCI:+ ($DEF_PCI${DEF_DESC:+ · $DEF_DESC})}，网卡直通不可选此口（工具会自动拦截）"
fi

# 7. 磁盘块映射（RDM）不依赖 IOMMU
echo "[信息] 磁盘块映射 (RDM)：不依赖 IOMMU，无需上述前提即可使用"

if [ "$FAIL" -gt 0 ]; then
 echo "[结果] 直通前置检查：$FAIL 项未满足，PCIe 直通（磁盘/网卡/显卡）前请先按提示处理"
else
 echo "[结果] 直通前置检查：全部前提满足，可进行磁盘/网卡/显卡直通"
fi
